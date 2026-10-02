"""Notification context: event-driven reactions (pub/sub over the EventBus).

Pattern implemented: pub/sub subscriber (Observer). ``NotificationService``
registers itself as a wildcard handler on the shared ``EventBus`` (see
``infra/container.py`` wiring) and reacts to committed domain facts -- it
never drives business state, it only observes it (downstream-conformist
context, context map §2).

Every domain event lands here. The service logs it (Logger ~ CloudWatch
Logs), counts it (Metrics ~ CloudWatch) and sends customer emails for the
moments a customer cares about (EmailClient ~ SES):

- OrderPlaced        -> order confirmation
- ShipmentShipped    -> shipped + tracking
- OrderDelivered     -> delivered
- ReturnRefunded     -> refund confirmation

AWS <-> local mapping: ``EmailClient`` is SES in AWS / ``LocalEmailClient``
(captures sends) locally; ``Logger``/``Metrics`` are CloudWatch in AWS /
``StdLogger``/``LocalMetrics`` locally. Same ``handle(event)`` contract in
both backends (LSP).

How it supports atomicity/testability: because the UoW publishes events only
on commit (transactional outbox), every event handled here reflects committed
state -- no "order confirmed" email for a rolled-back checkout. ``handled``
records every event received, so tests assert reactions without touching real
AWS services (testability via the bus + LocalEmailClient).

``handled`` records every event received, so tests assert reactions without
touching real AWS services (testability via the bus + LocalEmailClient).
"""
from __future__ import annotations

from domain.events import DomainEvent
from infra.blocks import EmailClient, Logger, Metrics
from infra.event_bus import EventBus


class NotificationService:
    """Subscribes to *all* domain events (pub/sub, context map §2).

    Observer over committed facts: logs + counts every event, sends email only
    for customer-meaningful moments. Holds no business state -- ``handled``
    is an append-only spy record for tests.
    """

    def __init__(self, mailer: EmailClient, logger: Logger, metrics: Metrics) -> None:
        """Wire Block ports (DIP -- concrete backends chosen by the container).

        Args:
            mailer: Transactional-email port (SES in AWS, capture in local).
            logger: Structured-log port (CloudWatch Logs in AWS).
            metrics: Counter port (CloudWatch in AWS).
        """
        self._mailer = mailer
        self._logger = logger
        self._metrics = metrics
        # Spy-friendly record of every event received, in dispatch order.
        self.handled: list[DomainEvent] = []

    # -- wiring -------------------------------------------------------------
    def register(self, bus: EventBus) -> None:
        """Subscribe to every event on the bus.

        Args:
            bus: The shared event bus. Registers :meth:`handle` as a wildcard
                handler so ALL committed events fan out here.
        """
        bus.subscribe_all(self.handle)

    # -- handling ------------------------------------------------------------
    def handle(self, event: DomainEvent) -> None:
        """React to one committed domain event: log, count, maybe email.

        Args:
            event: The committed fact. Unknown event names are still logged
                and counted (forward-compatible); only the four
                customer-meaningful names trigger email.
        """
        # Record first so inspection works even if a later email send raises.
        self.handled.append(event)
        self._logger.info("domain event", event=event.name)
        self._metrics.increment(f"events.{event.name}")
        self._metrics.increment("events.total")

        # Dispatch table (not if/elif chain): adding a new customer email is
        # one new dict entry + one small ``_on_*`` method (OCP).
        action = {
            "OrderPlaced": self._on_order_placed,
            "ShipmentShipped": self._on_shipped,
            "OrderDelivered": self._on_delivered,
            "ReturnRefunded": self._on_refunded,
        }.get(event.name)
        if action is not None:
            action(event)

    def _customer(self, event: DomainEvent) -> str:
        """Best-effort customer address for an event.

        Args:
            event: The event to extract ``customer_id`` from.

        Returns:
            The event's ``customer_id`` or a fallback placeholder when the
            event carries none (keeps local email capture total, never fails).
        """
        # ``getattr`` (not attribute access): events are heterogeneous, and
        # not every event type carries ``customer_id``.
        return getattr(event, "customer_id", "") or "customer@example.com"

    def _on_order_placed(self, e: DomainEvent) -> None:
        """Send the order-confirmation email (OrderPlaced -> SES).

        Args:
            e: The ``OrderPlaced`` event (order_id, money_total, customer).
        """
        total = getattr(e, "money_total", None)
        self._mailer.send(
            self._customer(e), "Order confirmed",
            f"Order {getattr(e, 'order_id', '')} placed for {total}.",
        )

    def _on_shipped(self, e: DomainEvent) -> None:
        """Send the shipped + tracking email (ShipmentShipped -> SES).

        Args:
            e: The ``ShipmentShipped`` event (shipment_id, tracking_number).
        """
        self._mailer.send(
            self._customer(e), "Order shipped",
            f"Shipment {getattr(e, 'shipment_id', '')} is on its way "
            f"(tracking {getattr(e, 'tracking_number', '')}).",
        )

    def _on_delivered(self, e: DomainEvent) -> None:
        """Send the delivered email (OrderDelivered -> SES).

        Args:
            e: The ``OrderDelivered`` event (order_id).
        """
        self._mailer.send(
            self._customer(e), "Order delivered",
            f"Order {getattr(e, 'order_id', '')} was delivered.",
        )

    def _on_refunded(self, e: DomainEvent) -> None:
        """Send the refund-confirmation email (ReturnRefunded -> SES).

        Args:
            e: The ``ReturnRefunded`` event (rma_id, amount).
        """
        self._mailer.send(
            self._customer(e), "Return refunded",
            f"Return {getattr(e, 'rma_id', '')} refunded {getattr(e, 'amount', '')}.",
        )

    # -- test helpers ----------------------------------------------------------
    def handled_names(self) -> list[str]:
        """Event names received, in order.

        Returns:
            List of ``event.name`` strings, oldest first (defensive copy
            semantics via list comprehension).
        """
        return [e.name for e in self.handled]

    def clear(self) -> None:
        """Discard the handled record (test isolation between scenarios)."""
        self.handled.clear()
