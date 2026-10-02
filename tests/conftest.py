"""Shared test fixtures: a fully-wired shop + the real services."""
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
    return Money(int(round(major * 100)), CUR)


class Shop:
    """Everything a scenario needs, wired like the real system."""

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
        now = datetime.now()
        cpn = Coupon(code, ctype, value,
                     valid_from or (now - timedelta(days=1)),
                     valid_to or (now + timedelta(days=30)),
                     usage_limit=usage_limit)
        self.coupons[code] = cpn
        return cpn

    def build_cart(self, customer: str = "cust-1", with_coupon: Coupon | None = None) -> Cart:
        cart = Cart(customer)
        cart.add_product(self.p1, 1)
        cart.add_product(self.p2, 1)
        cart.set_destination(ADDRESS)
        if with_coupon is not None:
            cart.apply_coupon(with_coupon, datetime.now())
        return cart


@pytest.fixture
def shop() -> Shop:
    return Shop()


def delivered_order(shop: Shop, customer: str = "cust-1",
                    coupon: Coupon | None = None) -> tuple[object, object, object, object]:
    """Run the whole forward path; return (order, payment, shipment, cart)."""
    cart = shop.build_cart(customer, coupon)
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    shop.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))
    return order, payment, shipment, cart
