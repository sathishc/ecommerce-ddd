"""Domain events (past tense, immutable). One class per event in the model.

Bounded-context role:
    The shared fact language every context publishes and the Notification
    context subscribes to (pub/sub). Events record what already happened —
    never intents — so downstream reactions (capture at shipment handoff,
    restock after ``PickupConfirmed``, coupon un-redeem on a full refund,
    derived ``OrderShipped`` / ``OrderDelivered`` / ``ReturnGoodsReceived``)
    stay purely event-driven with no extra commands.

Lifecycle notes:
    * Shop/Cart: ``CartCreated`` -> item/coupon events -> ``CartCheckedOut``.
    * Order Mgmt: ``OrderPlaced`` -> ``OrderPaymentAuthorized``/``OrderPaid``
      -> ``OrderShipped`` -> ``OrderDelivered`` -> ``OrderRefunded`` (or
      ``OrderCancelled``/``OrderExpired``). Refunded/Cancelled/Expired are
      terminal (O4).
    * Inventory: ``StockReserved`` -> ``StockCommitted`` (fulfillment) |
      ``StockReleased`` (cancel/expiry); ``StockRestocked`` only after
      ``PickupConfirmed`` (S4/RT5). ``StockInsufficientEvent`` is the
      all-or-nothing safety valve (S2).
    * Payment (generic): ``PaymentAuthorized`` -> ``PaymentCaptured`` |
      ``PaymentVoided``; ``PaymentRefunded`` per settlement (PM1/PM4; a
      partial refund keeps the Payment Captured).
    * Fulfillment: ``ShipmentCreated`` -> ``ShipmentShipped`` ->
      ``ShipmentDelivered`` (SH3); ``PickupScheduled`` -> ``PickupConfirmed``
      (PK3/PK5 possession transfer at the door).
    * Returns: ``ReturnRequested`` -> ``ReturnApproved`` ->
      ``ReturnGoodsReceived`` -> ``ReturnRefunded`` (RT3 pro-rata) or
      ``ReturnRejected`` (RT4). ``CouponUnredeemed`` fires only on a
      complete return (RT6/CO7).

All classes are frozen dataclasses; ``name`` (the class name) is the
pub/sub routing key used by the Outbox/EventBus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .value_objects import Money


@dataclass(frozen=True)
class DomainEvent:
    """Base class for all domain events (past tense, immutable facts).

    Role:
        Marker + routing helper: ``name`` (the class name, e.g.
        'OrderPlaced') is the pub/sub key used by the Outbox/EventBus and
        the Notification context.

    Members: none (subclasses carry the fact payload).
    """

    @property
    def name(self) -> str:
        """Return the pub/sub routing key (the concrete class name).

        Returns:
            str: e.g. 'OrderPlaced'.
        """
        return type(self).__name__


# -- Catalog -----------------------------------------------------------------
@dataclass(frozen=True)
class ProductPublished(DomainEvent):
    """A product entered the catalog (Catalog context)."""

    product_id: str = ""


@dataclass(frozen=True)
class ProductPriceChanged(DomainEvent):
    """A product's price changed (P3: repricing is an event, never silent)."""

    product_id: str = ""
    new_price: Money | None = None


# -- Shop (Cart + Coupon) ------------------------------------------------------
@dataclass(frozen=True)
class CartCreated(DomainEvent):
    """A customer opened a cart (Shop; from ``OpenCart``)."""

    cart_id: str = ""


@dataclass(frozen=True)
class CartItemAdded(DomainEvent):
    """A new product line entered the cart (C1; from ``AddToCart``)."""

    cart_id: str = ""
    product_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class CartItemChanged(DomainEvent):
    """A cart line quantity changed (C1; from ``AddToCart``/``UpdateCartLine``)."""

    cart_id: str = ""
    product_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class CartLineRemoved(DomainEvent):
    """A cart line was removed (from ``RemoveCartLine``)."""

    cart_id: str = ""
    product_id: str = ""


@dataclass(frozen=True)
class CouponApplied(DomainEvent):
    """A coupon preview was attached to the cart (C3/CO4; from ``ApplyCoupon``)."""

    cart_id: str = ""
    coupon_code: str = ""


@dataclass(frozen=True)
class CouponRemoved(DomainEvent):
    """A coupon preview left the cart (replace/remove; from ``ApplyCoupon``/``RemoveCoupon``)."""

    cart_id: str = ""


@dataclass(frozen=True)
class CartCheckedOut(DomainEvent):
    """A cart froze for checkout (C4: immutable after; from ``PlaceOrder``)."""

    cart_id: str = ""


@dataclass(frozen=True)
class CartAbandoned(DomainEvent):
    """A cart closed without buying (from ``CloseCart``)."""

    cart_id: str = ""


# -- Order Management ----------------------------------------------------------
@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    """An order was placed with its frozen total (O1/O2; from ``PlaceOrder``)."""

    order_id: str = ""
    money_total: Money | None = None


@dataclass(frozen=True)
class OrderPaymentAuthorized(DomainEvent):
    """An order's payment was authorized (O3: Placed -> Paid leg)."""

    order_id: str = ""


@dataclass(frozen=True)
class OrderPaid(DomainEvent):
    """An order became Paid; Fulfillment may now ship (O3/O4 partnership trigger)."""

    order_id: str = ""


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    """An order left Paid (O3; derived from ``ShipmentShipped`` at handoff)."""

    order_id: str = ""
    shipment_id: str = ""


@dataclass(frozen=True)
class OrderDelivered(DomainEvent):
    """An order reached the customer (O3; derived from ``ShipmentDelivered``; RT1 window anchor)."""

    order_id: str = ""
    at: date | None = None


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    """An order cancelled before shipment (O3; from ``CancelOrder`` -> void + release)."""

    order_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OrderRefunded(DomainEvent):
    """An order fully settled via return (O3/O4 terminal; complete returns only)."""

    order_id: str = ""
    amount: Money | None = None
    reason: str = ""


@dataclass(frozen=True)
class OrderExpired(DomainEvent):
    """An unpaid order lapsed with its reservation (O3: Placed -> Expired)."""

    order_id: str = ""


# -- Inventory -----------------------------------------------------------------
@dataclass(frozen=True)
class StockReserved(DomainEvent):
    """Stock was held for an order (S2 all-or-nothing leg of ``PlaceOrder``)."""

    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockInsufficientEvent(DomainEvent):
    """A reservation failed: not enough available (S2 safety valve; checkout rolls back)."""

    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockReleased(DomainEvent):
    """Held stock returned to available (S3: cancel/expiry/rollback; reason is the audit trail)."""

    product_id: str = ""
    order_id: str = ""
    quantity: int = 0
    reason: str = ""


@dataclass(frozen=True)
class StockCommitted(DomainEvent):
    """A hold became outflow at carrier handoff (S3; from ``HandToCarrier``)."""

    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockRestocked(DomainEvent):
    """Returned goods re-entered available (S4: only after ``PickupConfirmed``; from ``SettleReturn``)."""

    product_id: str = ""
    return_ref: str = ""
    quantity: int = 0


# -- Payment (generic context) ---------------------------------------------------
@dataclass(frozen=True)
class PaymentAuthorized(DomainEvent):
    """A charge was authorized, no money moved yet (from ``PlaceOrder`` via the ACL)."""

    payment_id: str = ""
    amount: Money | None = None


@dataclass(frozen=True)
class PaymentCaptured(DomainEvent):
    """Authorized money was captured at carrier handoff (PM2; from ``HandToCarrier``)."""

    payment_id: str = ""


@dataclass(frozen=True)
class PaymentVoided(DomainEvent):
    """An uncaptured authorization was voided (PM3; cancel-before-shipment)."""

    payment_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PaymentRefunded(DomainEvent):
    """Captured money was refunded, partially or fully (PM1/PM4; from ``SettleReturn`` pro-rata)."""

    payment_id: str = ""
    amount: Money | None = None
    reason: str = ""


# -- Fulfillment -------------------------------------------------------------------
@dataclass(frozen=True)
class ShipmentCreated(DomainEvent):
    """A dispatch was created for a Paid order (SH1; from ``HandToCarrier``)."""

    shipment_id: str = ""
    order_id: str = ""


@dataclass(frozen=True)
class ShipmentShipped(DomainEvent):
    """A parcel reached / moved with the carrier (handoff scan or tracking update)."""

    shipment_id: str = ""
    tracking_number: str = ""


@dataclass(frozen=True)
class ShipmentDelivered(DomainEvent):
    """A carrier confirmed delivery (SH3 terminal; from ``ConfirmDelivery``)."""

    shipment_id: str = ""


@dataclass(frozen=True)
class PickupScheduled(DomainEvent):
    """A doorstep collection was scheduled for an approved return (PK1/PK2)."""

    pickup_id: str = ""
    return_id: str = ""


@dataclass(frozen=True)
class PickupConfirmed(DomainEvent):
    """A courier took possession at the door with photo evidence (PK3/PK5; from ``ConfirmPickup``).

    This is the possession-transfer event gating ``ReturnGoodsReceived``,
    refund, and restock (RT5/S4).
    """

    pickup_id: str = ""
    return_id: str = ""
    evidence_ref: str = ""


# -- Returns ------------------------------------------------------------------------
@dataclass(frozen=True)
class ReturnRequested(DomainEvent):
    """A customer opened an RMA against a Delivered order (RT1/RT2; from ``RequestReturn``)."""

    rma_id: str = ""
    order_id: str = ""


@dataclass(frozen=True)
class ReturnApproved(DomainEvent):
    """Staff (auto-)approved an RMA, opening the pickup leg (from ``RequestReturn``/``ApproveReturn``)."""

    rma_id: str = ""


@dataclass(frozen=True)
class ReturnGoodsReceived(DomainEvent):
    """Collected goods were reconciled (RT5; derived from ``PickupConfirmed``)."""

    rma_id: str = ""


@dataclass(frozen=True)
class ReturnRefunded(DomainEvent):
    """An RMA settled with its pro-rata refund (RT3; from ``SettleReturn``; RT4 terminal)."""

    rma_id: str = ""
    amount: Money | None = None


@dataclass(frozen=True)
class ReturnRejected(DomainEvent):
    """An RMA was rejected before collection (RT4; from ``RejectReturn``)."""

    rma_id: str = ""
    reason: str = ""


# -- Shop / Pricing (return-driven) ---------------------------------------------------
@dataclass(frozen=True)
class CouponUnredeemed(DomainEvent):
    """A customer regained a coupon code (CO7/RT6; only on a complete return via ``SettleReturn``)."""

    coupon_code: str = ""
    customer_id: str = ""


def events_of(*aggregates) -> list[DomainEvent]:
    """Collect (and drain) pending events from any number of aggregates.

    Args:
        *aggregates: Aggregates whose pending events to collect.

    Returns:
        list[DomainEvent]: concatenated pending events, in aggregate order.
    """
    out: list[DomainEvent] = []
    for a in aggregates:
        out.extend(a.pop_events())
    return out
