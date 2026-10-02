"""Inventory context — the Stock aggregate (one per Product).

This is the anti-oversell spine of the model.

Invariants:
  S1  available >= 0 always
  S2  reserve(qty) fails (StockInsufficient) if available < qty —
      all-or-nothing per order, no partial reservations
  S3  commit on fulfillment; release on cancel/expiry
  S4  restock only through the Returns flow, only after the goods are
      actually back (PickupConfirmed) — never double-restocked
"""
from __future__ import annotations

from dataclasses import dataclass

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, StockInsufficient
from .events import StockCommitted, StockReleased, StockReserved, StockRestocked

TERMINAL_RESERVATION_STATES = ("committed", "released", "expired")


@dataclass
class Reservation:
    """Child of Stock: a temporary hold on available stock."""

    reservation_id: str
    order_id: str
    quantity: int
    expiry: object  # datetime
    state: str = "active"  # active -> committed | released | expired


class Stock(Aggregate):
    """Aggregate root (identity = product_id): stock of one product."""

    prefix = "stk"

    def __init__(self, product_id: str, available: int, reserved: int = 0) -> None:
        super().__init__()
        if available < 0:
            raise InvariantViolation(f"S1: available must be >= 0, got {available}")
        if reserved < 0:
            raise InvariantViolation(f"S1: reserved must be >= 0, got {reserved}")
        self._product_id = product_id
        self._available = available
        self._reservations: dict[str, Reservation] = {}
        if reserved:
            raise InvariantViolation("S1: pass reserved=0 and issue reservations explicitly")

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        return self._product_id

    @property
    def product_id(self) -> str:
        return self._product_id

    @property
    def available(self) -> int:
        return self._available

    @property
    def reserved(self) -> int:
        return sum(r.quantity for r in self._reservations.values() if r.state == "active")

    # -- behavior -------------------------------------------------------------
    def reserve(self, order_id: str, quantity: int, expiry: object) -> str:
        """S2: all-or-nothing. Emits StockReserved or raises StockInsufficient."""
        if quantity <= 0:
            raise InvariantViolation("quantity must be > 0")
        if self._available < quantity:
            raise StockInsufficient(
                f"S2: cannot reserve {quantity} of {self._product_id}; available={self._available}"
            )
        self._available -= quantity
        rid = _next_id("rsn")
        self._reservations[rid] = Reservation(rid, order_id, quantity, expiry)
        self.raise_event(StockReserved(self._product_id, order_id, quantity))
        return rid

    def _live_reservation(self, reservation_id: str) -> Reservation:
        r = self._reservations.get(reservation_id)
        if r is None:
            raise InvariantViolation(f"unknown reservation {reservation_id}")
        if r.state != "active":
            raise InvariantViolation(
                f"S3: reservation {reservation_id} is {r.state}; only active reservations can change"
            )
        return r

    def commit(self, reservation_id: str) -> None:
        r = self._live_reservation(reservation_id)
        r.state = "committed"
        self.raise_event(StockCommitted(self._product_id, r.order_id, r.quantity))

    def release(self, reservation_id: str, reason: str) -> None:
        r = self._live_reservation(reservation_id)
        r.state = "released"
        self._available += r.quantity
        self.raise_event(StockReleased(self._product_id, r.order_id, r.quantity, reason))

    def expire(self, reservation_id: str) -> None:
        r = self._live_reservation(reservation_id)
        r.state = "expired"
        self._available += r.quantity
        self.raise_event(StockReleased(self._product_id, r.order_id, r.quantity, "expired"))

    def restock(self, return_ref: str, product_qty: dict[str, int] | None = None) -> None:
        """S4: returns re-enter available stock only after the goods are physically back.

        product_qty lets a multi-product return reconcile several Stock aggregates
        from one call (orchestration passes {self.product_id: qty}).
        """
        qty = (product_qty or {self._product_id: 0}).get(self._product_id, 0)
        if qty <= 0:
            raise InvariantViolation("S4: nothing to restock")
        self._available += qty
        self.raise_event(StockRestocked(self._product_id, return_ref, qty))
