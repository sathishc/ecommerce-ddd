"""Unit of Work (atomic business transactions + outbox publishing).

Pattern implemented: Unit of Work + Transactional Outbox. One UoW wraps one
business transaction (e.g. one checkout): services mutate aggregates through
``uow.<repos>``, drain their pending domain events into an in-memory outbox
via ``collect()``, and on clean context exit the outbox is published through
the shared ``EventBus``. Any exception triggers ``rollback()`` instead --
repository snapshots are restored and the outbox is discarded, so observers
never see facts from failed transactions.

Pattern:
    with uow_factory() as uow:
        ... mutate aggregates via uow.<repos> ...
        uow.collect(*aggregates)   # drain pending domain events into outbox
    # <- __exit__ commits (publishes outbox via EventBus) or rolls back

- Atomicity: repositories are snapshotted on entry; any exception restores
  them, so a failed checkout leaves no dangling reservations/redemptions.
- Outbox: events are published to the EventBus only on successful commit —
  the transactional-outbox guarantee that makes the event architecture safe.
  ``commit()`` publishes exactly once and clears commit state (snapshots),
  so a second ``commit()`` is a harmless no-op rather than a duplicate
  publish.
- Services depend on the ``UnitOfWork`` ABC / factory (DIP); tests inject an
  in-memory factory, production would inject a DynamoDB-transaction factory.

AWS <-> local mapping: ``InMemoryUnitOfWork`` (deepcopy snapshots + EventBus
outbox) is the local Block; an AWS port would use DynamoDB transactions +
DynamoDB-Streams/EventBridge outbox under the same ``UnitOfWork`` ABC (LSP).
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
    """Interface services program against (DIP).

    Contract: expose one repository per aggregate (shared with the container
    in local runs); ``collect`` drains aggregate events into the outbox;
    ``commit`` publishes exactly once; ``rollback`` discards the outbox and
    restores pre-UoW repository state. Usable as a context manager so
    services cannot forget the commit/rollback branch.
    """

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
        """Drain pending domain events from aggregates into the commit outbox.

        Args:
            *aggregates: Aggregates whose ``pop_events()`` should be drained.
                ``None`` entries are skipped (lets callers pass optional
                aggregates unconditionally).
        """

    @abc.abstractmethod
    def commit(self) -> list[DomainEvent]:
        """Publish the outbox via the EventBus. Returns published events.

        Returns:
            The events published by this commit (in outbox order). Empty
                when nothing was collected -- never publishes duplicates.

        Contract note: publishes ONLY on commit; rolled-back UoWs publish
            nothing (transactional outbox).
        """

    @abc.abstractmethod
    def rollback(self) -> None:
        """Discard the outbox and restore repository state.

        Contract note: after rollback the bus stream is unchanged and repos
            hold pre-UoW snapshots -- a failed checkout leaves no dangling
            reservations, redemptions, or orders.
        """

    # context-manager protocol
    @abc.abstractmethod
    def __enter__(self) -> "UnitOfWork":
        """Enter the transaction: snapshot repos, reset the outbox.

        Returns:
            This UoW, ready for service mutations.
        """
        ...

    @abc.abstractmethod
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit: commit on clean exit, rollback on exception (never suppress).

        Args:
            exc_type: Exception type if the body raised, else None.
            exc: The exception instance, if any.
            tb: Traceback, if any.
        """
        ...


UoWFactory = Callable[[], UnitOfWork]


class InMemoryUnitOfWork(UnitOfWork):
    """Local UoW: shared repos + deepcopy snapshots + EventBus outbox.

    Local-first Block for atomic transactions. Holds REFERENCES to the
    container's shared repositories (not copies), so committed mutations are
    instantly visible to later UoWs, while ``__enter__`` snapshots give
    rollback something to restore on failure.
    """

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
        """Wire shared repos + the outbox drain (all keyword-only, explicit DI).

        Args:
            carts: Shared cart repository (snapshot/rollback participant).
            orders: Shared order repository.
            products: Shared product repository.
            stocks: Shared stock repository.
            payments: Shared payment repository.
            shipments: Shared shipment repository.
            pickups: Shared pickup repository.
            coupons: Shared coupon repository.
            returns: Shared RMA repository.
            bus: EventBus the outbox publishes through on commit ONLY.
        """
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
        # Transactional outbox: drained by collect(), published by commit(),
        # discarded by rollback() -- never published before commit.
        self._outbox: list[DomainEvent] = []
        # Per-repo deepcopy snapshots keyed by attribute name; taken on
        # __enter__, cleared on commit/rollback.
        self._snapshots: dict[str, dict] = {}
        # Name -> repo lookup so rollback can restore by snapshot key.
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
        """Drain each aggregate's pending events into the commit outbox.

        Args:
            *aggregates: Aggregates to drain. ``None`` is skipped; objects
                without a callable ``pop_events`` are ignored (duck-typing
                keeps the UoW decoupled from the aggregate base class).
        """
        for agg in aggregates:
            if agg is None:
                # Lets callers pass optional aggregates unconditionally.
                continue
            pop = getattr(agg, "pop_events", None)
            if callable(pop):
                # ``pop_events`` drains (returns + clears), so each event is
                # collected exactly once even if collect is called twice.
                self._outbox.extend(pop())

    def commit(self) -> list[DomainEvent]:
        """Publish the outbox via the EventBus (commit point).

        Returns:
            The published events in collection order ([] when empty).

        Contract: swaps the outbox BEFORE publishing (re-entrancy safe),
            clears snapshots (rollback window closes at commit), and only
            then publishes -- so handlers observe durable (committed) state.
        """
        # Swap-then-publish: handlers that trigger nested collects cannot
        # duplicate or corrupt this commit's outbox.
        outbox, self._outbox = self._outbox, []
        self._snapshots.clear()  # commit point: snapshots no longer needed
        if outbox:
            self._bus.publish(*outbox)
        return outbox

    def rollback(self) -> None:
        """Discard the outbox and restore every snapshotted repository.

        Contract: bus stream untouched (nothing was published), repos return
            to pre-UoW snapshots, snapshots cleared so the UoW is reusable.
        """
        self._outbox.clear()
        for name, snap in self._snapshots.items():
            self._repos[name]._restore_store(snap)
        self._snapshots.clear()

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "InMemoryUnitOfWork":
        """Snapshot all repos and reset the outbox for a fresh transaction.

        Returns:
            Self, with one deepcopy snapshot per repo and an empty outbox.
        """
        # Deepcopy per repo: in-place aggregate mutations inside the block
        # must not leak into the snapshot, or rollback would be hollow.
        self._snapshots = {n: r._snapshot_store() for n, r in self._repos.items()}
        self._outbox = []
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit on clean exit, rollback on any exception.

        Args:
            exc_type: Exception type from the body, or None on success.
            exc: Exception instance, if any.
            tb: Traceback, if any.

        Contract: never suppresses exceptions (returns None implicitly) --
            callers always see the original failure after rollback.
        """
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        # do not suppress exceptions
