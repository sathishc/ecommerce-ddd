"""Shared test fixtures: a fully-wired shop + the real services.

Scenario coverage: domain-level (pre-infra) orchestration tests. Provides the
``Shop`` helper (catalog products p1/p2, 10-unit stocks, coupon registry, and
the real ``CheckoutService``/``ShippingService``/``ReturnService``/
``CancelService`` over a shared ``Outbox``) plus the ``shop`` fixture and the
``delivered_order`` helper that runs the whole forward path
(Cart -> Order -> authorize -> ship -> capture -> deliver) to a Delivered
order for return-path tests.

Aggregates/flows locked: happy-path totals (C3 single-coupon, O2 frozen
total), stock reserve->commit accounting (S1..S4), payment authorize->capture
(PM2/PM3), and delivery as the return-path precondition (RT1/RT2).

Invariants using this file: C1..C4 (cart), CO2/CO3/CO7 (coupon), O2..O4
(order), PM1..PM4 (payment), S1..S4 (stock), RT1..RT6 (returns), PK2/PK4/PK5
(pickup). Uses wall-clock ``datetime.now()`` for coupon windows (the infra
suite supersedes this with ``FixedClock`` for determinism).
"""
from __future__ import annotations

import datetime
from datetime import date, datetime, timedelta

import pytest

from domain.cart import Cart
from domain.coupon import Coupon, PERCENTAGE, FIXED_AMOUNT, FREE_SHIPPING
from domain.errors import (CouponValidationError, DomainError, InvariantViolation,
                           InvalidStateTransition, NoAccessExhausted, StockInsufficient)
from domain.orchestration import (AUTO_COURIER, CancelService, CheckoutService,
                                  Outbox, ReturnService, ShippingService)
from domain.pickup import Pickup
from domain.product import Product
from domain.return_rma import _OrderFacts, Return
from domain.stock import Stock
from domain.value_objects import Address, Money, PickupEvidence

CUR = "USD"
ADDRESS = Address("1 Main St", "Springfield", "12345", "US")


def money(major: float) -> Money:
    """Build a USD Money from major units (e.g. money(100.00) == $100.00).

    Args:
        major: Dollar amount (float) converted to minor units (cents).

    Returns:
        ``Money`` in ``CUR`` (USD).
    """
    return Money(int(round(major * 100)), CUR)


class Shop:
    """Everything a scenario needs, wired like the real system.

    Test double for the application layer (pre-container era): holds the
    shared ``Outbox`` (event stream under test), seeded catalog (p1=$100,
    p2=$50), per-product ``Stock`` dict, coupon/payment registries, and the
    four real orchestration services. Tests mutate this directly -- no mocks.
    """

    def __init__(self) -> None:
        self.outbox = Outbox()
        self.p1 = Product(None, "SKU-A", "Gadget A", money(100.00))
        self.p2 = Product(None, "SKU-B", "Gadget B", money(50.00))
        self.stocks = {
            self.p1.product_id: Stock(self.p1.product_id, 10),
            self.p2.product_id: Stock(self.p2.product_id, 10),
        }
        self.coupons: dict[str, Coupon] = {}
        self.payments: dict[str, object] = {}
        self.checkout = CheckoutService(self.outbox, self.stocks, self.coupons)
        self.shipping = ShippingService(self.outbox, self.stocks, self.payments)
        self.returns = ReturnService(self.outbox, self.payments, self.stocks, self.coupons)
        self.cancels = CancelService(self.outbox)

    def add_coupon(self, code: str = "SAVE20", ctype=PERCENTAGE, value=20,
                   valid_from: datetime | None = None, valid_to: datetime | None = None,
                   usage_limit: int | None = 100) -> Coupon:
        """Register a coupon (default PCT-20, valid yesterday..+30d, limit 100).

        Args:
            code: Coupon code (default "SAVE20").
            ctype: Coupon type (default PERCENTAGE).
            value: Discount value (default 20).
            valid_from: Window start (defaults to now - 1 day).
            valid_to: Window end (defaults to now + 30 days).
            usage_limit: Max redemptions (default 100).

        Returns:
            The registered ``Coupon`` (also stored in ``self.coupons``).
        """
        now = datetime.now()
        cpn = Coupon(code, ctype, value,
                     valid_from or (now - timedelta(days=1)),
                     valid_to or (now + timedelta(days=30)),
                     usage_limit=usage_limit)
        self.coupons[code] = cpn
        return cpn

    def build_cart(self, customer: str = "cust-1", with_coupon: Coupon | None = None) -> Cart:
        """Build a 2-line cart (p1 x1 + p2 x1) with destination set.

        Args:
            customer: Customer ref for the cart.
            with_coupon: Optional coupon to preview-apply (C3 single-coupon).

        Returns:
            A destination-set ``Cart`` ready for ``place_and_pay``.
        """
        cart = Cart(customer)
        cart.add_product(self.p1, 1)
        cart.add_product(self.p2, 1)
        cart.set_destination(ADDRESS)
        if with_coupon is not None:
            cart.apply_coupon(with_coupon, datetime.now())
        return cart


@pytest.fixture
def shop() -> Shop:
    """Fresh ``Shop`` per test (isolation: no cross-test stock/coupon bleed)."""
    return Shop()


def delivered_order(shop: Shop, customer: str = "cust-1",
                    coupon: Coupon | None = None) -> tuple[object, object, object, object]:
    """Run the whole forward path; return (order, payment, shipment, cart).

    Given a shop (+ optional coupon), When placing, paying, shipping and
    delivering, Then returns the Delivered order graph. Precondition helper
    for return-path tests (RT1 needs Delivered + delivered_at).
    """
    cart = shop.build_cart(customer, coupon)
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    shop.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))
    return order, payment, shipment, cart
