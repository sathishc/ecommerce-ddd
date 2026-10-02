"""Value Objects — immutable, defined by their attributes.

Bounded context: shared kernel. ``Money`` is the ONE shared kernel across
all contexts (Catalog, Shop, Order, Payment, Fulfillment, Returns);
``Address``, ``Discount``, ``Quote``, ``RefundBreakdown`` and
``PickupEvidence`` are likewise dependency-free and safe to copy across
context boundaries.

Responsibility: carry *descriptive* state with no identity and no
lifecycle. Two VOs with equal attributes are interchangeable; all
operations return new instances (no in-place mutation).

Key invariants: ``Money.minor >= 0`` always (enforced in
``__post_init__``); currency must match on every binary operation;
fractional scaling uses round-half-up. ``Quote``/``RefundBreakdown`` are
*derived* snapshots (C2/RT3) — computed by the owning aggregate, never
hand-assigned.

Rule of thumb: if two instances with equal attributes are interchangeable
-> VO (``Money``, ``Address``, ``Discount``). If deleting it loses history
-> Entity (``Payment``, ``Return``, ``Pickup``, ``Reservation``).
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import InvariantViolation


@dataclass(frozen=True)
class Money:
    """THE shared kernel: minor units + ISO currency. Immutable, always >= 0.

    Role: the single Money representation for every context — prices,
    subtotals, discounts, taxes, shipping fees, refunds. Stored as integer
    minor units (cents) to avoid float rounding.

    Aggregate membership: value object — freely embedded in ``Product``,
    ``Cart`` lines, ``Order``/``Payment`` totals, ``Coupon`` values.

    Invariants enforced: ``minor >= 0`` (``__post_init__``); currency must
    match on ``plus``/``minus``/``__lt__``; ``times_fraction`` requires
    ``den > 0`` and ``num >= 0``.

    Example usage:
        price = Money(1999, "USD")          # $19.99
        total = price.plus(Money(500, "USD"))
    """

    minor: int
    """Amount in minor units (cents). Always >= 0."""
    currency: str
    """ISO currency code (e.g. ``"USD"``). Compared exactly."""

    def __post_init__(self) -> None:
        """Validate construction (``minor >= 0``).

        Raises:
            InvariantViolation: If ``minor`` is negative — Money can never
                represent a debt; refunds are positive amounts applied
                against a captured total.
        """
        if self.minor < 0:
            raise InvariantViolation(f"Money.minor must be >= 0, got {self.minor}")

    # -- arithmetic (all return new instances; round-half-up) ---------------
    def _check(self, other: "Money") -> None:
        """Guard a binary operation against mixed currencies.

        Args:
            other: The other operand.

        Raises:
            InvariantViolation: If currencies differ — implicit FX
                conversion is forbidden; callers must convert explicitly.
        """
        if self.currency != other.currency:
            raise InvariantViolation(
                f"currency mismatch: {self.currency} != {other.currency}"
            )

    def plus(self, other: "Money") -> "Money":
        """Add two same-currency amounts.

        Args:
            other: Amount to add.

        Returns:
            A new ``Money`` with the summed minor units.

        Raises:
            InvariantViolation: On currency mismatch (via ``_check``).
        """
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def minus(self, other: "Money") -> "Money":
        """Subtract a same-currency amount (result must stay >= 0).

        Args:
            other: Amount to subtract.

        Returns:
            A new ``Money`` with the difference.

        Raises:
            InvariantViolation: On currency mismatch, or if the result
                would be negative (via ``__post_init__``) — callers must
                clamp (see ``Coupon._compute_discount``) before subtracting.
        """
        self._check(other)
        return Money(self.minor - other.minor, self.currency)  # __post_init__ enforces >= 0

    def times_fraction(self, num: int, den: int) -> "Money":
        """Scale by num/den with round-half-up (den > 0, num >= 0).

        Used for percentage discounts (CO5: ``value/100``) and tax
        (``tax_rate_num/tax_rate_den``). Bias term ``den // 2`` rounds
        half minor-units up so penny splits favor the merchant
        consistently.

        Args:
            num: Numerator of the fraction (e.g. 20 for 20%).
            den: Denominator (e.g. 100). Must be positive.

        Returns:
            A new ``Money`` scaled by the fraction.

        Raises:
            InvariantViolation: If ``den <= 0`` or ``num < 0``.
        """
        if den <= 0:
            raise InvariantViolation("Money.times_fraction: denominator must be positive")
        if num < 0:
            raise InvariantViolation("Money.times_fraction: numerator must be non-negative")
        # Round-half-up: add half the divisor before truncating.
        return Money((self.minor * num + den // 2) // den, self.currency)

    def __lt__(self, other: "Money") -> bool:
        """Compare minor units (same currency only).

        Args:
            other: Amount to compare against.

        Returns:
            True iff this amount is strictly smaller.

        Raises:
            InvariantViolation: On currency mismatch.
        """
        self._check(other)
        return self.minor < other.minor

    @property
    def major(self) -> float:
        """Return the amount in major units (e.g. dollars).

        Returns:
            ``minor / 100`` as float — display only; never use for
            arithmetic (float rounding would break the minor-unit exactness).
        """
        return self.minor / 100

    def __str__(self) -> str:
        """Return a human-readable ``"19.99 USD"`` rendering."""
        return f"{self.major:.2f} {self.currency}"


@dataclass(frozen=True)
class Address:
    """Destination value object: where goods (or a return pickup) go.

    Role: opaque delivery location carried by ``Cart``/``Order``/``Shipment``
    and ``Pickup``. No validation of postal formats in v1 (single-carrier
    assumption) — all four fields are required strings.

    Invariants: none beyond presence (structural); the owning aggregate
    rejects a ``None`` destination (C-checkout guard).
    """
    line1: str
    city: str
    postal_code: str
    country: str


@dataclass(frozen=True)
class PickupEvidence:
    """Proof of doorstep collection (invariant PK5).

    Role: the courier's on-site proof that possession transferred at the
    customer's door — replaces the return-label scan of label-based models.

    Aggregate membership: value object attached to ``Pickup.confirmPickup``;
    carried on ``PickupConfirmed`` / ``ReturnGoodsReceived`` events.

    Invariants enforced (PK5): a photo reference is mandatory; the courier
    scan + photo is the customer's proof of collection since they hold no
    return label. Signature is optional (doorstep drop with photo suffices).
    """

    photo_ref: str
    """Storage reference (FileBucket key) of the doorstep photo. Required."""
    signature_ref: str | None = None
    """Optional signature capture reference."""


@dataclass(frozen=True)
class Discount:
    """VO — the *result* of applying a Coupon (deliberately split from the entity).

    Role: computed outcome (``Coupon`` is the instrument/entity with
    lifecycle + redemptions; ``Discount`` is the plain result). Carried on
    ``Quote`` and frozen onto the ``Order`` at placement.

    Aggregate membership: value object produced by ``Coupon.validate`` /
    ``compute_discount`` and consumed by ``Cart.quote``.

    Invariants: ``amount`` is already clamped to ``<= subtotal`` (CO5 plus
    the fixed-amount min-cap); ``rule`` names the producing rule for
    audit (``"PCT-20"``, ``"FIXED-500"``, ``"FREE_SHIPPING"``, ``"NONE"``).

    Example usage:
        discount = coupon.validate(customer_id, subtotal, now)
    """

    amount: Money
    """Money reduction to subtract from the pre-tax subtotal."""
    rule: str  # e.g. "PCT-20", "FIXED-500", "FREE_SHIPPING", "NONE"
    """Rule label that produced this discount (audit trail)."""


@dataclass(frozen=True)
class Quote:
    """The derived Cart total breakdown (invariant C2: never stored by hand).

    Role: point-in-time pricing snapshot — ``subtotal - discount + tax +
    shipping = total`` — computed *inside* ``Cart.quote()``. The checkout
    snapshot freezes this onto the ``Order`` (O2).

    Aggregate membership: value object returned by ``Cart.quote``; never an
    entity (no identity, no lifecycle — recompute instead of mutating).
    """

    subtotal: Money
    """Pre-discount, pre-tax, pre-shipping line total."""
    discount: Discount
    """Applied coupon's computed reduction (``NONE`` when no coupon)."""
    tax: Money
    """Tax on the discounted goods (pluggable rate; zero in v1)."""
    shipping_fee: Money
    """Flat fee, zeroed when a FreeShipping coupon applies."""
    total: Money
    """Final payable: discounted goods + tax + shipping."""


@dataclass(frozen=True)
class RefundBreakdown:
    """The RT3 pro-rata refund computation (goods + tax + shipping).

    Role: per-settlement refund snapshot produced by the Returns context:
    ``goodsRefund = returnedLineValue x (1 - discountShare)``,
    ``taxRefund`` proportional to net goods, ``shippingRefund =
    shippingFee x returnFraction`` (full fee iff ``is_full``).

    Invariants (RT3): every component scales with the fraction of the
    order's line value returned, so the effective per-unit price matches
    what the customer paid; a free-shipping order simply has zero shipping
    to refund (no special-casing).
    """

    goods: Money
    """Net goods refund (discount share already removed)."""
    tax: Money
    """Proportional tax refund."""
    shipping: Money
    """Proportional shipping refund (full fee on a full return)."""
    total: Money
    """Total hitting ``Payment.refund``: goods + tax + shipping."""
    is_full: bool  # True iff every shipped line is returned
    """True iff every shipped line was returned (drives CO7/RT6 unredeem)."""
