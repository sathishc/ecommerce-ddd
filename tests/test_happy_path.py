"""The forward (happy) path, driven through the real orchestration services:
    Cart -> Order -> Payment(authorize) -> Stock(reserve)
         -> Shipment -> Payment(capture) -> Delivery.
"""
from __future__ import annotations

import datetime
from datetime import date

import pytest

from conftest import delivered_order, money, ADDRESS
from domain.errors import DomainError, InvariantViolation
from domain.orchestration import Outbox

from domain.events import (OrderDelivered, OrderPaymentAuthorized, OrderPlaced,
                           OrderShipped, PaymentAuthorized, PaymentCaptured,
                           StockCommitted, StockReserved)


def test_full_happy_path(shop):
    cpn = shop.add_coupon()
    order, payment, shipment, cart = delivered_order(shop, coupon=cpn)

    # money: 150.00 - 30.00 (PCT-20) + 0 tax + 5.00 flat shipping = 125.00
    assert order.money_total == money(125.00)
    assert payment.amount == money(125.00)

    # state machine reached the end
    assert order.status == "Delivered"
    assert payment.status == "Captured"
    assert shipment.status == "Delivered"
    assert order.delivered_at == date(2026, 9, 28)

    # the customer-facing coupon was redeemed exactly once
    assert cpn.redemptions == 1


def test_stock_reserved_then_committed(shop):
    order, payment, shipment, _ = delivered_order(shop)
    s1 = shop.stocks[shop.p1.product_id]
    s2 = shop.stocks[shop.p2.product_id]
    # reserve at placement (10 -> 9 each), commit at shipment (available stays)
    assert s1.available == 9 and s1.reserved == 0  # committed, no longer reserved
    assert s2.available == 9


def test_capture_happens_at_shipment_not_placement(shop):
    cart = shop.build_cart()
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    # authorize at placement: money held, NOT moved
    assert payment.status == "Authorized"
    assert order.status == "Paid"
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    # capture at shipment: the goods left, so the money moves
    assert payment.status == "Captured"
    assert order.status == "Shipped"


def test_money_total_is_frozen(shop):
    """O2: the order's money_total can never change after placement."""
    cpn = shop.add_coupon()
    order, _, _, _ = delivered_order(shop, coupon=cpn)
    total = order.money_total
    assert total == money(125.00)
    # nothing on Order exposes a setter; the aggregate's money is immutable
    assert not hasattr(order, "set_money_total")
    assert not hasattr(order, "money_total.__setattr__")


def test_event_stream_order(shop):
    """The outbox (Notification context) sees the whole story, in order."""
    cpn = shop.add_coupon()
    cart = shop.build_cart(with_coupon=cpn)
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    shop.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))

    # The checkout/ship/deliver unit of work publishes these, in this order.
    # (The cart's own events — CartCreated/CartItemAdded/CouponApplied/
    # CartCheckedOut — were drained by the cart before the service ran; in a
    # real system the cart context would forward them to the outbox too.)
    names = [e.name for e in shop.outbox.all()]
    # Canonical stream of the checkout/ship/deliver unit of work (drain order:
    # order, shipment, stocks, payment). The cart's own events were drained by
    # the cart before the service ran.
    expected = [
        "OrderPlaced", "OrderPaymentAuthorized", "OrderPaid",
        "PaymentAuthorized", "StockReserved", "StockReserved",
        "OrderShipped", "ShipmentCreated", "ShipmentShipped",
        "StockCommitted", "StockCommitted", "PaymentCaptured",
        "ShipmentDelivered", "OrderDelivered",
    ]
    assert names == expected


def test_checkout_requires_destination(shop):
    cart = shop.build_cart()
    cart._destination = None  # unset it
    with pytest.raises(Exception, match="destination"):
        shop.checkout.place_and_pay(cart, "card-1")


def test_double_checkout_is_rejected(shop):
    cart = shop.build_cart()
    with pytest.raises(DomainError, match="C4"):
        shop.checkout.place_and_pay(cart, "card-1")
        shop.checkout.place_and_pay(cart, "card-1")  # C4: one-shot
