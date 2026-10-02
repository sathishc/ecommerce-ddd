"""Shop context — the Cart aggregate.

Invariants:
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
DEFAULT_SHIPPING = 500  # minor units — flat rate (single carrier, no routing)


@dataclass
class _CartLine:
    """Child of Cart: a Product + Quantity (immutable after placement)."""

    product_id: str
    unit_price: Money
    quantity: int

    @property
    def line_total(self) -> Money:
        return Money(self.unit_price.minor * self.quantity, self.unit_price.currency)


class Cart(Aggregate):
    """Aggregate root: the customer's pre-order collection (identity = cart_id)."""

    prefix = "cart"

    def __init__(self, customer_ref: str) -> None:
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
        return self._cart_id

    @property
    def customer_ref(self) -> str:
        return self._customer_ref

    @property
    def status(self) -> str:
        return self._status

    @property
    def applied_coupon_code(self) -> str | None:
        return self._applied_coupon_code

    @property
    def destination(self) -> Address | None:
        return self._destination

    def set_destination(self, destination: Address) -> None:
        self._open()
        if destination is None:
            raise InvariantViolation("C: a destination cannot be empty")
        self._destination = destination

    def _open(self) -> None:
        if self._status != "Open":
            raise InvalidStateTransition(f"C4: cart is {self._status}; only Open carts mutate")

    # -- cart manipulation (C1) ---------------------------------------------
    def add_product(self, product: Product, quantity: int = 1) -> None:
        self._open()
        if quantity < 1:
            raise InvariantViolation("C1: quantity must be >= 1")
        if product.status != "Active":
            raise InvariantViolation(f"cannot add a {product.status} product")
        existing = self._lines.get(product.product_id)
        if existing is None:
            self._lines[product.product_id] = _CartLine(product.product_id, product.price, quantity)
            self.raise_event(CartItemAdded(self._cart_id, product.product_id, quantity))
        else:
            existing.quantity += quantity   # C1: one line per product
            self.raise_event(CartItemChanged(self._cart_id, product.product_id, existing.quantity))

    def change_quantity(self, product_id: str, quantity: int) -> None:
        self._open()
        if quantity < 1:
            raise InvariantViolation(f"C1: quantity must be >= 1, got {quantity}")
        line = self._lines.get(product_id)
        if line is None:
            raise InvariantViolation(f"no line for product {product_id}")
        line.quantity = quantity
        self.raise_event(CartItemChanged(self._cart_id, product_id, quantity))

    def remove_line(self, product_id: str) -> None:
        self._open()
        if product_id not in self._lines:
            raise InvariantViolation(f"no line for product {product_id}")
        del self._lines[product_id]
        self.raise_event(CartLineRemoved(self._cart_id, product_id))

    # -- coupon (C3) -----------------------------------------------------------
    def apply_coupon(self, coupon: Coupon, now: datetime) -> Discount:
        """C3: preview + replace. Validation failure RAISES (rejected, not ignored)."""
        self._open()
        discount = coupon.validate(self._customer_ref, self.subtotal(), now)  # may raise CO2/CO3
        if self._applied_coupon is not None and self._applied_coupon is not coupon:
            self.raise_event(CouponRemoved(self._cart_id))
        self._applied_coupon = coupon
        self._applied_coupon_code = coupon.code
        self.raise_event(CouponApplied(self._cart_id, coupon.code))
        return discount

    def remove_coupon(self) -> None:
        self._open()
        if self._applied_coupon is None:
            return
        self._applied_coupon = None
        self._applied_coupon_code = None
        self.raise_event(CouponRemoved(self._cart_id))

    # -- pricing (C2: everything derived) ----------------------------------------
    def subtotal(self) -> Money:
        """Pre-discount, pre-tax, pre-shipping line total (the RT3 denominator)."""
        if not self._lines:
            raise InvariantViolation("quote of an empty cart")
        cur = next(iter(self._lines.values())).unit_price.currency
        return Money(sum(l.line_total.minor for l in self._lines.values()), cur)

    def _applied_discount(self, subtotal: Money) -> Discount:
        if self._applied_coupon is None:
            return Discount(Money(0, subtotal.currency), "NONE")
        # C3: validation happens once, at apply time (apply_coupon -> validate).
        # The quote only *recomputes* the amount against the current subtotal,
        # because the cart can change after the coupon is applied. (No re-check
        # of the validity window against wall clock — that would make a valid
        # cart un-quoteable once the coupon window lapses.)
        return self._applied_coupon.compute_discount(subtotal)

    def shipping_fee(self) -> Money:
        """Flat rate, zeroed when a FreeShipping coupon applies."""
        cur = self.subtotal().currency
        if self._applied_coupon is not None:
            d = self._applied_discount(self.subtotal())
            if d.rule == FREE_SHIPPING:
                return Money(0, cur)
        return Money(DEFAULT_SHIPPING, cur)

    def quote(self, tax_rate_num: int = TAX_RATE_NUM, tax_rate_den: int = TAX_RATE_DEN) -> Quote:
        """C2: total is computed *inside* the aggregate; never set from outside."""
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
        """Close without buying (customer walked away) → `CartAbandoned`."""
        self._open()
        self._status = "Abandoned"  # terminal + immutable, like checkout
        self.raise_event(CartAbandoned(self._cart_id))

    def checkout(self, destination: Address) -> dict:
        """Freeze the cart; the caller places the Order from this snapshot (C4)."""
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
        return [(lid, l.quantity, l.unit_price) for lid, l in self._lines.items()]
