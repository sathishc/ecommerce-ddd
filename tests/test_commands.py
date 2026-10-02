"""Command pattern: every user-initiated intent goes through the CommandBus.

Scenario coverage (infra container + CommandBus, ``build_test_container`` with
FixedClock): actor-initiated rows of domain-model.md §5 -- customer
(OpenCart/AddToCart/ApplyCoupon/RemoveCartLine/PlaceOrder/RequestReturn),
staff (HandToCarrier/ConfirmDelivery/SchedulePickup/SettleReturn/CancelOrder/
CloseCart), courier (ConfirmPickup with photo evidence), admin
(PublishProduct). Also locks CommandBus semantics: unregistered commands
raise, system reactions have no command, failed commands roll back with no
trace (atomic UoW + silent outbox).

System follow-ons (reserve → authorize → capture → restock → un-redeem,
derived OrderShipped/OrderDelivered/ReturnGoodsReceived) stay purely
event-driven — there is intentionally NO command for them (asserted).

Flows/invariants locked: C3/C4 (cart via commands), O2/O3 (frozen total +
Delivered/Refunded/Cancelled via commands), RT3/RT6 (pro-rata + coupon via
SettleReturn event, not a command), PM4, PK5, C4/CloseCart abandonment.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

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
    RequestReturn,
    SchedulePickup,
    SettleReturn,
)
from domain.errors import DomainError
from domain.value_objects import Address, Money, PickupEvidence
from domain.coupon import Coupon, PERCENTAGE
from domain.product import Product
from domain.stock import Stock
from infra.command_bus import UnregisteredCommand
from infra.container import build_test_container

ADDRESS = Address("1 Main St", "Springfield", "12345", "US")


def money(major: float) -> Money:
    return Money(int(round(major * 100)), "USD")


@pytest.fixture
def container():
    """Fresh test container per test: FixedClock + seeded catalog/stocks/coupon.

    Seeds p1 ($100) / p2 ($50) with 10 units each and SAVE20 (PCT-20), so
    command tests start from a sellable catalog without touching AWS.
    """
    c = build_test_container()
    p1 = Product(None, "SKU-A", "Gadget A", money(100.00))
    p2 = Product(None, "SKU-B", "Gadget B", money(50.00))
    c.products.add(p1)
    c.products.add(p2)
    c.stocks.add(Stock(p1.product_id, 10))
    c.stocks.add(Stock(p2.product_id, 10))
    now = c.clock.now()
    cpn = Coupon("SAVE20", PERCENTAGE, 20, now - timedelta(days=1),
                 now + timedelta(days=30), usage_limit=100)
    c.coupons.add(cpn)
    c._p1, c._p2, c._cpn = p1, p2, cpn
    return c


def _cart_id(container, coupon=None) -> str:
    """Open a cart with both products (+ optional coupon) via commands.

    Args:
        container: The test container (provides ``commands`` bus + products).
        coupon: Optional coupon code to ApplyCoupon with.

    Returns:
        The new cart's aggregate id.
    """
    bus = container.commands
    cart = bus.dispatch(OpenCart(customer_ref="cust-1"))
    bus.dispatch(AddToCart(cart_id=cart.aggregate_id(),
                           product_id=container._p1.product_id, quantity=1))
    bus.dispatch(AddToCart(cart_id=cart.aggregate_id(),
                           product_id=container._p2.product_id, quantity=1))
    if coupon is not None:
        bus.dispatch(ApplyCoupon(cart_id=cart.aggregate_id(),
                                 coupon_code=coupon))
    return cart.aggregate_id()


class TestUserCommands:
    """Actor-initiated commands (customer/staff/courier/admin) end-to-end via the bus."""

    def test_shop_cart_flow_via_commands(self, container):
        """Behavior: Open->Add->Apply->Remove emits the cart event stream; Invariants: C1/C3."""
        bus = container.commands
        cart = bus.dispatch(OpenCart(customer_ref="cust-1"))
        assert cart.status == "Open"
        bus.dispatch(AddToCart(cart_id=cart.aggregate_id(),
                               product_id=container._p1.product_id, quantity=2))
        bus.dispatch(ApplyCoupon(cart_id=cart.aggregate_id(),
                                 coupon_code="SAVE20"))
        bus.dispatch(RemoveCartLine(cart_id=cart.aggregate_id(),
                                    product_id=container._p1.product_id))
        stored = container.carts.get(cart.aggregate_id())
        assert stored.applied_coupon_code == "SAVE20"
        assert [e.name for e in container.bus.published()] == [
            "CartCreated", "CartItemAdded",
            "CouponApplied", "CartLineRemoved",
        ]

    def test_place_order_command_drives_checkout(self, container):
        """Behavior: PlaceOrder yields frozen 125.00 + Paid/Authorized + redemption; Invariants: C3/O2/CO3."""
        cart_id = _cart_id(container, coupon="SAVE20")
        order, payment = container.commands.dispatch(
            PlaceOrder(cart_id=cart_id, instrument_ref="card-1",
                       destination=ADDRESS))
        assert order.money_total == money(125.00)
        assert (order.status, payment.status) == ("Paid", "Authorized")
        assert container._cpn.redemptions == 1

    def test_full_path_via_commands_only(self, container):
        """Behavior: full order->delivery->return->settle via commands only (125.00, Refunded, un-redeemed).

        Invariants: O3 (Delivered->Refunded), PM4 (Captured->Refunded), RT3
        (full pro-rata), RT6 (complete un-redeems via event, not a command).
        """
        bus = container.commands
        cart_id = _cart_id(container, coupon="SAVE20")
        order, payment = bus.dispatch(
            PlaceOrder(cart_id=cart_id, instrument_ref="card-1",
                       destination=ADDRESS))
        # Given a Paid order (When: handing to carrier + confirming delivery)
        shipment = bus.dispatch(HandToCarrier(order_id=order.order_id,
                                              carrier="UPS",
                                              tracking_number="TRK-1"))
        bus.dispatch(ConfirmDelivery(shipment_id=shipment.shipment_id,
                                     order_id=order.order_id,
                                     at=date(2026, 9, 28)))
        assert order.status == "Delivered"
        assert payment.status == "Captured"

        rma = bus.dispatch(RequestReturn(
            order_id=order.order_id,
            lines=((container._p1.product_id, 1),
                   (container._p2.product_id, 1)),
            reason="broken"))
        pickup = bus.dispatch(SchedulePickup(
            rma_id=rma.rma_id,
            slot=container.clock.now() + timedelta(hours=24)))
        bus.dispatch(ConfirmPickup(
            pickup_id=pickup.pickup_id,
            evidence=PickupEvidence("photo-1.jpg")))
        # When running the doorstep return + settle (Then: full 125.00 refund, Refunded, coupon back)
        bd = bus.dispatch(SettleReturn(rma_id=rma.rma_id))
        assert bd.is_full is True and bd.total == money(125.00)
        assert order.status == "Refunded" and payment.status == "Refunded"
        assert container._cpn.redemptions == 0  # RT6 via event, not a command

    def test_cancel_and_close_are_commands(self, container):
        """Behavior: CancelOrder voids payment; CloseCart abandons cart; Invariants: O3/C4 (cancel + abandon)."""
        bus = container.commands
        cart_id = _cart_id(container)
        order, payment = bus.dispatch(
            PlaceOrder(cart_id=cart_id, instrument_ref="card-1",
                       destination=ADDRESS))
        bus.dispatch(CancelOrder(order_id=order.order_id, reason="changed mind"))
        assert order.status == "Cancelled" and payment.status == "Voided"

        cart = bus.dispatch(OpenCart(customer_ref="cust-2"))
        cart.set_destination(ADDRESS)
        bus.dispatch(AddToCart(cart_id=cart.aggregate_id(),
                               product_id=container._p1.product_id, quantity=1))
        bus.dispatch(CloseCart(cart_id=cart.aggregate_id()))
        assert container.carts.get(cart.aggregate_id()).status == "Abandoned"
        assert "CartAbandoned" in container.bus.names()

    def test_publish_product_is_admin_command(self, container):
        """Behavior: PublishProduct registers product + zero stock, ctor silent; Invariant: P1/P5 (sellable setup)."""
        product = container.commands.dispatch(PublishProduct(
            sku="SKU-C", name="Gadget C", price=money(10.00)))
        assert container.products.get(product.product_id) is product
        assert "ProductPublished" not in container.bus.names()  # ctor is silent
        # stock record is ensured so the product is sellable
        assert container.stocks.get(product.product_id).available == 0


class TestCommandBusSemantics:
    """CommandBus contract: routing, intent/event split, atomicity."""

    def test_unregistered_command_raises(self, container):
        """Behavior: dispatch of an unregistered Command raises; Invariant: bus fail-fast wiring."""
        class _NeverRegistered(Command):
            pass

        with pytest.raises(UnregisteredCommand):
            container.commands.dispatch(_NeverRegistered())

    def test_system_reactions_have_no_command(self, container):
        """Behavior: system follow-ons absent from domain.commands; Invariant: intent/event split (§5)."""
        import domain.commands as cmds

        command_names = {n for n in dir(cmds) if not n.startswith("_")}
        for system_only in ("ReserveStock", "AuthorizePayment", "CapturePayment",
                            "RefundPayment", "UnredeemCoupon", "ExpireOrder"):
            assert system_only not in command_names

    def test_failed_command_leaves_no_trace(self, container):
        """Behavior: PlaceOrder without destination fails with no orders/events; Invariant: UoW atomicity."""
        cart_id = _cart_id(container)
        before = list(container.bus.names())
        with pytest.raises(DomainError, match="destination"):
            container.commands.dispatch(
                PlaceOrder(cart_id=cart_id, instrument_ref="card-1"))
        assert container.orders.list() == []
        assert list(container.bus.names()) == before
