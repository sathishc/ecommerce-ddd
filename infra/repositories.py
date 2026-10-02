"""Repositories (DIP): persistence behind small interfaces.

Pattern implemented: Repository (DDD) -- one narrow persistence port per
aggregate. Application services program against these ABCs and never touch
storage directly, so persistence can be swapped without rewriting services.

SOLID notes:
- Each aggregate gets its own narrow repository interface (ISP) so services
  depend only on what they use.
- A single generic in-memory implementation backs them all; swapping in a
  DynamoDB-backed repository later needs no service changes (OCP/LSP).
- Repositories store the live aggregate objects for local testing. A real
  DynamoDB implementation would serialize snapshots instead — the interface
  is deliberately serialization-agnostic (``add``/``get`` take aggregates).

AWS <-> local mapping: ``InMemoryRepository`` (dict-backed) is the local
Block; a DynamoDB-backed ``Repository[T]`` subclass would be the AWS Block
(same ``add``/``get``/``list``/``clear`` contract, selected by
``BLOCKS_BACKEND`` in the container). ``CouponRepository.find_by_code``
mirrors a DynamoDB GSI lookup on coupon code.

How it supports atomicity/testability: ``_snapshot_store``/``_restore_store``
give the Unit-of-Work deepcopy snapshots for atomic rollback (see
``infra/unit_of_work.py``); tests seed repos directly (``c.products.add(...)``)
with zero infrastructure.
"""
from __future__ import annotations

import abc
from typing import Generic, TypeVar

T = TypeVar("T")


class Repository(abc.ABC, Generic[T]):
    """Minimal persistence contract (ISP): only what application services need.

    Contract: ``add`` upserts by ``aggregate_id()``; ``get`` returns the live
    aggregate or raises ``KeyError`` (callers treat missing as a domain
    failure, e.g. "no stock record"); ``list``/``clear`` support inspection
    and test isolation. Implementations must be serialization-agnostic --
    they take aggregates, not dicts/rows.
    """

    @abc.abstractmethod
    def add(self, agg: T) -> None:
        """Upsert an aggregate, keyed by ``agg.aggregate_id()``.

        Args:
            agg: The aggregate to store (stored live locally; a DynamoDB
                port would serialize a snapshot instead).
        """
        ...

    @abc.abstractmethod
    def get(self, agg_id: str) -> T:
        """Return the aggregate or raise KeyError.

        Args:
            agg_id: The value of ``agg.aggregate_id()`` to look up.

        Returns:
            The stored aggregate.

        Raises:
            KeyError: If no aggregate with this id exists.
        """
        ...

    @abc.abstractmethod
    def list(self) -> list[T]:
        """Return all stored aggregates (snapshot inspection for tests).

        Returns:
            List of stored aggregates in insertion order.
        """
        ...

    @abc.abstractmethod
    def clear(self) -> None:
        """Remove all stored aggregates (test isolation / UoW sabotage tests)."""
        ...


class InMemoryRepository(Repository[T]):
    """Local (dev/test) repository: dict-backed, zero infrastructure.

    The local Block for every aggregate port. Stores live aggregate objects
    (no serialization round-trip), so tests observe the exact instances
    services mutated. A DynamoDB port would subclass ``Repository[T]``
    with mappers and keep this identical interface (LSP).
    """

    def __init__(self) -> None:
        # Keyed by ``agg.aggregate_id()``; values are live aggregate objects.
        self._store: dict[str, T] = {}

    def add(self, agg: T) -> None:
        """Upsert by aggregate id.

        Args:
            agg: Aggregate exposing ``aggregate_id()`` (all domain
                aggregates do via ``domain/aggregate.py``).
        """
        self._store[agg.aggregate_id()] = agg  # type: ignore[attr-defined]

    def get(self, agg_id: str) -> T:
        """Look up one aggregate by id.

        Args:
            agg_id: The aggregate id to fetch.

        Returns:
            The live stored aggregate.

        Raises:
            KeyError: If ``agg_id`` is unknown (message includes the id).
        """
        try:
            return self._store[agg_id]
        except KeyError:
            # ``from None``: the missing id is the domain-relevant fact, not
            # the dict internals.
            raise KeyError(f"no aggregate with id {agg_id!r}") from None

    def list(self) -> list[T]:
        """Return all stored aggregates.

        Returns:
            Live aggregate objects in insertion order (defensive list copy;
            the aggregates themselves are shared, like a real identity map).
        """
        return list(self._store.values())

    def clear(self) -> None:
        """Drop all entries (used for test isolation and failure injection)."""
        self._store.clear()

    # -- UoW support (snapshot/restore for atomic rollback) -----------------
    def _snapshot_store(self) -> dict:
        """Deepcopy the whole store for UoW rollback.

        Returns:
            A deepcopy snapshot the UoW holds until commit/rollback. Deep
            (not shallow) so in-place aggregate mutations after the snapshot
            cannot leak into it -- rollback restores true pre-UoW state.
        """
        import copy

        # Local import: ``copy`` is only needed on the UoW path, keeping the
        # hot ``add``/``get`` path import-light.
        return copy.deepcopy(self._store)

    def _restore_store(self, snapshot: dict) -> None:
        """Restore a snapshot taken by :meth:`_snapshot_store` (UoW rollback).

        Args:
            snapshot: A dict previously returned by ``_snapshot_store``.
                Applied via clear+update (in place) so existing repo
                references held by services stay valid.
        """
        self._store.clear()
        self._store.update(snapshot)


# Concrete per-aggregate repositories: distinct types for DI clarity, one impl.
# (A DynamoDB port would subclass Repository[T] per aggregate with mappers.)
# Narrow per-aggregate types (ISP): services declare e.g. ``OrderRepository``
# rather than a god-repo, so each depends only on what it uses.
class CartRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Shop cart store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class OrderRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Order store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class ProductRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Catalog product store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class StockRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Inventory stock store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class PaymentRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Payment store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class ShipmentRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Shipment store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class PickupRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Doorstep-pickup store (local dict impl; a DynamoDB port keeps this type)."""

    pass


class CouponRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Coupon store: storage keyed by id, domain lookup by code.

    Mirrors a DynamoDB GSI on coupon code: same ``find_by_code`` contract a
    DynamoDB port would implement via a code-index query (LSP).
    """

    def find_by_code(self, code: str):
        """Find a coupon by its human code.

        Args:
            code: The coupon code (e.g. "SAVE20").

        Returns:
            The matching coupon aggregate.

        Raises:
            KeyError: If no stored coupon carries this code.
        """
        # Linear scan is fine locally (few coupons); a DynamoDB port would
        # query the code GSI here under the same contract.
        for cpn in self.list():
            if getattr(cpn, "code", None) == code:
                return cpn
        raise KeyError(f"no coupon with code {code!r}")


class ReturnRepository(InMemoryRepository):  # type: ignore[type-arg]
    """RMA store (local dict impl; a DynamoDB port keeps this type)."""

    pass
