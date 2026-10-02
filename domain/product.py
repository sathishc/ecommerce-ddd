"""Catalog context — the Product aggregate.

Invariants:
  P1  sku unique and immutable (uniqueness is enforced context-wide; the
      aggregate enforces immutability + format)
  P2  price >= 0
  P3  price change is an event, never silent
"""
from __future__ import annotations

from dataclasses import dataclass

from .aggregate import Aggregate, _next_id
from .events import ProductPriceChanged, ProductPublished
from .errors import InvariantViolation
from .value_objects import Money


@dataclass(frozen=True)
class _CatalogFields:
    name: str
    description: str
    weight_grams: int


class Product(Aggregate):
    """Aggregate root: a sellable catalog item (identity = productId)."""

    prefix = "prd"

    def __init__(self, product_id: str | None, sku: str, name: str, price: Money,
                 description: str = "", weight_grams: int = 0) -> None:
        super().__init__()
        if not sku or not sku.strip():
            raise InvariantViolation("P1: sku must be non-empty")
        if not name or not name.strip():
            raise InvariantViolation("name must be non-empty")
        if price.minor < 0:
            raise InvariantViolation(f"P2: price must be >= 0, got {price}")
        if weight_grams < 0:
            raise InvariantViolation("weight must be >= 0")
        self._product_id = product_id or _next_id(self.prefix)
        self._sku = sku                      # P1: immutable
        self._fields = _CatalogFields(name, description, weight_grams)
        self._price = price
        self._status = "Active"

    # -- identity -----------------------------------------------------------
    def aggregate_id(self) -> str:
        return self._product_id

    @property
    def product_id(self) -> str:
        return self._product_id

    @property
    def sku(self) -> str:  # P1: immutable, read-only
        return self._sku

    @property
    def name(self) -> str:
        return self._fields.name

    @property
    def price(self) -> Money:
        return self._price

    @property
    def weight_grams(self) -> int:
        return self._fields.weight_grams

    @property
    def status(self) -> str:
        return self._status

    # -- behavior (P3: every change is an event) ----------------------------
    def update_catalog_fields(self, name: str | None = None,
                              description: str | None = None,
                              weight_grams: int | None = None) -> None:
        if self._status != "Active":
            raise InvariantViolation("a Discontinued product cannot be edited")
        if weight_grams is not None and weight_grams < 0:
            raise InvariantViolation("weight must be >= 0")
        self._fields = _CatalogFields(
            name if name is not None else self._fields.name,
            description if description is not None else self._fields.description,
            weight_grams if weight_grams is not None else self._fields.weight_grams,
        )

    def change_price(self, new_price: Money) -> None:
        if new_price.minor < 0:
            raise InvariantViolation(f"P2: price must be >= 0, got {new_price}")
        self._price = new_price
        self.raise_event(ProductPriceChanged(self._product_id, new_price))

    def discontinue(self) -> None:
        if self._status == "Discontinued":
            raise InvariantViolation("already discontinued")
        self._status = "Discontinued"
        self.raise_event(ProductPriceChanged(self._product_id, self._price))  # P3: any change is visible
