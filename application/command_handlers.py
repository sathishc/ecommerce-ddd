"""Command handlers: user-initiated Commands -> aggregates -> Events.

Each handler is the application-layer ACL for one external intent: it loads
aggregates by ID inside a single atomic ``UnitOfWork``, invokes domain
behavior, persists, and drains events into the outbox (published via the
``EventBus`` on commit). System follow-ons (reserve → authorize → capture →
restock → un-redeem) run *inside the same UoW* — they surface as Events, not
as further Commands, because no external actor initiates them.

Existing ``application.services`` methods stay as the aggregate-level API;
these handlers are the ID-based, dispatchable front door wired to the
``CommandBus`` (see ``register_command_handlers`` + ``infra.container``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from domain.cart import Cart
from domain.commands import (
    AddToCart,
    ApplyCoupon,
    CancelOrder,
    CloseCart,
    Command,
    ConfirmDelivery,
    ConfirmPickup,
    HandToCarrier,
    OpenCart,
    PlaceOrder,
    PublishProduct,
    RemoveCartLine,
    RemoveCoupon,
    RejectReturn,
    RequestReturn,
    SchedulePickup,
    SettleReturn,
    UpdateCartLine,
)
from domain.errors import DomainError
from domain.order import Order
from domain.payment import Payment
from domain.pickup import Pickup
from domain.product import Product
from domain.return_rma import Return, _OrderFacts
from domain.shipment import Shipment
from domain.stock import Stock
from domain.value_objects import RefundBreakdown
from infra.blocks import AsyncJob, FileBucket
from infra.clock import Clock, SystemClock
from infra.command_bus import CommandBus
from infra.unit_of_work import UoWFactory

TAX_NUM, TAX_DEN = 0, 100  # v1: no tax (pluggable, mirrored into _OrderFacts)
AUTO_COURIER = "courier-default"


def _future(days: int, clock: Clock) -> datetime:
    return clock.now() + timedelta(days=days)


class CommandHandlers:
    """ID-based handlers for every user-initiated Command."""

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        jobs: AsyncJob | None = None,
        files: FileBucket | None = None,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer
        self._jobs = jobs
        self._files = files

    # -- Catalog ----------------------------------------------------------
    def publish_product(self, cmd: PublishProduct) -> Product:
        if cmd.price is None:
            raise DomainError("PublishProduct needs a price")
        with self._uow() as uow:
            product = Product(None, cmd.sku, cmd.name, cmd.price,
                              cmd.description, cmd.weight_grams)
            uow.products.add(product)
            try:
                uow.stocks.get(product.product_id)
            except KeyError:
                uow.stocks.add(Stock(product.product_id, 0))
            uow.collect(product)
        return product

    # -- Shop: cart -------------------------------------------------------
    def open_cart(self, cmd: OpenCart) -> Cart:
        with self._uow() as uow:
            cart = Cart(cmd.customer_ref)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def add_to_cart(self, cmd: AddToCart) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            product = uow.products.get(cmd.product_id)
            cart.add_product(product, cmd.quantity)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def update_cart_line(self, cmd: UpdateCartLine) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.change_quantity(cmd.product_id, cmd.quantity)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def remove_cart_line(self, cmd: RemoveCartLine) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.remove_line(cmd.product_id)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def apply_coupon(self, cmd: ApplyCoupon) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            coupon = uow.coupons.find_by_code(cmd.coupon_code)
            cart.apply_coupon(coupon, self._clock.now())
            uow.carts.add(cart)
            uow.coupons.add(coupon)
            uow.collect(cart)
        return cart

    def remove_coupon(self, cmd: RemoveCoupon) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.remove_coupon()
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def close_cart(self, cmd: CloseCart) -> Cart:
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.abandon()
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    # -- Shop -> Order (checkout) -----------------------------------------
    def place_order(self, cmd: PlaceOrder) -> tuple[Order, Payment]:
        """Authorize at placement; capture happens later, at shipment."""
        if self._tracer is not None:
            self._tracer.trace("checkout.place_order", cart=cmd.cart_id)
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            if cmd.destination is not None and cart.destination is None:
                cart.set_destination(cmd.destination)
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
            coupon = snapshot["coupon"]
            try:
                if coupon is not None:
                    coupon.redeem(cart.customer_ref, order.order_id)
                for pid, qty, _ in snapshot["lines"]:
                    try:
                        stock = uow.stocks.get(pid)
                    except KeyError:
                        raise DomainError(f"no stock record for {pid}") from None
                    order.link_reservation(
                        pid,
                        stock.reserve(
                            order.order_id, qty,
                            _future(cmd.reservation_ttl_days, self._clock),
                        ),
                    )
            except DomainError:
                for pid, rid in order.reservation_ids.items():
                    try:
                        uow.stocks.get(pid).release(rid, "checkout rollback")
                    except KeyError:
                        pass
                if coupon is not None:
                    coupon.unredeem(cart.customer_ref, order.order_id)
                raise
            payment = Payment(order.money_total, cmd.instrument_ref)
            order.pay(payment.payment_id)
            uow.carts.add(cart)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(
                cart, order, payment, *uow.stocks.list(),
                *([coupon] if coupon else []),
            )
            if self._jobs is not None:
                self._jobs.enqueue("order.placed", {"order_id": order.order_id})
        return order, payment

    # -- Order ------------------------------------------------------------
    def cancel_order(self, cmd: CancelOrder) -> Order:
        with self._uow() as uow:
            order = uow.orders.get(cmd.order_id)
            payment = uow.payments.get(order.payment_ref)
            order.cancel(cmd.reason)
            payment.void(cmd.reason)
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).release(rid, cmd.reason)
            coupon = None
            if order.coupon_code:
                try:
                    coupon = uow.coupons.find_by_code(order.coupon_code)
                    coupon.unredeem(order.customer_ref, order.order_id)
                except KeyError:
                    coupon = None
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(order, payment, coupon, *uow.stocks.list())
        return order

    # -- Fulfillment ------------------------------------------------------
    def hand_to_carrier(self, cmd: HandToCarrier) -> Shipment:
        with self._uow() as uow:
            order = uow.orders.get(cmd.order_id)
            shipment = Shipment(
                order.order_id,
                [(line.product_id, line.quantity) for line in order.lines],
                order.destination,
            )
            order.confirm_shipped(shipment.shipment_id)
            shipment.hand_to_carrier(cmd.carrier, cmd.tracking_number)
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).commit(rid)
            payment = uow.payments.get(order.payment_ref)
            payment.capture()
            uow.orders.add(order)
            uow.shipments.add(shipment)
            uow.payments.add(payment)
            uow.collect(order, shipment, *uow.stocks.list(), payment)
        return shipment

    def confirm_delivery(self, cmd: ConfirmDelivery) -> None:
        with self._uow() as uow:
            shipment = uow.shipments.get(cmd.shipment_id)
            order = uow.orders.get(cmd.order_id)
            shipment.confirm_delivered()
            order.deliver(cmd.at or date.today())
            uow.shipments.add(shipment)
            uow.orders.add(order)
            uow.collect(shipment, order)

    # -- Returns ----------------------------------------------------------
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

    def request_return(self, cmd: RequestReturn) -> Return:
        with self._uow() as uow:
            order = uow.orders.get(cmd.order_id)
            if order.status != "Delivered":
                raise DomainError(
                    f"returns only against Delivered orders (was {order.status})"
                )
            rma = Return(
                self._facts(order), list(cmd.lines), cmd.reason,
                self._clock.now(),
            )
            rma.approve()
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.collect(rma)
        return rma

    def reject_return(self, cmd: RejectReturn) -> Return:
        with self._uow() as uow:
            rma = uow.returns.get(cmd.rma_id)
            rma.reject(cmd.reason)
            uow.returns.add(rma)
            uow.collect(rma)
        return rma

    def schedule_pickup(self, cmd: SchedulePickup) -> Pickup:
        if cmd.slot is None:
            raise DomainError("SchedulePickup needs a slot")
        with self._uow() as uow:
            rma = uow.returns.get(cmd.rma_id)
            pickup = Pickup(rma.rma_id, rma.destination, cmd.slot,
                            now=self._clock.now())
            pickup.assign_courier(AUTO_COURIER)
            rma.link_pickup(pickup.pickup_id)
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)
        return pickup

    def confirm_pickup(self, cmd: ConfirmPickup) -> None:
        if cmd.evidence is None:
            raise DomainError("ConfirmPickup needs evidence")
        with self._uow() as uow:
            pickup = uow.pickups.get(cmd.pickup_id)
            rma = uow.returns.get(pickup.return_id)
            pickup.confirm_pickup(cmd.evidence)
            rma.on_pickup_confirmed(pickup.pickup_id, cmd.evidence)
            rma.receive_goods()
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)

    def settle_return(self, cmd: SettleReturn) -> RefundBreakdown:
        with self._uow() as uow:
            rma = uow.returns.get(cmd.rma_id)
            order = uow.orders.get(rma.order_id)
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
                rma, order, payment, *([coupon] if coupon else []),
                *uow.stocks.list(),
            )
        return breakdown


def register_command_handlers(
    bus: CommandBus,
    uow_factory: UoWFactory,
    clock: Clock | None = None,
    tracer=None,
    jobs: AsyncJob | None = None,
    files: FileBucket | None = None,
) -> CommandHandlers:
    """Create handlers and bind every user-initiated Command to the bus."""
    handlers = CommandHandlers(uow_factory, clock, tracer, jobs, files)
    bus.register(PublishProduct, handlers.publish_product)
    bus.register(OpenCart, handlers.open_cart)
    bus.register(AddToCart, handlers.add_to_cart)
    bus.register(UpdateCartLine, handlers.update_cart_line)
    bus.register(RemoveCartLine, handlers.remove_cart_line)
    bus.register(ApplyCoupon, handlers.apply_coupon)
    bus.register(RemoveCoupon, handlers.remove_coupon)
    bus.register(PlaceOrder, handlers.place_order)
    bus.register(CancelOrder, handlers.cancel_order)
    bus.register(CloseCart, handlers.close_cart)
    bus.register(HandToCarrier, handlers.hand_to_carrier)
    bus.register(ConfirmDelivery, handlers.confirm_delivery)
    bus.register(RequestReturn, handlers.request_return)
    bus.register(RejectReturn, handlers.reject_return)
    bus.register(SchedulePickup, handlers.schedule_pickup)
    bus.register(ConfirmPickup, handlers.confirm_pickup)
    bus.register(SettleReturn, handlers.settle_return)
    return handlers


__all__ = ["CommandHandlers", "register_command_handlers", "AUTO_COURIER"]
