"""Domain events (past tense, immutable). One class per event in the model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .value_objects import Money


@dataclass(frozen=True)
class DomainEvent:
    """Base class. `name` is the class name (e.g. 'OrderPlaced')."""

    @property
    def name(self) -> str:
        return type(self).__name__


# -- Catalog -----------------------------------------------------------------
@dataclass(frozen=True)
class ProductPublished(DomainEvent):
    product_id: str = ""


@dataclass(frozen=True)
class ProductPriceChanged(DomainEvent):
    product_id: str = ""
    new_price: Money | None = None


# -- Shop (Cart + Coupon) ------------------------------------------------------
@dataclass(frozen=True)
class CartCreated(DomainEvent):
    cart_id: str = ""


@dataclass(frozen=True)
class CartItemAdded(DomainEvent):
    cart_id: str = ""
    product_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class CartItemChanged(DomainEvent):
    cart_id: str = ""
    product_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class CartLineRemoved(DomainEvent):
    cart_id: str = ""
    product_id: str = ""


@dataclass(frozen=True)
class CouponApplied(DomainEvent):
    cart_id: str = ""
    coupon_code: str = ""


@dataclass(frozen=True)
class CouponRemoved(DomainEvent):
    cart_id: str = ""


@dataclass(frozen=True)
class CartCheckedOut(DomainEvent):
    cart_id: str = ""


@dataclass(frozen=True)
class CartAbandoned(DomainEvent):
    cart_id: str = ""


# -- Order Management ----------------------------------------------------------
@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str = ""
    money_total: Money | None = None


@dataclass(frozen=True)
class OrderPaymentAuthorized(DomainEvent):
    order_id: str = ""


@dataclass(frozen=True)
class OrderPaid(DomainEvent):
    order_id: str = ""


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: str = ""
    shipment_id: str = ""


@dataclass(frozen=True)
class OrderDelivered(DomainEvent):
    order_id: str = ""
    at: date | None = None


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    order_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class OrderRefunded(DomainEvent):
    order_id: str = ""
    amount: Money | None = None
    reason: str = ""


@dataclass(frozen=True)
class OrderExpired(DomainEvent):
    order_id: str = ""


# -- Inventory -----------------------------------------------------------------
@dataclass(frozen=True)
class StockReserved(DomainEvent):
    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockInsufficientEvent(DomainEvent):
    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockReleased(DomainEvent):
    product_id: str = ""
    order_id: str = ""
    quantity: int = 0
    reason: str = ""


@dataclass(frozen=True)
class StockCommitted(DomainEvent):
    product_id: str = ""
    order_id: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class StockRestocked(DomainEvent):
    product_id: str = ""
    return_ref: str = ""
    quantity: int = 0


# -- Payment (generic context) ---------------------------------------------------
@dataclass(frozen=True)
class PaymentAuthorized(DomainEvent):
    payment_id: str = ""
    amount: Money | None = None


@dataclass(frozen=True)
class PaymentCaptured(DomainEvent):
    payment_id: str = ""


@dataclass(frozen=True)
class PaymentVoided(DomainEvent):
    payment_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PaymentRefunded(DomainEvent):
    payment_id: str = ""
    amount: Money | None = None
    reason: str = ""


# -- Fulfillment -------------------------------------------------------------------
@dataclass(frozen=True)
class ShipmentCreated(DomainEvent):
    shipment_id: str = ""
    order_id: str = ""


@dataclass(frozen=True)
class ShipmentShipped(DomainEvent):
    shipment_id: str = ""
    tracking_number: str = ""


@dataclass(frozen=True)
class ShipmentDelivered(DomainEvent):
    shipment_id: str = ""


@dataclass(frozen=True)
class PickupScheduled(DomainEvent):
    pickup_id: str = ""
    return_id: str = ""


@dataclass(frozen=True)
class PickupConfirmed(DomainEvent):
    pickup_id: str = ""
    return_id: str = ""
    evidence_ref: str = ""


# -- Returns ------------------------------------------------------------------------
@dataclass(frozen=True)
class ReturnRequested(DomainEvent):
    rma_id: str = ""
    order_id: str = ""


@dataclass(frozen=True)
class ReturnApproved(DomainEvent):
    rma_id: str = ""


@dataclass(frozen=True)
class ReturnGoodsReceived(DomainEvent):
    rma_id: str = ""


@dataclass(frozen=True)
class ReturnRefunded(DomainEvent):
    rma_id: str = ""
    amount: Money | None = None


@dataclass(frozen=True)
class ReturnRejected(DomainEvent):
    rma_id: str = ""
    reason: str = ""


# -- Shop / Pricing (return-driven) ---------------------------------------------------
@dataclass(frozen=True)
class CouponUnredeemed(DomainEvent):
    coupon_code: str = ""
    customer_id: str = ""


def events_of(*aggregates) -> list[DomainEvent]:
    """Collect (and drain) pending events from any number of aggregates."""
    out: list[DomainEvent] = []
    for a in aggregates:
        out.extend(a.pop_events())
    return out
