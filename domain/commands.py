"""Commands (imperative, user-initiated) — the Command pattern entry points.

Design rule (§5 of domain-model.md):
- A **Command** is an intent from an external actor (customer, staff/courier,
  catalog admin). Imperative name: ``AddToCart``, ``PlaceOrder``,
  ``RequestReturn``. Commands are validated and handled exactly once; they
  either succeed (emitting past-tense **Events**) or are rejected.
- A **Domain Event** is a fact about what already happened (``CartItemAdded``,
  ``OrderPlaced``). System reactions (capture at shipment, restock after
  pickup, coupon un-redeem on full refund, derived ``OrderShipped`` /
  ``OrderDelivered`` / ``ReturnGoodsReceived``) stay purely event-driven —
  they are NOT commands because no external actor initiates them.

So this module models ONLY the user-initiated rows of the §5 table:

| Command (here)        | Actor            | Event(s) emitted                          |
|-----------------------|------------------|-------------------------------------------|
| ``PublishProduct``    | catalog admin    | ``ProductPublished``                      |
| ``OpenCart``          | customer         | ``CartCreated``                           |
| ``AddToCart``         | customer         | ``CartItemAdded`` / ``CartItemChanged``   |
| ``UpdateCartLine``    | customer         | ``CartItemChanged``                       |
| ``RemoveCartLine``    | customer         | ``CartLineRemoved``                       |
| ``ApplyCoupon``       | customer         | ``CouponApplied`` (+ ``CouponRemoved`` on replace) |
| ``RemoveCoupon``      | customer         | ``CouponRemoved``                         |
| ``PlaceOrder``        | customer         | ``CartCheckedOut`` + ``OrderPlaced`` + ``OrderPaymentAuthorized`` + ``OrderPaid`` + ``PaymentAuthorized`` + ``StockReserved`` |
| ``CancelOrder``       | customer / staff | ``OrderCancelled`` + ``StockReleased`` + ``PaymentVoided`` |
| ``CloseCart``         | customer         | ``CartAbandoned``                         |
| ``HandToCarrier``     | warehouse staff  | ``ShipmentCreated`` + ``ShipmentShipped`` + ``OrderShipped`` + ``StockCommitted`` + ``PaymentCaptured`` |
| ``ConfirmDelivery``   | carrier scan     | ``ShipmentDelivered`` + ``OrderDelivered``|
| ``RequestReturn``     | customer         | ``ReturnRequested`` + ``ReturnApproved``  |
| ``RejectReturn``      | staff            | ``ReturnRejected``                        |
| ``SchedulePickup``    | staff / ops      | ``PickupScheduled``                       |
| ``ConfirmPickup``     | courier (door)   | ``PickupConfirmed`` + ``ReturnGoodsReceived`` |
| ``SettleReturn``      | ops / staff      | ``ReturnRefunded`` (+ ``PaymentRefunded`` + ``StockRestocked`` + ``OrderRefunded`` + ``CouponUnredeemed`` on full return) |

Internal follow-ons (``ReserveStock``, ``AuthorizePayment``, ``CapturePayment``,
``RefundPayment``, ``UnredeemCoupon``, ``ExpireOrder``) are NOT commands here:
handlers invoke that behavior directly inside the same unit of work and the
result surfaces as events.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .value_objects import Address, Money, PickupEvidence


@dataclass(frozen=True)
class Command:
    """Base class. ``command_name`` is the class name (e.g. 'PlaceOrder')."""

    @property
    def command_name(self) -> str:
        return type(self).__name__


# -- Catalog (admin) -----------------------------------------------------------
@dataclass(frozen=True)
class PublishProduct(Command):
    sku: str = ""
    name: str = ""
    price: Money | None = None
    description: str = ""
    weight_grams: int = 0


# -- Shop (customer) -----------------------------------------------------------
@dataclass(frozen=True)
class OpenCart(Command):
    customer_ref: str = ""


@dataclass(frozen=True)
class AddToCart(Command):
    cart_id: str = ""
    product_id: str = ""
    quantity: int = 1


@dataclass(frozen=True)
class UpdateCartLine(Command):
    cart_id: str = ""
    product_id: str = ""
    quantity: int = 1


@dataclass(frozen=True)
class RemoveCartLine(Command):
    cart_id: str = ""
    product_id: str = ""


@dataclass(frozen=True)
class ApplyCoupon(Command):
    cart_id: str = ""
    coupon_code: str = ""


@dataclass(frozen=True)
class RemoveCoupon(Command):
    cart_id: str = ""


@dataclass(frozen=True)
class PlaceOrder(Command):
    """Customer checkout: places the order from the cart (destination may ride
    on the command or already be set on the cart)."""

    cart_id: str = ""
    instrument_ref: str = ""
    reservation_ttl_days: int = 30
    destination: Address | None = None


@dataclass(frozen=True)
class CancelOrder(Command):
    order_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CloseCart(Command):
    cart_id: str = ""


# -- Fulfillment (warehouse staff / carrier / courier) -------------------------
@dataclass(frozen=True)
class HandToCarrier(Command):
    order_id: str = ""
    carrier: str = ""
    tracking_number: str = ""


@dataclass(frozen=True)
class ConfirmDelivery(Command):
    shipment_id: str = ""
    order_id: str = ""
    at: date | None = None


@dataclass(frozen=True)
class SchedulePickup(Command):
    rma_id: str = ""
    slot: datetime | None = None


@dataclass(frozen=True)
class ConfirmPickup(Command):
    pickup_id: str = ""
    evidence: PickupEvidence | None = None


# -- Returns (customer opens, staff settles) -----------------------------------
@dataclass(frozen=True)
class RequestReturn(Command):
    order_id: str = ""
    # tuple of (product_id, quantity) — tuple (not list) to stay frozen/hashable
    lines: tuple[tuple[str, int], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RejectReturn(Command):
    rma_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SettleReturn(Command):
    rma_id: str = ""


__all__ = [
    "Command",
    "PublishProduct",
    "OpenCart",
    "AddToCart",
    "UpdateCartLine",
    "RemoveCartLine",
    "ApplyCoupon",
    "RemoveCoupon",
    "PlaceOrder",
    "CancelOrder",
    "CloseCart",
    "HandToCarrier",
    "ConfirmDelivery",
    "SchedulePickup",
    "ConfirmPickup",
    "RequestReturn",
    "RejectReturn",
    "SettleReturn",
]
