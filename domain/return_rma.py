"""Returns context — the Return (RMA) aggregate.

Customer-initiated only: a customer requests, staff approve/reject, the
platform's courier collects at the door (Fulfillment's Pickup), and the
settlement drives refund + restock + (on a complete return) coupon re-entitlement.

Invariants:
  RT1  may be opened only against a Delivered order, within the return
       window (30 days from delivery, configurable)
  RT2  returnLines qty <= the shipped qty for each product (no over-returning)
  RT3  refund amount is pro-rata: goods + tax + shipping all scale with the
       fraction of the order's line value that is returned (see _compute_refund)
  RT4  Refunded is terminal
  RT5  goods are collected at the customer's door via a Pickup; only after the
       courier actually has them (PickupConfirmed, PK5 evidence) is settlement
       and Inventory restock allowed
  RT6  a COMPLETE return triggers Coupon.unredeem (customer regains the code);
       a PARTIAL return keeps the redemption burned
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
    """Child of Return: a product + quantity to return."""

    product_id: str
    quantity: int


@dataclass(frozen=True)
class _OrderFacts:
    """Snapshot of the delivered order taken at request time (ACL from Order Mgmt):
    everything RT1/RT3 need, frozen so later catalog changes cannot alter the math."""

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
    """Aggregate root (identity = rma_id): one customer return against an order."""

    prefix = "rma"

    def __init__(self, facts: _OrderFacts, return_lines: list[tuple[str, int]],
                 reason: str, requested_at: datetime,
                 window_days: int = DEFAULT_WINDOW_DAYS) -> None:
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
        return self._rma_id

    @property
    def rma_id(self) -> str:
        return self._rma_id

    @property
    def order_id(self) -> str:
        return self._facts.order_id

    @property
    def destination(self) -> Address:
        return self._facts.destination

    @property
    def customer_id(self) -> str:
        return self._facts.customer_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def lines(self) -> list[ReturnLine]:
        return list(self._lines)

    @property
    def settlement(self) -> RefundBreakdown | None:
        return self._settlement

    @property
    def is_complete(self) -> bool:
        """RT6: every shipped line returned in full."""
        shipped = {pid: qty for pid, qty, _ in self._facts.shipped_lines}
        returned = {l.product_id: l.quantity for l in self._lines}
        return all(returned.get(pid) == qty for pid, qty in shipped.items()) and \
            set(returned) == set(shipped)

    # -- state machine ---------------------------------------------------------
    def approve(self) -> None:
        if self._status != REQUESTED:
            raise InvalidStateTransition(f"can only approve a Requested RMA, not {self._status}")
        self._status = APPROVED
        self.raise_event(ReturnApproved(self._rma_id))

    def link_pickup(self, pickup_id: str) -> None:
        """The orchestration layer (Fulfillment) scheduled a Pickup for this RMA."""
        if self._status != APPROVED:
            raise InvalidStateTransition(f"pickup links to an Approved RMA, not {self._status}")
        self._pickup_id = pickup_id
        self._status = PICKUP_SCHEDULED

    def on_pickup_confirmed(self, pickup_id: str, evidence: PickupEvidence) -> None:
        """PK3/RT5: the courier's on-site scan (with mandatory evidence) is the
        possession-transfer event. Only this gates the subsequent settlement."""
        if self._status != PICKUP_SCHEDULED or pickup_id != self._pickup_id:
            raise InvalidStateTransition(
                f"RT5: pickup {pickup_id} does not match the linked pickup for this RMA")
        if evidence is None or not evidence.photo_ref:
            raise InvariantViolation("PK5: evidence (photo) is mandatory")
        self._evidence = evidence
        self._status = PICKED_UP

    def receive_goods(self) -> None:
        """Derived from PickupConfirmed (model: ReturnGoodsReceived)."""
        if self._status != PICKED_UP:
            raise InvalidStateTransition(f"RT5: goods received only after PickedUp, not {self._status}")
        self._status = GOODS_RECEIVED
        self.raise_event(ReturnGoodsReceived(self._rma_id))

    def _compute_refund(self) -> RefundBreakdown:
        """RT3 (model §5): goods + tax + shipping, all pro-rata to the returned
        share of the order's line value. Round-half-up throughout."""
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
        """RT5/RT3/RT4: compute the pro-rata refund and finalize.

        The caller (orchestration) then applies: Payment.refund(breakdown.total),
        Stock.restock(...), Order.refund_completed(...), and — iff breakdown.is_full
        — Coupon.unredeem (RT6)."""
        if self._status != GOODS_RECEIVED:
            raise InvalidStateTransition(
                f"RT5: settlement requires the goods to be back (GoodsReceived), not {self._status}")
        self._settlement = self._compute_refund()
        self._status = REFUNDED
        self.raise_event(ReturnRefunded(self._rma_id, self._settlement.total))
        return self._settlement

    def reject(self, reason: str) -> None:
        """RT4: rejection is only possible before the goods are collected."""
        if self._status not in (REQUESTED, APPROVED, PICKUP_SCHEDULED):
            raise InvalidStateTransition(f"RT4: cannot reject a {self._status} RMA")
        self._status = REJECTED
        self.raise_event(ReturnRejected(self._rma_id, reason))
