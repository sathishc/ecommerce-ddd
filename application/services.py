"""Application services: thin orchestration over the domain, inside a UoW.

Responsibility
--------------
Each service class is the anti-corruption layer (ACL) for one lifecycle
transition that necessarily spans several bounded contexts (context map,
``domain-model.md`` §2):

- ``CheckoutAppService``: Shop (Cart + Coupon + pricing quote) ->
  Order Management (Order) + Inventory (Stock reservations) + Payment
  (authorize) + Shop/Coupon (redeem).
- ``ShippingAppService``: Order Management -> Fulfillment (Shipment
  handoff) + Inventory (commit reservations) + Payment (capture).
- ``CancelAppService``: Order Management + Payment (void — never refund,
  because nothing was captured yet) + Inventory (release) + Shop
  (coupon un-redeem).
- ``ReturnAppService``: Returns (RMA) -> Fulfillment (doorstep Pickup)
  + Payment (pro-rata refund) + Inventory (restock) + Shop (coupon
  un-redeem on full returns) + Order Management (Refunded / accept).

UoW atomicity contract
----------------------
Every public method opens exactly one ``with self._uow() as uow:`` block.
All repository reads/writes go through ``uow.<repos>``; all cross-context
side effects after commit go through the EventBus outbox (populated by
``uow.collect(...)``, published on ``__exit__``) plus the optional
``AsyncJob`` enqueue (``order.placed`` — consumed outside the UoW, so it
fires only after the ``with`` block exits without error). Any
``DomainError`` (or any exception) triggers snapshot-rollback and outbox
discard: no dangling reservations, redemptions, payments, or events.

Context-map crossing
--------------------
Services translate between context languages and never let one context's
model leak into another: Payment receives only ``Money`` + an opaque
instrument ref (it never learns "Order"); Inventory receives only
``order_id``/qty/expiry tuples; Coupon sees only customer/order IDs.
Order Management stays upstream; Inventory conforms but keeps its
``StockInsufficient``/``StockReleased``/``StockCommitted`` safety valve.

Command -> events flow
----------------------
Callers invoke a service method directly with loaded aggregates (the
ID-based ``CommandHandlers.place_order`` etc. in
``application/command_handlers.py`` is the dispatchable twin with
identical steps). Inside the UoW the flow is always:
load (args are pre-loaded here) -> validate (guard clauses + domain
``place``/``reserve``/``redeem`` checks) -> mutate (link reservations,
pay/void/capture/refund, pickup transitions) -> persist
(``uow.<repos>.add``) -> collect (``uow.collect`` drains aggregate events
into the outbox for post-commit ``EventBus`` publication).

SOLID wiring: services take a ``UoWFactory`` + ``Clock`` (+ optional
observability Blocks like ``tracer``/``jobs``/``files``), never
concretes — tests inject in-memory fakes, production injects AWS-backed
Blocks with no application-code change.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.cart import Cart
from domain.coupon import Coupon
from domain.errors import DomainError
from domain.order import Order
from domain.payment import Payment
from domain.pickup import Pickup
from domain.return_rma import Return, _OrderFacts
from domain.shipment import Shipment
from domain.value_objects import Address, Money, PickupEvidence, RefundBreakdown
from infra.blocks import AsyncJob, FileBucket
from infra.clock import Clock, SystemClock
from infra.unit_of_work import UoWFactory

TAX_NUM, TAX_DEN = 0, 100  # v1: no tax (pluggable, mirrored into _OrderFacts)
AUTO_COURIER = "courier-default"  # v1: single-carrier default (domain-model.md §6)


def _future(days: int, clock: Clock) -> datetime:
    """Compute a reservation/pickup expiry timestamp relative to the Clock.

    Uses the injected ``Clock`` (not wall time) so tests with ``FixedClock``
    stay deterministic.
    """
    return clock.now() + timedelta(days=days)


class CheckoutAppService:
    """Shop -> Order + Inventory + Payment + Coupon (one atomic unit of work).

    Contexts bridged: Shop (Cart snapshot/quote/coupon preview) supplies the
    upstream intent; Order Management (``Order.place``/``pay``) freezes the
    totals; Inventory (``Stock.reserve`` per line) guarantees the
    all-or-nothing anti-oversell spine (S2); Payment (``Payment`` authorize)
    charges generically via Money + instrument ref; Coupon (``redeem``)
    commits the single-coupon redemption atomically here (cart apply was only
    a preview — CO4 requires re-validation/redeem at placement).

    Atomicity: the whole ``place_and_pay`` body after tracing runs in one
    UoW. Success commits Order + Payment + Cart + Coupon + Stock changes and
    publishes their events; any ``DomainError`` first runs *compensating
    domain actions* (release already-taken reservations, un-redeem the
    coupon) and then re-raises so the UoW snapshot-rollback discards the
    rest. Rollback behavior: reservations taken before the failure are
    explicitly released with reason ``"checkout rollback"`` (so Inventory
    emits ``StockReleased`` instead of silently vanishing), and a coupon
    redeemed earlier in the same attempt is un-redeemed (CO7 mirror) —
    both compensations happen *inside* the failing UoW so they too are
    rolled back consistently if anything else fails.

    Events (via ``uow.collect`` -> outbox -> EventBus on commit):
    ``CartCheckedOut``, ``OrderPlaced``, ``StockReserved`` (per line),
    ``CouponRedeemed`` (if coupon), ``OrderPaymentAuthorized``/``OrderPaid``.
    """

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        jobs: AsyncJob | None = None,
    ) -> None:
        # DIP: depend on the UoW factory + Clock abstractions, never concretes.
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer  # optional X-Ray-style span emitter (observability only)
        self._jobs = jobs  # optional SQS-style queue for post-commit follow-ons

    def place_and_pay(
        self, cart: Cart, instrument_ref: str, reservation_ttl_days: int = 30
    ) -> tuple[Order, Payment]:
        """Authorize at placement; capture happens later, at shipment.

        Steps (load -> validate -> mutate -> persist -> collect):
          1. Validate cart is ``Open`` and has a destination (C4 guards —
             fail fast *before* opening the UoW so no transaction is spent).
          2. Load/mutate inside one UoW: ``cart.checkout`` (freezes the
             Shop quote: lines - discount + tax + shipping), ``Order.place``
             (freezes totals O2), ``coupon.redeem`` + ``stock.reserve`` per
             line + ``order.link_reservation``.
          3. ``Payment(order.money_total, instrument_ref)`` + ``order.pay``
             (authorize-only; Payment stays ``Authorized`` until shipment).
          4. Persist every touched aggregate via ``uow.<repos>.add`` and
             drain events via ``uow.collect`` (outbox published on commit).
          5. After the ``with`` block commits, enqueue the ``order.placed``
             async job (outside the UoW so a queue failure can't corrupt
             the atomic commit).

        Why each step exists:
          - Coupon redeem *at placement* (not at cart apply): cart apply is
            a preview; CO4 mandates the commit happen atomically with the
            order so two racing checkouts cannot double-spend one code.
          - Per-line ``stock.reserve`` with a TTL: S2 all-or-nothing — one
            short line must fail the whole checkout (no partial orders).
          - ``Payment`` authorize (not capture): domain-model.md §6.3 —
            money moves only when the carrier takes the parcel.
          - Rollback compensations (release + unredeem): keep Inventory and
            Coupon consistent when a *later* line fails after earlier lines
            already reserved; the UoW snapshot then discards everything.

        Args:
            cart: Loaded Shop ``Cart`` (caller-owned; mutated to CheckedOut).
            instrument_ref: Opaque Payment instrument reference (ACL: Payment
                never sees order/cart internals).
            reservation_ttl_days: Reservation expiry horizon from now.

        Returns:
            ``(order, payment)`` — the frozen Order and the Authorized Payment.

        Raises:
            DomainError: Cart not Open / missing destination (C4); coupon
                invalid at placement (CO2-CO4); any line short on stock
                (S2 ``StockInsufficient``); missing stock record; payment
                invariant violation. All raise *before* commit, so the UoW
                rolls back.

        Events: ``CartCheckedOut``, ``OrderPlaced``, ``StockReserved`` × N,
            ``CouponRedeemed`` (if coupon), ``OrderPaymentAuthorized``,
            ``OrderPaid`` — all published post-commit via the outbox.
        """
        # -- validate (pre-UoW fast fail: no transaction spent on bad input)
        if cart.status != "Open":
            raise DomainError("C4: a cart can only be checked out once")
        if cart.destination is None:
            raise DomainError("C4: a checkout needs a destination address")
        if self._tracer is not None:
            # Observability only — never affects domain state or atomicity.
            self._tracer.trace("checkout.place_and_pay", customer=cart.customer_ref)
        # -- mutate inside one atomic UoW ----------------------------------
        with self._uow() as uow:
            # 1. Freeze the Shop view: snapshot lines + quote (subtotal,
            #    discount, tax, shipping) and flip cart to CheckedOut (C4:
            #    cart becomes immutable from here on).
            snapshot = cart.checkout(cart.destination)
            quote = snapshot["quote"]
            # 2. Freeze the Order view (O2: money_total fixed at placement;
            #    later catalog price changes cannot alter this order).
            order = Order()
            order.place(
                lines=[(pid, qty, unit) for pid, qty, unit in snapshot["lines"]],
                destination=cart.destination,
                customer_ref=cart.customer_ref,
                discount=quote.discount.amount,
                shipping_fee=quote.shipping_fee,
                tax=quote.tax,
                coupon_code=snapshot["coupon"].code if snapshot["coupon"] else None,
            )
            coupon = snapshot["coupon"]
            try:
                # 3a. Commit the coupon redemption NOW (CO4: the atomic
                #     commit point; cart-level apply was only a preview).
                if coupon is not None:
                    coupon.redeem(cart.customer_ref, order.order_id)
                # 3b. Reserve every line (S2 all-or-nothing) and link the
                #     reservation handles back onto the order.
                for pid, qty, _ in snapshot["lines"]:
                    stock = self._lookup_stock(uow, pid)
                    order.link_reservation(
                        pid,
                        stock.reserve(
                            order.order_id, qty, _future(reservation_ttl_days, self._clock)
                        ),
                    )
            except DomainError:
                # -- error path: compensate in-UoW, then re-raise for rollback.
                # Release any reservations taken before the failure so
                # Inventory stays consistent (emits StockReleased per line).
                for pid, rid in order.reservation_ids.items():
                    self._lookup_stock(uow, pid).release(rid, "checkout rollback")
                # Mirror the earlier redeem so the customer is not burned by
                # a checkout that never completed (CO7 unredeem semantics).
                if coupon is not None:
                    coupon.unredeem(cart.customer_ref, order.order_id)
                raise  # UoW __exit__ sees the exception -> snapshot rollback.
            # 4. Order -> Payment ACL: Payment sees Money + opaque ref, never
            #    "orders" (downstream-via-events relationship). Authorize
            #    only — capture waits for HandToCarrier.
            payment = Payment(order.money_total, instrument_ref)
            order.pay(payment.payment_id)
            # -- persist: register every mutated aggregate with its repo ----
            uow.carts.add(cart)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            # -- collect: drain pending domain events into the commit outbox
            # (stocks via list() so every touched Stock's StockReserved is
            # published; published to EventBus only on successful __exit__).
            uow.collect(cart, order, payment, *uow.stocks.list(),
                        *([coupon] if coupon else []))
            # Post-commit fan-out stays INSIDE the `with` only as an enqueue
            # call on the AsyncJob block; the job itself runs after commit.
            if self._jobs is not None:
                self._jobs.enqueue("order.placed", {"order_id": order.order_id})
        return order, payment

    @staticmethod
    def _lookup_stock(uow, product_id: str):
        """Fetch the Inventory ``Stock`` for a product or raise DomainError.

        Translates a ``KeyError`` (repository miss) into a ``DomainError``
        so the checkout error path (rollback + compensation above) treats a
        missing stock record like any other reservation failure.
        """
        try:
            return uow.stocks.get(product_id)
        except KeyError:
            raise DomainError(f"no stock record for {product_id}") from None


class ShippingAppService:
    """Order -> Fulfillment + Payment: hand to carrier, then capture.

    Contexts bridged: Order Management (``Order.confirm_shipped`` guards
    O3/O4: only ``Paid`` may ship, exactly once); Fulfillment
    (``Shipment`` creation + ``hand_to_carrier`` sets immutable carrier +
    tracking); Inventory (``Stock.commit`` converts each reservation into a
    real decrement — ``StockCommitted``); Payment (``capture`` moves money
    only at handoff per §6.3).

    Atomicity: one UoW per method. Shipment build + order transition +
    stock commits + payment capture all commit together; any failure (e.g.
    double-ship, capture from non-Authorized) rolls everything back so the
    order is never marked shipped without the money captured (and vice
    versa). No compensation block is needed here because all mutations are
    idempotent-safe domain transitions that the snapshot rollback discards
    wholesale — unlike checkout there is no partial multi-line loop to undo.

    Events: ``ShipmentCreated``/``ShipmentShipped``/``OrderShipped``,
    ``StockCommitted`` × N, ``PaymentCaptured`` (ship); ``ShipmentDelivered``
    + ``OrderDelivered`` (mark_delivered).
    """

    def __init__(self, uow_factory: UoWFactory, tracer=None) -> None:
        self._uow = uow_factory
        self._tracer = tracer

    def ship(self, order: Order, carrier: str, tracking_number: str) -> Shipment:
        """Hand a Paid order to the carrier and capture payment (atomic).

        Steps: build ``Shipment`` from order lines -> ``order.confirm_shipped``
        (O3 guard) -> ``shipment.hand_to_carrier`` (SH1/SH2) ->
        ``stock.commit`` per reservation -> ``payment.capture`` -> persist +
        collect.

        Why this order: the order/shipment transitions validate first (cheap
        domain checks); stock commits and payment capture run only once the
        handoff is known-good, so a capture can never precede a real
        carrier handoff (authorize-then-capture §6.3). Committing stock
        (not releasing) is correct here because the goods physically left.

        Args:
            order: Loaded ``Paid`` Order.
            carrier: Carrier name for the handoff.
            tracking_number: Immutable carrier tracking number (SH2).

        Returns:
            The handed-to-carrier ``Shipment``.

        Raises:
            DomainError: Order not ``Paid`` / already shipped (O3/O4);
                shipment invariant violation; payment not Authorized (PM2).

        Events: ``ShipmentCreated``, ``ShipmentShipped``, ``OrderShipped``,
            ``StockCommitted`` × N, ``PaymentCaptured`` (post-commit).
        """
        if self._tracer is not None:
            self._tracer.trace("shipping.ship", order=order.order_id)
        with self._uow() as uow:
            # 1. Build the Fulfillment view from the Order (SH1: exactly the
            #    order's lines — v1 has no split shipments).
            shipment = Shipment(
                order.order_id,
                [(line.product_id, line.quantity) for line in order.lines],
                order.destination,
            )
            # 2. Mutate: Paid -> Shipped (O3/O4 guard lives in the domain),
            #    then record the carrier handoff (tracking immutable, SH2).
            order.confirm_shipped(shipment.shipment_id)
            shipment.hand_to_carrier(carrier, tracking_number)
            # 3. Convert each soft reservation into a hard decrement. Must
            #    precede capture so failed commits never leave money moved.
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).commit(rid)
            # 4. Capture-at-handoff (§6.3): Authorized -> Captured. Loaded by
            #    the order's opaque payment ref (Payment stays generic).
            payment = uow.payments.get(order.payment_ref)
            payment.capture()
            # -- persist + collect (outbox published on commit) -------------
            uow.orders.add(order)
            uow.shipments.add(shipment)
            uow.payments.add(payment)
            uow.collect(order, shipment, *uow.stocks.list(), payment)
        return shipment

    def mark_delivered(self, shipment: Shipment, order: Order, at=None) -> None:
        """Record carrier delivery confirmation (atomic).

        Steps: ``shipment.confirm_delivered`` (SH3 terminal) ->
        ``order.deliver`` (O3: Shipped -> Delivered) -> persist + collect.

        Why: delivery is the gate for the entire Returns flow (RT1: only
        ``Delivered`` orders may open an RMA), so both aggregates must flip
        together — a shipment marked delivered without the order following
        would strand the customer with no return rights (or vice versa).

        Args:
            shipment: Loaded in-transit ``Shipment``.
            order: Loaded ``Shipped`` Order for that shipment.
            at: Delivery date; defaults to today (carrier confirmation date).

        Raises:
            DomainError: Shipment already terminal / order not Shipped.

        Events: ``ShipmentDelivered``, ``OrderDelivered`` (post-commit).
        """
        from datetime import date as _date  # local import: only needed here

        with self._uow() as uow:
            # Mutate both sides of the partnership together.
            shipment.confirm_delivered()
            order.deliver(at or _date.today())
            # Persist + drain events into the outbox (commit publishes).
            uow.shipments.add(shipment)
            uow.orders.add(order)
            uow.collect(shipment, order)


class CancelAppService:
    """Cancel before shipment: void auth, release stock, un-redeem coupon.

    Contexts bridged: Order Management (``Order.cancel`` — only pre-shipment
    per O3); Payment (``void`` — correct because capture never happened, so
    *void* not *refund*: no money moved); Inventory (``Stock.release`` per
    reservation — ``StockReleased``); Shop/Coupon (``unredeem`` restores the
    customer's single redemption).

    Atomicity: one UoW. Order + payment + all stock releases + coupon
    un-redeem commit together; any failure (e.g. order already shipped,
    payment already captured) rolls back so a half-cancel (order cancelled
    but money still authorized, or stock still held) is impossible.
    This is *compensation* in the business sense (undoing checkout effects)
    but needs no try/except because each step is a plain domain transition
    discarded wholesale by snapshot rollback on error.

    Events: ``OrderCancelled``, ``PaymentVoided``, ``StockReleased`` × N,
    ``CouponUnredeemed`` (if a coupon was used).
    """

    def __init__(self, uow_factory: UoWFactory, tracer=None) -> None:
        self._uow = uow_factory
        self._tracer = tracer

    def cancel(
        self,
        order: Order,
        reason: str,
        payment: Payment,
        coupon: Coupon | None,
        customer: str,
    ) -> None:
        """Cancel a pre-shipment order and unwind its side effects (atomic).

        Steps: ``order.cancel`` -> ``payment.void`` -> ``stock.release`` per
        reservation -> ``coupon.unredeem`` (if used) -> persist + collect.

        Why void vs refund: cancel runs only before shipment, while Payment
        is still ``Authorized`` — ``void`` tears down the hold with no money
        movement. (Post-delivery money-back is ``ReturnAppService.settle``'s
        ``refund``, which requires a captured payment.) Why release (not
        commit/restock): the goods never left the warehouse, so reservations
        simply dissolve. Why un-redeem: CO7 — a cancelled order must not burn
        the customer's single redemption.

        Args:
            order: Loaded cancellable (pre-shipment) Order.
            reason: Audit reason threaded into cancel/void/release.
            payment: Loaded Authorized Payment for the order.
            coupon: Loaded Coupon if the order used one, else None.
            customer: Customer ref for the ``unredeem`` idempotency key.

        Raises:
            DomainError: Order already Shipped/Delivered (O3 — use the
                Returns flow instead); payment not voidable (PM3).

        Events: ``OrderCancelled``, ``PaymentVoided``, ``StockReleased``,
            ``CouponUnredeemed`` (post-commit).
        """
        with self._uow() as uow:
            # 1-2. Flip Order + tear down the auth hold (void, not refund —
            #    nothing was captured, so no money moves).
            order.cancel(reason)
            payment.void(reason)
            # 3. Dissolve every reservation (goods never left the warehouse).
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).release(rid, reason)
            # 4. Restore the coupon redemption (guarded: only when the order
            #    actually carries a coupon code — mirrors redeem exactly).
            if coupon is not None and order.coupon_code:
                coupon.unredeem(customer, order.order_id)
            # -- persist + collect (outbox published on commit) -------------
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(order, payment, coupon, *uow.stocks.list())


class ReturnAppService:
    """Returns -> Fulfillment (Pickup) + Payment + Inventory + Shop (coupon).

    Contexts bridged: Returns (``Return`` RMA lifecycle — Requested ->
    Approved -> PickupScheduled -> PickedUp -> GoodsReceived -> Refunded);
    Fulfillment (doorstep ``Pickup``: schedule/assign/confirm with mandatory
    photo evidence PK5); Payment (pro-rata ``refund`` — partial refunds keep
    ``Captured`` per PM4); Inventory (``restock`` only after courier
    possession per S4/RT5); Order Management (``accept_return`` for partial,
    ``refund_completed`` for full); Shop/Coupon (``unredeem`` only on a
    *complete* return per CO7/RT6).

    Atomicity: one UoW per method; the RMA pipeline is deliberately split
    into four UoWs (request/schedule/complete/settle) because each step
    waits on a real-world actor (customer request, courier slot, doorstep
    scan, settlement run). Within each step, all aggregates commit together
    or roll back together — e.g. ``settle`` never refunds money without
    restocking and updating the order.

    Events: ``ReturnRequested``/``ReturnApproved`` (request_return),
    ``PickupScheduled`` (schedule_pickup), ``PickupConfirmed`` +
    ``ReturnGoodsReceived`` (complete_pickup), ``ReturnRefunded`` +
    ``PaymentRefunded`` + ``StockRestocked`` (+ ``CouponUnredeemed`` on full
    returns) + ``OrderRefunded`` (settle).
    """

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        files: FileBucket | None = None,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer
        self._files = files  # optional S3-style bucket for pickup photo evidence

    def _facts(self, order: Order) -> _OrderFacts:
        """Build the immutable order-facts snapshot the ``Return`` validates.

        Freezes shipped lines + discount + shipping fee + zero-tax rate so
        the pro-rata refund math (§5) stays stable even if the order or
        catalog later changes. Internal helper — no UoW of its own.
        """
        return _OrderFacts(
            order_id=order.order_id,
            customer_id=order.customer_ref,
            destination=order.destination,
            shipped_lines=tuple(
                (line.product_id, line.quantity, line.unit_price)
                for line in order.lines
            ),
            discount=order.discount,
            shipping_fee=order.shipping_fee,
            tax_num=TAX_NUM,
            tax_den=TAX_DEN,
            delivered_at=order.delivered_at,
        )

    def request_return(
        self, order: Order, return_lines: list[tuple[str, int]], reason: str,
        now: datetime | None = None,
    ) -> Return:
        """Open + auto-approve an RMA against a Delivered order (atomic).

        Steps: validate order is ``Delivered`` (RT1) -> build ``_OrderFacts``
        snapshot -> ``Return(...)`` (validates 30-day window + qty ≤ shipped
        per RT1/RT2) -> ``rma.approve()`` (v1 auto-approves) -> persist +
        collect.

        Why Delivered-only + window check: only delivered goods physically
        exist at the customer's door for the courier to collect, and the 30-day
        window bounds the seller's liability (domain-model.md §6). Why
        auto-approve: v1 has no manual review queue — approval is a recorded
        no-op transition so the event log still shows Requested -> Approved.

        Args:
            order: Loaded ``Delivered`` Order.
            return_lines: ``[(product_id, qty)]`` with qty ≤ shipped qty.
            reason: Customer reason (recorded on the RMA).
            now: Request timestamp; defaults to the injected Clock.

        Returns:
            The approved ``Return`` (RMA).

        Raises:
            DomainError: Order not ``Delivered``; return window expired;
                over-returning qty (RT2).

        Events: ``ReturnRequested``, ``ReturnApproved`` (post-commit).
        """
        # Pre-UoW guard: fail fast without spending a transaction.
        if order.status != "Delivered":
            raise DomainError(
                f"returns only against Delivered orders (was {order.status})"
            )
        with self._uow() as uow:
            # Build the frozen facts snapshot, then validate + approve the
            # RMA against it (window + qty checks live in the domain).
            rma = Return(
                self._facts(order), return_lines, reason, now or self._clock.now()
            )
            rma.approve()
            # Persist + drain RMA events into the outbox (commit publishes).
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.collect(rma)
        return rma

    def schedule_pickup(self, rma: Return, slot: datetime) -> Pickup:
        """Schedule the doorstep courier collection for an approved RMA.

        Steps: ``Pickup(rma, destination, slot)`` (PK2 slot validation) ->
        ``assign_courier(AUTO_COURIER)`` (v1 single-carrier default) ->
        ``rma.link_pickup`` (Approved -> PickupScheduled) -> persist + collect.

        Why a separate UoW from request: scheduling waits on courier capacity
        (a real-world actor), so it cannot share the request transaction.
        Why link both ways (pickup.return_id + rma.pickup_id): PK1 — a pickup
        belongs to exactly one RMA, enforced from both sides.

        Args:
            rma: Loaded approved ``Return``.
            slot: Courier service-window datetime for the doorstep visit.

        Returns:
            The scheduled (courier-assigned) ``Pickup``.

        Raises:
            DomainError: RMA not in an pickup-linkable state; slot outside
                service area (PK2).

        Events: ``PickupScheduled`` (post-commit).
        """
        with self._uow() as uow:
            # Create the Fulfillment leg and bind it to the RMA in one step.
            pickup = Pickup(rma.rma_id, rma.destination, slot, now=self._clock.now())
            pickup.assign_courier(AUTO_COURIER)
            rma.link_pickup(pickup.pickup_id)
            # Persist + drain events into the outbox (commit publishes).
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)
        return pickup

    def complete_pickup(
        self, rma: Return, pickup: Pickup, evidence: PickupEvidence,
        photo_bytes: bytes | None = None,
    ) -> None:
        """Confirm courier possession at the door; mark goods received (atomic).

        Steps: optionally store ``photo_bytes`` in the FileBucket (S3/local)
        -> ``pickup.confirm_pickup`` (PK3/PK5: courier scan + mandatory photo
        evidence flips Scheduled/EnRoute -> PickedUp) ->
        ``rma.on_pickup_confirmed`` (PickedUp) -> ``rma.receive_goods``
        (GoodsReceived) -> persist + collect.

        Why photo-first: the FileBucket put is infrastructure I/O that must
        precede the domain transition — without stored evidence the
        confirmation would violate PK5. (Local impl is in-memory; AWS impl
        targets S3 with no application change.) Why two RMA transitions:
        ``on_pickup_confirmed`` records possession transfer while
        ``receive_goods`` records warehouse reconciliation intent — restock
        itself still waits for ``settle`` (S4/RT5: never restock before the
        courier actually has the goods).

        Args:
            rma: Loaded pickup-linked ``Return``.
            pickup: Loaded scheduled/en-route ``Pickup``.
            evidence: ``PickupEvidence`` (photo ref + optional signature).
            photo_bytes: Raw photo bytes to store under
                ``evidence.photo_ref``; skipped when None (already stored).

        Raises:
            DomainError: Missing/invalid evidence (PK5); pickup in a
                non-confirmable state.

        Events: ``PickupConfirmed``, ``ReturnGoodsReceived`` (post-commit).
        """
        with self._uow() as uow:
            # Infrastructure side effect first (evidence must exist before
            # the domain accepts the confirmation). Guarded: only when a
            # bucket is wired AND bytes were actually supplied.
            if photo_bytes is not None and self._files is not None:
                self._files.put(evidence.photo_ref, photo_bytes)
            # Domain transitions: possession transfer, then goods-received.
            pickup.confirm_pickup(evidence)
            rma.on_pickup_confirmed(pickup.pickup_id, evidence)
            rma.receive_goods()
            # Persist + drain events into the outbox (commit publishes).
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)

    def settle(self, rma: Return, order: Order) -> RefundBreakdown:
        """Settle an RMA: pro-rata refund + restock + order/coupon update.

        Steps: ``rma.settle`` (computes the §5 pro-rata ``RefundBreakdown``:
        goods + tax + shipping scaled by return fraction) ->
        ``payment.refund`` (partial keeps ``Captured`` per PM4) ->
        ``stock.restock`` per returned line (S4: only now, after possession) ->
        coupon lookup (tolerates missing coupon) -> full-return?
        ``order.refund_completed`` + ``coupon.unredeem`` (CO7/RT6) : partial?
        ``order.accept_return`` -> persist + collect.

        Why refund-vs-void: unlike cancel, the payment here is already
        ``Captured`` (money moved at shipment), so only ``refund`` is legal.
        Why restock here and not at pickup: S4/RT5 — inventory returns only
        once the return is settled, preventing double-restock if settlement
        is retried. Why full-vs-partial split: a complete return makes the
        customer whole (order ``Refunded`` terminal + redemption restored);
        a partial return keeps the order usable and the coupon burned (CO7).
        Why the coupon lookup tolerates ``KeyError``: a code deleted after
        placement must not block a legitimate money-back settlement.

        Args:
            rma: Loaded goods-received ``Return`` ready to settle.
            order: Loaded ``Delivered`` Order the RMA was opened against.

        Returns:
            The pro-rata ``RefundBreakdown`` (goods/tax/shipping/total).

        Raises:
            DomainError: RMA not settleable (e.g. already Refunded RT4);
                refund exceeds captured (PM1).

        Events: ``ReturnRefunded``, ``PaymentRefunded``, ``StockRestocked``
            × N, ``OrderRefunded`` (full) or order return-accepted (partial),
            ``CouponUnredeemed`` (full returns with a coupon) — post-commit.
        """
        with self._uow() as uow:
            # 1. Domain computes the money (pure function of frozen facts).
            breakdown = rma.settle()
            # 2. Move the money back (Payment stays generic: amount + reason).
            payment = uow.payments.get(order.payment_ref)
            payment.refund(breakdown.total, f"return {rma.rma_id}")
            # 3. Return the goods to sellable inventory (audited by rma_id).
            for line in rma.lines:
                uow.stocks.get(line.product_id).restock(
                    rma.rma_id, {line.product_id: line.quantity}
                )
            # 4. Best-effort coupon lookup: a deleted code degrades to None
            #    (settlement must never fail for a missing coupon record).
            coupon = None
            if order.coupon_code:
                try:
                    coupon = uow.coupons.find_by_code(order.coupon_code)
                except KeyError:
                    coupon = None
            # 5. Full vs partial: terminal refund + re-entitlement vs. the
            #    order stays open and the redemption stays burned (CO7/RT6).
            if breakdown.is_full:
                order.refund_completed(breakdown.total, f"return {rma.rma_id}")
                if coupon is not None:
                    coupon.unredeem(order.customer_ref, order.order_id)
            else:
                order.accept_return(rma.rma_id)
            # -- persist + collect (outbox published on commit) -------------
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(
                rma, order, payment, *([coupon] if coupon else []), *uow.stocks.list()
            )
        return breakdown


# Re-export stable address type for callers that only import application.
__all__ = [
    "Address",
    "Money",
    "CheckoutAppService",
    "ShippingAppService",
    "CancelAppService",
    "ReturnAppService",
]
