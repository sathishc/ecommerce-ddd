"""Shop context — the Cart aggregate.

Bounded context: Shop (pre-order experience: cart manipulation,
single-coupon discounting, pricing including shipping, and checkout
orchestration). Checkout produces ``PlaceOrder`` (carrying the coupon
discount + shipping result) for the Order context (Customer/Supplier).

Responsibility: own the mutable pre-order collection — lines, at most one
coupon reference (CO6), destination, and the derived price breakdown —
until ``checkout`` freezes a snapshot for the ``Order`` (O1/O2).

Key invariants:
  C1  quantity >= 1 per line; at most one line per Product (adding the same
      product increases quantity)
  C2  total is always *derived* (quote): lines - discount + tax + shipping;
      never stored by hand
  C3  at most one coupon; applying a new one replaces the old; a coupon that
      doesn't validate is REJECTED, not silently ignored
  C4  a CheckedOut cart is immutable
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .aggregate import Aggregate, _next_id
from .errors import CouponValidationError, InvalidStateTransition, InvariantViolation
from .events import (CartAbandoned, CartCheckedOut, CartCreated, CartItemAdded, CartItemChanged,
                     CartLineRemoved, CouponApplied, CouponRemoved)
from .coupon import Coupon, FREE_SHIPPING
from .product import Product
from .value_objects import Address, Discount, Money, Quote

TAX_RATE_NUM = 0   # pluggable: numerator of the tax fraction (0 = no tax in v1)
TAX_RATE_DEN = 100
"""Tax fraction denominator (percent base). ``quote`` scales via ``times_fraction``."""
DEFAULT_SHIPPING = 500  # minor units — flat rate (single carrier, no routing)
"""Flat shipping fee in minor units (v1: single carrier, no routing)."""


@dataclass
class _CartLine:
    """Child of Cart: a Product + Quantity (immutable after placement).

    Role: one ``product_id`` entry (C1) snapshotting the unit price at
    add-time, so later catalog ``change_price`` calls never rewrite an
    open cart's lines (mirrors O2 freeze at the order level).

    Aggregate membership: owned by ``Cart`` (``product_id -> _CartLine``);
    mutated only through ``Cart`` root methods. ``quantity`` is a plain
    mutable int (the root guards ``>= 1``); price snapshot is fixed at
    construction.
    """

    product_id: str
    """Referenced product identity."""
    unit_price: Money
    """Price snapshot taken from ``Product.price`` at add-time."""
    quantity: int
    """Units in the cart (C1: always >= 1; root methods enforce)."""

    @property
    def line_total(self) -> Money:
        """Return the extended line price (unit x quantity).

        Returns:
            New ``Money`` in the unit price's currency. Pure derivation —
            no rounding involved (integer multiplication).
        """
        return Money(self.unit_price.minor * self.quantity, self.unit_price.currency)


class Cart(Aggregate):
    """Aggregate root: the customer's pre-order collection (identity = cart_id).

    Role: the Shop context's mutable basket between browsing and checkout —
    line edits, single-coupon previewing (C3), derived quoting (C2), and
    the terminal ``checkout``/``abandon`` transitions (C4).

    Aggregate membership: root; children = ``_CartLine`` rows plus a
    reference (not containment) to at most one ``Coupon`` (CO6). Produces
    the checkout snapshot consumed by ``Order.place``.

    Invariants enforced: C1 (line uniqueness + qty >= 1), C2 (derived
    totals only), C3 (single coupon, reject-on-invalid), C4 (CheckedOut /
    Abandoned immutability).

    Example usage:
        cart = Cart(customer_ref="cust-1")
        cart.add_product(product, quantity=2)
        cart.apply_coupon(coupon, now)   # raises CouponValidationError if invalid (C3)
        quote = cart.quote()
        snapshot = cart.checkout(destination)
    """

    prefix = "cart"
    """ID namespace for carts (``cart-<hex>``)."""

    def __init__(self, customer_ref: str) -> None:
        """Open a new cart for a customer.

        Args:
            customer_ref: Opaque customer reference (external identity
                context). Must be non-empty.

        Raises:
            InvariantViolation: If ``customer_ref`` is empty.

        Events emitted: ``CartCreated(cart_id)``.
        """
        super().__init__()
        if not customer_ref:
            raise InvariantViolation("a cart needs a customer reference")
        self._cart_id = _next_id(self.prefix)
        self._customer_ref = customer_ref
        self._lines: dict[str, _CartLine] = {}   # product_id -> line (C1)
        self._applied_coupon: Coupon | None = None
        self._applied_coupon_code: str | None = None
        self._destination: Address | None = None
        self._status = "Open"
        self.raise_event(CartCreated(self._cart_id))

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        """Return the cart identity string.

        Returns:
            The ``cart_id`` (``cart-<hex>``).
        """
        return self._cart_id

    @property
    def customer_ref(self) -> str:
        """Return the owning customer reference.

        Returns:
            The opaque customer ref given at construction (immutable).
        """
        return self._customer_ref

    @property
    def status(self) -> str:
        """Return the lifecycle status.

        Returns:
            ``"Open"``, ``"CheckedOut"``, or ``"Abandoned"``. Only
            ``"Open"`` carts mutate (C4).
        """
        return self._status

    @property
    def applied_coupon_code(self) -> str | None:
        """Return the applied coupon code, if any.

        Returns:
            The coupon ``code`` (C3/CO6: at most one) or ``None``.
        """
        return self._applied_coupon_code

    @property
    def destination(self) -> Address | None:
        """Return the shipping destination, if set.

        Returns:
            The ``Address`` set via ``set_destination``/``checkout``, or
            ``None`` when not yet provided.
        """
        return self._destination

    def set_destination(self, destination: Address) -> None:
        """Set the shipping destination on an open cart.

        Args:
            destination: Delivery ``Address`` value object. Must not be
                ``None`` (present-but-empty addresses are the caller's
                responsibility in v1 — no postal validation).

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            InvariantViolation: If ``destination`` is ``None``.

        Events emitted: none (destination is read state until checkout).
        """
        self._open()
        if destination is None:
            raise InvariantViolation("C: a destination cannot be empty")
        self._destination = destination

    def _open(self) -> None:
        """Guard that the cart is still mutable.

        Raises:
            InvalidStateTransition: If status is not ``"Open"`` (C4 —
                ``CheckedOut``/``Abandoned`` carts are terminal/immutable).
        """
        if self._status != "Open":
            raise InvalidStateTransition(f"C4: cart is {self._status}; only Open carts mutate")

    # -- cart manipulation (C1) ---------------------------------------------
    def add_product(self, product: Product, quantity: int = 1) -> None:
        """Add a product line, or bump quantity if already present (C1).

        Args:
            product: The ``Product`` to add. Must be ``Active`` — carts
                never hold ``Discontinued`` items.
            quantity: Units to add. Must be >= 1 (C1).

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            InvariantViolation: If ``quantity < 1`` (C1), the product is
                not ``Active``, or (implicitly) on currency/price misuse.

        Events emitted: ``CartItemAdded`` for a new line, ``CartItemChanged``
            (with the merged quantity) when the product was already present.
        Invariant refs: C1, C4.
        """
        self._open()
        if quantity < 1:
            raise InvariantViolation("C1: quantity must be >= 1")
        if product.status != "Active":
            raise InvariantViolation(f"cannot add a {product.status} product")
        existing = self._lines.get(product.product_id)
        if existing is None:
            # New line: snapshot the live catalog price (later price changes
            # don't rewrite this line — mirrors O2 at order level).
            self._lines[product.product_id] = _CartLine(product.product_id, product.price, quantity)
            self.raise_event(CartItemAdded(self._cart_id, product.product_id, quantity))
        else:
            existing.quantity += quantity   # C1: one line per product
            self.raise_event(CartItemChanged(self._cart_id, product.product_id, existing.quantity))

    def change_quantity(self, product_id: str, quantity: int) -> None:
        """Set an existing line's quantity (C1).

        Args:
            product_id: Identity of the line to change.
            quantity: New quantity. Must be >= 1 (C1) — use
                ``remove_line`` to drop a line.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            InvariantViolation: If ``quantity < 1`` (C1) or no line exists
                for ``product_id``.

        Events emitted: ``CartItemChanged(cart_id, product_id, quantity)``.
        Invariant refs: C1, C4.
        """
        self._open()
        if quantity < 1:
            raise InvariantViolation(f"C1: quantity must be >= 1, got {quantity}")
        line = self._lines.get(product_id)
        if line is None:
            raise InvariantViolation(f"no line for product {product_id}")
        line.quantity = quantity
        self.raise_event(CartItemChanged(self._cart_id, product_id, quantity))

    def remove_line(self, product_id: str) -> None:
        """Remove a line entirely.

        Args:
            product_id: Identity of the line to drop.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            InvariantViolation: If no line exists for ``product_id``.

        Events emitted: ``CartLineRemoved(cart_id, product_id)``.
        Invariant refs: C4.
        """
        self._open()
        if product_id not in self._lines:
            raise InvariantViolation(f"no line for product {product_id}")
        del self._lines[product_id]
        self.raise_event(CartLineRemoved(self._cart_id, product_id))

    # -- coupon (C3) -----------------------------------------------------------
    def apply_coupon(self, coupon: Coupon, now: datetime) -> Discount:
        """C3: preview + replace. Validation failure RAISES (rejected, not ignored).

        Single-coupon policy (CO6): the new coupon replaces any previously
        applied one (emitting ``CouponRemoved`` first when swapping between
        *different* coupon objects). Re-applying the *same* object refreshes
        the preview without a removal event.

        Args:
            coupon: The ``Coupon`` instrument to preview-apply. Validated
                via ``coupon.validate`` against this cart's customer,
                subtotal, and ``now``.
            now: Wall-clock instant for the CO2 validity-window check.

        Returns:
            The preview ``Discount`` VO for the current subtotal.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            CouponValidationError: On CO2/CO3 failure — the coupon is
                REJECTED (C3), never silently ignored; prior coupon (if any)
                stays applied.

        Events emitted: ``CouponRemoved`` (only when replacing a *different*
            coupon) then ``CouponApplied(cart_id, code)``.
        Invariant refs: C3, CO2, CO3, CO6.
        """
        self._open()
        discount = coupon.validate(self._customer_ref, self.subtotal(), now)  # may raise CO2/CO3
        # Swap path: the old coupon was valid when applied, so its removal
        # is announced; the new apply is announced below. Same-object
        # re-apply skips the removal (idempotent refresh, no event churn).
        if self._applied_coupon is not None and self._applied_coupon is not coupon:
            self.raise_event(CouponRemoved(self._cart_id))
        self._applied_coupon = coupon
        self._applied_coupon_code = coupon.code
        self.raise_event(CouponApplied(self._cart_id, coupon.code))
        return discount

    def remove_coupon(self) -> None:
        """Detach the currently applied coupon, if any.

        No-op (no event) when no coupon is applied — safe to call
        unconditionally from handlers/UI toggles.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).

        Events emitted: ``CouponRemoved(cart_id)`` when a coupon was
            actually detached.
        Invariant refs: C3, C4.
        """
        self._open()
        if self._applied_coupon is None:
            return
        self._applied_coupon = None
        self._applied_coupon_code = None
        self.raise_event(CouponRemoved(self._cart_id))

    # -- pricing (C2: everything derived) ----------------------------------------
    def subtotal(self) -> Money:
        """Pre-discount, pre-tax, pre-shipping line total (the RT3 denominator).

        Returns:
            Sum of ``line_total`` across lines, in the first line's
            currency (single-currency cart assumption — mixed currencies
            raise via ``Money`` guards).

        Raises:
            InvariantViolation: If the cart is empty (no meaningful total).

        Invariant refs: C2.
        """
        if not self._lines:
            raise InvariantViolation("quote of an empty cart")
        # Single-currency assumption: every line snapshots a same-currency
        # price; the first line's currency anchors the sum.
        cur = next(iter(self._lines.values())).unit_price.currency
        return Money(sum(l.line_total.minor for l in self._lines.values()), cur)

    def _applied_discount(self, subtotal: Money) -> Discount:
        """Recompute the coupon's discount against the live subtotal.

        Args:
            subtotal: Current pre-discount subtotal (from ``subtotal()``).

        Returns:
            The ``Discount`` VO, or a zero ``NONE`` discount when no coupon
            is applied. Never exceeds the subtotal (clamped by CO5 / the
            fixed-amount min-cap upstream).
        """
        if self._applied_coupon is None:
            return Discount(Money(0, subtotal.currency), "NONE")
        # C3: validation happens once, at apply time (apply_coupon -> validate).
        # The quote only *recomputes* the amount against the current subtotal,
        # because the cart can change after the coupon is applied. (No re-check
        # of the validity window against wall clock — that would make a valid
        # cart un-quoteable once the coupon window lapses.)
        return self._applied_coupon.compute_discount(subtotal)

    def shipping_fee(self) -> Money:
        """Flat rate, zeroed when a FreeShipping coupon applies.

        Returns:
            ``Money(0, currency)`` when the applied coupon's recomputed
            rule is ``FREE_SHIPPING``; otherwise the ``DEFAULT_SHIPPING``
            flat rate in the cart's currency.

        Raises:
            InvariantViolation: If the cart is empty (via ``subtotal()`` —
                currency is undefined without lines).
        """
        cur = self.subtotal().currency
        if self._applied_coupon is not None:
            d = self._applied_discount(self.subtotal())
            if d.rule == FREE_SHIPPING:
                return Money(0, cur)
        return Money(DEFAULT_SHIPPING, cur)

    def quote(self, tax_rate_num: int = TAX_RATE_NUM, tax_rate_den: int = TAX_RATE_DEN) -> Quote:
        """C2: total is computed *inside* the aggregate; never set from outside.

        Formula: ``total = (subtotal - discount) + tax + shipping`` where
        ``tax = discounted_goods x tax_rate`` (round-half-up) and shipping
        comes from ``shipping_fee()`` (free-shipping aware).

        Args:
            tax_rate_num: Tax numerator (default ``TAX_RATE_NUM`` = 0 in v1).
            tax_rate_den: Tax denominator (default 100 = percent base).

        Returns:
            A ``Quote`` VO breakdown (subtotal, discount, tax, shipping,
            total) — a snapshot, not stored state.

        Raises:
            InvariantViolation: If the cart is empty (C2), or the discount
                exceeds the subtotal (C2 last-line defense — CO5 already
                caps, but the aggregate holds regardless of coupon
                correctness).

        Invariant refs: C2.
        """
        if not self._lines:
            raise InvariantViolation("C2: cannot quote an empty cart")
        subtotal = self.subtotal()
        discount = self._applied_discount(subtotal)
        # C2: discount never exceeds the subtotal (CO5 already caps, but the
        # aggregate must hold regardless of the coupon's correctness)
        if discount.amount > subtotal:
            raise InvariantViolation("C2: discount exceeds subtotal")
        discount_goods = subtotal.minus(discount.amount)
        tax = discount_goods.times_fraction(tax_rate_num, tax_rate_den)
        shipping = self.shipping_fee()
        total = discount_goods.plus(tax).plus(shipping)
        return Quote(subtotal=subtotal, discount=discount, tax=tax,
                     shipping_fee=shipping, total=total)

    # -- checkout -----------------------------------------------------------------
    def abandon(self) -> None:
        """Close without buying (customer walked away) → `CartAbandoned`.

        Terminal + immutable like checkout (C4): no further lines, coupons,
        quotes, or checkout calls are permitted afterwards.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).

        Events emitted: ``CartAbandoned(cart_id)``.
        Invariant refs: C4.
        """
        self._open()
        self._status = "Abandoned"  # terminal + immutable, like checkout
        self.raise_event(CartAbandoned(self._cart_id))

    def checkout(self, destination: Address) -> dict:
        """Freeze the cart; the caller places the Order from this snapshot (C4).

        Marks the cart ``CheckedOut`` (terminal/immutable) and returns a
        plain-dict snapshot — lines, destination, quote, coupon, customer —
        that ``Order.place`` consumes. The quote is recomputed here so the
        frozen total always reflects the final lines + coupon.

        Args:
            destination: Delivery address. Used only as a fallback when no
                destination was set via ``set_destination`` (an explicitly
                passed address never overwrites a previously set one).

        Returns:
            Snapshot dict with ``lines`` (``[(product_id, qty, unit_price)]``),
            ``destination``, ``quote`` (``Quote`` VO), ``coupon``
            (applied ``Coupon`` or ``None``), ``customer``.

        Raises:
            InvalidStateTransition: If the cart is not ``Open`` (C4).
            InvariantViolation: If the cart is empty, or quoting fails
                (C2 — e.g. discount exceeds subtotal).

        Events emitted: ``CartCheckedOut(cart_id)``.
        Invariant refs: C2, C4.
        """
        self._open()
        if not self._lines:
            raise InvariantViolation("cannot checkout an empty cart")
        if self._destination is None:
            self._destination = destination
        q = self.quote()
        self._status = "CheckedOut"
        self.raise_event(CartCheckedOut(self._cart_id))
        return {
            "lines": [(lid, qty, unit) for lid, qty, unit in self.lines],
            "destination": self._destination,
            "quote": q,
            "coupon": self._applied_coupon,
            "customer": self._customer_ref,
        }

    @property
    def lines(self) -> list[tuple[str, int, Money]]:
        """Return a snapshot of the cart lines.

        Returns:
            List of ``(product_id, quantity, unit_price)`` tuples in
            insertion order. A fresh list each call — mutating it does not
            affect the aggregate (C1 stays guarded behind root methods).
        """
        return [(lid, l.quantity, l.unit_price) for lid, l in self._lines.items()]
