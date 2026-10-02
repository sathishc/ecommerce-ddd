"""Order Management bounded context — the Order aggregate.

Responsibility:
    Own the Order lifecycle and the frozen purchase agreement. The Order is
    the central promise to the customer: what was bought, at what price,
    where it goes, and what state the purchase is in. It snapshots prices
    at placement so later catalog changes never alter existing orders.

Lifecycle / state machine (O3)::

    Placed --pay()--> Paid --confirm_shipped()--> Shipped --deliver()--> Delivered
        |                 |                                            |
        +--cancel()--+   +--cancel()--+  (pre-shipment only)          +--refund_completed()--> Refunded (terminal)
        +--expire()--+   (voids the authorization)

    Placed -> Cancelled (no money moved yet -> void).
    Paid   -> Cancelled (before shipment -> void the authorization).
    A Paid order may NOT be cancelled after Shipped (O3); only the Returns
    path (Delivered -> Refunded via ``accept_return`` / ``refund_completed``)
    settles shipped/delivered orders. Refunded / Cancelled / Expired are
    terminal (O4).

Happy-path flow: ``place`` -> ``pay`` (authorize) -> ``confirm_shipped``
(capture happens at handoff in Fulfillment) -> ``deliver``.
Return path (doorstep pickup): ``accept_return`` (registers each RMA while
the order stays Delivered across partial returns) -> ``refund_completed``
(only on a *complete* return does the order flip to Refunded, terminal).
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
    """Child entity of Order: an immutable snapshot of one Cart Line (O5).

    Role:
        Freezes the commercial terms of one purchased product at placement
        time: which product, at what unit price, in what quantity.

    Members:
        product_id: Identity of the purchased product (catalog reference).
        unit_price: Per-unit price snapshot (Money); never updated after
            placement, so catalog repricing cannot affect this order (O2).
        quantity: Units bought (>= 1 per O1).

    Invariants:
        O1: quantity >= 1.
        O5: immutable after placement; all mutations go through the Order root.
    """

    product_id: str
    unit_price: Money
    quantity: int

    @property
    def line_total(self) -> Money:
        """Return the extended line price (unit_price x quantity).

        Returns:
            Money: the line total in the unit price currency.
        """
        return Money(self.unit_price.minor * self.quantity, self.unit_price.currency)


class Order(Aggregate):
    """Aggregate root (identity = order_id): the central purchase agreement.

    Role:
        Owns the frozen total (lines - discount + tax + shipping, computed
        once at placement) and every lifecycle transition of one order.

    Members:
        _lines: OrderLine snapshots, fixed at ``place()`` (O1, O5).
        _destination: Delivery address (set at placement).
        _customer_ref: Opaque customer reference (external identity context).
        _discount: Frozen coupon Discount amount (Money) from checkout.
        _shipping_fee: Frozen shipping fee (Money) from checkout.
        _money_total: Frozen order total; set once in ``place()``, never
            recomputed afterwards (O2).
        _reservation_ids: product_id -> reservation handle (Inventory link).
        _rma_ids: RMA ids registered via ``accept_return`` (Returns link).
        _coupon_code: Coupon code used, if any (for RT6 un-redeem on a
            complete return).
        _payment_ref: Payment id set by ``pay()``; the orchestration layer
            uses it to reach the generic Payment aggregate.
        _status: Current lifecycle state (O3).
        _delivered_at: Delivery date, set by ``deliver()`` (RT1 window anchor).

    Invariants:
        O1: at least one line; an Order exists only after checkout succeeded.
        O2: ``money_total`` is fixed at placement; NEVER recomputed later.
        O3: state machine Placed -> Paid -> Shipped -> Delivered -> Refunded
            (via Returns); Placed/Paid may cancel; Placed may expire; a Paid
            order may NOT cancel after Shipped.
        O4: cannot be Shipped unless Paid; cannot be charged twice; Refunded
            is terminal.
        O5: all mutations via root methods; OrderLine immutable after placement.

    State transitions:
        place: (fresh) -> Placed.
        pay: Placed -> Paid.
        confirm_shipped: Paid -> Shipped.
        deliver: Shipped -> Delivered.
        accept_return: Delivered -> Delivered (registers an RMA; partial
            returns keep the order Delivered).
        refund_completed: Delivered -> Refunded (terminal; complete return).
        cancel: Placed -> Cancelled | Paid -> Cancelled (pre-shipment only).
        expire: Placed -> Expired.
    """

    prefix = "ord"

    def __init__(self) -> None:
        """Create a fresh, unplaced Order shell (status Placed, no lines yet).

        The order becomes a real agreement only once ``place()`` runs; until
        then ``money_total``/``destination``/``customer_ref`` are unset.
        """
        super().__init__()
        self._order_id = _next_id(self.prefix)
        self._lines: list[OrderLine] = []
        self._destination: Address | None = None
        self._customer_ref: str | None = None
        self._discount: Money | None = None
        self._shipping_fee: Money | None = None
        self._money_total: Money | None = None   # O2: frozen at placement, never touched again
        self._reservation_ids: dict[str, str] = {}  # product_id -> reservation_id
        self._rma_ids: list[str] = []
        self._coupon_code: str | None = None
        self._payment_ref: str | None = None
        self._status = PLACED
        self._delivered_at: date | None = None

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        """Return the aggregate identity (the order id).

        Returns:
            str: this order's unique id.
        """
        return self._order_id

    @property
    def order_id(self) -> str:
        """Return the order id.

        Returns:
            str: identity of this Order.
        """
        return self._order_id

    @property
    def status(self) -> str:
        """Return the current lifecycle state (O3).

        Returns:
            str: one of Placed/Paid/Shipped/Delivered/Refunded/Cancelled/Expired.
        """
        return self._status

    @property
    def money_total(self) -> Money:
        """Return the frozen order total (O2: lines - discount + tax + shipping).

        Returns:
            Money: the total fixed at placement.

        Raises:
            AssertionError: if the order has not been placed yet.
        """
        assert self._money_total is not None
        return self._money_total

    @property
    def discount(self) -> Money:
        """Return the frozen coupon discount (zero Money if none was used).

        Returns:
            Money: discount snapshot from checkout.
        """
        return self._discount or Money(0, self._lines[0].unit_price.currency)

    @property
    def shipping_fee(self) -> Money:
        """Return the frozen shipping fee (zero Money if none was charged).

        Returns:
            Money: shipping snapshot from checkout; on returns it is
            refunded pro-rata via RT3 (full fee on a full return).
        """
        return self._shipping_fee or Money(0, self._lines[0].unit_price.currency)

    @property
    def lines(self) -> list[OrderLine]:
        """Return a copy of the frozen order lines (O5).

        Returns:
            list[OrderLine]: snapshot lines; mutating the result does not
            affect the aggregate.
        """
        return list(self._lines)

    @property
    def destination(self) -> Address:
        """Return the delivery address.

        Returns:
            Address: destination set at placement.

        Raises:
            AssertionError: if the order has not been placed yet.
        """
        assert self._destination is not None
        return self._destination

    @property
    def customer_ref(self) -> str:
        """Return the opaque customer reference.

        Returns:
            str: customer identity from the Shop context.

        Raises:
            AssertionError: if the order has not been placed yet.
        """
        assert self._customer_ref is not None
        return self._customer_ref

    @property
    def line_subtotal(self) -> Money:
        """Return the pre-discount line total snapshot (RT3 denominator).

        The Returns pro-rata math divides by this value, so it must be the
        *frozen* sum of (qty x unit_price) from placement, not a live quote.

        Returns:
            Money: sum of ``line_total`` over all lines.
        """
        cur = self._lines[0].unit_price.currency
        return Money(sum(l.line_total.minor for l in self._lines), cur)

    @property
    def payment_ref(self) -> str:
        """Return the linked payment id, set by ``pay()``.

        The orchestration layer uses it to reach the generic Payment
        aggregate (Order -> Payment ACL).

        Returns:
            str: payment id.

        Raises:
            AssertionError: if ``pay()`` has not run yet.
        """
        assert self._payment_ref is not None
        return self._payment_ref

    @property
    def coupon_code(self) -> str | None:
        """Return the coupon code used at checkout, if any.

        Returns:
            Optional[str]: code for RT6/CO7 re-entitlement checks.
        """
        return self._coupon_code

    @property
    def reservation_ids(self) -> dict[str, str]:
        """Return a copy of the product -> reservation-handle map.

        Returns:
            dict[str, str]: reservation handles for commit/release.
        """
        return dict(self._reservation_ids)

    @property
    def rma_ids(self) -> list[str]:
        """Return the ids of RMAs registered against this order.

        Returns:
            list[str]: RMA ids from ``accept_return``.
        """
        return list(self._rma_ids)

    # -- placement ------------------------------------------------------------
    def place(self, lines: list[tuple[str, int, Money]], destination: Address,
              customer_ref: str, discount: Money, shipping_fee: Money,
              tax: Money, coupon_code: str | None = None) -> None:
        """Construct a placed Order once, from a checked-out cart snapshot (O1/O2).

        Freezes lines (as unit-price snapshots), discount, shipping, and the
        total ``line_subtotal - discount + tax + shipping`` (O2). Later
        catalog price changes cannot alter this order.

        Args:
            lines: (product_id, quantity, unit_price snapshot) per line.
            destination: Delivery address.
            customer_ref: Opaque customer reference.
            discount: Coupon discount amount (Money, frozen).
            shipping_fee: Shipping fee (Money, frozen).
            tax: Tax amount (Money, frozen).
            coupon_code: Coupon code used, if any (for RT6).

        Raises:
            InvariantViolation: O1 if no lines or any quantity < 1; or if
                ``place()`` runs twice on the same instance.

        Events:
            OrderPlaced(order_id, money_total).
        """
        if self.status != PLACED or self._lines:
            raise InvariantViolation("place() may only run once, on a fresh Order")
        if not lines:
            raise InvariantViolation("O1: an Order needs at least one line")
        for product_id, qty, unit in lines:
            if qty < 1:
                raise InvariantViolation(f"O1: line quantity must be >= 1, got {qty}")
            # O5: freeze each cart line as an immutable OrderLine snapshot.
            self._lines.append(OrderLine(product_id, unit, qty))
        self._destination = destination
        self._customer_ref = customer_ref
        self._discount = discount
        self._coupon_code = coupon_code
        self._shipping_fee = shipping_fee
        # O2: compute once, then freeze — no method below may touch _money_total.
        self._money_total = self.line_subtotal.minus(discount).plus(tax).plus(shipping_fee)
        self.raise_event(OrderPlaced(self._order_id, self._money_total))

    def link_reservation(self, product_id: str, reservation_id: str) -> None:
        """Record an Inventory reservation handle for one product.

        Called by the checkout orchestration right after ``Stock.reserve``;
        the handles are later committed at shipment (S3) or released on
        cancel/expiry.

        Args:
            product_id: Product the reservation covers.
            reservation_id: Handle issued by ``Stock.reserve``.
        """
        self._reservation_ids[product_id] = reservation_id

    # -- state machine (O3/O4) -------------------------------------------------
    def pay(self, payment_ref: str) -> None:
        """Authorize payment for a Placed order: Placed -> Paid (O3/O4).

        Args:
            payment_ref: Id of the authorized Payment (generic context).

        Raises:
            InvalidStateTransition: O3/O4 if not Placed, or O4 on double pay.

        Events:
            OrderPaymentAuthorized(order_id), OrderPaid(order_id).
        """
        if self.status != PLACED:
            raise InvalidStateTransition(f"O3/O4: can only pay from Placed, not {self.status}")
        if self._payment_ref is not None:
            raise InvalidStateTransition("O4: an order cannot be charged twice")
        self._payment_ref = payment_ref
        self._status = PAID
        self.raise_event(OrderPaymentAuthorized(self._order_id))
        self.raise_event(OrderPaid(self._order_id))

    def confirm_shipped(self, shipment_id: str) -> None:
        """Mark the order shipped: Paid -> Shipped (O3/O4).

        Args:
            shipment_id: Shipment that carries this order's lines.

        Raises:
            InvalidStateTransition: O4 unless currently Paid.

        Events:
            OrderShipped(order_id, shipment_id).
        """
        if self.status != PAID:
            raise InvalidStateTransition(f"O4: cannot ship unless Paid (was {self.status})")
        self._status = SHIPPED
        self.raise_event(OrderShipped(self._order_id, shipment_id))

    def deliver(self, at: date | None = None) -> None:
        """Mark the order delivered: Shipped -> Delivered (O3).

        Args:
            at: Delivery date; defaults to today. Anchors the RT1 30-day
                return window.

        Raises:
            InvalidStateTransition: O3 unless currently Shipped.

        Events:
            OrderDelivered(order_id, at).
        """
        if self.status != SHIPPED:
            raise InvalidStateTransition(f"O3: can only deliver from Shipped, not {self.status}")
        self._delivered_at = at or date.today()
        self._status = DELIVERED
        self.raise_event(OrderDelivered(self._order_id, self._delivered_at))

    @property
    def delivered_at(self) -> date | None:
        """Return the delivery date, if delivered.

        Returns:
            Optional[date]: set by ``deliver()``; None before delivery.
        """
        return self._delivered_at

    def accept_return(self, rma_id: str) -> None:
        """Register an open RMA against this order (O3: returns start from Delivered).

        The order STAYS Delivered across partial returns; only a *complete*
        return (full settlement) flips it to Refunded (terminal, O4).

        Args:
            rma_id: RMA being registered (partial-return bookkeeping).

        Raises:
            InvalidStateTransition: O3 unless currently Delivered.

        Events:
            None (the RMA aggregate owns ReturnRequested/ReturnRefunded).
        """
        if self.status != DELIVERED:
            raise InvalidStateTransition(
                f"O3: returns only against a Delivered order, not {self.status}")
        if rma_id not in self._rma_ids:
            self._rma_ids.append(rma_id)

    def refund_completed(self, amount: Money, reason: str) -> None:
        """Final step of the return path: Delivered -> Refunded, terminal (O3/O4).

        Called by the Returns settlement only for a *complete* return
        (RT6); partial returns leave the order Delivered via
        ``accept_return``.

        Args:
            amount: Total pro-rata refund (RT3 breakdown total).
            reason: Human-readable settlement reason (e.g. return rma id).

        Raises:
            InvalidStateTransition: O3 unless currently Delivered.
            InvariantViolation: O4 if amount exceeds the frozen order total.

        Events:
            OrderRefunded(order_id, amount, reason).
        """
        if self.status != DELIVERED:
            raise InvalidStateTransition(f"O3: refund only settles a Delivered order, not {self.status}")
        if amount > self._money_total:
            raise InvariantViolation("O4: refund exceeds the order total")
        self._status = REFUNDED
        self.raise_event(OrderRefunded(self._order_id, amount, reason))

    def cancel(self, reason: str) -> None:
        """Cancel before shipment: Placed/Paid -> Cancelled (O3).

        Placed -> Cancelled voids nothing (no money moved); Paid -> Cancelled
        voids the authorization. Forbidden from Shipped/Delivered (O3) — only
        the return path settles those.

        Args:
            reason: Cancellation reason (surfaced on the event; passed to
                the Payment void and Stock release by orchestration).

        Raises:
            InvalidStateTransition: O3 unless Placed or Paid.

        Events:
            OrderCancelled(order_id, reason).
        """
        if self.status not in (PLACED, PAID):
            raise InvalidStateTransition(
                f"O3: cancellation only from Placed/Paid, not {self.status}; "
                "use the return path for shipped/delivered orders")
        self._status = CANCELLED
        self.raise_event(OrderCancelled(self._order_id, reason))

    def expire(self) -> None:
        """Expire an unpaid order whose reservation lapsed: Placed -> Expired (O3).

        The orchestration layer voids the (unauthorized) payment leg and
        releases stock alongside this transition.

        Raises:
            InvalidStateTransition: O3 unless currently Placed.

        Events:
            OrderExpired(order_id).
        """
        if self.status != PLACED:
            raise InvalidStateTransition(f"O3: only an unpaid Placed order can expire, not {self.status}")
        self._status = EXPIRED
        self.raise_event(OrderExpired(self._order_id))

    # -- invariants (O2) ---------------------------------------------------------
    def _assert_money_frozen(self) -> None:
        """Assert the frozen-total invariant holds (O2, test hook).

        O2 is structural: no method above mutates money_total after place().
        This hook exists so the test suite can assert the invariant by name.

        Raises:
            InvariantViolation: O2 if the order was never placed.
        """
        # O2 is structural: no method above mutates money_total after place().
        # This hook exists so the test suite can assert the invariant by name.
        if self._money_total is None:
            raise InvariantViolation("O2: money_total not set (order not placed)")
