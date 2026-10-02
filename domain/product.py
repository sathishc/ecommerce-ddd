"""Catalog context — the Product aggregate.

Bounded context: Catalog. Owns the authoring and maintenance of sellable
products; publishes stable views (``productId``, ``name``, ``price``) to
Shop/Order via Open Host Service. The ``Order`` snapshots price at
placement (O2), so later catalog changes never alter existing orders.

Responsibility: guard sellability — every sellable item has a unique,
immutable SKU (P1), a non-negative price (P2), and every price/catalog
change is observable (P3).

Key invariants:
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
    """Child of Product: descriptive catalog data (no identity, no events).

    Role: groups the mutable display/physical attributes so
    ``update_catalog_fields`` can replace them atomically. Frozen so
    callers cannot mutate a product's description behind the root's back.
    """

    name: str
    description: str
    weight_grams: int


class Product(Aggregate):
    """Aggregate root: a sellable catalog item (identity = productId).

    Role: the Catalog context's sellable thing — referenced by SKU in the
    business language, by ``product_id`` for identity. Guards sellability
    (P1/P2) and makes every price change visible (P3).

    Aggregate membership: root; children = ``_CatalogFields`` (descriptive
    only). Referenced (not contained) by ``Cart._CartLine`` (which snapshots
    the unit price) and ``Stock`` (one per product).

    Invariants enforced: P1 (non-empty, immutable SKU), P2 (price >= 0),
    P3 (``change_price``/``discontinue`` emit ``ProductPriceChanged``).

    Example usage:
        p = Product(None, sku="WIDGET-1", name="Widget", price=Money(999, "USD"))
        p.change_price(Money(1299, "USD"))
        events = p.pop_events()  # [ProductPriceChanged(...)]
    """

    prefix = "prd"
    """ID namespace for products (``prd-<hex>``)."""

    def __init__(self, product_id: str | None, sku: str, name: str, price: Money,
                 description: str = "", weight_grams: int = 0) -> None:
        """Create a sellable product in ``Active`` status.

        Args:
            product_id: Explicit identity, or ``None`` to auto-generate
                (``prd-<hex>`` via ``_next_id``).
            sku: Business code (P1). Must be non-empty/non-blank; stored
                as given and never mutable afterwards.
            name: Display name. Must be non-empty/non-blank.
            price: Sell price (P2). ``Money.minor`` must be >= 0.
            description: Free-text description (default ``""``).
            weight_grams: Physical weight for shipping (default 0; >= 0).

        Raises:
            InvariantViolation: On empty SKU (P1), empty name, negative
                price (P2), or negative weight.

        Events emitted: none here (creation published upstream as
            ``ProductPublished`` by the application layer, not the root).
        """
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
        """Return the product identity string.

        Returns:
            The ``product_id`` (explicit or auto-generated).
        """
        return self._product_id

    @property
    def product_id(self) -> str:
        """Return the product identity string.

        Returns:
            Same as ``aggregate_id()``; the Cart/Stock-facing alias.
        """
        return self._product_id

    @property
    def sku(self) -> str:  # P1: immutable, read-only
        """Return the business SKU code.

        Returns:
            The immutable SKU (P1 — no setter exists by design).
        """
        return self._sku

    @property
    def name(self) -> str:
        """Return the display name.

        Returns:
            Current catalog name (mutable via ``update_catalog_fields``).
        """
        return self._fields.name

    @property
    def price(self) -> Money:
        """Return the current sell price.

        Returns:
            The live ``Money`` price. Note: carts snapshot this value at
            ``add_product`` time, and orders freeze it (O2) — a later
            ``change_price`` never rewrites history.
        """
        return self._price

    @property
    def weight_grams(self) -> int:
        """Return the shipping weight in grams.

        Returns:
            Current weight (>= 0).
        """
        return self._fields.weight_grams

    @property
    def status(self) -> str:
        """Return the lifecycle status.

        Returns:
            ``"Active"`` or ``"Discontinued"``. Only ``Active`` products
            may be added to carts or have catalog fields edited.
        """
        return self._status

    # -- behavior (P3: every change is an event) ----------------------------
    def update_catalog_fields(self, name: str | None = None,
                              description: str | None = None,
                              weight_grams: int | None = None) -> None:
        """Patch descriptive fields atomically (``None`` = keep current).

        Args:
            name: New display name, or ``None`` to keep.
            description: New description, or ``None`` to keep.
            weight_grams: New weight, or ``None`` to keep. Must be >= 0.

        Raises:
            InvariantViolation: If the product is ``Discontinued`` (frozen
                catalog entry), or if ``weight_grams`` is negative.

        Events emitted: none — descriptive edits are silent by design (P3
            covers *price* visibility, not display text).
        """
        if self._status != "Active":
            raise InvariantViolation("a Discontinued product cannot be edited")
        if weight_grams is not None and weight_grams < 0:
            raise InvariantViolation("weight must be >= 0")
        # Replace the frozen VO wholesale so no alias can observe a half-edit.
        self._fields = _CatalogFields(
            name if name is not None else self._fields.name,
            description if description is not None else self._fields.description,
            weight_grams if weight_grams is not None else self._fields.weight_grams,
        )

    def change_price(self, new_price: Money) -> None:
        """Set a new sell price and announce it (P3).

        Args:
            new_price: The new price (P2: ``minor >= 0``).

        Raises:
            InvariantViolation: If ``new_price`` is negative (P2).

        Events emitted: ``ProductPriceChanged(product_id, new_price)``
            (P3 — never silent, so Shop/Order views can react).
        """
        if new_price.minor < 0:
            raise InvariantViolation(f"P2: price must be >= 0, got {new_price}")
        self._price = new_price
        self.raise_event(ProductPriceChanged(self._product_id, new_price))

    def discontinue(self) -> None:
        """Retire the product from sale.

        Raises:
            InvariantViolation: If already ``Discontinued`` (idempotency is
                explicit rejection, not silent success, so double-retire
                bugs surface).

        Events emitted: ``ProductPriceChanged(product_id, price)`` (P3 —
            reuses the price-visibility event so downstream views refresh;
            the price value itself is unchanged).
        """
        if self._status == "Discontinued":
            raise InvariantViolation("already discontinued")
        self._status = "Discontinued"
        self.raise_event(ProductPriceChanged(self._product_id, self._price))  # P3: any change is visible
