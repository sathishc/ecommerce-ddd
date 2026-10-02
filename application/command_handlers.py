"""Command handlers: user-initiated Commands -> aggregates -> Events.

Responsibility
--------------
Each handler method is the application-layer ACL for one external intent
(one immutable ``domain.commands`` value, e.g. ``PlaceOrder``,
``RequestReturn``). It loads aggregates *by ID* inside a single atomic
``UnitOfWork``, invokes domain behavior, persists via ``uow.<repos>.add``,
and drains events into the outbox via ``uow.collect`` (published through
the ``EventBus`` on commit). System follow-ons (reserve → authorize →
capture → restock → un-redeem) run *inside the same UoW* — they surface
as Events, not as further Commands, because no external actor initiates
them (domain-model.md §5 table; README "Order flows").

UoW atomicity contract
----------------------
Identical to ``application.services``: one ``with self._uow() as uow:``
per handler call; snapshot-on-entry, commit-publishes-outbox on success,
snapshot-restore + outbox-discard on any exception. ``uow.collect`` is the
only path for domain events to reach the ``EventBus``/NotificationService.
``AsyncJob.enqueue("order.placed", ...)`` fires only after a successful
``place_order`` commit (outside the transaction's critical section).

Context-map crossing (domain-model.md §2)
-----------------------------------------
- Catalog (``publish_product``; auto-creates a zeroed ``Stock`` so later
  checkout never hits a missing-record edge).
- Shop (cart CRUD + coupon preview; ``place_order`` bridges Shop ->
  Order Mgmt + Inventory + Payment + Coupon atomically, with compensating
  release + un-redeem on partial failure).
- Order Management (``cancel_order``: void + release + un-redeem).
- Fulfillment + Payment (``hand_to_carrier``: commit stock + capture at
  handoff; ``confirm_delivery`` gates all returns).
- Returns + Fulfillment + Payment + Inventory + Shop (``request_return`` /
  ``schedule_pickup`` / ``confirm_pickup`` / ``settle_return``: pickup +
  pro-rata refund + restock + full-return un-redeem).

Command -> events flow
----------------------
``CommandBus.dispatch(cmd)`` -> exactly one handler here (wired by
``register_command_handlers`` / ``infra.container``) -> load (``uow.<repos>
.get`` by the IDs carried on the command) -> validate (command-field
guards + domain invariants) -> mutate (aggregate methods) -> persist
(``uow.<repos>.add``) -> collect (``uow.collect`` -> outbox -> EventBus).

Existing ``application.services`` methods stay as the aggregate-level API
(callers pass loaded aggregates); these handlers are the ID-based,
dispatchable front door wired to the ``CommandBus`` (see
``register_command_handlers`` + ``infra.container``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from domain.cart import Cart
from domain.commands import (
    AddToCart,
    ApplyCoupon,
    CancelOrder,
    CloseCart,
    Command,
    ConfirmDelivery,
    ConfirmPickup,
    HandToCarrier,
    OpenCart,
    PlaceOrder,
    PublishProduct,
    RemoveCartLine,
    RemoveCoupon,
    RejectReturn,
    RequestReturn,
    SchedulePickup,
    SettleReturn,
    UpdateCartLine,
)
from domain.errors import DomainError
from domain.order import Order
from domain.payment import Payment
from domain.pickup import Pickup
from domain.product import Product
from domain.return_rma import Return, _OrderFacts
from domain.shipment import Shipment
from domain.stock import Stock
from domain.value_objects import RefundBreakdown
from infra.blocks import AsyncJob, FileBucket
from infra.clock import Clock, SystemClock
from infra.command_bus import CommandBus
from infra.unit_of_work import UoWFactory

TAX_NUM, TAX_DEN = 0, 100  # v1: no tax (pluggable, mirrored into _OrderFacts)
AUTO_COURIER = "courier-default"  # v1 single-carrier default (see §6 assumptions)


def _future(days: int, clock: Clock) -> datetime:
    """Expiry timestamp ``days`` from the injected Clock's now.

    Keeps reservation TTLs deterministic under ``FixedClock`` in tests.
    """
    return clock.now() + timedelta(days=days)


class CommandHandlers:
    """ID-based handlers for every user-initiated Command.

    Bridges caller intent to the domain: each method takes an immutable
    ``Command`` carrying IDs (+ payload), loads the aggregates in one UoW,
    and commits or rolls back atomically. Wired 1:1 onto the ``CommandBus``
    by ``register_command_handlers`` so ``bus.dispatch(cmd)`` reaches exactly
    one handler. Depends only on the ``UoWFactory`` + ``Clock`` (+ optional
    tracer/jobs/files Blocks) — never on concrete repos or buses.

    Atomicity: one UoW per handler call (see module docstring). ``place_order``
    additionally performs in-UoW compensation (release + un-redeem) when a
    later reservation fails after earlier ones succeeded; ``cancel_order``
    and ``settle_return`` tolerate a missing coupon record (``KeyError`` ->
    ``None``) so coupon-bookkeeping gaps never block money movement.
    """

    def __init__(
        self,
        uow_factory: UoWFactory,
        clock: Clock | None = None,
        tracer=None,
        jobs: AsyncJob | None = None,
        files: FileBucket | None = None,
    ) -> None:
        # DIP wiring: factory + clock + optional observability/file/job blocks.
        self._uow = uow_factory
        self._clock = clock or SystemClock()
        self._tracer = tracer  # optional span emitter (observability only)
        self._jobs = jobs  # optional post-commit async queue (order.placed)
        self._files = files  # optional evidence bucket (pickup photos; unused here)

    # -- Catalog ----------------------------------------------------------
    def publish_product(self, cmd: PublishProduct) -> Product:
        """Publish a catalog product (+ zeroed stock record) atomically.

        Steps: load (none — creates new) -> validate (price present, P2
        price ≥ 0 lives in the domain) -> mutate (``Product(...)`` +
        ``Stock(product_id, 0)`` when no stock row exists) -> persist
        (``uow.products.add`` + maybe ``uow.stocks.add``) -> collect.

        Why the zeroed ``Stock``: Inventory is conformist to Catalog — a
        product without a stock row would fail later at checkout with "no
        stock record"; creating it here keeps Catalog publish total and
        makes the failure mode explicit (zero available -> StockInsufficient).

        Args:
            cmd: ``PublishProduct`` with sku/name/price/description/weight.

        Returns:
            The new ``Product`` (ID auto-assigned by the domain).

        Raises:
            DomainError: Missing price; price invariant violation (P2).

        Events: ``ProductPublished`` (post-commit via outbox).
        """
        # Validate command payload before opening the UoW (cheap fast-fail).
        if cmd.price is None:
            raise DomainError("PublishProduct needs a price")
        with self._uow() as uow:
            # 1. Create the Catalog aggregate (ID assigned inside the domain).
            product = Product(None, cmd.sku, cmd.name, cmd.price,
                              cmd.description, cmd.weight_grams)
            uow.products.add(product)
            # 2. Ensure the Inventory counterpart exists (idempotent: skip
            #    when a stock row already tracks this product).
            try:
                uow.stocks.get(product.product_id)
            except KeyError:
                # Error path handled locally: missing row -> seed zero stock
                # (a product with no availability, not an error).
                uow.stocks.add(Stock(product.product_id, 0))
            # Drain ProductPublished into the commit outbox.
            uow.collect(product)
        return product

    # -- Shop: cart -------------------------------------------------------
    def open_cart(self, cmd: OpenCart) -> Cart:
        """Open a new Shop cart for a customer (atomic).

        Steps: ``Cart(customer_ref)`` -> persist -> collect.

        Args:
            cmd: ``OpenCart`` carrying the opaque external customer ref.

        Returns:
            The new ``Open`` Cart.

        Events: ``CartCreated`` (post-commit).
        """
        with self._uow() as uow:
            cart = Cart(cmd.customer_ref)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def add_to_cart(self, cmd: AddToCart) -> Cart:
        """Add a catalog product to an open cart (atomic).

        Steps: load cart + product by ID -> ``cart.add_product`` (C1: qty ≥ 1,
        one line per product; snapshots unit price) -> persist -> collect.

        Why load the ``Product`` (not just price): the cart snapshots the
        authoritative catalog price at add time (later price changes via
        ``ProductPriceChanged`` never rewrite existing cart lines silently).

        Args:
            cmd: ``AddToCart`` with cart/product IDs + quantity.

        Returns:
            The mutated ``Cart``.

        Raises:
            KeyError: Unknown cart or product ID (propagates -> UoW rollback).
            DomainError: Quantity < 1; cart already checked out (C4).

        Events: ``CartItemAdded`` (post-commit).
        """
        with self._uow() as uow:
            # Load both sides of the Catalog -> Shop OHS view.
            cart = uow.carts.get(cmd.cart_id)
            product = uow.products.get(cmd.product_id)
            cart.add_product(product, cmd.quantity)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def update_cart_line(self, cmd: UpdateCartLine) -> Cart:
        """Change a cart line's quantity (atomic).

        Steps: load cart -> ``cart.change_quantity`` (C1/C2: qty ≥ 1, total
        re-derived) -> persist -> collect.

        Args:
            cmd: ``UpdateCartLine`` with cart/product IDs + new quantity.

        Returns:
            The mutated ``Cart``.

        Raises:
            KeyError: Unknown cart ID. DomainError: Bad qty / immutable cart.

        Events: ``CartItemChanged`` (post-commit).
        """
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.change_quantity(cmd.product_id, cmd.quantity)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def remove_cart_line(self, cmd: RemoveCartLine) -> Cart:
        """Remove one product line from the cart (atomic).

        Steps: load cart -> ``cart.remove_line`` -> persist -> collect.

        Args:
            cmd: ``RemoveCartLine`` with cart + product IDs.

        Returns:
            The mutated ``Cart``.

        Raises:
            KeyError: Unknown cart ID. DomainError: Immutable cart / no line.

        Events: ``CartLineRemoved`` (post-commit).
        """
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.remove_line(cmd.product_id)
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def apply_coupon(self, cmd: ApplyCoupon) -> Cart:
        """Preview-apply a coupon code to the cart (atomic).

        Steps: load cart + coupon (``find_by_code``) ->
        ``cart.apply_coupon`` (C3 single-coupon replace; validates window/
        threshold at ``clock.now()``) -> persist both -> collect.

        Why "preview": per CO4 the redemption counter is NOT incremented
        here — only ``place_order``'s ``redeem`` commits it, so racing carts
        cannot double-spend one code and an abandoned cart burns nothing.

        Args:
            cmd: ``ApplyCoupon`` with cart ID + coupon code.

        Returns:
            The mutated ``Cart`` (discount reflected in the derived total).

        Raises:
            KeyError: Unknown cart ID / coupon code.
            DomainError: Coupon invalid for this cart/total/window; cart
                already checked out.

        Events: ``CouponApplied`` (post-commit).
        """
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            coupon = uow.coupons.find_by_code(cmd.coupon_code)
            cart.apply_coupon(coupon, self._clock.now())
            uow.carts.add(cart)
            uow.coupons.add(coupon)
            uow.collect(cart)
        return cart

    def remove_coupon(self, cmd: RemoveCoupon) -> Cart:
        """Detach the currently applied coupon (atomic).

        Steps: load cart -> ``cart.remove_coupon`` -> persist -> collect.
        No Coupon mutation (nothing was redeemed — preview only).

        Args:
            cmd: ``RemoveCoupon`` with the cart ID.

        Returns:
            The mutated ``Cart`` (total re-derived without discount).

        Events: ``CouponRemoved`` (post-commit).
        """
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.remove_coupon()
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    def close_cart(self, cmd: CloseCart) -> Cart:
        """Abandon a cart without checkout (atomic).

        Steps: load cart -> ``cart.abandon`` (Open -> Abandoned) -> persist
        -> collect. The cart becomes unusable for checkout (C4).

        Args:
            cmd: ``CloseCart`` with the cart ID.

        Returns:
            The abandoned ``Cart``.

        Events: ``CartAbandoned`` (post-commit).
        """
        with self._uow() as uow:
            cart = uow.carts.get(cmd.cart_id)
            cart.abandon()
            uow.carts.add(cart)
            uow.collect(cart)
        return cart

    # -- Shop -> Order (checkout) -----------------------------------------
    def place_order(self, cmd: PlaceOrder) -> tuple[Order, Payment]:
        """Authorize at placement; capture happens later, at shipment.

        The big cross-context ACL (Shop -> Order Mgmt + Inventory + Payment
        + Coupon) in one atomic UoW — the ID-based twin of
        ``CheckoutAppService.place_and_pay`` (same steps + same compensation,
        but loads the cart by ``cmd.cart_id`` and honors an optional
        ``cmd.destination`` override).

        Steps (load -> validate -> mutate -> persist -> collect):
          1. Load cart by ID; apply ``cmd.destination`` when the cart has
             none (lets checkout set the address in the same intent).
          2. Validate ``Open`` + destination present (C4; pre-mutation
             fast-fail inside the UoW since the cart had to be loaded).
          3. ``cart.checkout`` (freeze Shop quote) -> ``Order.place``
             (freeze O2 totals) -> ``coupon.redeem`` (CO4 commit point) +
             per-line ``stock.reserve`` + ``order.link_reservation``.
          4. ``Payment(money_total, instrument_ref)`` + ``order.pay``
             (authorize-only; capture waits for ``hand_to_carrier``).
          5. Persist cart/order/payment/(coupon) + ``uow.collect`` all touched
             aggregates (outbox -> EventBus on commit); enqueue
             ``order.placed`` post-commit.

        Why each step exists: coupon redeemed at placement (cart apply was a
        preview — CO4 concurrency safety); all-or-nothing reservation (S2 —
        no partial orders); authorize-not-capture (§6.3 — money moves at
        carrier handoff); ``KeyError`` -> ``DomainError`` translation for
        missing stock rows so the compensation path treats them uniformly.

        Error path: any ``DomainError`` triggers in-UoW compensation —
        release already-linked reservations (``"checkout rollback"`` ->
        ``StockReleased``) and un-redeem the coupon (mirror of redeem) —
        then re-raises so the UoW snapshot-rollback discards everything
        (per-line release tolerates ``KeyError`` for robustness against
        concurrently removed stock rows).

        Args:
            cmd: ``PlaceOrder`` with cart ID, instrument ref, optional
                destination override, and reservation TTL days.

        Returns:
            ``(order, payment)`` — frozen Order + Authorized Payment.

        Raises:
            DomainError: Cart not Open / missing destination (C4); coupon
                invalid at placement (CO2-CO4); short stock (S2); missing
                stock record; payment invariant violation. All roll back.

        Events: ``CartCheckedOut``, ``OrderPlaced``, ``StockReserved`` × N,
            ``CouponRedeemed`` (if coupon), ``OrderPaymentAuthorized``,
            ``OrderPaid`` (post-commit).
        """
        if self._tracer is not None:
            # Observability only — emitted before the UoW opens.
            self._tracer.trace("checkout.place_order", cart=cmd.cart_id)
        with self._uow() as uow:
            # 1. Load the Shop aggregate by ID (ID-based front door: the
            #    service twin receives a loaded Cart instead).
            cart = uow.carts.get(cmd.cart_id)
            # Optional destination override: lets the PlaceOrder intent set
            # the address when the cart was built without one.
            if cmd.destination is not None and cart.destination is None:
                cart.set_destination(cmd.destination)
            # 2. C4 guards (must hold before any money/stock moves).
            if cart.status != "Open":
                raise DomainError("C4: a cart can only be checked out once")
            if cart.destination is None:
                raise DomainError("C4: a checkout needs a destination address")
            # 3. Freeze the Shop view (quote + CheckedOut flip).
            snapshot = cart.checkout(cart.destination)
            quote = snapshot["quote"]
            # 4. Freeze the Order view (O2: totals fixed at placement).
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
                # 5a. Commit the coupon redemption NOW (CO4 atomic commit).
                if coupon is not None:
                    coupon.redeem(cart.customer_ref, order.order_id)
                # 5b. Reserve every line (S2 all-or-nothing) and link handles.
                for pid, qty, _ in snapshot["lines"]:
                    try:
                        stock = uow.stocks.get(pid)
                    except KeyError:
                        # Translate repo miss -> domain error so the except
                        # below compensates uniformly.
                        raise DomainError(f"no stock record for {pid}") from None
                    order.link_reservation(
                        pid,
                        stock.reserve(
                            order.order_id, qty,
                            _future(cmd.reservation_ttl_days, self._clock),
                        ),
                    )
            except DomainError:
                # -- error path: compensate, then re-raise for UoW rollback.
                for pid, rid in order.reservation_ids.items():
                    try:
                        uow.stocks.get(pid).release(rid, "checkout rollback")
                    except KeyError:
                        # Tolerate concurrently-removed stock rows: the UoW
                        # snapshot restore still discards the reservation.
                        pass
                if coupon is not None:
                    coupon.unredeem(cart.customer_ref, order.order_id)
                raise
            # 6. Order -> Payment ACL (generic Money + opaque ref; Payment
            #    never learns "Order"). Authorize-only here.
            payment = Payment(order.money_total, cmd.instrument_ref)
            order.pay(payment.payment_id)
            # -- persist every touched aggregate ---------------------------
            uow.carts.add(cart)
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            # -- collect: drain events into the commit outbox (stocks via
            # list() so each StockReserved is published on commit) ---------
            uow.collect(
                cart, order, payment, *uow.stocks.list(),
                *([coupon] if coupon else []),
            )
            # Post-commit fan-out (job runs after the UoW commits).
            if self._jobs is not None:
                self._jobs.enqueue("order.placed", {"order_id": order.order_id})
        return order, payment

    # -- Order ------------------------------------------------------------
    def cancel_order(self, cmd: CancelOrder) -> Order:
        """Cancel pre-shipment: void auth, release stock, un-redeem (atomic).

        Steps: load order + payment (via ``order.payment_ref``) ->
        ``order.cancel`` (O3: pre-shipment only) -> ``payment.void`` (not
        refund — nothing captured) -> ``stock.release`` per reservation ->
        coupon lookup + ``unredeem`` (best-effort: missing code -> None) ->
        persist + collect.

        Why void vs refund: Payment is still ``Authorized`` (capture happens
        at carrier handoff), so ``void`` tears down the hold with no money
        movement. Why best-effort coupon: a code deleted after placement
        must not block the cancel — money/stock consistency outranks
        coupon bookkeeping.

        Args:
            cmd: ``CancelOrder`` with order ID + reason.

        Returns:
            The cancelled ``Order``.

        Raises:
            DomainError: Order already Shipped/Delivered (O3 — use Returns);
                payment not voidable. KeyError: unknown order/payment.

        Events: ``OrderCancelled``, ``PaymentVoided``, ``StockReleased`` × N,
            ``CouponUnredeemed`` (if a coupon was used and still exists).
        """
        with self._uow() as uow:
            # 1. Load the Order and its Payment (opaque ref keeps Payment
            #    generic — no order knowledge leaks downstream).
            order = uow.orders.get(cmd.order_id)
            payment = uow.payments.get(order.payment_ref)
            # 2. Domain transitions: cancel order, void the auth hold.
            order.cancel(cmd.reason)
            payment.void(cmd.reason)
            # 3. Dissolve reservations (goods never left the warehouse).
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).release(rid, cmd.reason)
            # 4. Best-effort coupon restore (CO7 mirror of redeem): a deleted
            #    code degrades to None instead of failing the cancel.
            coupon = None
            if order.coupon_code:
                try:
                    coupon = uow.coupons.find_by_code(order.coupon_code)
                    coupon.unredeem(order.customer_ref, order.order_id)
                except KeyError:
                    coupon = None
            # -- persist + collect (outbox published on commit) -------------
            uow.orders.add(order)
            uow.payments.add(payment)
            if coupon is not None:
                uow.coupons.add(coupon)
            uow.collect(order, payment, coupon, *uow.stocks.list())
        return order

    # -- Fulfillment ------------------------------------------------------
    def hand_to_carrier(self, cmd: HandToCarrier) -> Shipment:
        """Hand a Paid order to the carrier; commit stock + capture (atomic).

        Steps: load order -> build ``Shipment`` from order lines ->
        ``order.confirm_shipped`` (O3/O4) -> ``shipment.hand_to_carrier``
        (SH1/SH2) -> ``stock.commit`` per reservation -> ``payment.capture``
        -> persist + collect.

        Why this order: domain guards first (cheap), then the irreversible
        legs — stock commit before capture so money never moves without the
        goods leaving. Capture-at-handoff is the §6.3 authorize-then-capture
        rule; committing (not releasing) is correct because the parcel
        physically left the warehouse.

        Args:
            cmd: ``HandToCarrier`` with order ID, carrier, tracking number.

        Returns:
            The handed-to-carrier ``Shipment``.

        Raises:
            KeyError: Unknown order/payment. DomainError: Order not Paid /
                double-ship; payment not Authorized (PM2).

        Events: ``ShipmentCreated``, ``ShipmentShipped``, ``OrderShipped``,
            ``StockCommitted`` × N, ``PaymentCaptured`` (post-commit).
        """
        with self._uow() as uow:
            # 1. Load the Order (upstream aggregate driving Fulfillment).
            order = uow.orders.get(cmd.order_id)
            # 2. Build the Fulfillment view (SH1: exactly the order's lines;
            #    v1 has no split shipments).
            shipment = Shipment(
                order.order_id,
                [(line.product_id, line.quantity) for line in order.lines],
                order.destination,
            )
            # 3. Flip Order + record the carrier handoff (tracking immutable).
            order.confirm_shipped(shipment.shipment_id)
            shipment.hand_to_carrier(cmd.carrier, cmd.tracking_number)
            # 4. Convert soft reservations to hard decrements.
            for pid, rid in order.reservation_ids.items():
                uow.stocks.get(pid).commit(rid)
            # 5. Move the money (Authorized -> Captured) via the opaque ref.
            payment = uow.payments.get(order.payment_ref)
            payment.capture()
            # -- persist + collect (outbox published on commit) -------------
            uow.orders.add(order)
            uow.shipments.add(shipment)
            uow.payments.add(payment)
            uow.collect(order, shipment, *uow.stocks.list(), payment)
        return shipment

    def confirm_delivery(self, cmd: ConfirmDelivery) -> None:
        """Record carrier delivery confirmation (atomic).

        Steps: load shipment + order -> ``shipment.confirm_delivered``
        (SH3 terminal) -> ``order.deliver`` (O3: Shipped -> Delivered) ->
        persist + collect.

        Why both together: ``Delivered`` is the gate for the whole Returns
        flow (RT1) — flipping one without the other would strand return
        rights or invent them without carrier proof. Defaults the delivery
        date to today when the command carries none (carrier-scan date).

        Args:
            cmd: ``ConfirmDelivery`` with shipment + order IDs and optional
                delivery date.

        Raises:
            KeyError: Unknown shipment/order. DomainError: Already terminal /
                order not Shipped.

        Events: ``ShipmentDelivered``, ``OrderDelivered`` (post-commit).
        """
        with self._uow() as uow:
            shipment = uow.shipments.get(cmd.shipment_id)
            order = uow.orders.get(cmd.order_id)
            shipment.confirm_delivered()
            order.deliver(cmd.at or date.today())
            uow.shipments.add(shipment)
            uow.orders.add(order)
            uow.collect(shipment, order)

    # -- Returns ----------------------------------------------------------
    def _facts(self, order: Order) -> _OrderFacts:
        """Freeze order facts for RMA validation + pro-rata refund math.

        Captures shipped lines + discount + shipping + tax rate + delivery
        timestamp so the §5 refund rule stays stable post-placement.
        Internal helper — no UoW of its own.
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

    def request_return(self, cmd: RequestReturn) -> Return:
        """Open + auto-approve an RMA against a Delivered order (atomic).

        Steps: load order -> validate ``Delivered`` (RT1 fast-fail) ->
        ``Return(facts, lines, reason, clock.now())`` (validates 30-day
        window + qty ≤ shipped per RT1/RT2) -> ``rma.approve()`` (v1
        auto-approve) -> persist + collect.

        Why Delivered-only: only delivered goods sit at the customer's door
        for doorstep collection; why window + qty checks live in the domain:
        they are RT1/RT2 invariants shared with the services front door.

        Args:
            cmd: ``RequestReturn`` with order ID, return lines, reason.

        Returns:
            The approved ``Return`` (RMA).

        Raises:
            KeyError: Unknown order ID. DomainError: Order not Delivered;
                window expired; over-returning qty.

        Events: ``ReturnRequested``, ``ReturnApproved`` (post-commit).
        """
        with self._uow() as uow:
            order = uow.orders.get(cmd.order_id)
            # RT1 gate: fail fast before constructing the RMA.
            if order.status != "Delivered":
                raise DomainError(
                    f"returns only against Delivered orders (was {order.status})"
                )
            rma = Return(
                self._facts(order), list(cmd.lines), cmd.reason,
                self._clock.now(),
            )
            rma.approve()
            uow.returns.add(rma)
            uow.orders.add(order)
            uow.collect(rma)
        return rma

    def reject_return(self, cmd: RejectReturn) -> Return:
        """Reject an RMA (e.g. failed review) atomically.

        Steps: load RMA -> ``rma.reject`` (Requested -> Rejected, RT4
        terminal for this path) -> persist -> collect.

        Args:
            cmd: ``RejectReturn`` with RMA ID + reason.

        Returns:
            The rejected ``Return``.

        Raises:
            KeyError: Unknown RMA ID. DomainError: RMA not rejectable
                (already approved/settled).

        Events: ``ReturnRejected`` (post-commit).
        """
        with self._uow() as uow:
            rma = uow.returns.get(cmd.rma_id)
            rma.reject(cmd.reason)
            uow.returns.add(rma)
            uow.collect(rma)
        return rma

    def schedule_pickup(self, cmd: SchedulePickup) -> Pickup:
        """Schedule the doorstep courier collection for an RMA (atomic).

        Steps: validate slot present -> load RMA ->
        ``Pickup(rma, destination, slot)`` (PK2 slot validation) ->
        ``assign_courier(AUTO_COURIER)`` (v1 single carrier) ->
        ``rma.link_pickup`` (Approved -> PickupScheduled) -> persist + collect.

        Why slot is command-validated pre-UoW: a missing slot is a caller
        bug — fail without spending a transaction. Why link both ways
        (``pickup.return_id`` + ``rma.pickup_id``): PK1 one-pickup-per-RMA.

        Args:
            cmd: ``SchedulePickup`` with RMA ID + courier slot datetime.

        Returns:
            The scheduled (courier-assigned) ``Pickup``.

        Raises:
            DomainError: Missing slot; RMA not linkable; slot outside
                service area.

        Events: ``PickupScheduled`` (post-commit).
        """
        # Pre-UoW command validation (caller bug -> no transaction spent).
        if cmd.slot is None:
            raise DomainError("SchedulePickup needs a slot")
        with self._uow() as uow:
            rma = uow.returns.get(cmd.rma_id)
            # Create the Fulfillment leg bound to this RMA's destination.
            pickup = Pickup(rma.rma_id, rma.destination, cmd.slot,
                            now=self._clock.now())
            pickup.assign_courier(AUTO_COURIER)
            rma.link_pickup(pickup.pickup_id)
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)
        return pickup

    def confirm_pickup(self, cmd: ConfirmPickup) -> None:
        """Confirm courier possession at the door (atomic).

        Steps: validate evidence present (PK5 pre-UoW fast-fail) -> load
        pickup + its RMA (via ``pickup.return_id``) ->
        ``pickup.confirm_pickup`` (courier scan + photo -> PickedUp, PK3/PK5) ->
        ``rma.on_pickup_confirmed`` -> ``rma.receive_goods`` (GoodsReceived)
        -> persist + collect.

        Why evidence is mandatory: the customer holds no return label, so the
        courier scan + photo is their only proof of collection (PK5). Why the
        RMA is loaded transitively (not by command ID): the command names the
        Fulfillment leg; the Returns leg is derived — keeping one pickup
        bound to exactly one RMA (PK1).

        Note: unlike ``ReturnAppService.complete_pickup`` this ID-based
        handler performs no FileBucket I/O — photo bytes are stored by the
        caller/services path; the domain only checks the evidence reference.

        Args:
            cmd: ``ConfirmPickup`` with pickup ID + ``PickupEvidence``.

        Raises:
            DomainError: Missing evidence (PK5); pickup not confirmable.
            KeyError: Unknown pickup/RMA ID.

        Events: ``PickupConfirmed``, ``ReturnGoodsReceived`` (post-commit).
        """
        # Pre-UoW guard: PK5 evidence is mandatory — reject early.
        if cmd.evidence is None:
            raise DomainError("ConfirmPickup needs evidence")
        with self._uow() as uow:
            # Load the Fulfillment leg, then derive the Returns leg from it.
            pickup = uow.pickups.get(cmd.pickup_id)
            rma = uow.returns.get(pickup.return_id)
            # Possession transfer, then goods-received (restock still waits
            # for settle per S4/RT5).
            pickup.confirm_pickup(cmd.evidence)
            rma.on_pickup_confirmed(pickup.pickup_id, cmd.evidence)
            rma.receive_goods()
            uow.pickups.add(pickup)
            uow.returns.add(rma)
            uow.collect(rma, pickup)

    def settle_return(self, cmd: SettleReturn) -> RefundBreakdown:
        """Settle an RMA: pro-rata refund + restock + order/coupon (atomic).

        Steps: load RMA + its order (via ``rma.order_id``) -> ``rma.settle``
        (§5 pro-rata math -> ``RefundBreakdown``) -> ``payment.refund``
        (partial keeps ``Captured`` per PM4) -> ``stock.restock`` per line
        (S4: only now, after courier possession) -> best-effort coupon lookup
        (``KeyError`` -> None) -> full? ``order.refund_completed`` +
        ``coupon.unredeem`` (CO7/RT6) : ``order.accept_return`` -> persist +
        collect.

        Why refund (not void): the payment was captured at shipment — money
        moved, so only ``refund`` is legal (PM1: Σ refunds ≤ captured). Why
        restock here: S4/RT5 forbid restock before the courier has the goods
        and settlement prevents double-restock on retry. Why full-vs-partial:
        full returns close the order (``Refunded`` terminal) and restore the
        coupon; partials keep both alive. Why tolerate a missing coupon: a
        code deleted after placement must not block money-back.

        Args:
            cmd: ``SettleReturn`` with the RMA ID.

        Returns:
            The pro-rata ``RefundBreakdown`` (goods/tax/shipping/total).

        Raises:
            KeyError: Unknown RMA/order/payment. DomainError: RMA not
                settleable (already Refunded, RT4); refund exceeds captured.

        Events: ``ReturnRefunded``, ``PaymentRefunded``, ``StockRestocked``
            × N, ``OrderRefunded`` (full) / return-accepted (partial),
            ``CouponUnredeemed`` (full + coupon found) — post-commit.
        """
        with self._uow() as uow:
            # 1. Load the Returns leg, then derive the Order leg from it.
            rma = uow.returns.get(cmd.rma_id)
            order = uow.orders.get(rma.order_id)
            # 2. Domain computes the money (pure function of frozen facts).
            breakdown = rma.settle()
            # 3. Move money back (generic Payment: amount + reason only).
            payment = uow.payments.get(order.payment_ref)
            payment.refund(breakdown.total, f"return {rma.rma_id}")
            # 4. Return goods to sellable inventory (audited by rma_id).
            for line in rma.lines:
                uow.stocks.get(line.product_id).restock(
                    rma.rma_id, {line.product_id: line.quantity}
                )
            # 5. Best-effort coupon lookup (never blocks settlement).
            coupon = None
            if order.coupon_code:
                try:
                    coupon = uow.coupons.find_by_code(order.coupon_code)
                except KeyError:
                    coupon = None
            # 6. Full vs partial close-out (CO7/RT6 coupon re-entitlement).
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
                rma, order, payment, *([coupon] if coupon else []),
                *uow.stocks.list(),
            )
        return breakdown


def register_command_handlers(
    bus: CommandBus,
    uow_factory: UoWFactory,
    clock: Clock | None = None,
    tracer=None,
    jobs: AsyncJob | None = None,
    files: FileBucket | None = None,
) -> CommandHandlers:
    """Create handlers and bind every user-initiated Command to the bus.

    Composition helper (called by ``infra.container``): constructs one
    ``CommandHandlers`` with the given UoW factory + collaborators and
    registers each ``domain.commands`` type to exactly one handler method,
    enforcing the one-command-one-handler dispatch rule of the ``CommandBus``.

    Args:
        bus: The ``CommandBus`` to register routes on.
        uow_factory: Factory producing one atomic ``UnitOfWork`` per call.
        clock: Deterministic time source (defaults to ``SystemClock``).
        tracer: Optional observability span emitter.
        jobs: Optional post-commit async-job queue (``order.placed``).
        files: Optional evidence file bucket (pickup photos).

    Returns:
        The wired ``CommandHandlers`` instance (also usable directly in
        tests without going through ``bus.dispatch``).
    """
    handlers = CommandHandlers(uow_factory, clock, tracer, jobs, files)
    # Catalog (1) + Shop/cart (6) + checkout/cancel/close (3) + Fulfillment
    # (2) + Returns (5): every user intent gets exactly one route.
    bus.register(PublishProduct, handlers.publish_product)
    bus.register(OpenCart, handlers.open_cart)
    bus.register(AddToCart, handlers.add_to_cart)
    bus.register(UpdateCartLine, handlers.update_cart_line)
    bus.register(RemoveCartLine, handlers.remove_cart_line)
    bus.register(ApplyCoupon, handlers.apply_coupon)
    bus.register(RemoveCoupon, handlers.remove_coupon)
    bus.register(PlaceOrder, handlers.place_order)
    bus.register(CancelOrder, handlers.cancel_order)
    bus.register(CloseCart, handlers.close_cart)
    bus.register(HandToCarrier, handlers.hand_to_carrier)
    bus.register(ConfirmDelivery, handlers.confirm_delivery)
    bus.register(RequestReturn, handlers.request_return)
    bus.register(RejectReturn, handlers.reject_return)
    bus.register(SchedulePickup, handlers.schedule_pickup)
    bus.register(ConfirmPickup, handlers.confirm_pickup)
    bus.register(SettleReturn, handlers.settle_return)
    return handlers


__all__ = ["CommandHandlers", "register_command_handlers", "AUTO_COURIER"]
