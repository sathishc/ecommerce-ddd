"""The doorstep-return path:
    RequestReturn -> Approve -> SchedulePickup -> ConfirmPickup(evidence)
         -> SettleReturn -> PaymentRefunded + StockRestocked
              (+ Order->Refunded, CouponUnredeemed  on a COMPLETE return)
"""
from __future__ import annotations

import datetime
from datetime import date, datetime, timedelta

import pytest

from conftest import delivered_order, money, ADDRESS
from domain.errors import (DomainError, InvariantViolation, InvalidStateTransition,
                           NoAccessExhausted, StockInsufficient)
from domain.events import (CouponUnredeemed, OrderRefunded, PaymentRefunded,
                           ReturnApproved, ReturnGoodsReceived, ReturnRefunded,
                           ReturnRequested, StockRestocked)
from domain.pickup import Pickup
from domain.value_objects import PickupEvidence

LATE = datetime(2026, 9, 29, 10, 0)


def run_pickup(shop, rma, evidence=None) -> Pickup:
    evidence = evidence or PickupEvidence("photo-1.jpg", "sig-1.png")
    pickup = shop.returns.schedule_pickup(rma, datetime.now() + timedelta(hours=24))
    shop.returns.complete_pickup(rma, pickup, evidence)
    return pickup


def test_full_return_refunds_everything(shop):
    cpn = shop.add_coupon()
    order, payment, _, _ = delivered_order(shop, coupon=cpn)

    rma = shop.returns.request_return(
        order, [(shop.p1.product_id, 1), (shop.p2.product_id, 1)], "broken", LATE)
    run_pickup(shop, rma)
    bd = shop.returns.settle(rma, order)

    # RT3 pro-rata, full return (f == 1): everything back
    assert bd.is_full is True
    assert bd.total == money(125.00)          # goods 120 + tax 0 + shipping 5
    assert bd.goods == money(120.00)          # 150 - 30 (discount share) = 120
    assert bd.tax == money(0.00)
    assert bd.shipping == money(5.00)         # full fee on a full return

    # across every context
    assert order.status == "Refunded"         # terminal (O3)
    assert payment.status == "Refunded"       # PM4 full settlement
    assert payment.refunded == money(125.00)
    assert shop.stocks[shop.p1.product_id].available == 10   # restocked
    assert shop.stocks[shop.p2.product_id].available == 10
    assert cpn.redemptions == 0               # RT6: complete return re-entitles

    names = [e.name for e in shop.outbox.all()]
    for expected in ("ReturnRequested", "ReturnApproved", "ReturnGoodsReceived",
                     "ReturnRefunded", "PaymentRefunded", "StockRestocked",
                     "StockRestocked", "OrderRefunded", "CouponUnredeemed"):
        assert expected in names


def test_partial_return_keeps_coupon_burned(shop):
    cpn = shop.add_coupon()
    order, payment, _, _ = delivered_order(shop, coupon=cpn)

    # return ONLY the $100 gadget, keep the $50 one
    rma = shop.returns.request_return(order, [(shop.p1.product_id, 1)], "broken", LATE)
    run_pickup(shop, rma)
    bd = shop.returns.settle(rma, order)

    # RT3: fraction = 100/150 = 2/3 of the order
    assert bd.is_full is False
    # goods = round(100 * (150 - 30)/150) = round(80.0) = 80.00
    assert bd.goods == money(80.00)
    # shipping = round(500 * 100/150) = round(333.33) = 333.33 -> 333 -> 3.33
    assert bd.shipping == money(3.33)
    assert bd.total == money(83.33)

    # the order stays alive (Delivered) — it wasn't fully returned
    assert order.status == "Delivered"
    assert order.rma_ids == [rma.rma_id]
    assert payment.status == "Captured"        # PM4: partial refund accrues
    assert payment.refunded == money(83.33)
    # RT6: partial return does NOT re-entitle the coupon
    assert cpn.redemptions == 1


def test_pro_rata_math_round_half_up(shop):
    """Verify the RT3 fraction math directly on the aggregate (no services)."""
    from domain.return_rma import Return, _OrderFacts
    facts = _OrderFacts(
        order_id="ord-x", customer_id="c1", destination=ADDRESS,
        shipped_lines=(("a", 1, money(100)), ("b", 1, money(50))),
        discount=money(30), shipping_fee=money(5), tax_num=0, tax_den=100,
        delivered_at=date(2026, 9, 28),
    )
    rma = Return(facts, [("a", 1)], "broken", LATE)
    rma.approve()
    # walk the full public state chain so the machine is exercised, not bypassed
    rma.link_pickup("pku-1")
    rma.on_pickup_confirmed("pku-1", PickupEvidence("photo.jpg"))
    rma.receive_goods()
    bd = rma.settle()
    assert bd.is_full is False
    assert bd.goods == money(80.00)
    assert bd.shipping == money(3.33)
    assert bd.total == money(83.33)


def test_return_window_30_days(shop):
    order, payment, _, _ = delivered_order(shop)
    # delivered 2026-09-28; request at 31 days -> out of window (RT1)
    too_late = datetime(2026, 10, 29, 10, 0)
    with pytest.raises(InvariantViolation, match="window"):
        shop.returns.request_return(order, [(shop.p1.product_id, 1)], "late", too_late)


def test_cannot_return_before_delivery(shop):
    cart = shop.build_cart()
    order, payment = shop.checkout.place_and_pay(cart, "card-1")
    with pytest.raises(DomainError, match="Delivered"):
        shop.returns.request_return(order, [(shop.p1.product_id, 1)], "early", LATE)


def test_no_over_returning(shop):
    order, payment, _, _ = delivered_order(shop)
    # only 1 was shipped; requesting 2 violates RT2
    with pytest.raises(InvariantViolation, match="exceeds shipped"):
        shop.returns.request_return(order, [(shop.p1.product_id, 2)], "dupe", LATE)


def test_restock_only_after_pickup_confirmed(shop):
    """RT5/S4: the courier's on-site scan is the gate. No restock before it."""
    order, payment, _, _ = delivered_order(shop)
    rma = shop.returns.request_return(order, [(shop.p1.product_id, 1)], "broken", LATE)
    pickup = shop.returns.schedule_pickup(rma, datetime.now() + timedelta(hours=24))

    # trying to settle before the courier confirms raises (RT5)
    with pytest.raises(InvalidStateTransition, match="GoodsReceived"):
        rma.settle()
    # and stock must NOT have changed yet
    assert shop.stocks[shop.p1.product_id].available == 9


def test_pickup_requires_photo_evidence(shop):
    """PK5: a doorstep pickup cannot be confirmed without captured proof."""
    order, payment, _, _ = delivered_order(shop)
    rma = shop.returns.request_return(order, [(shop.p1.product_id, 1)], "broken", LATE)
    pickup = shop.returns.schedule_pickup(rma, datetime.now() + timedelta(hours=24))
    # no photo -> the possession transfer is refused
    with pytest.raises(InvariantViolation, match="photo"):
        shop.returns.complete_pickup(rma, pickup, PickupEvidence(""))


def test_no_access_reschedules_then_exhausts(shop):
    """PK4: missed visits re-schedule up to N attempts, then flag manual."""
    order, payment, _, _ = delivered_order(shop)
    rma = shop.returns.request_return(order, [(shop.p1.product_id, 1)], "broken", LATE)
    pickup = Pickup(rma.rma_id, ADDRESS, datetime.now() + timedelta(hours=24), max_attempts=2)
    pickup.assign_courier("courier-x")

    pickup.mark_no_access("nobody home", datetime.now() + timedelta(hours=48))
    assert pickup.status == "NoAccess" and pickup.attempts == 1
    pickup.reschedule(datetime.now() + timedelta(hours=72))
    pickup.assign_courier("courier-y")
    pickup.mark_no_access("nobody again", datetime.now() + timedelta(hours=96))
    assert pickup.attempts == 2  # at the limit, still re-schedulable
    pickup.reschedule(datetime.now() + timedelta(hours=99))
    pickup.assign_courier("courier-z")
    with pytest.raises(NoAccessExhausted):
        pickup.mark_no_access("third miss", datetime.now() + timedelta(hours=100))


def test_reject_before_pickup(shop):
    order, payment, _, _ = delivered_order(shop)
    rma = shop.returns.request_return(order, [(shop.p1.product_id, 1)], "broken", LATE)
    # request_return already approved it (service shortcut); from Approved,
    # rejection is allowed (RT4) — no second approve needed.
    rma.reject("not eligible")
    assert rma.status == "Rejected"
    assert shop.stocks[shop.p1.product_id].available == 9  # untouched
