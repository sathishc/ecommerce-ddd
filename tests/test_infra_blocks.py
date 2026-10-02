"""Infrastructure tests: AWS Blocks (local), DI, Unit-of-Work, event-driven flow.

These run with NO AWS account (BLOCKS_BACKEND=local): every Block uses its
in-memory implementation. The same application services deploy unchanged
against real AWS Blocks (DynamoDB/SES/SQS) via ``build_container(backend="aws")``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from application.services import CheckoutAppService
from domain.cart import Cart
from domain.coupon import Coupon, PERCENTAGE
from domain.errors import InvariantViolation
from domain.errors import DomainError
from domain.product import Product
from domain.stock import Stock
from domain.value_objects import Address, Money, PickupEvidence
from infra.blocks import (
    LocalAsyncJob,
    LocalEmailClient,
    LocalFileBucket,
    LocalKVStore,
    LocalMetrics,
    LocalTable,
    StdLogger,
)
from infra.clock import FixedClock
from infra.container import build_test_container

CUR = "USD"
ADDRESS = Address("1 Main St", "Springfield", "12345", "US")


def money(major: float) -> Money:
    return Money(int(round(major * 100)), CUR)


@pytest.fixture
def container():
    c = build_test_container()
    # seed catalog + inventory through the repositories (DI, not globals)
    p1 = Product(None, "SKU-A", "Gadget A", money(100.00))
    p2 = Product(None, "SKU-B", "Gadget B", money(50.00))
    c.products.add(p1)
    c.products.add(p2)
    c.stocks.add(Stock(p1.product_id, 10))
    c.stocks.add(Stock(p2.product_id, 10))
    c._p1, c._p2 = p1, p2  # test convenience only
    return c


def _cart(c, customer="cust-1", coupon=None) -> Cart:
    cart = Cart(customer)
    cart.add_product(c._p1, 1)
    cart.add_product(c._p2, 1)
    cart.set_destination(ADDRESS)
    if coupon is not None:
        cart.apply_coupon(coupon, c.clock.now())
    return cart


def _coupon(c, code="SAVE20") -> Coupon:
    now = c.clock.now()
    cpn = Coupon(code, PERCENTAGE, 20, now - timedelta(days=1),
                 now + timedelta(days=30), usage_limit=100)
    c.coupons.add(cpn)
    return cpn


def _delivered(c, coupon=None):
    cart = _cart(c, coupon=coupon)
    order, payment = c.checkout.place_and_pay(cart, "card-1")
    shipment = c.shipping.ship(order, "UPS", "TRK-1")
    c.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))
    return order, payment, shipment, cart


# -- Blocks: local implementations ------------------------------------------------

class TestBlocksLocal:
    def test_kv_conditional_write(self):
        kv = LocalKVStore()
        assert kv.put_if_not_exists("k", "1") is True
        assert kv.put_if_not_exists("k", "2") is False  # conditional, like DynamoDB
        assert kv.get("k") == "1"

    def test_table_query_orders_by_pk(self):
        t = LocalTable()
        t.put("ord-1", "line#1", {"sku": "A"})
        t.put("ord-1", "line#2", {"sku": "B"})
        assert [r["sku"] for r in t.query("ord-1")] == ["A", "B"]
        assert t.get("ord-1", "line#9") is None

    def test_file_bucket_roundtrip(self):
        b = LocalFileBucket()
        ref = b.put("photo-1.jpg", b"\xff\xd8fake-jpeg")
        assert b.get(ref) == b"\xff\xd8fake-jpeg"

    def test_email_captured_not_sent(self):
        mail = LocalEmailClient()
        mail.send("a@x.com", "hi", "body")
        assert len(mail.sent) == 1 and mail.sent[0].to == "a@x.com"

    def test_async_job_queue(self):
        jobs = LocalAsyncJob()
        jobs.enqueue("order.placed", {"order_id": "ord-1"})
        assert jobs.pending() == [{"job": "order.placed", "order_id": "ord-1"}]
        assert jobs.drain() == [{"job": "order.placed", "order_id": "ord-1"}]
        assert jobs.pending() == []


# -- DI container -----------------------------------------------------------------

class TestContainer:
    def test_services_are_injected_not_constructed(self, container):
        assert isinstance(container.checkout, CheckoutAppService)
        assert container.checkout is not container.shipping
        # one shared bus: services publish where notifications listen
        assert container.bus.published() == []

    def test_fixed_clock_controls_time(self):
        c = build_test_container(at=datetime(2026, 9, 28, 12, 0))
        assert isinstance(c.clock, FixedClock)
        c.clock.advance(days=1)
        assert c.clock.now() == datetime(2026, 9, 29, 12, 0)


# -- Unit of Work: atomicity + outbox --------------------------------------------

class TestUnitOfWork:
    def test_failed_checkout_rolls_back(self, container):
        """Reserve-then-fail leaves stock + events exactly as before (atomic)."""
        cart = _cart(container)
        # sabotage: second product has no stock record -> checkout must fail atomically
        container.stocks.clear()
        p1 = container.products.list()[0]
        container.stocks.add(Stock(p1.product_id, 10))
        before = container.bus.names()
        with pytest.raises(DomainError, match="no stock record"):
            container.checkout.place_and_pay(cart, "card-1")
        # rollback: first product's reservation was released, nothing published
        assert container.stocks.get(p1.product_id).available == 10
        assert container.bus.names() == before
        assert container.orders.list() == []

    def test_commit_publishes_outbox_in_order(self, container):
        cart = _cart(container)
        order, payment = container.checkout.place_and_pay(cart, "card-1")
        names = container.bus.names()
        # cart context first (drained first in collect), then the order unit of work
        assert names[:4] == ["CartCreated", "CartItemAdded",
                             "CartItemAdded", "CartCheckedOut"]
        assert names[4:8] == ["OrderPlaced", "OrderPaymentAuthorized",
                              "OrderPaid", "PaymentAuthorized"]
        assert names.count("StockReserved") == 2
        # UoW persisted aggregates (not just in-memory locals)
        assert container.orders.get(order.order_id) is order
        assert container.payments.get(payment.payment_id) is payment


# -- Event-driven architecture -----------------------------------------------------

class TestEventDriven:
    def test_notification_reacts_to_domain_events(self, container):
        _delivered(container)
        handled = container.notifications.handled_names()
        for e in ("OrderPlaced", "OrderPaid", "PaymentCaptured",
                  "ShipmentShipped", "OrderDelivered"):
            assert e in handled
        subjects = [m.subject for m in container.mailer.sent]
        assert "Order confirmed" in subjects
        assert "Order shipped" in subjects
        assert "Order delivered" in subjects

    def test_handler_spy_without_mocks(self, container):
        seen: list = []
        container.bus.subscribe("OrderPlaced", seen.append)
        _delivered(container)
        assert [e.name for e in seen] == ["OrderPlaced"]

    def test_metrics_count_events(self, container):
        _delivered(container)
        assert container.metrics.count("events.total") > 0
        assert container.metrics.count("events.OrderPlaced") == 1


# -- End-to-end through the new services ------------------------------------------

class TestEndToEnd:
    def test_happy_path_money_and_states(self, container):
        cpn = _coupon(container)
        order, payment, shipment, _ = _delivered(container, coupon=cpn)
        # 150.00 - 30.00 (PCT-20) + 0 tax + 5.00 shipping = 125.00
        assert order.money_total == money(125.00)
        assert payment.amount == money(125.00)
        assert (order.status, payment.status, shipment.status) == (
            "Delivered", "Captured", "Delivered")
        assert cpn.redemptions == 1
        assert container.jobs.pending()[0]["job"] == "order.placed"
        assert container.tracer.spans  # traced without touching services

    def test_full_return_settles_everywhere(self, container):
        cpn = _coupon(container)
        order, payment, _, _ = _delivered(container, coupon=cpn)
        rma = container.returns_svc.request_return(
            order, [(container._p1.product_id, 1), (container._p2.product_id, 1)],
            "broken", container.clock.now())
        pickup = container.returns_svc.schedule_pickup(
            rma, container.clock.now() + timedelta(hours=24))
        container.files.put("photo-1.jpg", b"jpeg-bytes")  # FileBucket ~ S3
        container.returns_svc.complete_pickup(
            rma, pickup, PickupEvidence("photo-1.jpg", "sig-1.png"))
        bd = container.returns_svc.settle(rma, order)
        assert bd.is_full is True
        assert bd.total == money(125.00)
        assert bd.goods == money(120.00) and bd.shipping == money(5.00)
        assert order.status == "Refunded" and payment.status == "Refunded"
        assert container.stocks.get(container._p1.product_id).available == 10
        assert cpn.redemptions == 0  # complete return re-entitles (RT6)
        assert "Return refunded" in [m.subject for m in container.mailer.sent]
        assert container.files.get("photo-1.jpg") == b"jpeg-bytes"

    def test_partial_return_keeps_coupon_and_order_alive(self, container):
        cpn = _coupon(container)
        order, payment, _, _ = _delivered(container, coupon=cpn)
        rma = container.returns_svc.request_return(
            order, [(container._p1.product_id, 1)], "broken", container.clock.now())
        pickup = container.returns_svc.schedule_pickup(
            rma, container.clock.now() + timedelta(hours=24))
        container.returns_svc.complete_pickup(
            rma, pickup, PickupEvidence("photo-1.jpg"))
        bd = container.returns_svc.settle(rma, order)
        assert bd.is_full is False
        assert bd.total == money(83.33)  # RT3 pro-rata
        assert order.status == "Delivered"  # stays alive
        assert payment.status == "Captured"  # PM4 partial accrues
        assert cpn.redemptions == 1  # RT6: partial keeps it burned

    def test_pickup_evidence_still_mandatory(self, container):
        _, _, _, _ = _delivered(container)
        order = container.orders.list()[0]
        rma = container.returns_svc.request_return(
            order, [(container._p1.product_id, 1)], "broken", container.clock.now())
        pickup = container.returns_svc.schedule_pickup(
            rma, container.clock.now() + timedelta(hours=24))
        with pytest.raises(InvariantViolation, match="photo"):
            container.returns_svc.complete_pickup(
                rma, pickup, PickupEvidence(""))

    def test_logger_records_event_stream(self, container):
        _delivered(container)
        assert isinstance(container.logger, StdLogger)
        assert any(r["event"] == "OrderPlaced" for r in container.logger.records)
