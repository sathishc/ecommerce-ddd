"""Value Objects — immutable, defined by their attributes.

Two VOs with equal attributes are interchangeable. If deleting one loses
history, it is an Entity, not a VO.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import InvariantViolation


@dataclass(frozen=True)
class Money:
    """THE shared kernel: minor units + ISO currency. Immutable, always >= 0."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if self.minor < 0:
            raise InvariantViolation(f"Money.minor must be >= 0, got {self.minor}")

    # -- arithmetic (all return new instances; round-half-up) ---------------
    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise InvariantViolation(
                f"currency mismatch: {self.currency} != {other.currency}"
            )

    def plus(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def minus(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.minor - other.minor, self.currency)  # __post_init__ enforces >= 0

    def times_fraction(self, num: int, den: int) -> "Money":
        """Scale by num/den with round-half-up (den > 0, num >= 0)."""
        if den <= 0:
            raise InvariantViolation("Money.times_fraction: denominator must be positive")
        if num < 0:
            raise InvariantViolation("Money.times_fraction: numerator must be non-negative")
        return Money((self.minor * num + den // 2) // den, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check(other)
        return self.minor < other.minor

    @property
    def major(self) -> float:
        return self.minor / 100

    def __str__(self) -> str:
        return f"{self.major:.2f} {self.currency}"


@dataclass(frozen=True)
class Address:
    line1: str
    city: str
    postal_code: str
    country: str


@dataclass(frozen=True)
class PickupEvidence:
    """Proof of doorstep collection (invariant PK5)."""

    photo_ref: str
    signature_ref: str | None = None


@dataclass(frozen=True)
class Discount:
    """VO — the *result* of applying a Coupon (deliberately split from the entity)."""

    amount: Money
    rule: str  # e.g. "PCT-20", "FIXED-500", "FREE_SHIPPING", "NONE"


@dataclass(frozen=True)
class Quote:
    """The derived Cart total breakdown (invariant C2: never stored by hand)."""

    subtotal: Money
    discount: Discount
    tax: Money
    shipping_fee: Money
    total: Money


@dataclass(frozen=True)
class RefundBreakdown:
    """The RT3 pro-rata refund computation (goods + tax + shipping)."""

    goods: Money
    tax: Money
    shipping: Money
    total: Money
    is_full: bool  # True iff every shipped line is returned
