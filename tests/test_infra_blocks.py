"""Infrastructure tests: AWS Blocks (local), DI, Unit-of-Work, event-driven flow.

Scenario coverage (all on ``build_test_container`` -- FixedClock + in-memory
everything, NO AWS account, ``BLOCKS_BACKEND=local``): local Block ports
(KVStore conditional writes ~ DynamoDB, Table query ~ DynamoDB, FileBucket
roundtrip ~ S3 photo evidence, EmailClient capture ~ SES, AsyncJob queue ~
SQS); DI container wiring (injected services, shared bus, FixedClock control);
UoW atomicity (failed checkout rolls back stock + publishes nothing) and
outbox ordering (cart events then order events); event-driven Notification
reactions (emails/logs/metrics via handler spy, no mocks); end-to-end money
+ states for full and partial returns through the new app services.

The same application services deploy unchanged against real AWS Blocks
(DynamoDB/SES/SQS) via ``build_container(backend="aws")`` (LSP).

Flows/invariants locked: O2/O3 (frozen total + Delivered/Refunded), PM4
(Captured vs Refunded), RT3 (83.33 partial / 125.00 full pro-rata), RT6
(coupon burned vs re-entitled), PK5 (evidence mandatory), S3 (rollback),
outbox-only-on-commit.
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
    """Fresh test container per test, seeded with p1/p2 ($100/$50, 10 units each)."""
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
    """Build a 2-line destination-set cart against the container catalog.

    Args:
        c: Test container (provides products + clock).
        customer: Customer ref.
        coupon: Optional coupon to apply at the container clock's now.

    Returns:
        A ready-to-checkout ``Cart``.
    """
    cart = Cart(customer)
    cart.add_product(c._p1, 1)
    cart.add_product(c._p2, 1)
    cart.set_destination(ADDRESS)
    if coupon is not None:
        cart.apply_coupon(coupon, c.clock.now())
    return cart


def _coupon(c, code="SAVE20") -> Coupon:
    """Register a PCT-20 coupon valid around the container clock's now.

    Args:
        c: Test container (provides clock + coupon repo).
        code: Coupon code (default "SAVE20").

    Returns:
        The registered ``Coupon``.
    """
    now = c.clock.now()
    cpn = Coupon(code, PERCENTAGE, 20, now - timedelta(days=1),
                 now + timedelta(days=30), usage_limit=100)
    c.coupons.add(cpn)
    return cpn


def _delivered(c, coupon=None):
    """Run place->pay->ship->deliver through app services; return (order, payment, shipment, cart)."""
    cart = _cart(c, coupon=coupon)
    order, payment = c.checkout.place_and_pay(cart, "card-1")
    shipment = c.shipping.ship(order, "UPS", "TRK-1")
    c.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))
    return order, payment, shipment, cart


# -- Blocks: local implementations ------------------------------------------------

class TestBlocksLocal:
    """Local Block ports behave like their AWS counterparts (no account needed)."""

    def test_kv_conditional_write(self):
        """Behavior: put_if_not_exists writes once, second write False; Invariant: DynamoDB conditional."""
        kv = LocalKVStore()
        assert kv.put_if_not_exists("k", "1") is True
        assert kv.put_if_not_exists("k", "2") is False  # conditional, like DynamoDB
        assert kv.get("k") == "1"

    def test_table_query_orders_by_pk(self):
        """Behavior: query returns rows ordered by sk, missing row None; Invariant: DynamoDB Query order."""
        t = LocalTable()
        t.put("ord-1", "line#1", {"sku": "A"})
        t.put("ord-1", "line#2", {"sku": "B"})
        assert [r["sku"] for r in t.query("ord-1")] == ["A", "B"]
        assert t.get("ord-1", "line#9") is None

    def test_file_bucket_roundtrip(self):
        """Behavior: put/get roundtrips bytes; Invariant: S3 photo-evidence store (PK5 support)."""
        b = LocalFileBucket()
        ref = b.put("photo-1.jpg", b"\xff\xd8fake-jpeg")
        assert b.get(ref) == b"\xff\xd8fake-jpeg"

    def test_email_captured_not_sent(self):
        """Behavior: send captured in memory (never SES); Invariant: Notification email port."""
        mail = LocalEmailClient()
        mail.send("a@x.com", "hi", "body")
        assert len(mail.sent) == 1 and mail.sent[0].to == "a@x.com"

    def test_async_job_queue(self):
        """Behavior: enqueue->pending->drain exactly once; Invariant: SQS fire-and-forget port."""
        jobs = LocalAsyncJob()
        jobs.enqueue("order.placed", {"order_id": "ord-1"})
        assert jobs.pending() == [{"job": "order.placed", "order_id": "ord-1"}]
        assert jobs.drain() == [{"job": "order.placed", "order_id": "ord-1"}]
        assert jobs.pending() == []


# -- DI container -----------------------------------------------------------------

class TestContainer:
    """DI composition root: services injected, one shared bus, deterministic clock."""

    def test_services_are_injected_not_constructed(self, container):
        """Behavior: checkout/shipping injected + share one empty bus; Invariant: DIP composition root."""
        assert isinstance(container.checkout, CheckoutAppService)
        assert container.checkout is not container.shipping
        # one shared bus: services publish where notifications listen
        assert container.bus.published() == []

    def test_fixed_clock_controls_time(self):
        """Behavior: FixedClock freezes then advances deterministically; Invariant: Clock port (RT1 testing)."""
        c = build_test_container(at=datetime(2026, 9, 28, 12, 0))
        assert isinstance(c.clock, FixedClock)
        c.clock.advance(days=1)
        assert c.clock.now() == datetime(2026, 9, 29, 12, 0)


# -- Unit of Work: atomicity + outbox --------------------------------------------

class TestUnitOfWork:
    """Atomic transactions + transactional outbox (commit publishes, failure is silent)."""

    def test_failed_checkout_rolls_back(self, container):
        """Behavior: checkout with missing stock record fails with stock/events/orders unchanged.

        Invariants: UoW atomicity (S3 reservation rolled back), outbox-silent-on-failure.
        """
        # Given a cart but sabotaged inventory (second product has no stock record)
        cart = _cart(container)
        # sabotage: second product has no stock record -> checkout must fail atomically
        container.stocks.clear()
        p1 = container.products.list()[0]
        container.stocks.add(Stock(p1.product_id, 10))
        before = container.bus.names()
        # When checkout fails atomically (Then: rollback -- reservation released, nothing published)
        with pytest.raises(DomainError, match="no stock record"):
            container.checkout.place_and_pay(cart, "card-1")
        assert container.stocks.get(p1.product_id).available == 10
        assert container.bus.names() == before
        assert container.orders.list() == []

    def test_commit_publishes_outbox_in_order(self, container):
        """Behavior: commit persists aggregates + publishes cart-then-order stream; Invariant: outbox order."""
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
    """Pub/sub Notification reactions observed without mocks (bus spies + local ports)."""

    def test_notification_reacts_to_domain_events(self, container):
        """Behavior: delivered flow handled everywhere + 3 customer emails; Invariant: Notification fan-out."""
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
        """Behavior: per-event subscribe sees exactly one OrderPlaced; Invariant: spy-friendly bus (no mocks)."""
        seen: list = []
        container.bus.subscribe("OrderPlaced", seen.append)
        _delivered(container)
        assert [e.name for e in seen] == ["OrderPlaced"]

    def test_metrics_count_events(self, container):
        """Behavior: CloudWatch-port counters record the stream; Invariant: Metrics fan-out."""
        _delivered(container)
        assert container.metrics.count("events.total") > 0
        assert container.metrics.count("events.OrderPlaced") == 1


# -- End-to-end through the new services ------------------------------------------

class TestEndToEnd:
    """End-to-end through app services: money + states + jobs + tracing."""

    def test_happy_path_money_and_states(self, container):
        """Behavior: 125.00 Delivered/Captured + coupon burned + job + spans; Invariants: O2/O3/PM2/C3/CO3."""
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
        """Behavior: full return settles order/payment/stock/coupon/email/blob; Invariants: RT3/O3/PM4/S4/RT6/PK5."""
        # Given a delivered order with coupon
        cpn = _coupon(container)
        order, payment, _, _ = _delivered(container, coupon=cpn)
        rma = container.returns_svc.request_return(
            order, [(container._p1.product_id, 1), (container._p2.product_id, 1)],
            "broken", container.clock.now())
        pickup = container.returns_svc.schedule_pickup(
            rma, container.clock.now() + timedelta(hours=24))
        # When storing evidence (FileBucket ~ S3) + confirming pickup + settling (Then: 125.00 everywhere)
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
        """Behavior: partial 83.33 keeps Delivered/Captured/burned; Invariants: RT3/O3/PM4/RT6."""
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
        """Behavior: complete_pickup with empty evidence raises photo; Invariant: PK5."""
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
        """Behavior: StdLogger records contain OrderPlaced; Invariant: CloudWatch-Logs port."""
        _delivered(container)
        assert isinstance(container.logger, StdLogger)
        assert any(r["event"] == "OrderPlaced" for r in container.logger.records)
