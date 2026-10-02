"""Composition root (DI container): the ONLY place concretes are chosen.

``build_container()`` wires the whole backend for local testing with zero
AWS dependencies:

    container = build_container()
    order, payment = container.checkout.place_and_pay(cart, "card-1")

To deploy against AWS, the same container is built with AWS-backed Blocks
(``BLOCKS_BACKEND=aws`` selects DynamoDB/SES Block backends) — application
services do not change (Blocks philosophy: same code, local or cloud).

For deterministic tests use ``build_test_container(fixed_now=...)`` which
injects a ``FixedClock`` and fresh in-memory everything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from application.services import (
    CancelAppService,
    CheckoutAppService,
    ReturnAppService,
    ShippingAppService,
)
from application.command_handlers import CommandHandlers, register_command_handlers
from infra.command_bus import CommandBus, UnregisteredCommand
from infra.blocks import (
    AppSetting,
    AsyncJob,
    EmailClient,
    FileBucket,
    LocalAppSetting,
    LocalAsyncJob,
    LocalEmailClient,
    LocalFileBucket,
    LocalMetrics,
    LocalTable,
    LocalTracer,
    Logger,
    Metrics,
    StdLogger,
    backend_name,
    make_kv_store,
)
from infra.clock import Clock, FixedClock, SystemClock
from infra.event_bus import EventBus
from infra.handlers import NotificationService
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
from infra.unit_of_work import InMemoryUnitOfWork, UnitOfWork, UoWFactory


@dataclass
class Container:
    """Everything the application needs, injected (no globals, no singletons)."""

    clock: Clock
    bus: EventBus
    mailer: EmailClient
    logger: Logger
    metrics: Metrics
    tracer: LocalTracer
    jobs: AsyncJob
    files: FileBucket
    settings: AppSetting
    notifications: NotificationService
    # shared repositories (the "database" for local runs)
    carts: CartRepository = field(default_factory=CartRepository)
    orders: OrderRepository = field(default_factory=OrderRepository)
    products: ProductRepository = field(default_factory=ProductRepository)
    stocks: StockRepository = field(default_factory=StockRepository)
    payments: PaymentRepository = field(default_factory=PaymentRepository)
    shipments: ShipmentRepository = field(default_factory=ShipmentRepository)
    pickups: PickupRepository = field(default_factory=PickupRepository)
    coupons: CouponRepository = field(default_factory=CouponRepository)
    returns: ReturnRepository = field(default_factory=ReturnRepository)
    # application services (built in __post_init__)
    checkout: CheckoutAppService | None = None
    shipping: ShippingAppService | None = None
    cancels: CancelAppService | None = None
    returns_svc: ReturnAppService | None = None
    # command side: user-initiated intents dispatched as Command objects
    commands: CommandBus | None = None
    command_handlers: CommandHandlers | None = None

    def __post_init__(self) -> None:
        factory: UoWFactory = self.uow_factory
        self.checkout = CheckoutAppService(factory, self.clock, self.tracer, self.jobs)
        self.shipping = ShippingAppService(factory, self.tracer)
        self.cancels = CancelAppService(factory, self.tracer)
        self.returns_svc = ReturnAppService(factory, self.clock, self.tracer, self.files)
        self.commands = CommandBus()
        self.command_handlers = register_command_handlers(
            self.commands, factory, self.clock, self.tracer, self.jobs, self.files
        )
        self.uow_factory_fn = factory

    def uow_factory(self) -> UnitOfWork:
        """Fresh UoW over the SHARED repos (atomic per use, outbox via bus)."""
        return InMemoryUnitOfWork(
            carts=self.carts,
            orders=self.orders,
            products=self.products,
            stocks=self.stocks,
            payments=self.payments,
            shipments=self.shipments,
            pickups=self.pickups,
            coupons=self.coupons,
            returns=self.returns,
            bus=self.bus,
        )


def build_container(
    *,
    clock: Clock | None = None,
    backend: str | None = None,
    settings_values: dict[str, str] | None = None,
    quiet_logger: bool = True,
) -> Container:
    """Build a fully-wired container (DI composition root).

    Args:
        clock: time source (default SystemClock).
        backend: ``local`` (default) or ``aws``; also honours BLOCKS_BACKEND.
        settings_values: AppSetting overrides (e.g. return window days).
        quiet_logger: False to also echo logs to stdout.
    """
    import os

    backend = backend or backend_name()
    if backend not in ("local", "aws"):  # pragma: no cover - guard rail
        raise ValueError(f"unknown backend {backend!r}; want 'local' or 'aws'")

    the_clock: Clock = clock or SystemClock()
    bus = EventBus()
    mailer: EmailClient = LocalEmailClient()
    logger: Logger = StdLogger(quiet=quiet_logger)
    metrics: Metrics = LocalMetrics()
    tracer = LocalTracer()
    jobs: AsyncJob = LocalAsyncJob()
    files: FileBucket = LocalFileBucket()
    settings: AppSetting = LocalAppSetting(
        {"RETURN_WINDOW_DAYS": "30", "BACKEND": backend, **(settings_values or {})}
    )
    notifications = NotificationService(mailer, logger, metrics)
    notifications.register(bus)

    # Touch the Blocks conditional-loading path so the local/AWS selection is
    # exercised even though repos are in-memory (mirrors `new KVStore(...)`).
    make_kv_store("ecommerce")
    LocalTable()

    c = Container(
        clock=the_clock,
        bus=bus,
        mailer=mailer,
        logger=logger,
        metrics=metrics,
        tracer=tracer,
        jobs=jobs,
        files=files,
        settings=settings,
        notifications=notifications,
    )
    # honour an explicit env hint for observability in tests/deploys
    os.environ.setdefault("BLOCKS_BACKEND", backend)
    return c


def build_test_container(at: datetime | None = None, **kwargs) -> Container:
    """Test container: frozen clock + isolated in-memory everything."""
    from datetime import datetime as _dt

    return build_container(clock=FixedClock(at or _dt(2026, 9, 28, 12, 0)), **kwargs)
