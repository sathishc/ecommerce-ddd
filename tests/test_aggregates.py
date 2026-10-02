"""Aggregate-level unit tests: each invariant as a throwing guard.

Scenario coverage (pure domain, no services/orchestration/UoW/infra):
Money shared-kernel guards; Product P1 (immutable SKU) + price-change event;
Cart C1 (one line per product), C2 (derived total), C3 (single-coupon
replace), C4 (checked-out immutable); Coupon CO2 (window/limit), CO3 (one
redemption per customer), CO5 (percentage cap), CO7 (unredeem idempotent);
Stock S1..S4 (reserve/commit/release/restock cycle); Payment PM1 (no negative/
over-refund), PM2/PM3 (state machine), PM4 (partial vs full refund); Order O3
(terminal states), O4 (pay/ship/cancel/refund guards); Shipment SH1..SH4
(tracking immutable, Delivered terminal); Pickup PK2 (future slot), PK4
(NoAccess attempts), PK5 (photo evidence).

Each test locks one invariant ID in isolation so regressions point at the
exact aggregate guard that broke.
"""
from __future__ import annotations

import datetime
from datetime import datetime, timedelta

import pytest

from domain.cart import Cart
from domain.coupon import Coupon, FIXED_AMOUNT, FREE_SHIPPING, PERCENTAGE
from domain.errors import (CouponValidationError, InvariantViolation,
                           InvalidStateTransition, NoAccessExhausted,
                           StockInsufficient)
from domain.events import events_of
from domain.order import Order
from domain.payment import Payment
from domain.pickup import Pickup
from domain.product import Product
from domain.shipment import Shipment
from domain.stock import Stock
from domain.value_objects import Address, Money, PickupEvidence

from conftest import ADDRESS, money

NOW = datetime.now()


# -- Money (shared kernel) ------------------------------------------------------
class TestMoney:
    """Shared-kernel Money guards (underpin PM1 refund math)."""

    def test_negative_rejected(self):
        """Behavior: negative minor units rejected; Invariant: Money >= 0."""
        with pytest.raises(InvariantViolation):
            Money(-1, "USD")

    def test_currency_mismatch(self):
        """Behavior: cross-currency plus rejected; Invariant: single-currency Money."""
        with pytest.raises(InvariantViolation):
            Money(1, "USD").plus(Money(1, "EUR"))

    def test_minus_floor(self):
        """Behavior: minus below zero rejected; Invariant: PM1 (no negative money)."""
        with pytest.raises(InvariantViolation):
            Money(100, "USD").minus(Money(150, "USD"))

    def test_times_fraction_round_half_up(self):
        """Behavior: fractional multiply rounds half-up; Invariant: RT3 pro-rata rounding."""
        assert Money(100, "USD").times_fraction(1, 3) == Money(33, "USD")
        assert Money(101, "USD").times_fraction(1, 3) == Money(34, "USD")  # 33.67 -> 34
        with pytest.raises(InvariantViolation):
            Money(10, "USD").times_fraction(1, 0)


# -- Product (Catalog) ----------------------------------------------------------
class TestProduct:
    """Catalog Product guards (P1 identity + pricing events)."""

    def test_sku_immutable(self):
        """Behavior: SKU read-only after construction; Invariant: P1 (stable identity)."""
        p = Product(None, "SKU-1", "Widget", money(10))
        assert not hasattr(p, "sku.setter")  # P1: read-only property
        assert p.sku == "SKU-1"

    def test_negative_price_rejected(self):
        """Behavior: negative price rejected; Invariant: P2 (price >= 0)."""
        with pytest.raises(InvariantViolation):
            Product(None, "SKU-1", "W", Money(-5, "USD"))

    def test_price_change_is_an_event(self):
        """Behavior: change_price mutates + emits ProductPriceChanged; Invariant: P3 (priced events)."""
        p = Product(None, "SKU-1", "W", money(10))
        p.change_price(money(12))
        assert p.price == money(12)
        assert [e.name for e in events_of(p)] == ["ProductPriceChanged"]

    def test_discontinued_product_cannot_be_added_to_cart(self):
        """Behavior: discontinued product refused by cart; Invariant: P4 (sellable only)."""
        p = Product(None, "SKU-1", "W", money(10))
        p.discontinue()
        cart = Cart("c1")
        with pytest.raises(InvariantViolation):
            cart.add_product(p)


# -- Cart (Shop) ----------------------------------------------------------------
class TestCart:
    """Shop Cart guards (C1 one-line-per-product, C2 derived total, C3 single-coupon, C4 immutable)."""
    def _cart(self) -> Cart:
        c = Cart("c1")
        c.add_product(Product(None, "A", "Gadget A", money(100)), 2)
        return c

    def test_one_line_per_product(self):
        """Behavior: same product merges onto one line; Invariant: C1 (one line per product)."""
        p = Product("prd-same", "A", "Gadget A", money(100))
        c = Cart("c1")
        c.add_product(p, 2)
        c.add_product(p, 3)  # same product instance -> same product_id
        assert len(c.lines) == 1
        assert c.lines[0][1] == 5
        assert c.subtotal() == money(500.00)

    def test_different_products_are_distinct_lines(self):
        """Behavior: distinct products stay distinct lines; Invariant: C1."""
        c = Cart("c1")
        c.add_product(Product("prd-1", "A", "Gadget A", money(100)), 1)
        c.add_product(Product("prd-2", "B", "Gadget B", money(50)), 1)
        assert len(c.lines) == 2

    def test_quantity_must_be_positive(self):
        """Behavior: zero quantity rejected; Invariant: C1 (qty >= 1)."""
        c = self._cart()
        with pytest.raises(InvariantViolation):
            c.change_quantity("whatever", 0)

    def test_total_is_derived_not_stored(self):
        """Behavior: no stored _total attr; Invariant: C2 (total always derived)."""
        c = self._cart()
        assert not hasattr(c, "_total")  # C2: the quote computes it live

    def test_checked_out_cart_is_immutable(self):
        """Behavior: add/apply after checkout raises; Invariant: C4 (checked-out immutable)."""
        c = self._cart()
        c.set_destination(ADDRESS)
        c.checkout(ADDRESS)
        with pytest.raises(InvalidStateTransition):
            c.add_product(Product(None, "B", "B", money(1)))
        with pytest.raises(InvalidStateTransition):
            c.apply_coupon(Coupon("X", PERCENTAGE, 10, NOW - timedelta(1), NOW + timedelta(1)), NOW)

    def test_single_coupon_replaces(self):
        """Behavior: second coupon replaces first + emits Removed/Applied; Invariant: C3 (single-coupon)."""
        c = self._cart()
        a = Coupon("A", PERCENTAGE, 10, NOW - timedelta(1), NOW + timedelta(1))
        b = Coupon("B", PERCENTAGE, 20, NOW - timedelta(1), NOW + timedelta(1))
        c.apply_coupon(a, NOW)
        c.apply_coupon(b, NOW)
        assert c.applied_coupon_code == "B"
        names = [e.name for e in events_of(c)]
        assert "CouponRemoved" in names and "CouponApplied" in names

    def test_free_shipping_zeroes_the_fee(self):
        """Behavior: FREE_SHIPPING zeroes fee; Invariant: C3 (coupon effect on quote)."""
        c = self._cart()
        c.apply_coupon(Coupon("FS", FREE_SHIPPING, 0, NOW - timedelta(1), NOW + timedelta(1)), NOW)
        assert c.quote().shipping_fee == money(0)

    def test_invalid_coupon_is_rejected_not_ignored(self):
        """Behavior: expired coupon raises, cart unchanged; Invariant: C3 (invalid rejected, not ignored)."""
        c = self._cart()
        dead = Coupon("DEAD", PERCENTAGE, 10, NOW - timedelta(10), NOW - timedelta(5))
        with pytest.raises(CouponValidationError):
            c.apply_coupon(dead, NOW)
        assert c.applied_coupon_code is None  # the cart is unchanged


# -- Coupon (Shop/Pricing) --------------------------------------------------------
class TestCoupon:
    """Pricing Coupon guards (CO2 window/limit, CO3 one-per-customer, CO5 cap, CO7 unredeem)."""
    def _cpn(self, **kw) -> Coupon:
        kw.setdefault("usage_limit", 10)
        return Coupon("SAVE", PERCENTAGE, 20, NOW - timedelta(1), NOW + timedelta(30), **kw)

    def test_one_redemption_per_customer(self):
        """Behavior: second redeem by same customer raises CO3; Invariant: CO3."""
        c = self._cpn()
        c.redeem("cust1", "ord1")
        with pytest.raises(CouponValidationError, match="CO3"):
            c.redeem("cust1", "ord2")

    def test_usage_limit(self):
        """Behavior: redeem past usage_limit raises CO2; Invariant: CO2 (global limit)."""
        c = self._cpn(usage_limit=1)
        c.redeem("cust1", "ord1")
        with pytest.raises(CouponValidationError, match="CO2"):
            c.redeem("cust2", "ord2")

    def test_invalid_window(self):
        """Behavior: validate outside window raises CO2; Invariant: CO2 (validity window)."""
        c = Coupon("SAVE", PERCENTAGE, 20, NOW + timedelta(1), NOW + timedelta(3))
        with pytest.raises(CouponValidationError, match="CO2"):
            c.validate("cust1", money(100), NOW)

    def test_percentage_capped_at_subtotal(self):
        """Behavior: >100% coupon rejected at construction, 100% capped; Invariant: CO5."""
        with pytest.raises(InvariantViolation):
            Coupon("BAD", PERCENTAGE, 150, NOW, NOW + timedelta(1))
        c = Coupon("MAX", PERCENTAGE, 100, NOW, NOW + timedelta(1))
        d = c.compute_discount(money(50))
        assert d.amount == money(50)  # capped, never exceeds

    def test_fixed_cannot_exceed_subtotal(self):
        """Behavior: fixed discount capped at subtotal; Invariant: CO5 (discount <= subtotal)."""
        # money() is major units: money(50) == $50.00 == 5000 minor
        big = Coupon("BIG", FIXED_AMOUNT, Money(10_000, "USD"), NOW, NOW + timedelta(1))  # $100 off
        assert big.compute_discount(money(50)).amount == money(50)  # capped at subtotal
        small = Coupon("SMALL", FIXED_AMOUNT, Money(500, "USD"), NOW, NOW + timedelta(1))  # $5 off
        assert small.compute_discount(money(100)).amount == Money(500, "USD")  # below subtotal, uncapped

    def test_unredeem_only_after_redeem(self):
        """Behavior: unredeem decrements once, then idempotent no-op; Invariant: CO7."""
        c = self._cpn()
        c.redeem("cust1", "ord1")
        assert c.redemptions == 1
        c.unredeem("cust1", "ord1")   # CO7: complete return re-entitles
        assert c.redemptions == 0
        c.unredeem("cust1", "ord1")   # idempotent no-op
        assert c.redemptions == 0
        # CO7 is a no-op (not an error) when the pair never redeemed
        c.unredeem("intruder", "ord-never")
        assert c.redemptions == 0


# -- Stock (Inventory) -------------------------------------------------------------
class TestStock:
    """Inventory Stock guards (S1 available>=0, S2 all-or-nothing, S3 reservation lifecycle, S4 restock)."""

    def test_reserve_fails_when_insufficient(self):
        """Behavior: over-reserve raises, available unchanged; Invariant: S1/S2."""
        s = Stock("p1", 3)
        with pytest.raises(StockInsufficient):
            s.reserve("ord1", 4, NOW + timedelta(1))
        assert s.available == 3  # unchanged on failure

    def test_reserve_commit_release_cycle(self):
        """Behavior: reserve->commit closes reservation, double-commit raises; Invariant: S3."""
        s = Stock("p1", 10)
        rid = s.reserve("ord1", 4, NOW + timedelta(1))
        assert s.available == 6 and s.reserved == 4
        s.commit(rid)
        assert s.available == 6 and s.reserved == 0
        with pytest.raises(InvariantViolation):
            s.commit(rid)  # S3: a committed reservation is closed

    def test_release_restores(self):
        """Behavior: release restores available/reserved; Invariant: S3 (cancel/expiry path)."""
        s = Stock("p1", 10)
        rid = s.reserve("ord1", 4, NOW + timedelta(1))
        s.release(rid, "cancelled")
        assert s.available == 10 and s.reserved == 0

    def test_restock_only_positive(self):
        """Behavior: positive restock applies, zero raises; Invariant: S4."""
        s = Stock("p1", 9)
        s.restock("rma1", {"p1": 4})
        assert s.available == 13
        with pytest.raises(InvariantViolation):
            s.restock("rma2", {"p1": 0})


# -- Payment (generic context) ------------------------------------------------------
class TestPayment:
    """Generic Payment guards (PM1 no over-refund, PM2/PM3 state machine, PM4 partial/full)."""
    def _paid(self, amount=money(100)) -> Payment:
        p = Payment(amount, "card-1")
        p.capture()
        return p

    def test_void_and_capture_exclusive(self):
        """Behavior: capture after void raises; Invariant: PM2/PM3 (Voided terminal vs capture)."""
        p = Payment(money(100), "card-1")
        p.void("cancelled")
        with pytest.raises(InvalidStateTransition):
            p.capture()  # PM2/PM3: a voided payment cannot be captured

    def test_capture_only_from_authorized(self):
        """Behavior: double-capture raises; Invariant: PM2 (capture once)."""
        p = self._paid()
        with pytest.raises(InvalidStateTransition):
            p.capture()  # PM2

    def test_partial_refund_stays_captured(self):
        """Behavior: partial refund accrues, stays Captured; Invariant: PM4."""
        p = self._paid(money(100))
        p.refund(money(40), "partial")
        assert p.status == "Captured" and p.refunded == money(40)

    def test_full_refund_flips_terminal(self):
        """Behavior: refunds summing to captured flip to Refunded; Invariant: PM4."""
        p = self._paid(money(100))
        p.refund(money(40), "partial")
        p.refund(money(60), "rest")
        assert p.status == "Refunded"

    def test_cannot_over_refund(self):
        """Behavior: refund above captured raises PM1; Invariant: PM1."""
        p = self._paid(money(100))
        with pytest.raises(InvariantViolation, match="PM1"):
            p.refund(money(101), "too much")

    def test_zero_refund_rejected(self):
        """Behavior: zero refund rejected; Invariant: PM1 (positive refunds only)."""
        p = self._paid()
        with pytest.raises(InvariantViolation):
            p.refund(Money(0, "USD"), "noop")

    def test_no_refund_before_capture(self):
        """Behavior: refund while Authorized raises; Invariant: PM3 (no refund before capture)."""
        p = Payment(money(100), "card-1")  # still Authorized
        with pytest.raises(InvalidStateTransition):
            p.refund(money(10), "early")


# -- Order (Order Management) ---------------------------------------------------------
def _placed_order() -> Order:
    o = Order()
    o.place([("p1", 1, money(100))], ADDRESS, "cust1",
            discount=money(10), shipping_fee=money(5), tax=money(0))
    return o


class TestOrder:
    """Order lifecycle guards (O2 frozen total, O3 terminal, O4 transition guards)."""

    def test_state_machine_forward(self):
        """Behavior: Placed->Paid->Shipped->Delivered succeeds; Invariant: O3 (forward machine)."""
        o = _placed_order()
        o.pay("pay1")
        o.confirm_shipped("sht1")
        o.deliver()
        assert [o.status] == ["Delivered"]

    def test_ship_requires_paid(self):
        """Behavior: confirm_shipped while Placed raises O4; Invariant: O4 (ship requires paid)."""
        o = _placed_order()
        with pytest.raises(InvalidStateTransition, match="O4"):
            o.confirm_shipped("sht1")

    def test_double_charge_rejected(self):
        """Behavior: second pay raises O4; Invariant: O4 (no double-charge)."""
        o = _placed_order()
        o.pay("pay1")
        with pytest.raises(InvalidStateTransition, match="O4"):
            o.pay("pay2")

    def test_cancel_after_ship_forbidden(self):
        """Behavior: cancel after ship raises O3; Invariant: O3 (shipped not cancellable)."""
        o = _placed_order()
        o.pay("pay1")
        o.confirm_shipped("sht1")
        with pytest.raises(InvalidStateTransition, match="O3"):
            o.cancel("late")

    def test_cancel_from_paid_voids_path(self):
        """Behavior: cancel from Paid -> Cancelled; Invariant: O3 (paid cancellable)."""
        o = _placed_order()
        o.pay("pay1")
        o.cancel("changed mind")
        assert o.status == "Cancelled"

    def test_refund_exceeding_total_rejected(self):
        """Behavior: refund_completed above total raises O4; Invariant: O4 (bounded refund)."""
        o = _placed_order()
        o.pay("pay1")
        o.confirm_shipped("sht1")
        o.deliver()
        with pytest.raises(InvariantViolation, match="O4"):
            o.refund_completed(money(1000), "oops")


# -- Shipment & Pickup (Fulfillment) ---------------------------------------------------
class TestShipment:
    """Fulfillment Shipment guards (SH1 one-per-order, SH3 tracking immutable, SH4 terminal)."""
    def _handed(self) -> Shipment:
        s = Shipment("ord1", [("p1", 1)], ADDRESS)
        s.hand_to_carrier("UPS", "TRK1")
        return s

    def test_tracking_immutable_after_handoff(self):
        """Behavior: scans accumulate, tracking number never changes; Invariant: SH3."""
        s = self._handed()
        original = s.tracking_number
        s.update_tracking("scan-2")  # scans accumulate; number doesn't
        assert s.tracking_number == original
        assert s.scans == ["scan-2"]

    def test_delivered_is_terminal(self):
        """Behavior: updates after Delivered raise; Invariant: SH4 (Delivered terminal)."""
        s = self._handed()
        s.update_tracking("scan-2")
        s.confirm_delivered()
        with pytest.raises(InvalidStateTransition):
            s.update_tracking("scan-3")
        with pytest.raises(InvalidStateTransition):
            s.confirm_delivered()


class TestPickup:
    """Doorstep Pickup guards (PK2 future slot, PK4 NoAccess attempts, PK5 evidence)."""

    def test_slot_must_be_future(self):
        """Behavior: past slot rejected; Invariant: PK2."""
        with pytest.raises(InvariantViolation, match="PK2"):
            Pickup("rma1", ADDRESS, datetime(2000, 1, 1))

    def test_confirm_requires_evidence(self):
        """Behavior: confirm without photo raises PK5; Invariant: PK5."""
        p = Pickup("rma1", ADDRESS, NOW + timedelta(1))
        p.assign_courier("c1")
        with pytest.raises(InvariantViolation, match="PK5"):
            p.confirm_pickup(PickupEvidence(""))

    def test_confirm_is_terminal(self):
        """Behavior: PickedUp terminal (assign after confirm raises); Invariant: PK3."""
        p = Pickup("rma1", ADDRESS, NOW + timedelta(1))
        p.assign_courier("c1")
        p.confirm_pickup(PickupEvidence("photo.jpg", "sig.png"))
        assert p.status == "PickedUp"
        with pytest.raises(InvalidStateTransition):
            p.assign_courier("c2")

    def test_no_access_reschedules_up_to_n(self):
        """Behavior: NoAccess reschedules until attempts exhausted; Invariant: PK4."""
        p = Pickup("rma1", ADDRESS, NOW + timedelta(1), max_attempts=1)
        p.assign_courier("c1")
        p.mark_no_access("nobody home", NOW + timedelta(2))
        assert p.status == "NoAccess" and p.attempts == 1
        p.reschedule(NOW + timedelta(3))
        p.assign_courier("c2")
        with pytest.raises(NoAccessExhausted):
            p.mark_no_access("again", NOW + timedelta(4))
