"""Inventory bounded context — the Stock aggregate (one per Product).

Responsibility:
    Guard sellable quantity for a single product at the single warehouse.
    This is the anti-oversell spine of the model: every order reserves
    before it can be paid, reservations are committed when goods leave, and
    returned goods re-enter only after the courier physically has them.

Lifecycle / state machine:
    Per-reservation holds move active -> committed (fulfillment, S3) |
    active -> released (cancel/expiry, S3) | active -> expired (TTL lapse).
    Available stock itself only moves down on ``reserve`` and back up on
    ``release``/``expire``/``restock``. Returned units re-enter ``available``
    via ``restock`` (S4) only after ``PickupConfirmed`` (RT5), never earlier,
    and never twice for the same return.
"""
from __future__ import annotations

from dataclasses import dataclass

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, StockInsufficient
from .events import StockCommitted, StockReleased, StockReserved, StockRestocked

TERMINAL_RESERVATION_STATES = ("committed", "released", "expired")


@dataclass
class Reservation:
    """Child entity of Stock: a temporary hold on available stock.

    Role:
        Tracks one order's claim on ``quantity`` units until it is committed
        (goods left), released (cancel), or expired (TTL lapse).

    Members:
        reservation_id: Unique handle for commit/release/expire (S3).
        order_id: Order this hold belongs to.
        quantity: Units held (all-or-nothing per S2).
        expiry: Expiry timestamp (datetime); the expiry sweep releases it.
        state: "active" -> "committed" | "released" | "expired" (S3).

    Invariants:
        S3: only an "active" reservation may transition; terminal states are
            final (see TERMINAL_RESERVATION_STATES).
    """

    reservation_id: str
    order_id: str
    quantity: int
    expiry: object  # datetime
    state: str = "active"  # active -> committed | released | expired


class Stock(Aggregate):
    """Aggregate root (identity = product_id): stock of one product.

    Role:
        Own ``available`` for one product and issue/exPIRE/commit/release
        reservation holds against it. One instance exists per product at the
        single warehouse.

    Members:
        _product_id: Product this stock record guards (aggregate identity).
        _available: Free-to-reserve units (S1: always >= 0; decremented by
            ``reserve``, restored by ``release``/``expire``/``restock``).
        _reservations: reservation_id -> Reservation holds; ``reserved``
            (derived) is the sum over active holds.

    Invariants:
        S1: ``available`` >= 0 always.
        S2: ``reserve(qty)`` fails with StockInsufficient if available < qty
            — all-or-nothing per order, no partial reservations.
        S3: ``commit`` on fulfillment; ``release`` on cancel/expiry; only
            active reservations may transition.
        S4: restock only through the Returns flow, only after the goods are
            actually back (PickupConfirmed) — never double-restocked.

    State transitions (per reservation):
        reserve: available -= qty, new hold active.
        commit: active -> committed (available stays down: goods left).
        release/expire: active -> released/expired, available += qty.
        restock: available += qty (audited return, no reservation involved).
    """

    prefix = "stk"

    def __init__(self, product_id: str, available: int, reserved: int = 0) -> None:
        """Create the stock record for one product.

        Args:
            product_id: Product this record guards (aggregate identity).
            available: Initial free units (S1: must be >= 0).
            reserved: Must be 0; reservations are issued explicitly via
                ``reserve`` so every hold is an audited event (S1/S3).

        Raises:
            InvariantViolation: S1 if available/reserved < 0, or if a
                nonzero reserved seed is passed.
        """
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
        """Return the aggregate identity (the product id).

        Returns:
            str: product this stock record guards.
        """
        return self._product_id

    @property
    def product_id(self) -> str:
        """Return the guarded product id.

        Returns:
            str: product id.
        """
        return self._product_id

    @property
    def available(self) -> int:
        """Return free-to-reserve units (S1: always >= 0).

        Returns:
            int: available count.
        """
        return self._available

    @property
    def reserved(self) -> int:
        """Return units currently held by active reservations (derived).

        Returns:
            int: sum of quantity over active holds.
        """
        return sum(r.quantity for r in self._reservations.values() if r.state == "active")

    # -- behavior -------------------------------------------------------------
    def reserve(self, order_id: str, quantity: int, expiry: object) -> str:
        """Hold stock for one order, all-or-nothing (S1/S2).

        Either the full ``quantity`` is held (available decremented, a
        reservation issued) or nothing happens and StockInsufficient is
        raised — there are intentionally no partial reservations, so the
        checkout orchestration must roll back sibling products on failure.

        Args:
            order_id: Order claiming the stock.
            quantity: Units to hold (must be > 0).
            expiry: Hold expiry timestamp (datetime).

        Returns:
            str: the new reservation handle for commit/release/expire.

        Raises:
            InvariantViolation: if quantity <= 0.
            StockInsufficient: S2 if available < quantity (nothing held).

        Events:
            StockReserved(product_id, order_id, quantity) on success.
        """
        if quantity <= 0:
            raise InvariantViolation("quantity must be > 0")
        if self._available < quantity:
            raise StockInsufficient(
                f"S2: cannot reserve {quantity} of {self._product_id}; available={self._available}"
            )
        # S2: decrement first so available never drifts; the hold is the audit trail.
        self._available -= quantity
        rid = _next_id("rsn")
        self._reservations[rid] = Reservation(rid, order_id, quantity, expiry)
        self.raise_event(StockReserved(self._product_id, order_id, quantity))
        return rid

    def _live_reservation(self, reservation_id: str) -> Reservation:
        """Return an active reservation or raise (S3 guard).

        Args:
            reservation_id: Handle to look up.

        Returns:
            Reservation: the active hold.

        Raises:
            InvariantViolation: if unknown, or if not active (S3: only
                active reservations can change; terminal ones are final).
        """
        r = self._reservations.get(reservation_id)
        if r is None:
            raise InvariantViolation(f"unknown reservation {reservation_id}")
        if r.state != "active":
            raise InvariantViolation(
                f"S3: reservation {reservation_id} is {r.state}; only active reservations can change"
            )
        return r

    def commit(self, reservation_id: str) -> None:
        """Convert a hold into outflow when goods leave (S3).

        Called at carrier handoff; ``available`` stays down because the
        units are no longer in the warehouse.

        Args:
            reservation_id: Active hold to commit.

        Raises:
            InvariantViolation: S3 if unknown or not active.

        Events:
            StockCommitted(product_id, order_id, quantity).
        """
        r = self._live_reservation(reservation_id)
        r.state = "committed"
        self.raise_event(StockCommitted(self._product_id, r.order_id, r.quantity))

    def release(self, reservation_id: str, reason: str) -> None:
        """Return held units to available on cancel/rollback (S1/S3).

        Args:
            reservation_id: Active hold to release.
            reason: Why (cancel reason / "checkout rollback" / etc., for audit).

        Raises:
            InvariantViolation: S3 if unknown or not active.

        Events:
            StockReleased(product_id, order_id, quantity, reason).
        """
        r = self._live_reservation(reservation_id)
        r.state = "released"
        self._available += r.quantity
        self.raise_event(StockReleased(self._product_id, r.order_id, r.quantity, reason))

    def expire(self, reservation_id: str) -> None:
        """Return held units to available when the hold TTL lapses (S1/S3).

        The unpaid order moves Placed -> Expired alongside this.

        Args:
            reservation_id: Active hold whose TTL lapsed.

        Raises:
            InvariantViolation: S3 if unknown or not active.

        Events:
            StockReleased(product_id, order_id, quantity, "expired").
        """
        r = self._live_reservation(reservation_id)
        r.state = "expired"
        self._available += r.quantity
        self.raise_event(StockReleased(self._product_id, r.order_id, r.quantity, "expired"))

    def restock(self, return_ref: str, product_qty: dict[str, int] | None = None) -> None:
        """Re-enter returned units into available after physical return (S4).

        May only be called from the Returns settlement, which runs strictly
        after the courier's possession-transfer scan (PickupConfirmed, RT5).
        Calling earlier would re-sell goods still at the customer's door;
        calling twice for the same return would double-count them — the
        caller must settle each RMA exactly once.

        Args:
            return_ref: RMA id being reconciled (audit trail on the event).
            product_qty: Map passed by orchestration as
                ``{self.product_id: qty}`` so one multi-product return can
                reconcile several Stock aggregates from one call.

        Raises:
            InvariantViolation: S4 if the resolved quantity <= 0.

        Events:
            StockRestocked(product_id, return_ref, quantity).
        """
        qty = (product_qty or {self._product_id: 0}).get(self._product_id, 0)
        if qty <= 0:
            raise InvariantViolation("S4: nothing to restock")
        self._available += qty
        self.raise_event(StockRestocked(self._product_id, return_ref, qty))
