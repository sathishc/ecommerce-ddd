"""Unit of Work (atomic business transactions + outbox publishing).

Pattern:
    with uow_factory() as uow:
        ... mutate aggregates via uow.<repos> ...
        uow.collect(*aggregates)   # drain pending domain events into outbox
    # <- __exit__ commits (publishes outbox via EventBus) or rolls back

- Atomicity: repositories are snapshotted on entry; any exception restores
  them, so a failed checkout leaves no dangling reservations/redemptions.
- Outbox: events are published to the EventBus only on successful commit —
  the transactional-outbox guarantee that makes the event architecture safe.
- Services depend on the ``UnitOfWork`` ABC / factory (DIP); tests inject an
  in-memory factory, production would inject a DynamoDB-transaction factory.
"""
from __future__ import annotations

import abc
from collections.abc import Callable
from types import TracebackType

from domain.events import DomainEvent
from infra.event_bus import EventBus
from infra.repositories import (
    CartRepository,
    CouponRepository,
    OrderRepository,
    PaymentRepository,
    PickupRepository,
    ProductRepository,
    ReturnRepository,
    ShipmentRepository,
    StockRepository,
)


class UnitOfWork(abc.ABC):
    """Interface services program against (DIP)."""

    carts: CartRepository
    orders: OrderRepository
    products: ProductRepository
    stocks: StockRepository
    payments: PaymentRepository
    shipments: ShipmentRepository
    pickups: PickupRepository
    coupons: CouponRepository
    returns: ReturnRepository

    @abc.abstractmethod
    def collect(self, *aggregates) -> None:
        """Drain pending domain events from aggregates into the commit outbox."""

    @abc.abstractmethod
    def commit(self) -> list[DomainEvent]:
        """Publish the outbox via the EventBus. Returns published events."""

    @abc.abstractmethod
    def rollback(self) -> None:
        """Discard the outbox and restore repository state."""

    # context-manager protocol
    @abc.abstractmethod
    def __enter__(self) -> "UnitOfWork": ...

    @abc.abstractmethod
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


UoWFactory = Callable[[], UnitOfWork]


class InMemoryUnitOfWork(UnitOfWork):
    """Local UoW: shared repos + deepcopy snapshots + EventBus outbox."""

    def __init__(
        self,
        *,
        carts: CartRepository,
        orders: OrderRepository,
        products: ProductRepository,
        stocks: StockRepository,
        payments: PaymentRepository,
        shipments: ShipmentRepository,
        pickups: PickupRepository,
        coupons: CouponRepository,
        returns: ReturnRepository,
        bus: EventBus,
    ) -> None:
        self.carts = carts
        self.orders = orders
        self.products = products
        self.stocks = stocks
        self.payments = payments
        self.shipments = shipments
        self.pickups = pickups
        self.coupons = coupons
        self.returns = returns
        self._bus = bus
        self._outbox: list[DomainEvent] = []
        self._snapshots: dict[str, dict] = {}
        self._repos = {
            "carts": carts,
            "orders": orders,
            "products": products,
            "stocks": stocks,
            "payments": payments,
            "shipments": shipments,
            "pickups": pickups,
            "coupons": coupons,
            "returns": returns,
        }

    # -- UnitOfWork --------------------------------------------------------
    def collect(self, *aggregates) -> None:
        for agg in aggregates:
            if agg is None:
                continue
            pop = getattr(agg, "pop_events", None)
            if callable(pop):
                self._outbox.extend(pop())

    def commit(self) -> list[DomainEvent]:
        outbox, self._outbox = self._outbox, []
        self._snapshots.clear()  # commit point: snapshots no longer needed
        if outbox:
            self._bus.publish(*outbox)
        return outbox

    def rollback(self) -> None:
        self._outbox.clear()
        for name, snap in self._snapshots.items():
            self._repos[name]._restore_store(snap)
        self._snapshots.clear()

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "InMemoryUnitOfWork":
        self._snapshots = {n: r._snapshot_store() for n, r in self._repos.items()}
        self._outbox = []
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        # do not suppress exceptions
