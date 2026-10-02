"""Application services: thin orchestration over the domain, inside a UoW.

Each service is the ACL that crosses bounded contexts (context map §2):
- Checkout: Shop -> Order Mgmt + Inventory + Payment + Coupon
- Shipping: Order Mgmt -> Fulfillment + Payment (capture at handoff)
- Cancel:   Order Mgmt + Payment (void) + Inventory (release) + Shop (unredeem)
- Returns:  Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop

SOLID wiring: services take a ``UoWFactory`` + ``Clock`` (+ optional
observability Blocks), never concretes. All persistence goes through
``uow.<repos>``; all cross-context communication after commit goes through
the EventBus (subscribed by NotificationService). Every public method is one
atomic UoW: success commits (and publishes), failure rolls back.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.cart import Cart
from domain.coupon import Coupon
from domain.errors import DomainError
from domain.order import Order
from domain.payment import Payment
from domain.pickup import Pickup
from domain.return_rma import Return, _OrderFacts
from domain.shipment import Shipment
from domain.value_objects import Address, Money, PickupEvidence, RefundBreakdown
from infra.blocks import AsyncJob, FileBucket
from infra.clock import Clock, SystemClock
from infra.unit_of_work import UoWFactory

TAX_NUM, TAX_DEN = 0, 100  # v1: no tax (pluggable, mirrored into _OrderFacts)
AUTO_COURIER = "courier-default"


def _future(days: int, clock: Clock) -> datetime:
    return clock.now() + timedelta(days=days)


class CheckoutAppService:
    """Shop -> Order + Inventory + Payment + Coupon (one atomic unit of work)."""

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        jobs: AsyncJob | None = None,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer
        self._jobs = jobs

    def place_and_pay(
        self, cart: Cart, instrument_ref: str, reservation_ttl_days: int = 30
    ) -> tuple[Order, Payment]:
        """Authorize at placement; capture happens later, at shipment."""
        if cart.status != "Open":
            raise DomainError("C4: a cart can only be checked out once")
        if cart.destination is None:
            raise DomainError("C4: a checkout needs a destination address")
        if self._tracer is not None:
            self._tracer.trace("checkout.place_and_pay", customer=cart.customer_ref)
        with self._uow() as uow:
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
            coupon = snapshot["coupon"]
            try:
                if coupon is not None:
                    coupon.redeem(cart.customer_ref, order.order_id)
                for pid, qty, _ in snapshot["lines"]:
                    stock = self._lookup_stock(uow, pid)
                    order.link_reservation(
                        pid,
                        stock.reserve(
                            order.order_id, qty, _future(reservation_ttl_days, self._clock)
                        ),
                    )
            except DomainError:
                for pid, rid in order.reservation_ids.items():
                    self._lookup_stock(uow, pid).release(rid, "checkout rollback")
                if coupon is not None:
                    coupon.unredeem(cart.customer_ref, order.order_id)
                raise
            # Order -> Payment ACL: Payment sees Money + opaque ref, never "orders"
            payment = Payment(order.money_total, instrument_ref)
            order.pay(payment.payment_id)
            # persist
            uow.carts.add(cart)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(cart, order, payment, *uow.stocks.list(),
                        *([coupon] if coupon else []))
            if self._jobs is not None:
                self._jobs.enqueue("order.placed", {"order_id": order.order_id})
        return order, payment

    @staticmethod
    def _lookup_stock(uow, product_id: str):
        try:
            return uow.stocks.get(product_id)
        except KeyError:
            raise DomainError(f"no stock record for {product_id}") from None


class ShippingAppService:
    """Order -> Fulfillment + Payment: hand to carrier, then capture."""

    def __init__(self, uow_factory: UoWFactory, tracer=None) -> None:
        self._uow = uow_factory
        self._tracer = tracer

    def ship(self, order: Order, carrier: str, tracking_number: str) -> Shipment:
        if self._tracer is not None:
            self._tracer.trace("shipping.ship", order=order.order_id)
        with self._uow() as uow:
            shipment = Shipment(
                order.order_id,
                [(line.product_id, line.quantity) for line in order.lines],
                order.destination,
            )
            order.confirm_shipped(shipment.shipment_id)
            shipment.hand_to_carrier(carrier, tracking_number)
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).commit(rid)
            payment = uow.payments.get(order.payment_ref)
            payment.capture()
            uow.orders.add(order)
            uow.shipments.add(shipment)
            uow.payments.add(payment)
            uow.collect(order, shipment, *uow.stocks.list(), payment)
        return shipment

    def mark_delivered(self, shipment: Shipment, order: Order, at=None) -> None:
        from datetime import date as _date

        with self._uow() as uow:
            shipment.confirm_delivered()
            order.deliver(at or _date.today())
            uow.shipments.add(shipment)
            uow.orders.add(order)
            uow.collect(shipment, order)


class CancelAppService:
    """Cancel before shipment: void auth, release stock, un-redeem coupon."""

    def __init__(self, uow_factory: UoWFactory, tracer=None) -> None:
        self._uow = uow_factory
        self._tracer = tracer

    def cancel(
        self,
        order: Order,
        reason: str,
        payment: Payment,
        coupon: Coupon | None,
        customer: str,
    ) -> None:
        with self._uow() as uow:
            order.cancel(reason)
            payment.void(reason)
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).release(rid, reason)
            if coupon is not None and order.coupon_code:
                coupon.unredeem(customer, order.order_id)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(order, payment, coupon, *uow.stocks.list())


class ReturnAppService:
    """Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop (coupon)."""

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        files: FileBucket | None = None,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer
        self._files = files

    def _facts(self, order: Order) -> _OrderFacts:
        return _OrderFacts(
            order_id=order.order_id,
            customer_id=order.customer_ref,
            destination=order.destination,
            shipped_lines=tuple(
                (line.product_id, line.quantity, line.unit_price)
                for line in order.lines
            ),
            discount=order.discount,
            shipping_fee=order.shipping_fee,
            tax_num=TAX_NUM,
            tax_den=TAX_DEN,
            delivered_at=order.delivered_at,
        )

    def request_return(
        self, order: Order, return_lines: list[tuple[str, int]], reason: str,
        now: datetime | None = None,
    ) -> Return:
        if order.status != "Delivered":
            raise DomainError(
                f"returns only against Delivered orders (was {order.status})"
            )
        with self._uow() as uow:
            rma = Return(
                self._facts(order), return_lines, reason, now or self._clock.now()
            )
            rma.approve()
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.collect(rma)
        return rma

    def schedule_pickup(self, rma: Return, slot: datetime) -> Pickup:
        with self._uow() as uow:
            pickup = Pickup(rma.rma_id, rma.destination, slot, now=self._clock.now())
            pickup.assign_courier(AUTO_COURIER)
            rma.link_pickup(pickup.pickup_id)
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)
        return pickup

    def complete_pickup(
        self, rma: Return, pickup: Pickup, evidence: PickupEvidence,
        photo_bytes: bytes | None = None,
    ) -> None:
        with self._uow() as uow:
            if photo_bytes is not None and self._files is not None:
                self._files.put(evidence.photo_ref, photo_bytes)
            pickup.confirm_pickup(evidence)
            rma.on_pickup_confirmed(pickup.pickup_id, evidence)
            rma.receive_goods()
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)

    def settle(self, rma: Return, order: Order) -> RefundBreakdown:
        with self._uow() as uow:
            breakdown = rma.settle()
            payment = uow.payments.get(order.payment_ref)
            payment.refund(breakdown.total, f"return {rma.rma_id}")
            for line in rma.lines:
                uow.stocks.get(line.product_id).restock(
                    rma.rma_id, {line.product_id: line.quantity}
                )
            coupon = None
            if order.coupon_code:
                try:
                    coupon = uow.coupons.find_by_code(order.coupon_code)
                except KeyError:
                    coupon = None
            if breakdown.is_full:
                order.refund_completed(breakdown.total, f"return {rma.rma_id}")
                if coupon is not None:
                    coupon.unredeem(order.customer_ref, order.order_id)
            else:
                order.accept_return(rma.rma_id)
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(
                rma, order, payment, *([coupon] if coupon else []), *uow.stocks.list()
            )
        return breakdown


# Re-export stable address type for callers that only import application.
__all__ = [
    "Address",
    "Money",
    "CheckoutAppService",
    "ShippingAppService",
    "CancelAppService",
    "ReturnAppService",
]
