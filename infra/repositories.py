"""Repositories (DIP): persistence behind small interfaces.

SOLID notes:
- Each aggregate gets its own narrow repository interface (ISP) so services
  depend only on what they use.
- A single generic in-memory implementation backs them all; swapping in a
  DynamoDB-backed repository later needs no service changes (OCP/LSP).
- Repositories store the live aggregate objects for local testing. A real
  DynamoDB implementation would serialize snapshots instead — the interface
  is deliberately serialization-agnostic (``add``/``get`` take aggregates).
"""
from __future__ import annotations

import abc
from typing import Generic, TypeVar

T = TypeVar("T")


class Repository(abc.ABC, Generic[T]):
    """Minimal persistence contract (ISP): only what application services need."""

    @abc.abstractmethod
    def add(self, agg: T) -> None: ...

    @abc.abstractmethod
    def get(self, agg_id: str) -> T:
        """Return the aggregate or raise KeyError."""

    @abc.abstractmethod
    def list(self) -> list[T]: ...

    @abc.abstractmethod
    def clear(self) -> None: ...


class InMemoryRepository(Repository[T]):
    """Local (dev/test) repository: dict-backed, zero infrastructure."""

    def __init__(self) -> None:
        self._store: dict[str, T] = {}

    def add(self, agg: T) -> None:
        self._store[agg.aggregate_id()] = agg  # type: ignore[attr-defined]

    def get(self, agg_id: str) -> T:
        try:
            return self._store[agg_id]
        except KeyError:
            raise KeyError(f"no aggregate with id {agg_id!r}") from None

    def list(self) -> list[T]:
        return list(self._store.values())

    def clear(self) -> None:
        self._store.clear()

    # -- UoW support (snapshot/restore for atomic rollback) -----------------
    def _snapshot_store(self) -> dict:
        import copy

        return copy.deepcopy(self._store)

    def _restore_store(self, snapshot: dict) -> None:
        self._store.clear()
        self._store.update(snapshot)


# Concrete per-aggregate repositories: distinct types for DI clarity, one impl.
# (A DynamoDB port would subclass Repository[T] per aggregate with mappers.)
class CartRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class OrderRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class ProductRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class StockRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class PaymentRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class ShipmentRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class PickupRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass


class CouponRepository(InMemoryRepository):  # type: ignore[type-arg]
    """Coupons are looked up by code (\"SAVE20\"); storage is keyed by id."""

    def find_by_code(self, code: str):
        for cpn in self.list():
            if getattr(cpn, "code", None) == code:
                return cpn
        raise KeyError(f"no coupon with code {code!r}")


class ReturnRepository(InMemoryRepository):  # type: ignore[type-arg]
    pass
