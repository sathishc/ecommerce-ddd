"""Thin application/orchestration layer — the ACLs that cross bounded contexts.

Each service maps commands to aggregate behavior *and* translates between
context languages:
  * the Order context tells the generic Payment context about money, never
    about "orders" (Order -> Payment ACL)
  * the Returns context asks Fulfillment for a Pickup, and on a complete
    return asks Shop to un-redeem the coupon (Returns -> Fulfillment/Shop)
  * Stock is consulted via Stock.reserve/release/commit/restock

This is NOT part of the domain model — it exists to make the context
boundaries concrete and testable.
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
    """In-memory pub/sub for the Notification context."""

    def __init__(self) -> None:
        self._queues: dict[str, list[DomainEvent]] = {}
        self._all: list[DomainEvent] = []

    def publish(self, *events: DomainEvent) -> None:
        self._all.extend(events)
        for ev in events:
            self._queues.setdefault(ev.name, []).append(ev)

    def drain(self, event_name: str) -> list[DomainEvent]:
        evs, self._queues[event_name] = self._queues.get(event_name, []), []
        return evs

    def all(self) -> list[DomainEvent]:
        return list(self._all)


def _drain(*aggregates) -> list[DomainEvent]:
    out: list[DomainEvent] = []
    for a in aggregates:
        out.extend(a.pop_events())
    return out


def _future(days: int) -> datetime:
    return datetime.now() + timedelta(days=days)


class CheckoutService:
    """Shop -> Order + Inventory + Payment + Coupon (one bounded unit of work).

    happy path: PlaceOrder -> ReserveStock -> AuthorizePayment.
    """

    def __init__(self, outbox: Outbox, stocks: dict[str, Stock],
                 coupons: dict[str, Coupon]) -> None:
        self._outbox = outbox
        self._stocks = stocks        # product_id -> Stock (single warehouse)
        self._coupons = coupons      # code -> Coupon

    def place_and_pay(self, cart: Cart, instrument_ref: str,
                      reservation_ttl_days: int = 30) -> tuple[Order, Payment]:
        """Authorizes at placement; capture happens later, at shipment."""
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
    """Order -> Fulfillment + Payment: hand to carrier, then capture."""

    def __init__(self, outbox: Outbox, stocks: dict[str, Stock],
                 payments: dict[str, Payment]) -> None:
        self._outbox = outbox
        self._stocks = stocks
        self._payments = payments

    def ship(self, order: Order, carrier: str, tracking_number: str) -> Shipment:
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
        shipment.confirm_delivered()
        order.deliver(at)
        self._outbox.publish(*_drain(shipment, order))


class CancelService:
    """Cancel before shipment: void the authorization, release stock,
    un-redeem the coupon (nothing left the 'available' state)."""

    def __init__(self, outbox: Outbox) -> None:
        self._outbox = outbox

    def cancel(self, order: Order, reason: str, payment: Payment,
               stocks: dict[str, Stock], coupon: Coupon | None,
               customer: str) -> None:
        order.cancel(reason)
        payment.void(reason)
        for pid, rid in order.reservation_ids.items():
            stocks[pid].release(rid, reason)
        if coupon is not None and order.coupon_code:
            coupon.unredeem(customer, order.order_id)
        self._outbox.publish(*_drain(order, payment, coupon, *stocks.values()))


class ReturnService:
    """Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop (coupon).

    Drives the whole doorstep-return path and applies the settlement across
    contexts in one bounded step."""

    def __init__(self, outbox: Outbox, payments: dict[str, Payment],
                 stocks: dict[str, Stock], coupons: dict[str, Coupon]) -> None:
        self._outbox = outbox
        self._payments = payments
        self._stocks = stocks
        self._coupons = coupons

    def request_return(self, order: Order, return_lines: list[tuple[str, int]],
                       reason: str, now: datetime) -> Return:
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
        pickup = Pickup(rma.rma_id, rma.destination, slot)
        self.schedule_pickup_on(rma, pickup)
        return pickup

    def schedule_pickup_on(self, rma: Return, pickup: Pickup) -> None:
        pickup.assign_courier(AUTO_COURIER)
        rma.link_pickup(pickup.pickup_id)
        self._outbox.publish(*_drain(rma, pickup))

    def complete_pickup(self, rma: Return, pickup: Pickup,
                        evidence: PickupEvidence) -> None:
        pickup.confirm_pickup(evidence)
        rma.on_pickup_confirmed(pickup.pickup_id, evidence)
        rma.receive_goods()
        self._outbox.publish(*_drain(rma, pickup))

    def settle(self, rma: Return, order: Order) -> "RefundBreakdown":
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
