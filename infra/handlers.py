"""Notification context: event-driven reactions (pub/sub over the EventBus).

Every domain event lands here. The service logs it (Logger ~ CloudWatch
Logs), counts it (Metrics ~ CloudWatch) and sends customer emails for the
moments a customer cares about (EmailClient ~ SES):

- OrderPlaced        -> order confirmation
- ShipmentShipped    -> shipped + tracking
- OrderDelivered     -> delivered
- ReturnRefunded     -> refund confirmation

``handled`` records every event received, so tests assert reactions without
touching real AWS services (testability via the bus + LocalEmailClient).
"""
from __future__ import annotations

from domain.events import DomainEvent
from infra.blocks import EmailClient, Logger, Metrics
from infra.event_bus import EventBus


class NotificationService:
    """Subscribes to *all* domain events (pub/sub, context map §2)."""

    def __init__(self, mailer: EmailClient, logger: Logger, metrics: Metrics) -> None:
        self._mailer = mailer
        self._logger = logger
        self._metrics = metrics
        self.handled: list[DomainEvent] = []

    # -- wiring -------------------------------------------------------------
    def register(self, bus: EventBus) -> None:
        """Subscribe to every event on the bus."""
        bus.subscribe_all(self.handle)

    # -- handling ------------------------------------------------------------
    def handle(self, event: DomainEvent) -> None:
        self.handled.append(event)
        self._logger.info("domain event", event=event.name)
        self._metrics.increment(f"events.{event.name}")
        self._metrics.increment("events.total")

        action = {
            "OrderPlaced": self._on_order_placed,
            "ShipmentShipped": self._on_shipped,
            "OrderDelivered": self._on_delivered,
            "ReturnRefunded": self._on_refunded,
        }.get(event.name)
        if action is not None:
            action(event)

    def _customer(self, event: DomainEvent) -> str:
        return getattr(event, "customer_id", "") or "customer@example.com"

    def _on_order_placed(self, e: DomainEvent) -> None:
        total = getattr(e, "money_total", None)
        self._mailer.send(
            self._customer(e), "Order confirmed",
            f"Order {getattr(e, 'order_id', '')} placed for {total}.",
        )

    def _on_shipped(self, e: DomainEvent) -> None:
        self._mailer.send(
            self._customer(e), "Order shipped",
            f"Shipment {getattr(e, 'shipment_id', '')} is on its way "
            f"(tracking {getattr(e, 'tracking_number', '')}).",
        )

    def _on_delivered(self, e: DomainEvent) -> None:
        self._mailer.send(
            self._customer(e), "Order delivered",
            f"Order {getattr(e, 'order_id', '')} was delivered.",
        )

    def _on_refunded(self, e: DomainEvent) -> None:
        self._mailer.send(
            self._customer(e), "Return refunded",
            f"Return {getattr(e, 'rma_id', '')} refunded {getattr(e, 'amount', '')}.",
        )

    # -- test helpers ----------------------------------------------------------
    def handled_names(self) -> list[str]:
        """Event names received, in order."""
        return [e.name for e in self.handled]

    def clear(self) -> None:
        self.handled.clear()
