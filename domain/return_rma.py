"""Returns bounded context — the Return (RMA) aggregate.

Responsibility:
    Own the customer-initiated return of delivered goods collected via
    doorstep pickup: request validation (window + quantities), approval,
    pickup linkage, possession-transfer gating, and the pro-rata refund
    settlement that drives Payment refund + Inventory restock + Order
    finalization (+ coupon re-entitlement on a complete return).

Lifecycle / state machine (RT4/RT5)::

    Requested --approve()--> Approved --link_pickup()--> PickupScheduled
        |                         |                            |
        +------reject()-----+-----+----reject()----------------+--reject()--+
        |                   |     (rejection only before       |            |
        |                   |      goods are collected, RT4)    v            v
        |                   |                      PickedUp --receive_goods()--> GoodsReceived --settle()--> Refunded (terminal)
        |                   |                      (via on_pickup_confirmed, PK3/RT5)
        v                   v
      Rejected (terminal) Rejected (terminal)                              Rejected impossible after pickup

    Customer-initiated only: staff may approve/reject, but only a customer
    opens an RMA (RT1). The customer does NOT ship back — the platform's
    courier collects at the door; the courier's on-site scan + photo is the
    possession-transfer event (PickedUp) that gates settlement and restock
    (RT5/S4). Refund math is pro-rata (RT3, model §5): goods + tax +
    shipping all scale with the fraction of the order's line value returned;
    a full return refunds the full shipping fee. Pickup itself is free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, InvalidStateTransition
from .events import (ReturnApproved, ReturnGoodsReceived, ReturnRefunded,
                     ReturnRejected, ReturnRequested)
from .value_objects import Address, Money, RefundBreakdown, PickupEvidence

REQUESTED, APPROVED, PICKUP_SCHEDULED, PICKED_UP, GOODS_RECEIVED, REFUNDED, REJECTED = (
    "Requested", "Approved", "PickupScheduled", "PickedUp", "GoodsReceived",
    "Refunded", "Rejected")
TERMINAL = {REFUNDED, REJECTED}

DEFAULT_WINDOW_DAYS = 30  # RT1 (assumed 30 days from delivery, configurable)


@dataclass(frozen=True)
class ReturnLine:
    """Child entity of Return: one product + quantity to return.

    Role:
        The customer-requested return quantity for one shipped product.

    Members:
        product_id: Product to take back.
        quantity: Units to take back (>= 1; <= shipped qty per RT2).

    Invariants:
        RT2: quantity >= 1 and <= the original shipped qty for that product
            (no over-returning); enforced in ``Return.__init__``.
    """

    product_id: str
    quantity: int


@dataclass(frozen=True)
class _OrderFacts:
    """Snapshot of the delivered order taken at request time (ACL from Order Mgmt).

    Role:
        Freeze everything RT1/RT3 need — order identity, shipped lines with
        unit prices, discount, shipping fee, tax rate, delivery date — so
        later catalog or order changes cannot alter return validation or the
        pro-rata refund math.

    Members:
        order_id: Delivered order under return.
        customer_id: Customer who owns the order (coupon re-entitlement key).
        destination: Doorstep where the Pickup collects (Fulfillment link).
        shipped_lines: (product_id, qty, unit_price) tuples; RT2 bounds and
            the RT3 originalLineSubtotal denominator come from these.
        discount: Frozen coupon discount (Money; RT3 discount-share numerator).
        shipping_fee: Frozen shipping fee (Money; RT3 shipping-refund base).
        tax_num/tax_den: Tax fraction (v1: 0/100, pluggable).
        delivered_at: Delivery date (RT1 window anchor).

    Invariants:
        RT1: delivered_at must be set (order must be Delivered).
        RT3: shipped line subtotal must be positive (division denominator).
    """

    order_id: str
    customer_id: str
    destination: Address
    shipped_lines: tuple[tuple[str, int, Money], ...]  # (product_id, qty, unit_price)
    discount: Money
    shipping_fee: Money
    tax_num: int
    tax_den: int
    delivered_at: date


class Return(Aggregate):
    """Aggregate root (identity = rma_id): one customer return against an order.

    Role:
        Validate the return window and quantities, link the Fulfillment
        Pickup that collects the goods, gate settlement on physical
        possession, then compute the pro-rata refund the orchestration layer
        applies across contexts.

    Members:
        _facts: Frozen _OrderFacts snapshot (RT1/RT3 source of truth).
        _lines: ReturnLine list (RT2-validated).
        _reason: Customer's return reason.
        _requested_at: Request timestamp (RT1 window check endpoint).
        _window_days: Return window in days (RT1; default 30, configurable).
        _pickup_id: Linked Pickup id (None until ``link_pickup``).
        _evidence: Collection evidence from ``on_pickup_confirmed`` (PK5).
        _settlement: RefundBreakdown computed by ``settle`` (RT3).
        _status: Requested | Approved | PickupScheduled | PickedUp |
            GoodsReceived | Refunded (terminal) | Rejected (terminal).

    Invariants:
        RT1: opened only against a Delivered order, within the return window
            (30 days from delivery, configurable).
        RT2: returnLines qty <= shipped qty per product (no over-returning).
        RT3: refund is pro-rata — goods + tax + shipping scale with the
            returned fraction of line value (see ``_compute_refund``).
        RT4: Refunded is terminal; rejection only before goods collected.
        RT5: goods collected at the door via a Pickup; settlement and
            restock only after the courier actually has them.
        RT6: a COMPLETE return triggers Coupon.unredeem; a PARTIAL return
            keeps the redemption burned.

    State transitions:
        __init__: (new) -> Requested.
        approve: Requested -> Approved.
        link_pickup: Approved -> PickupScheduled.
        on_pickup_confirmed: PickupScheduled -> PickedUp.
        receive_goods: PickedUp -> GoodsReceived.
        settle: GoodsReceived -> Refunded (terminal).
        reject: Requested/Approved/PickupScheduled -> Rejected (terminal).
    """

    prefix = "rma"

    def __init__(self, facts: _OrderFacts, return_lines: list[tuple[str, int]],
                 reason: str, requested_at: datetime,
                 window_days: int = DEFAULT_WINDOW_DAYS) -> None:
        """Open an RMA against a delivered order (RT1/RT2).

        Args:
            facts: Frozen order snapshot (shipped lines, prices, discount,
                shipping, delivery date).
            return_lines: (product_id, quantity) pairs requested back.
            reason: Customer's return reason.
            requested_at: Request timestamp (RT1 window endpoint).
            window_days: Return window in days (RT1; default 30).

        Raises:
            InvariantViolation: RT2 if no lines, qty < 1, unknown product,
                or qty exceeds shipped; RT1 if the order is not Delivered,
                the request predates delivery, or the window lapsed.

        Events:
            ReturnRequested(rma_id, order_id).
        """
        super().__init__()
        # RT2: quantity bounds (no over-returning)
        shipped = {pid: qty for pid, qty, _ in facts.shipped_lines}
        if not return_lines:
            raise InvariantViolation("RT2: a return needs at least one line")
        for pid, qty in return_lines:
            if qty < 1:
                raise InvariantViolation(f"RT2: quantity must be >= 1, got {qty}")
            if pid not in shipped:
                raise InvariantViolation(f"RT2: {pid} was never shipped on this order")
            if qty > shipped[pid]:
                raise InvariantViolation(
                    f"RT2: returning {qty} of {pid} exceeds shipped {shipped[pid]}")
        # RT1: window check (delivered_at is a snapshot; now is the request time)
        if facts.delivered_at is None:
            raise InvariantViolation("RT1: the order must be Delivered")
        age_days = (requested_at.date() - facts.delivered_at).days
        if age_days < 0:
            raise InvariantViolation("RT1: cannot return before delivery")
        if age_days > window_days:
            raise InvariantViolation(
                f"RT1: return window is {window_days} days; request is {age_days} days after delivery")
        self._rma_id = _next_id(self.prefix)
        self._facts = facts
        self._lines = [ReturnLine(pid, qty) for pid, qty in return_lines]
        self._reason = reason
        self._requested_at = requested_at
        self._window_days = window_days
        self._pickup_id: str | None = None
        self._evidence: PickupEvidence | None = None
        self._settlement: RefundBreakdown | None = None
        self._status = REQUESTED
        self.raise_event(ReturnRequested(self._rma_id, facts.order_id))

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        """Return the aggregate identity (the RMA id).

        Returns:
            str: this return's unique id.
        """
        return self._rma_id

    @property
    def rma_id(self) -> str:
        """Return the RMA id.

        Returns:
            str: identity of this return.
        """
        return self._rma_id

    @property
    def order_id(self) -> str:
        """Return the delivered order under return.

        Returns:
            str: order id from the frozen facts.
        """
        return self._facts.order_id

    @property
    def destination(self) -> Address:
        """Return the doorstep where the Pickup collects.

        Returns:
            Address: delivery address snapshot (Fulfillment link).
        """
        return self._facts.destination

    @property
    def customer_id(self) -> str:
        """Return the owning customer id (coupon re-entitlement key, RT6).

        Returns:
            str: customer from the frozen facts.
        """
        return self._facts.customer_id

    @property
    def status(self) -> str:
        """Return the current RMA state (RT4/RT5).

        Returns:
            str: Requested | Approved | PickupScheduled | PickedUp |
            GoodsReceived | Refunded | Rejected.
        """
        return self._status

    @property
    def lines(self) -> list[ReturnLine]:
        """Return a copy of the return lines (RT2-validated).

        Returns:
            list[ReturnLine]: requested-back lines.
        """
        return list(self._lines)

    @property
    def settlement(self) -> RefundBreakdown | None:
        """Return the computed refund breakdown, if settled (RT3).

        Returns:
            Optional[RefundBreakdown]: set by ``settle``; None before.
        """
        return self._settlement

    @property
    def is_complete(self) -> bool:
        """Report whether every shipped line is returned in full (RT6).

        A complete return triggers Coupon.unredeem (customer regains the
        code) and flips the Order to Refunded; a partial return keeps the
        redemption burned and leaves the Order Delivered.

        Returns:
            bool: True iff returned (product -> qty) exactly equals shipped.
        """
        shipped = {pid: qty for pid, qty, _ in self._facts.shipped_lines}
        returned = {l.product_id: l.quantity for l in self._lines}
        return all(returned.get(pid) == qty for pid, qty in shipped.items()) and \
            set(returned) == set(shipped)

    # -- state machine ---------------------------------------------------------
    def approve(self) -> None:
        """Approve a requested RMA: Requested -> Approved.

        Raises:
            InvalidStateTransition: unless currently Requested.

        Events:
            ReturnApproved(rma_id).
        """
        if self._status != REQUESTED:
            raise InvalidStateTransition(f"can only approve a Requested RMA, not {self._status}")
        self._status = APPROVED
        self.raise_event(ReturnApproved(self._rma_id))

    def link_pickup(self, pickup_id: str) -> None:
        """Link the Fulfillment Pickup scheduled for this RMA (PK1).

        The orchestration layer creates the Pickup, assigns a courier, then
        calls this to bind the two aggregates.

        Args:
            pickup_id: Pickup collecting this RMA's goods.

        Raises:
            InvalidStateTransition: unless currently Approved.

        Events:
            None (PickupScheduled is owned by the Pickup aggregate).
        """
        if self._status != APPROVED:
            raise InvalidStateTransition(f"pickup links to an Approved RMA, not {self._status}")
        self._pickup_id = pickup_id
        self._status = PICKUP_SCHEDULED

    def on_pickup_confirmed(self, pickup_id: str, evidence: PickupEvidence) -> None:
        """Record the courier's possession-transfer scan (PK3/RT5).

        Only this gates the subsequent settlement: no pickup confirmation,
        no refund/restock. The pickup id must match the linked pickup and
        photo evidence is mandatory (PK5).

        Args:
            pickup_id: Confirming pickup (must equal the linked one).
            evidence: Collection proof (photo_ref required).

        Raises:
            InvalidStateTransition: RT5 unless PickupScheduled with a
                matching pickup id.
            InvariantViolation: PK5 if evidence is missing or has no photo.

        Events:
            None (ReturnGoodsReceived fires in ``receive_goods``).
        """
        if self._status != PICKUP_SCHEDULED or pickup_id != self._pickup_id:
            raise InvalidStateTransition(
                f"RT5: pickup {pickup_id} does not match the linked pickup for this RMA")
        if evidence is None or not evidence.photo_ref:
            raise InvariantViolation("PK5: evidence (photo) is mandatory")
        self._evidence = evidence
        self._status = PICKED_UP

    def receive_goods(self) -> None:
        """Reconcile the collected goods: PickedUp -> GoodsReceived (RT5).

        Derived from PickupConfirmed (model: ReturnGoodsReceived). After
        this, ``settle`` may compute the refund and orchestration may
        restock Inventory (S4).

        Raises:
            InvalidStateTransition: RT5 unless currently PickedUp.

        Events:
            ReturnGoodsReceived(rma_id).
        """
        if self._status != PICKED_UP:
            raise InvalidStateTransition(f"RT5: goods received only after PickedUp, not {self._status}")
        self._status = GOODS_RECEIVED
        self.raise_event(ReturnGoodsReceived(self._rma_id))

    def _compute_refund(self) -> RefundBreakdown:
        """Compute the pro-rata refund breakdown (RT3, model §5).

        Pro-rata rule (round-half-up throughout)::

            originalLineSubtotal  = Σ (originalQty x originalUnitPrice)
            discountShare         = discount / originalLineSubtotal
            returnedValue         = Σ (returnedQty x originalUnitPrice)
            goodsRefund           = returnedValue x (1 - discountShare)
            taxRefund             = goodsRefund x taxRate
            shippingRefund        = shippingFee x returnedValue / originalLineSubtotal
            totalRefund           = goodsRefund + taxRefund + shippingRefund

        The customer's effective per-unit price stays identical to what they
        paid (a 20%-off order returns 20% less); shipping scales with the
        returned share (full fee on a full return; zero on free-shipping
        orders without special-casing).

        Returns:
            RefundBreakdown: goods/tax/shipping/total + is_full flag (RT6).

        Raises:
            InvariantViolation: RT3 if the order line subtotal is not positive.
        """
        f = self._facts
        original_line_subtotal = sum(
            qty * unit.minor for _, qty, unit in f.shipped_lines
        )
        cur = next(unit.currency for _, _, unit in f.shipped_lines)
        if original_line_subtotal <= 0:
            raise InvariantViolation("RT3: order line subtotal must be positive")
        discount_share = f.discount.minor / original_line_subtotal  # 0..1
        returned_value = sum(
            l.quantity * next(u.minor for p, _, u in f.shipped_lines if p == l.product_id)
            for l in self._lines
        )
        # goods: net of the coupon share, scaled to the returned value (round-half-up)
        goods = Money(
            (returned_value * (original_line_subtotal - f.discount.minor) + original_line_subtotal // 2)
            // original_line_subtotal,
            cur,
        )
        # tax: proportional to the net goods
        tax = goods.times_fraction(f.tax_num, f.tax_den)
        # shipping: proportional to the returned share (full fee when f == 1)
        shipping = Money(
            (f.shipping_fee.minor * returned_value + original_line_subtotal // 2)
            // original_line_subtotal,
            cur,
        )
        total = goods.plus(tax).plus(shipping)
        return RefundBreakdown(goods=goods, tax=tax, shipping=shipping, total=total,
                               is_full=self.is_complete)

    def settle(self) -> RefundBreakdown:
        """Settle the return: GoodsReceived -> Refunded, terminal (RT5/RT3/RT4).

        Computes the RT3 pro-rata breakdown and finalizes the RMA. The
        caller (orchestration) then applies it across contexts:
        ``Payment.refund(breakdown.total)``, ``Stock.restock(...)``,
        ``Order.refund_completed(...)`` (iff complete), and — iff
        ``breakdown.is_full`` — ``Coupon.unredeem`` (RT6).

        Returns:
            RefundBreakdown: the computed goods/tax/shipping/total refund.

        Raises:
            InvalidStateTransition: RT5 unless GoodsReceived (goods must be
                back before money moves).

        Events:
            ReturnRefunded(rma_id, total).
        """
        if self._status != GOODS_RECEIVED:
            raise InvalidStateTransition(
                f"RT5: settlement requires the goods to be back (GoodsReceived), not {self._status}")
        self._settlement = self._compute_refund()
        self._status = REFUNDED
        self.raise_event(ReturnRefunded(self._rma_id, self._settlement.total))
        return self._settlement

    def reject(self, reason: str) -> None:
        """Reject the RMA before goods are collected (RT4).

        Args:
            reason: Staff rejection reason (surfaced on the event).

        Raises:
            InvalidStateTransition: RT4 unless Requested, Approved, or
                PickupScheduled (never after pickup).

        Events:
            ReturnRejected(rma_id, reason).
        """
        if self._status not in (REQUESTED, APPROVED, PICKUP_SCHEDULED):
            raise InvalidStateTransition(f"RT4: cannot reject a {self._status} RMA")
        self._status = REJECTED
        self.raise_event(ReturnRejected(self._rma_id, reason))
