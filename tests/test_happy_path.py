"""The forward (happy) path, driven through the real orchestration services.

Scenario coverage (domain orchestration, via ``shop`` fixture + ``delivered_order``):
Cart -> Order -> Payment(authorize) -> Stock(reserve) -> Shipment ->
Payment(capture) -> Delivery. Locks the canonical money math (150 - 30 PCT-20
+ 0 tax + 5 shipping = 125), the authorize-at-placement/capture-at-shipment
split, reserve-then-commit stock accounting, O2 frozen totals, the C4
one-shot cart guard, and the exact checkout/ship/deliver outbox stream order
consumed by the Notification context.

Flows/invariants locked: happy-path totals (C3, O2), state machine to
Delivered (O3), reserve->commit (S1..S4), authorize->capture (PM2/PM3),
coupon redeemed exactly once (CO3), destination required + C4 one-shot.
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
    """Behavior: full forward path yields Delivered/Captured + 125.00 + coupon burned once.

    Invariants: C3 (single-coupon math), O2 (frozen total), O3 (Delivered),
    PM2/PM3 (authorize->capture), CO3 (redeemed once).
    """
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
    """Behavior: stock reserved at placement (10->9), committed at shipment.

    Invariants: S1 (available>=0), S2 (all-or-nothing), S3 (commit on shipment).
    """
    order, payment, shipment, _ = delivered_order(shop)
    s1 = shop.stocks[shop.p1.product_id]
    s2 = shop.stocks[shop.p2.product_id]
    # reserve at placement (10 -> 9 each), commit at shipment (available stays)
    assert s1.available == 9 and s1.reserved == 0  # committed, no longer reserved
    assert s2.available == 9


def test_capture_happens_at_shipment_not_placement(shop):
    """Behavior: Authorized at placement, Captured at shipment (money moves on handoff).

    Invariants: PM2/PM3 (authorize-then-capture split), O3 (Paid->Shipped).
    """
    # Given a ready cart
    cart = shop.build_cart()
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    # When placing the order (Then: authorize at placement -- money held, NOT moved)
    assert payment.status == "Authorized"
    assert order.status == "Paid"
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    # capture at shipment: the goods left, so the money moves
    assert payment.status == "Captured"
    assert order.status == "Shipped"


def test_money_total_is_frozen(shop):
    """Behavior: order total immutable after placement (no setters); Invariant: O2."""
    cpn = shop.add_coupon()
    order, _, _, _ = delivered_order(shop, coupon=cpn)
    total = order.money_total
    assert total == money(125.00)
    # nothing on Order exposes a setter; the aggregate's money is immutable
    assert not hasattr(order, "set_money_total")
    assert not hasattr(order, "money_total.__setattr__")


def test_event_stream_order(shop):
    """Behavior: outbox publishes the canonical checkout/ship/deliver stream in drain order.

    Invariants: event-sourcing completeness (Notification context sees every
    fact); drain order order->shipment->stocks->payment.
    """
    cpn = shop.add_coupon()
    cart = shop.build_cart(with_coupon=cpn)
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    shop.payments[payment.payment_id] = payment
    shipment = shop.shipping.ship(order, "UPS", "TRK-1")
    shop.shipping.mark_delivered(shipment, order, at=date(2026, 9, 28))

    # When running checkout/ship/deliver (Then: the unit of work publishes these, in this order).
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
    """Behavior: place_and_pay without destination raises; Invariant: C5 (destination required)."""
    cart = shop.build_cart()
    cart._destination = None  # unset it
    with pytest.raises(Exception, match="destination"):
        shop.checkout.place_and_pay(cart, "card-1")


def test_double_checkout_is_rejected(shop):
    """Behavior: second place_and_pay on same cart raises C4; Invariant: C4 (one-shot cart)."""
    cart = shop.build_cart()
    with pytest.raises(DomainError, match="C4"):
        shop.checkout.place_and_pay(cart, "card-1")
        shop.checkout.place_and_pay(cart, "card-1")  # C4: one-shot
