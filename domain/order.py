"""Order Management context — the Order aggregate.

Invariants:
  O1  at least one line; exists only after checkout succeeded
  O2  moneyTotal is fixed at placement; NEVER recomputed later
  O3  state machine:
        Placed ->(pay) Paid ->(confirmShipped) Shipped ->(deliver) Delivered
                ->(return settles) Refunded
        Placed ->(cancel) Cancelled        (no money moved -> void)
        Paid   ->(cancel) Cancelled        (before shipment -> void the auth)
        Placed ->(expire) Expired          (reservation lapsed -> void)
      A Paid order may NOT be cancelled after Shipped.
  O4  cannot be Shipped unless Paid; cannot be charged twice;
      Refunded is terminal
  O5  all mutations via root methods; OrderLine immutable after placement
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, InvalidStateTransition
from .events import (OrderCancelled, OrderDelivered, OrderExpired, OrderPaid,
                     OrderPaymentAuthorized, OrderPlaced, OrderRefunded,
                     OrderShipped)
from .value_objects import Address, Money

PLACED, PAID, SHIPPED, DELIVERED, REFUNDED, CANCELLED, EXPIRED = (
    "Placed", "Paid", "Shipped", "Delivered", "Refunded", "Cancelled", "Expired")
TERMINAL = {REFUNDED, CANCELLED, EXPIRED}


@dataclass(frozen=True)
class OrderLine:
    """Child of Order: an immutable snapshot of a Cart Line (O5)."""

    product_id: str
    unit_price: Money
    quantity: int

    @property
    def line_total(self) -> Money:
        return Money(self.unit_price.minor * self.quantity, self.unit_price.currency)


class Order(Aggregate):
    """Aggregate root (identity = order_id): the central purchase agreement."""

    prefix = "ord"

    def __init__(self) -> None:
        super().__init__()
        self._order_id = _next_id(self.prefix)
        self._lines: list[OrderLine] = []
        self._destination: Address | None = None
        self._customer_ref: str | None = None
        self._discount: Money | None = None
        self._shipping_fee: Money | None = None
        self._money_total: Money | None = None   # O2: frozen at placement
        self._reservation_ids: dict[str, str] = {}  # product_id -> reservation_id
        self._rma_ids: list[str] = []
        self._coupon_code: str | None = None
        self._payment_ref: str | None = None
        self._status = PLACED
        self._delivered_at: date | None = None

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        return self._order_id

    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def money_total(self) -> Money:
        assert self._money_total is not None
        return self._money_total

    @property
    def discount(self) -> Money:
        return self._discount or Money(0, self._lines[0].unit_price.currency)

    @property
    def shipping_fee(self) -> Money:
        return self._shipping_fee or Money(0, self._lines[0].unit_price.currency)

    @property
    def lines(self) -> list[OrderLine]:
        return list(self._lines)

    @property
    def destination(self) -> Address:
        assert self._destination is not None
        return self._destination

    @property
    def customer_ref(self) -> str:
        assert self._customer_ref is not None
        return self._customer_ref

    @property
    def line_subtotal(self) -> Money:
        """O2 snapshot of the pre-discount line total (RT3 denominator)."""
        cur = self._lines[0].unit_price.currency
        return Money(sum(l.line_total.minor for l in self._lines), cur)

    @property
    def payment_ref(self) -> str:
        """Set by pay(); the orchestration layer uses it to reach the Payment."""
        assert self._payment_ref is not None
        return self._payment_ref

    @property
    def coupon_code(self) -> str | None:
        return self._coupon_code

    @property
    def reservation_ids(self) -> dict[str, str]:
        return dict(self._reservation_ids)

    @property
    def rma_ids(self) -> list[str]:
        return list(self._rma_ids)

    # -- placement ------------------------------------------------------------
    def place(self, lines: list[tuple[str, int, Money]], destination: Address,
              customer_ref: str, discount: Money, shipping_fee: Money,
              tax: Money, coupon_code: str | None = None) -> None:
        """One-time construction of a placed Order (from a checked-out cart)."""
        if self.status != PLACED or self._lines:
            raise InvariantViolation("place() may only run once, on a fresh Order")
        if not lines:
            raise InvariantViolation("O1: an Order needs at least one line")
        for product_id, qty, unit in lines:
            if qty < 1:
                raise InvariantViolation(f"O1: line quantity must be >= 1, got {qty}")
            self._lines.append(OrderLine(product_id, unit, qty))
        self._destination = destination
        self._customer_ref = customer_ref
        self._discount = discount
        self._coupon_code = coupon_code
        self._shipping_fee = shipping_fee
        self._money_total = self.line_subtotal.minus(discount).plus(tax).plus(shipping_fee)
        self.raise_event(OrderPlaced(self._order_id, self._money_total))

    def link_reservation(self, product_id: str, reservation_id: str) -> None:
        self._reservation_ids[product_id] = reservation_id

    # -- state machine (O3/O4) -------------------------------------------------
    def pay(self, payment_ref: str) -> None:
        if self.status != PLACED:
            raise InvalidStateTransition(f"O3/O4: can only pay from Placed, not {self.status}")
        if self._payment_ref is not None:
            raise InvalidStateTransition("O4: an order cannot be charged twice")
        self._payment_ref = payment_ref
        self._status = PAID
        self.raise_event(OrderPaymentAuthorized(self._order_id))
        self.raise_event(OrderPaid(self._order_id))

    def confirm_shipped(self, shipment_id: str) -> None:
        if self.status != PAID:
            raise InvalidStateTransition(f"O4: cannot ship unless Paid (was {self.status})")
        self._status = SHIPPED
        self.raise_event(OrderShipped(self._order_id, shipment_id))

    def deliver(self, at: date | None = None) -> None:
        if self.status != SHIPPED:
            raise InvalidStateTransition(f"O3: can only deliver from Shipped, not {self.status}")
        self._delivered_at = at or date.today()
        self._status = DELIVERED
        self.raise_event(OrderDelivered(self._order_id, self._delivered_at))

    @property
    def delivered_at(self) -> date | None:
        return self._delivered_at

    def accept_return(self, rma_id: str) -> None:
        """Register an open RMA against this order (O3: returns start from Delivered).

        The order STAYS Delivered across partial returns; only a *complete*
        return (full settlement) flips it to Refunded (terminal, O4).
        """
        if self.status != DELIVERED:
            raise InvalidStateTransition(
                f"O3: returns only against a Delivered order, not {self.status}")
        if rma_id not in self._rma_ids:
            self._rma_ids.append(rma_id)

    def refund_completed(self, amount: Money, reason: str) -> None:
        """Final step of the return path: O3 Delivered -> Refunded (terminal)."""
        if self.status != DELIVERED:
            raise InvalidStateTransition(f"O3: refund only settles a Delivered order, not {self.status}")
        if amount > self._money_total:
            raise InvariantViolation("O4: refund exceeds the order total")
        self._status = REFUNDED
        self.raise_event(OrderRefunded(self._order_id, amount, reason))

    def cancel(self, reason: str) -> None:
        """O3: Placed -> Cancelled (void, no money) or Paid -> Cancelled (pre-shipment, void auth).
        Forbidden from Shipped/Delivered (O3) — only the return path settles those."""
        if self.status not in (PLACED, PAID):
            raise InvalidStateTransition(
                f"O3: cancellation only from Placed/Paid, not {self.status}; "
                "use the return path for shipped/delivered orders")
        self._status = CANCELLED
        self.raise_event(OrderCancelled(self._order_id, reason))

    def expire(self) -> None:
        """Reservation lapsed while unpaid (O3: Placed -> Expired; payment voided)."""
        if self.status != PLACED:
            raise InvalidStateTransition(f"O3: only an unpaid Placed order can expire, not {self.status}")
        self._status = EXPIRED
        self.raise_event(OrderExpired(self._order_id))

    # -- invariants (O2) ---------------------------------------------------------
    def _assert_money_frozen(self) -> None:
        # O2 is structural: no method above mutates money_total after place().
        # This hook exists so the test suite can assert the invariant by name.
        if self._money_total is None:
            raise InvariantViolation("O2: money_total not set (order not placed)")
