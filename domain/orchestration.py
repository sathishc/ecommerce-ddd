"""Thin application/orchestration layer — the ACLs that cross bounded contexts.

Bounded-context role:
    Translate between context languages and drive multi-aggregate flows
    inside one bounded unit of work. Pure domain aggregates never import
    each other across contexts; these services do the cross-context wiring:

    * Shop -> Order + Inventory + Payment + Coupon (``CheckoutService``):
      happy-path ``PlaceOrder -> ReserveStock -> AuthorizePayment``.
    * Order -> Fulfillment + Payment (``ShippingService``): handoff commits
      stock (S3) and captures payment (capture-at-handoff).
    * Order -> Payment/Inventory/Shop (``CancelService``): pre-shipment void
      + release + un-redeem.
    * Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop
      (``ReturnService``): the full doorstep-pickup return path and the
      pro-rata settlement applied across contexts in one bounded step.

    This is NOT part of the domain model — it exists to make the context
    boundaries concrete and testable.

Lifecycle notes:
    * Checkout is all-or-nothing: coupon redemption (CO4) and every
      per-product ``Stock.reserve`` (S2) roll back together on failure, so
      a failed checkout leaves the world exactly as it was.
    * Returns settlement order is money (Payment.refund, PM1/PM4) -> goods
      (Stock.restock, S4, only after courier possession) -> order terminal
      flip (O3, iff complete) -> coupon re-entitlement (RT6/CO7, iff
      complete); partial returns keep the Order Delivered and the coupon
      burned.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .cart import Cart
from .coupon import Coupon
from .errors import DomainError
from .events import DomainEvent
from .order import Order
from .payment import Payment
from .pickup import Pickup
from .product import Product
from .return_rma import Return, _OrderFacts
from .shipment import Shipment
from .stock import Stock
from .value_objects import Address, Money, PickupEvidence

TAX_NUM, TAX_DEN = 0, 100   # v1: no tax (pluggable; mirrored into _OrderFacts)
AUTO_COURIER = "courier-default"


class Outbox:
    """In-memory pub/sub outbox for the Notification context.

    Role:
        Collect every domain event published by the services so subscribers
        (e.g. Notification: email/SMS) can react. ``drain`` is per-event-name
        (queue semantics); ``all`` is the full append-only history.

    Members:
        _queues: event-name -> pending events (drained by name).
        _all: every published event, in publish order (never drained).
    """

    def __init__(self) -> None:
        """Create an empty outbox (no queues, no history)."""
        self._queues: dict[str, list[DomainEvent]] = {}
        self._all: list[DomainEvent] = []

    def publish(self, *events: DomainEvent) -> None:
        """Publish events to the per-name queues and the full history.

        Args:
            *events: Domain events drained from aggregates (past tense).
        """
        self._all.extend(events)
        for ev in events:
            self._queues.setdefault(ev.name, []).append(ev)

    def drain(self, event_name: str) -> list[DomainEvent]:
        """Drain and return pending events for one event name.

        Args:
            event_name: Event class name (e.g. "OrderPlaced").

        Returns:
            list[DomainEvent]: pending events; empty if none.
        """
        evs, self._queues[event_name] = self._queues.get(event_name, []), []
        return evs

    def all(self) -> list[DomainEvent]:
        """Return every published event, in publish order.

        Returns:
            list[DomainEvent]: full event history (a copy).
        """
        return list(self._all)


def _drain(*aggregates) -> list[DomainEvent]:
    """Drain pending events from any number of aggregates, in order.

    Args:
        *aggregates: Aggregates whose pending events to collect.

    Returns:
        list[DomainEvent]: concatenated pending events.
    """
    out: list[DomainEvent] = []
    for a in aggregates:
        out.extend(a.pop_events())
    return out


def _future(days: int) -> datetime:
    """Return a datetime ``days`` in the future (reservation TTL helper).

    Args:
        days: Offset from now.

    Returns:
        datetime: now + days.
    """
    return datetime.now() + timedelta(days=days)


class CheckoutService:
    """Shop -> Order + Inventory + Payment + Coupon (one bounded unit of work).

    Role:
        Execute the happy-path head: ``PlaceOrder -> ReserveStock ->
        AuthorizePayment``. Re-validates/commits the cart coupon preview at
        placement (CO4) and reserves every product all-or-nothing (S2),
        rolling everything back if any leg fails.

    Members:
        _outbox: Event outbox (Notification fan-out).
        _stocks: product_id -> Stock (single warehouse).
        _coupons: code -> Coupon (redemption commit point).

    Happy path: PlaceOrder -> ReserveStock -> AuthorizePayment.
    """

    def __init__(self, outbox: Outbox, stocks: dict[str, Stock],
                 coupons: dict[str, Coupon]) -> None:
        """Wire the checkout service to shared infrastructure.

        Args:
            outbox: Event outbox to publish to.
            stocks: Product stocks (single warehouse).
            coupons: Coupons by code.
        """
        self._outbox = outbox
        self._stocks = stocks        # product_id -> Stock (single warehouse)
        self._coupons = coupons      # code -> Coupon

    def place_and_pay(self, cart: Cart, instrument_ref: str,
                      reservation_ttl_days: int = 30) -> tuple[Order, Payment]:
        """Place an order from an open cart and authorize payment (O1/O2/S2/CO4).

        Freezes the cart into an Order (O2 frozen total), commits the coupon
        redemption (CO4: the cart preview is re-validated at placement),
        reserves every line all-or-nothing (S2), then authorizes a generic
        Payment via the Order -> Payment ACL. Capture happens later, at
        shipment handoff.

        Args:
            cart: Open cart with a destination set (C4: checked out exactly once).
            instrument_ref: Opaque payment instrument reference.
            reservation_ttl_days: Reservation TTL in days (S3 expiry sweep).

        Returns:
            tuple[Order, Payment]: the placed (Paid) order and its
            Authorized payment.

        Raises:
            DomainError: C4 if the cart is not Open or has no destination;
                S2/no-stock if any reservation fails (with full rollback of
                prior reservations + the coupon redemption); coupon errors
                (CO2/CO3) if re-validation fails at placement.

        Events:
            OrderPlaced, StockReserved (per line), CouponRedeemed (if coupon),
            PaymentAuthorized, OrderPaymentAuthorized, OrderPaid.
        """
        if cart.status != "Open":
            raise DomainError("C4: a cart can only be checked out once")
        if cart.destination is None:
            raise DomainError("C4: a checkout needs a destination address")
        snapshot = cart.checkout(cart.destination)
        quote = snapshot["quote"]
        order = Order()
        order.place(
            lines=[(pid, qty, unit) for pid, qty, unit in snapshot["lines"]],
            destination=cart.destination,
            customer_ref=cart.customer_ref,
            discount=quote.discount.amount,
            shipping_fee=quote.shipping_fee,
            tax=quote.tax,
            coupon_code=snapshot["coupon"].code if snapshot["coupon"] else None,
        )
        # CO4: the cart preview is re-validated/committed at placement
        coupon = snapshot["coupon"]
        try:
            if coupon is not None:
                coupon.redeem(cart.customer_ref, order.order_id)
            # S2 at *order* level: all-or-nothing across products
            for pid, qty, _ in snapshot["lines"]:
                stock = self._stocks.get(pid)
                if stock is None:
                    raise DomainError(f"no stock record for {pid}")
                order.link_reservation(
                    pid, stock.reserve(order.order_id, qty, _future(reservation_ttl_days)))
        except DomainError:
            # Roll back whatever already succeeded so a failed checkout leaves
            # the world exactly as it was (no dangling reservations / redemptions).
            for pid, rid in order.reservation_ids.items():
                self._stocks[pid].release(rid, "checkout rollback")
            if coupon is not None:
                coupon.unredeem(cart.customer_ref, order.order_id)
            raise
        # Order -> Payment ACL: the Payment context sees Money + an opaque ref
        payment = Payment(order.money_total, instrument_ref)
        order.pay(payment.payment_id)
        self._outbox.publish(
            *_drain(order, payment, *(s for s in self._stocks.values()),
                    *([snapshot["coupon"]] if snapshot["coupon"] else [])))
        return order, payment


class ShippingService:
    """Order -> Fulfillment + Payment: hand to carrier, then capture.

    Role:
        Build the Shipment for a Paid order, move Order Paid -> Shipped,
        hand to the carrier, commit reservations (S3: goods left), and
        capture the authorized payment (capture-at-handoff). Delivery then
        moves Shipment -> Delivered and Order Shipped -> Delivered.

    Members:
        _outbox: Event outbox.
        _stocks: Product stocks (for S3 commit).
        _payments: Payments by id (for capture).
    """

    def __init__(self, outbox: Outbox, stocks: dict[str, Stock],
                 payments: dict[str, Payment]) -> None:
        """Wire the shipping service to shared infrastructure.

        Args:
            outbox: Event outbox to publish to.
            stocks: Product stocks (S3 commit at handoff).
            payments: Payments by id (capture at handoff).
        """
        self._outbox = outbox
        self._stocks = stocks
        self._payments = payments

    def ship(self, order: Order, carrier: str, tracking_number: str) -> Shipment:
        """Ship a Paid order: handoff + commit + capture (O4/S3/PM2/SH1/SH2).

        Args:
            order: Paid order to fulfill (O4: must be Paid).
            carrier: Carrier name (single carrier in v1).
            tracking_number: Carrier tracking number (SH2: immutable once set).

        Returns:
            Shipment: the HandedToCarrier shipment.

        Raises:
            InvalidStateTransition: O4/SH3 if the order/shipment states
                forbid the handoff; PM2 if payment capture is illegal.

        Events:
            ShipmentCreated, OrderShipped, ShipmentShipped, StockCommitted
            (per line), PaymentCaptured.
        """
        shipment = Shipment(
            order.order_id,
            [(l.product_id, l.quantity) for l in order.lines],
            order.destination,
        )
        order.confirm_shipped(shipment.shipment_id)
        shipment.hand_to_carrier(carrier, tracking_number)
        # S3: goods have left — the reservations become actual outflow
        for pid, rid in order.reservation_ids.items():
            self._stocks[pid].commit(rid)
        # capture at shipment (authorize-then-capture: money moves now)
        self._payments[order.payment_ref].capture()
        self._outbox.publish(
            *_drain(order, shipment, *self._stocks.values(),
                    self._payments[order.payment_ref]))
        return shipment

    def mark_delivered(self, shipment: Shipment, order: Order,
                       at: date | None = None) -> None:
        """Confirm carrier delivery: Shipment -> Delivered, Order -> Delivered (SH3/O3).

        Opens the RT1 30-day return window anchored at ``at``.

        Args:
            shipment: HandedToCarrier/InTransit shipment (carrier-confirmed).
            order: Shipped order matching the shipment.
            at: Delivery date (defaults to today).

        Raises:
            InvalidStateTransition: SH3/O3 if states forbid delivery.

        Events:
            ShipmentDelivered, OrderDelivered.
        """
        shipment.confirm_delivered()
        order.deliver(at)
        self._outbox.publish(*_drain(shipment, order))


class CancelService:
    """Cancel before shipment: void the authorization, release stock, un-redeem.

    Role:
        Move Order Placed/Paid -> Cancelled (O3), void the not-yet-captured
        authorization (PM3), release every reservation back to available
        (S3), and restore coupon eligibility (nothing left the warehouse).

    Members:
        _outbox: Event outbox.
    """

    def __init__(self, outbox: Outbox) -> None:
        """Wire the cancel service to the event outbox.

        Args:
            outbox: Event outbox to publish to.
        """
        self._outbox = outbox

    def cancel(self, order: Order, reason: str, payment: Payment,
               stocks: dict[str, Stock], coupon: Coupon | None,
               customer: str) -> None:
        """Cancel a Placed/Paid order before shipment (O3/PM3/S3/CO7-mirror).

        Args:
            order: Placed or Paid order (O3: never Shipped/Delivered).
            reason: Cancel reason (void + release audit trail).
            payment: The order's Authorized payment (voided, PM3).
            stocks: Product stocks (every reservation released, S3).
            coupon: Redeemed coupon to un-redeem, if one was used.
            customer: Customer regaining eligibility for the code.

        Raises:
            InvalidStateTransition: O3/PM3/S3 on illegal states.

        Events:
            OrderCancelled, PaymentVoided, StockReleased (per line),
            CouponUnredeemed (if a coupon was used).
        """
        order.cancel(reason)
        payment.void(reason)
        for pid, rid in order.reservation_ids.items():
            stocks[pid].release(rid, reason)
        if coupon is not None and order.coupon_code:
            coupon.unredeem(customer, order.order_id)
        self._outbox.publish(*_drain(order, payment, coupon, *stocks.values()))


class ReturnService:
    """Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop (coupon).

    Role:
        Drive the whole doorstep-return path and apply the RT3 pro-rata
        settlement across contexts in one bounded step: request/approve
        (RT1/RT2), schedule the doorstep Pickup (PK1/PK2), confirm
        possession with mandatory evidence (PK3/PK5 -> RT5), then settle
        money + goods + order + coupon (PM1/S4/O3/RT6).

    Members:
        _outbox: Event outbox.
        _payments: Payments by id (Returns -> Payment ACL: Money only).
        _stocks: Product stocks (S4 restock, only after possession).
        _coupons: Coupons by code (RT6 re-entitlement on complete returns).

    Drives the whole doorstep-return path and applies the settlement across
    contexts in one bounded step.
    """

    def __init__(self, outbox: Outbox, payments: dict[str, Payment],
                 stocks: dict[str, Stock], coupons: dict[str, Coupon]) -> None:
        """Wire the return service to shared infrastructure.

        Args:
            outbox: Event outbox to publish to.
            payments: Payments by id (refund target).
            stocks: Product stocks (restock target, S4).
            coupons: Coupons by code (RT6 re-entitlement).
        """
        self._outbox = outbox
        self._payments = payments
        self._stocks = stocks
        self._coupons = coupons

    def request_return(self, order: Order, return_lines: list[tuple[str, int]],
                       reason: str, now: datetime) -> Return:
        """Open and approve an RMA against a Delivered order (RT1/RT2).

        Snapshots the order into _OrderFacts (frozen RT3 inputs), validates
        the 30-day window and shipped-quantity bounds, and approves
        immediately (staff auto-approval in v1).

        Args:
            order: Delivered order under return (RT1).
            return_lines: (product_id, quantity) pairs (RT2-bounded).
            reason: Customer's return reason.
            now: Request timestamp (RT1 window endpoint).

        Returns:
            Return: the Approved RMA.

        Raises:
            DomainError: if the order is not Delivered.
            InvariantViolation: RT1/RT2 on window/quantity violations.

        Events:
            ReturnRequested, ReturnApproved.
        """
        if order.status != "Delivered":
            raise DomainError(f"returns only against Delivered orders (was {order.status})")
        facts = _OrderFacts(
            order_id=order.order_id,
            customer_id=order.customer_ref,
            destination=order.destination,
            shipped_lines=tuple((l.product_id, l.quantity, l.unit_price) for l in order.lines),
            discount=order.discount,
            shipping_fee=order.shipping_fee,
            tax_num=TAX_NUM, tax_den=TAX_DEN,
            delivered_at=order.delivered_at,
        )
        rma = Return(facts, return_lines, reason, now)
        rma.approve()
        self._outbox.publish(*_drain(rma))
        return rma

    def schedule_pickup(self, rma: Return, slot: datetime) -> Pickup:
        """Create and dispatch the doorstep Pickup for an approved RMA (PK1/PK2).

        Args:
            rma: Approved RMA needing collection.
            slot: Courier visit window (PK2: must be future).

        Returns:
            Pickup: the EnRoute pickup (courier assigned).

        Raises:
            InvariantViolation: PK1/PK2 on bad return link or past slot.
            InvalidStateTransition: unless the RMA is Approved.

        Events:
            PickupScheduled.
        """
        pickup = Pickup(rma.rma_id, rma.destination, slot)
        self.schedule_pickup_on(rma, pickup)
        return pickup

    def schedule_pickup_on(self, rma: Return, pickup: Pickup) -> None:
        """Bind a pre-built Pickup to an RMA and dispatch a courier.

        Test seam: lets callers inject a Pickup with a controlled clock /
        max_attempts, then performs the same assign + link as
        ``schedule_pickup``.

        Args:
            rma: Approved RMA needing collection.
            pickup: Pre-built pickup for this RMA (PK1).

        Raises:
            InvalidStateTransition: unless the RMA is Approved and the
                pickup can take a courier.

        Events:
            PickupScheduled (from the Pickup; RMA link itself is silent).
        """
        pickup.assign_courier(AUTO_COURIER)
        rma.link_pickup(pickup.pickup_id)
        self._outbox.publish(*_drain(rma, pickup))

    def complete_pickup(self, rma: Return, pickup: Pickup,
                        evidence: PickupEvidence) -> None:
        """Confirm doorstep possession and reconcile goods (PK3/PK5 -> RT5).

        The courier's on-site scan + mandatory photo: Pickup EnRoute ->
        PickedUp (terminal), RMA PickupScheduled -> PickedUp -> GoodsReceived
        (derived ReturnGoodsReceived). Only after this may ``settle`` run
        and Inventory restock (S4).

        Args:
            rma: PickupScheduled RMA linked to ``pickup``.
            pickup: EnRoute pickup collecting this RMA.
            evidence: Collection proof (photo required, PK5).

        Raises:
            InvalidStateTransition: PK3/RT5 on mismatched pickup or state.
            InvariantViolation: PK5 if evidence is missing/has no photo.

        Events:
            PickupConfirmed, ReturnGoodsReceived.
        """
        pickup.confirm_pickup(evidence)
        rma.on_pickup_confirmed(pickup.pickup_id, evidence)
        rma.receive_goods()
        self._outbox.publish(*_drain(rma, pickup))

    def settle(self, rma: Return, order: Order) -> "RefundBreakdown":
        """Apply the pro-rata settlement across contexts (RT3/PM1/S4/O3/RT6).

        Order of application: 1) money — ``Payment.refund(total)`` (Payment
        ACL: Money only); 2) goods — ``Stock.restock`` per line (S4: only
        after the courier physically has them); 3) order — terminal
        ``refund_completed`` iff the return is complete (O3), else
        ``accept_return`` bookkeeping (stays Delivered across partials);
        4) coupon — ``unredeem`` iff the return is complete (RT6).

        Args:
            rma: GoodsReceived RMA to settle (RT5).
            order: The Delivered order under return.

        Returns:
            RefundBreakdown: computed goods/tax/shipping/total + is_full.

        Raises:
            InvalidStateTransition: RT5 unless GoodsReceived; PM1/O3/S4 on
                downstream invariant violations.

        Events:
            ReturnRefunded, PaymentRefunded, StockRestocked (per line),
            OrderRefunded (iff complete), CouponUnredeemed (iff complete
            with a coupon).
        """
        breakdown = rma.settle()
        # 1) money (Payment ACL: Money only)
        self._payments[order.payment_ref].refund(breakdown.total, f"return {rma.rma_id}")
        # 2) goods (S4: only after the courier physically has them)
        for line in rma.lines:
            self._stocks[line.product_id].restock(
                rma.rma_id, {line.product_id: line.quantity})
        # 3) order: terminal Refunded iff complete, else stays Delivered
        coupon = self._coupons.get(order.coupon_code) if order.coupon_code else None
        if breakdown.is_full:
            order.refund_completed(breakdown.total, f"return {rma.rma_id}")
            # 4) RT6: complete return -> customer regains the coupon
            if coupon is not None:
                coupon.unredeem(order.customer_ref, order.order_id)
        else:
            order.accept_return(rma.rma_id)
        self._outbox.publish(
            *_drain(rma, order, self._payments[order.payment_ref],
                    *(filter(None, [coupon])), *self._stocks.values()))
        return breakdown
