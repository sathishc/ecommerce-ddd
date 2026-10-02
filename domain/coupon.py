"""Shop / Pricing context — the Coupon aggregate.

Invariants:
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
PERCENTAGE = "PCT"
FIXED_AMOUNT = "FIXED"


@dataclass
class _Redemption:
    customer_id: str
    order_id: str


class Coupon(Aggregate):
    """Aggregate root: a discount instrument (identity = coupon_id)."""

    prefix = "cpn"

    def __init__(self, code: str, type: str, value: int | Money,
                 valid_from: datetime, valid_to: datetime,
                 usage_limit: int | None = None,
                 min_order_value: Money | None = None) -> None:
        super().__init__()
        if not code or not code.strip():
            raise InvariantViolation("CO1: code must be non-empty")
        if type not in (PERCENTAGE, FIXED_AMOUNT, FREE_SHIPPING):
            raise InvariantViolation(f"unknown coupon type {type!r}")
        if isinstance(value, int):
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
        return self._coupon_id

    @property
    def code(self) -> str:
        return self._code

    @property
    def redemptions(self) -> int:
        return len(self._redemptions)

    @property
    def status(self) -> str:
        return self._status

    def is_redeemed_by(self, customer_id: str, order_id: str) -> bool:
        r = self._redemptions.get(order_id)
        return r is not None and r.customer_id == customer_id

    # -- behavior -------------------------------------------------------------
    def validate(self, customer_id: str, cart_total: Money, now: datetime) -> Discount:
        """Preview (CO4: does NOT increment). Raises CouponValidationError on CO2/CO3."""
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
        for r in self._redemptions.values():
            if r.customer_id == customer_id:
                raise CouponValidationError(
                    f"CO3: customer {customer_id} already redeemed {self._code}"
                )
        return self._compute_discount(cart_total)

    def compute_discount(self, cart_total: Money) -> Discount:
        """The raw discount for a given subtotal (used by the Cart's quote)."""
        return self._compute_discount(cart_total)

    def _compute_discount(self, cart_total: Money) -> Discount:
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
        """CO3/CO4: the only place redemptions increment. Idempotent per (customer, order)."""
        if self._status != "Active":
            raise CouponValidationError(f"coupon {self._code} is {self._status}")
        existing = self._redemptions.get(order_id)
        if existing is not None:
            if existing.customer_id == customer_id:
                return  # idempotent: same customer re-confirming the same order
            raise InvariantViolation(f"CO1: order {order_id} already redeemed by another customer")
        if self._usage_limit is not None and self.redemptions >= self._usage_limit:
            raise CouponValidationError(f"CO2: coupon {self._code} usage limit reached")
        for r in self._redemptions.values():
            if r.customer_id == customer_id:
                raise CouponValidationError(
                    f"CO3: customer {customer_id} already redeemed {self._code} (one per customer)"
                )
        self._redemptions[order_id] = _Redemption(customer_id, order_id)

    def unredeem(self, customer_id: str, order_id: str) -> None:
        """CO7: mirror of redeem — only on a *complete* return. Idempotent."""
        r = self._redemptions.get(order_id)
        if r is None or r.customer_id != customer_id:
            return  # idempotent / no-op
        del self._redemptions[order_id]
        self.raise_event(CouponUnredeemed(self._code, customer_id))

    def deactivate(self) -> None:
        if self._status == "Deactivated":
            raise InvariantViolation("already deactivated")
        self._status = "Deactivated"

    def expire(self) -> None:
        if self._status == "Expired":
            raise InvariantViolation("already expired")
        self._status = "Expired"
