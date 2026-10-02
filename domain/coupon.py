"""Shop / Pricing context — the Coupon aggregate.

Bounded context: Shop (Pricing side). Owns discount instruments; the
``Cart`` holds at most a *reference* to one coupon (CO6) and delegates
amount math here, so the check (cart preview) and the commit (atomic
redeem at ``PlaceOrder``) stay consistent under concurrency.

Responsibility: validate eligibility, compute the ``Discount`` VO for a
given subtotal, and count redemptions exactly once per (customer, order).

Key invariants:
  CO1  code unique (enforced context-wide; aggregate enforces immutability)
  CO2  applicable only when validFrom <= now <= validTo and redemptions < usageLimit
  CO3  one redemption per customer (idempotent by customer — no double-dip)
  CO4  redemptions increments only at PlaceOrder time (redeem); the cart
       apply is a *preview* (validate) and must be re-validated at placement
  CO5  a percentage coupon caps its own discount at 100% of the pre-tax subtotal
  CO6  one coupon per order (single-coupon policy) — enforced by the Cart
  CO7  a *complete* return triggers un-redeem (customer regains the code);
       a *partial* return keeps the redemption burned
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .aggregate import Aggregate, _next_id
from .errors import CouponValidationError, InvariantViolation
from .events import CouponUnredeemed
from .value_objects import Discount, Money

FREE_SHIPPING = "FREE_SHIPPING"
"""Coupon type + discount rule label: zeroes the cart shipping fee."""
PERCENTAGE = "PCT"
"""Coupon type: percentage-off (``value`` is an int in (0, 100])."""
FIXED_AMOUNT = "FIXED"
"""Coupon type: fixed Money-off (``value`` is a ``Money``)."""


@dataclass
class _Redemption:
    """Child of Coupon: one burned redemption (customer x order).

    Role: the audit row behind ``redemptions``. Keyed by ``order_id`` in
    the parent dict so ``redeem`` is idempotent per order and ``unredeem``
    (CO7) can delete exactly the fully-returned order's row.
    """

    customer_id: str
    order_id: str


class Coupon(Aggregate):
    """Aggregate root: a discount instrument (identity = coupon_id).

    Role: the concurrency-safe discount — the *check* (``validate`` preview
    on the Cart side) and the *commit* (atomic ``redeem`` at ``PlaceOrder``)
    both live here so a race between two sessions cannot double-spend a
    limited code (CO4).

    Aggregate membership: root; children = ``_Redemption`` rows
    (``order_id -> _Redemption``, enforcing CO3/CO7). Referenced (not
    contained) by ``Cart._applied_coupon`` (CO6: at most one).

    Invariants enforced: CO1 (upper-cased immutable code), CO2 (window +
    usage limit + min order value), CO3 (one per customer), CO4 (redeem-only
    counting), CO5 (percentage cap), CO7 (unredeem on complete return).

    Example usage:
        coupon.validate(customer_id, cart.subtotal(), now)  # preview (CO4)
        coupon.redeem(customer_id, order_id)                # commit at placement
        coupon.unredeem(customer_id, order_id)              # complete return only
    """

    prefix = "cpn"
    """ID namespace for coupons (``cpn-<hex>``)."""

    def __init__(self, code: str, type: str, value: int | Money,
                 valid_from: datetime, valid_to: datetime,
                 usage_limit: int | None = None,
                 min_order_value: Money | None = None) -> None:
        """Create a coupon instrument in ``Active`` status.

        Args:
            code: Business code (CO1). Uppercased and frozen; must be
                non-empty/non-blank. Uniqueness is enforced context-wide
                (repository), not inside the aggregate.
            type: One of ``PERCENTAGE`` (``PCT``), ``FIXED_AMOUNT``
                (``FIXED``), ``FREE_SHIPPING``.
            value: ``int`` percent for ``PERCENTAGE`` (CO5: 0 < value <= 100),
                ``Money`` for ``FIXED_AMOUNT``; ignored for free shipping.
            valid_from: Start of the redemption window (CO2, inclusive).
            valid_to: End of the redemption window (CO2, inclusive).
            usage_limit: Max total redemptions (CO2), or ``None`` for
                unlimited. Must be >= 1 when given.
            min_order_value: Minimum cart subtotal for eligibility (CO2),
                or ``None`` for no threshold.

        Raises:
            InvariantViolation: On empty code (CO1), unknown type, out-of-
                range percentage (CO5), negative fixed value, inverted
                window (``valid_from > valid_to``), or ``usage_limit < 1``.

        Events emitted: none (creation is announced by the Shop context,
            not the root itself).
        """
        super().__init__()
        if not code or not code.strip():
            raise InvariantViolation("CO1: code must be non-empty")
        if type not in (PERCENTAGE, FIXED_AMOUNT, FREE_SHIPPING):
            raise InvariantViolation(f"unknown coupon type {type!r}")
        if isinstance(value, int):
            # Percentage range is structural (CO5): >100% is impossible by
            # construction, so no computed cap can ever exceed the subtotal.
            if type == PERCENTAGE and not 0 < value <= 100:
                raise InvariantViolation("CO5: percentage must be in (0, 100]")
            if type == FIXED_AMOUNT and value < 0:
                raise InvariantViolation("FIXED value must be >= 0")
        if valid_from > valid_to:
            raise InvariantViolation("valid_from must be <= valid_to")
        if usage_limit is not None and usage_limit < 1:
            raise InvariantViolation("usage_limit must be >= 1 or None")
        self._coupon_id = _next_id(self.prefix)
        self._code = code.upper()      # CO1: immutable
        self._type = type
        self._value = value            # int (pct) or Money
        self._valid_from = valid_from
        self._valid_to = valid_to
        self._usage_limit = usage_limit
        self._min_order_value = min_order_value
        self._redemptions: dict[str, _Redemption] = {}   # order_id -> Redemption (CO3/CO7)
        self._status = "Active"

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        """Return the coupon identity string.

        Returns:
            The ``coupon_id`` (``cpn-<hex>``); the business ``code`` is a
            separate lookup key (see ``code`` property).
        """
        return self._coupon_id

    @property
    def code(self) -> str:
        """Return the upper-cased immutable business code.

        Returns:
            The CO1 code (e.g. ``"SAVE20"``) used in ``ApplyCoupon``.
        """
        return self._code

    @property
    def redemptions(self) -> int:
        """Return the number of burned redemptions.

        Returns:
            ``len(redemptions)`` — the CO2/usage-limit numerator.
        """
        return len(self._redemptions)

    @property
    def status(self) -> str:
        """Return the lifecycle status.

        Returns:
            ``"Active"``, ``"Deactivated"``, or ``"Expired"``. Only
            ``Active`` coupons validate/redeem.
        """
        return self._status

    def is_redeemed_by(self, customer_id: str, order_id: str) -> bool:
        """Check whether this (customer, order) pair burned the coupon.

        Args:
            customer_id: Opaque customer reference.
            order_id: Order that allegedly redeemed.

        Returns:
            True iff a redemption row exists for ``order_id`` *and* it
            belongs to ``customer_id`` (guards against order-id reuse
            across customers — CO1).
        """
        r = self._redemptions.get(order_id)
        return r is not None and r.customer_id == customer_id

    # -- behavior -------------------------------------------------------------
    def validate(self, customer_id: str, cart_total: Money, now: datetime) -> Discount:
        """Preview (CO4: does NOT increment). Raises CouponValidationError on CO2/CO3.

        The cart-side *check*: safe to call repeatedly (e.g. on every
        ``apply_coupon`` or quote) because it never mutates redemption
        state. The placement path must call this again and then ``redeem``
        atomically, since another session could race between preview and
        commit (CO4).

        Args:
            customer_id: Opaque customer reference (CO3 key).
            cart_total: Pre-discount subtotal to compute against and to
                compare with ``min_order_value``.
            now: Wall-clock instant for the validity-window check (CO2).

        Returns:
            The computed ``Discount`` VO for this subtotal.

        Raises:
            CouponValidationError: If inactive, outside the validity window
                or usage limit (CO2), below ``min_order_value`` (CO2), or
                already redeemed by this customer (CO3).

        Events emitted: none (preview only).
        Invariant refs: CO2, CO3, CO4.
        """
        if self._status != "Active":
            raise CouponValidationError(f"coupon {self._code} is {self._status}")
        if not (self._valid_from <= now <= self._valid_to):
            raise CouponValidationError(f"CO2: coupon {self._code} outside validity window")
        if self._usage_limit is not None and self.redemptions >= self._usage_limit:
            raise CouponValidationError(f"CO2: coupon {self._code} usage limit reached")
        if self._min_order_value is not None and cart_total < self._min_order_value:
            raise CouponValidationError(
                f"CO2: cart total {cart_total} below min order value {self._min_order_value}"
            )
        # CO3 scan: one redemption per customer across ALL orders — a
        # linear scan is fine (redemption sets are small; correctness over
        # index cleverness).
        for r in self._redemptions.values():
            if r.customer_id == customer_id:
                raise CouponValidationError(
                    f"CO3: customer {customer_id} already redeemed {self._code}"
                )
        return self._compute_discount(cart_total)

    def compute_discount(self, cart_total: Money) -> Discount:
        """The raw discount for a given subtotal (used by the Cart's quote).

        Pure recomputation without any eligibility re-check — the Cart
        calls this on every ``quote()`` because the cart can change after
        the coupon was applied (C3 comment in ``cart.py``).

        Args:
            cart_total: Pre-discount subtotal to compute against.

        Returns:
            The ``Discount`` VO (amount already capped — CO5).
        """
        return self._compute_discount(cart_total)

    def _compute_discount(self, cart_total: Money) -> Discount:
        """Shared amount math behind ``validate`` and ``compute_discount``.

        Args:
            cart_total: Pre-discount subtotal.

        Returns:
            ``Discount`` per type: percentage via ``times_fraction``
            (CO5-capped), fixed via minor-unit min-cap, free shipping as
            zero-money with the ``FREE_SHIPPING`` rule label (the Cart
            interprets the label to zero the fee).
        """
        if self._type == PERCENTAGE:
            # CO5: cap at 100% of the pre-tax subtotal
            amount = cart_total.times_fraction(self._value, 100)
            if amount > cart_total:
                amount = cart_total
            return Discount(amount, f"{PERCENTAGE}-{self._value}")
        if self._type == FIXED_AMOUNT:
            # can't discount more than the subtotal (compare in minor units;
            # don't rely on min() picking by __lt__ identity)
            amount = Money(min(self._value.minor, cart_total.minor), cart_total.currency)
            return Discount(amount, f"{FIXED_AMOUNT}-{self._value.minor}")
        return Discount(Money(0, cart_total.currency), FREE_SHIPPING)

    def redeem(self, customer_id: str, order_id: str) -> None:
        """CO3/CO4: the only place redemptions increment. Idempotent per (customer, order).

        The placement-side *commit*: called once inside the ``PlaceOrder``
        atomic UoW after re-validation. This is the single writer of the
        redemption set, which is what makes concurrent placements safe.

        Args:
            customer_id: Redeeming customer (CO3 key).
            order_id: New order burning the coupon (idempotency key).

        Raises:
            CouponValidationError: If inactive, usage limit reached (CO2),
                or this customer already redeemed on another order (CO3).
            InvariantViolation: If ``order_id`` was already redeemed by a
                *different* customer (CO1 — order-id reuse bug).

        Events emitted: none (consumption is observed via ``OrderPlaced``;
            restoration emits ``CouponUnredeemed`` from ``unredeem``).
        Invariant refs: CO1, CO2, CO3, CO4.
        """
        if self._status != "Active":
            raise CouponValidationError(f"coupon {self._code} is {self._status}")
        existing = self._redemptions.get(order_id)
        if existing is not None:
            if existing.customer_id == customer_id:
                return  # idempotent: same customer re-confirming the same order
            raise InvariantViolation(f"CO1: order {order_id} already redeemed by another customer")
        # Re-check the cap at commit time (CO4): the preview may be stale
        # because a concurrent placement burned the last slot first.
        if self._usage_limit is not None and self.redemptions >= self._usage_limit:
            raise CouponValidationError(f"CO2: coupon {self._code} usage limit reached")
        for r in self._redemptions.values():
            if r.customer_id == customer_id:
                raise CouponValidationError(
                    f"CO3: customer {customer_id} already redeemed {self._code} (one per customer)"
                )
        self._redemptions[order_id] = _Redemption(customer_id, order_id)

    def unredeem(self, customer_id: str, order_id: str) -> None:
        """CO7: mirror of redeem — only on a *complete* return. Idempotent.

        Called by the Returns flow when a full refund settles (RT6): the
        redemption row is deleted so the customer regains eligibility.
        Partial returns must NOT call this (redemption stays burned).

        Args:
            customer_id: Customer regaining eligibility.
            order_id: Fully-returned order whose row is removed.

        Returns:
            None. No-op when no matching row exists or the row belongs to
            another customer (idempotent — safe under retried settlements).

        Events emitted: ``CouponUnredeemed(code, customer_id)`` on actual
            removal (drives Notification + eligibility views).
        Invariant refs: CO7, RT6.
        """
        r = self._redemptions.get(order_id)
        if r is None or r.customer_id != customer_id:
            return  # idempotent / no-op
        del self._redemptions[order_id]
        self.raise_event(CouponUnredeemed(self._code, customer_id))

    def deactivate(self) -> None:
        """Deactivate the coupon (manual kill-switch).

        Raises:
            InvariantViolation: If already ``Deactivated`` (explicit
                rejection so double-deactivate bugs surface).

        Events emitted: none.
        """
        if self._status == "Deactivated":
            raise InvariantViolation("already deactivated")
        self._status = "Deactivated"

    def expire(self) -> None:
        """Expire the coupon (end of validity lifecycle).

        Raises:
            InvariantViolation: If already ``Expired``.

        Events emitted: none.
        """
        if self._status == "Expired":
            raise InvariantViolation("already expired")
        self._status = "Expired"
