"""Commands (imperative, user-initiated intents) — the CommandBus entry points.

Bounded-context role:
    The intent language external actors speak: every command below is an
    imperative request from a customer, staff/courier, carrier, or catalog
    admin, dispatched via the ``CommandBus`` (``infra/command_bus.py``) to
    exactly one handler in ``application/command_handlers.py``. Handlers
    load aggregates inside one atomic unit of work and emit past-tense
    ``Event``(s) from ``domain/events.py``.

Lifecycle notes:
    * Happy path: ``OpenCart`` -> ``AddToCart`` x N -> ``ApplyCoupon`` ->
      ``PlaceOrder`` (freezes the cart, reserves stock S2, redeems the
      coupon CO4, authorizes payment) -> ``HandToCarrier`` (commits stock
      S3 + captures payment at handoff) -> ``ConfirmDelivery``.
    * Doorstep-pickup return path: ``RequestReturn`` (customer, against a
      Delivered order within 30 days, RT1/RT2) -> staff ``SchedulePickup``
      -> courier ``ConfirmPickup`` (door scan + mandatory photo, PK3/PK5)
      -> ``SettleReturn`` (RT3 pro-rata goods + tax + shipping; drives
      ``PaymentRefunded`` + ``StockRestocked`` + ``OrderRefunded`` and, on
      a complete return, ``CouponUnredeemed`` RT6).
    * ``CancelOrder`` is pre-shipment only (O3: Placed/Paid -> Cancelled ->
      void + release); shipped/delivered orders settle via the return path.

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
    """Base class for all user-initiated intents (imperative, immutable).

    Role:
        Marker + dispatch helper: ``command_name`` (the class name, e.g.
        'PlaceOrder') routes the command to exactly one handler on the
        ``CommandBus``. Subclasses carry the validated intent payload.

    Members: none (subclasses carry the intent fields).
    """

    @property
    def command_name(self) -> str:
        """Return the dispatch key (the concrete class name).

        Returns:
            str: e.g. 'PlaceOrder'.
        """
        return type(self).__name__


# -- Catalog (admin) -----------------------------------------------------------
@dataclass(frozen=True)
class PublishProduct(Command):
    """Catalog-admin intent: publish a sellable product (Catalog context).

    Members:
        sku: Unique immutable product code (P1).
        name: Display name.
        price: Sell price snapshot (Money, P2 >= 0).
        description: Catalog copy.
        weight_grams: Physical weight (shipping input).

    Invariants: P1 (SKU unique/immutable), P2 (price >= 0).
    Events: ``ProductPublished``.
    """

    sku: str = ""
    name: str = ""
    price: Money | None = None
    description: str = ""
    weight_grams: int = 0


# -- Shop (customer) -----------------------------------------------------------
@dataclass(frozen=True)
class OpenCart(Command):
    """Customer intent: open a cart (Shop context).

    Members:
        customer_ref: Opaque customer reference (cart owner).

    Events: ``CartCreated``.
    """

    customer_ref: str = ""


@dataclass(frozen=True)
class AddToCart(Command):
    """Customer intent: add a product to an open cart (C1).

    Members:
        cart_id: Target cart (must be Open, C4).
        product_id: Product to add (must be Active).
        quantity: Units to add (>= 1, C1; same product merges, one line).

    Events: ``CartItemAdded`` (new line) / ``CartItemChanged`` (merged).
    """

    cart_id: str = ""
    product_id: str = ""
    quantity: int = 1


@dataclass(frozen=True)
class UpdateCartLine(Command):
    """Customer intent: set a cart line's quantity (C1).

    Members:
        cart_id: Target cart (must be Open, C4).
        product_id: Line to change.
        quantity: New quantity (>= 1, C1).

    Events: ``CartItemChanged``.
    """

    cart_id: str = ""
    product_id: str = ""
    quantity: int = 1


@dataclass(frozen=True)
class RemoveCartLine(Command):
    """Customer intent: drop a line from an open cart.

    Members:
        cart_id: Target cart (must be Open, C4).
        product_id: Line to remove.

    Events: ``CartLineRemoved``.
    """

    cart_id: str = ""
    product_id: str = ""


@dataclass(frozen=True)
class ApplyCoupon(Command):
    """Customer intent: preview a single coupon on the cart (C3/CO4).

    At most one coupon per cart (applying a new one replaces the old); a
    coupon that doesn't validate is rejected, not ignored (C3). The cart
    application is a preview re-validated and committed at ``PlaceOrder``
    (CO4), because another session could race.

    Members:
        cart_id: Target cart (must be Open, C4).
        coupon_code: Code to validate (CO2/CO3 window + per-customer limit).

    Events: ``CouponApplied`` (+ ``CouponRemoved`` when replacing).
    """

    cart_id: str = ""
    coupon_code: str = ""


@dataclass(frozen=True)
class RemoveCoupon(Command):
    """Customer intent: detach the coupon preview from the cart (C3).

    Members:
        cart_id: Target cart (must be Open, C4).

    Events: ``CouponRemoved``.
    """

    cart_id: str = ""


@dataclass(frozen=True)
class PlaceOrder(Command):
    """Customer intent: check out the cart into a frozen order and authorize (O1/O2/S2/CO4).

    Customer checkout: places the order from the cart (destination may ride
    on the command or already be set on the cart). Freezes lines/discount/
    shipping/total (O2), commits the coupon redemption (CO4), reserves
    every line all-or-nothing (S2), and authorizes the generic Payment
    (capture waits for ``HandToCarrier``).

    Members:
        cart_id: Checked-out cart source (C4: checkout runs once).
        instrument_ref: Opaque payment instrument (Payment ACL input).
        reservation_ttl_days: Stock hold TTL (S3 expiry sweep).
        destination: Delivery address if not already set on the cart.

    Events: ``CartCheckedOut`` + ``OrderPlaced`` + ``OrderPaymentAuthorized``
        + ``OrderPaid`` + ``PaymentAuthorized`` + ``StockReserved``.
    """

    cart_id: str = ""
    instrument_ref: str = ""
    reservation_ttl_days: int = 30
    destination: Address | None = None


@dataclass(frozen=True)
class CancelOrder(Command):
    """Customer/staff intent: cancel before shipment (O3 pre-shipment only).

    Placed -> Cancelled (voids nothing) or Paid -> Cancelled (voids the
    authorization, PM3) with stock released (S3) and the coupon un-redeemed.
    Forbidden from Shipped/Delivered — those settle via the return path.

    Members:
        order_id: Placed/Paid order to cancel.
        reason: Cancel reason (void + release audit trail).

    Events: ``OrderCancelled`` + ``StockReleased`` + ``PaymentVoided``
        (+ ``CouponUnredeemed`` when a coupon was used).
    """

    order_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CloseCart(Command):
    """Customer intent: abandon the cart without buying.

    Members:
        cart_id: Open cart to close.

    Events: ``CartAbandoned``.
    """

    cart_id: str = ""


# -- Fulfillment (warehouse staff / carrier / courier) -------------------------
@dataclass(frozen=True)
class HandToCarrier(Command):
    """Warehouse-staff intent: hand a Paid order's parcel to the carrier (capture-at-handoff).

    Builds the Shipment (SH1), moves Order Paid -> Shipped (O4), sets the
    immutable tracking number (SH2), commits reservations (S3), and captures
    the authorized payment (PM2: money moves now).

    Members:
        order_id: Paid order to ship (O4: must be Paid).
        carrier: Carrier name (single carrier in v1).
        tracking_number: Carrier tracking number (SH2: immutable once set).

    Events: ``ShipmentCreated`` + ``ShipmentShipped`` + ``OrderShipped`` +
        ``StockCommitted`` + ``PaymentCaptured``.
    """

    order_id: str = ""
    carrier: str = ""
    tracking_number: str = ""


@dataclass(frozen=True)
class ConfirmDelivery(Command):
    """Carrier-scan intent: confirm delivery (SH3 terminal, O3).

    Moves Shipment -> Delivered and derives Order Shipped -> Delivered,
    anchoring the RT1 30-day return window.

    Members:
        shipment_id: HandedToCarrier/InTransit shipment (carrier-confirmed).
        order_id: Shipped order matching the shipment.
        at: Delivery date (defaults to today in the handler).

    Events: ``ShipmentDelivered`` + ``OrderDelivered``.
    """

    shipment_id: str = ""
    order_id: str = ""
    at: date | None = None


@dataclass(frozen=True)
class SchedulePickup(Command):
    """Staff/ops intent: schedule the doorstep collection for an approved RMA (PK1/PK2).

    Members:
        rma_id: Approved RMA needing collection (exactly one pickup per PK1).
        slot: Courier visit window (PK2: must be future-dated).

    Events: ``PickupScheduled``.
    """

    rma_id: str = ""
    slot: datetime | None = None


@dataclass(frozen=True)
class ConfirmPickup(Command):
    """Courier intent (at the door): confirm possession with evidence (PK3/PK5 -> RT5).

    The on-site scan + mandatory photo (optional signature) transfers
    possession; only this gates ``ReturnGoodsReceived``, refund, and restock
    (RT5/S4). No-access instead re-schedules (PK4, up to 2 attempts).

    Members:
        pickup_id: EnRoute pickup being confirmed.
        evidence: Collection proof (photo_ref required, PK5).

    Events: ``PickupConfirmed`` + ``ReturnGoodsReceived``.
    """

    pickup_id: str = ""
    evidence: PickupEvidence | None = None


# -- Returns (customer opens, staff settles) -----------------------------------
@dataclass(frozen=True)
class RequestReturn(Command):
    """Customer intent: open an RMA against a Delivered order (RT1/RT2).

    Customer-initiated only; staff approve/reject afterwards. Validated
    against the 30-day window (RT1) and shipped-quantity bounds (RT2); the
    customer does not ship back — the platform's courier collects at the
    door via ``SchedulePickup``.

    Members:
        order_id: Delivered order under return.
        lines: (product_id, quantity) pairs as a tuple (frozen/hashable),
            each qty <= shipped qty (RT2).
        reason: Customer's return reason.

    Events: ``ReturnRequested`` + ``ReturnApproved``.
    """

    order_id: str = ""
    # tuple of (product_id, quantity) — tuple (not list) to stay frozen/hashable
    lines: tuple[tuple[str, int], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RejectReturn(Command):
    """Staff intent: reject an RMA before goods are collected (RT4).

    Members:
        rma_id: Requested/Approved/PickupScheduled RMA (never after pickup).
        reason: Staff rejection reason.

    Events: ``ReturnRejected``.
    """

    rma_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SettleReturn(Command):
    """Ops/staff intent: settle a collected return with the pro-rata refund (RT3).

    Requires the goods to be back (GoodsReceived, RT5). Computes
    goods + tax + shipping scaled to the returned share (full shipping on a
    full return), then drives ``PaymentRefunded`` + ``StockRestocked`` +
    ``OrderRefunded`` (iff complete) + ``CouponUnredeemed`` (iff complete
    with a coupon, RT6).

    Members:
        rma_id: GoodsReceived RMA to settle.

    Events: ``ReturnRefunded`` (+ ``PaymentRefunded`` + ``StockRestocked`` +
        ``OrderRefunded`` + ``CouponUnredeemed`` on a full return).
    """

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
