"""Event-driven core: publish/subscribe over immutable domain events.

Pattern implemented: synchronous in-memory pub/sub (Observer) over past-tense
domain facts from ``domain/events.py``. Producers (aggregates, via the UoW
outbox) never know their consumers (Notification context, test spies) --
the bus decouples them (DIP). This is the local-first port of the AWS
EventBridge/SQS + Realtime fan-out: same ``subscribe``/``publish`` contract,
no AWS account needed.

Design (testability first):
- Handlers are plain callables ``(event) -> None`` registered per event name
  (or for every event). No framework, no threads: synchronous dispatch keeps
  tests deterministic.
- The bus records every published event (``published``) so tests can assert
  the exact Notification-context stream without mocks.
- Failures in one handler never block the others; they are collected and
  re-raised as a single ``HandlerErrors`` after dispatch (or swallowed when
  the bus is constructed with ``raise_on_error=False``).

How it supports atomicity: the bus itself is NOT transactional -- the
transactional-outbox guarantee lives in ``infra/unit_of_work.py``, which only
calls ``publish`` on successful commit. Events observed here therefore always
reflect committed state; rolled-back UoWs never reach the bus.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from domain.events import DomainEvent

Handler = Callable[[DomainEvent], None]


class HandlerErrors(Exception):
    """One or more event handlers failed; carries each underlying error.

    Raised by :meth:`EventBus.publish` after ALL handlers ran, so one bad
    subscriber (e.g. a test spy) never starves the others. Inspect ``errors``
    for the per-handler exceptions in dispatch order.
    """

    def __init__(self, errors: list[BaseException]) -> None:
        """Collect per-handler failures into one exception.

        Args:
            errors: The underlying exceptions, in handler-dispatch order.
        """
        super().__init__(f"{len(errors)} event handler(s) failed")
        self.errors = errors


class EventBus:
    """In-memory pub/sub (local stand-in for EventBridge/SQS + Realtime).

    Contract: ``subscribe`` registers 1..N handlers per event name;
    ``subscribe_all`` registers wildcard (Notification-style) handlers;
    ``publish`` records then synchronously dispatches each event in order.
    Spy-friendly: ``published()``/``names()`` expose the recorded stream.
    """

    def __init__(self, raise_on_error: bool = True) -> None:
        """Create an empty bus.

        Args:
            raise_on_error: When True (default), collected handler failures
                are re-raised as :class:`HandlerErrors` after dispatch.
                When False, failures are swallowed -- useful for
                best-effort notification paths where one bad subscriber
                must not fail the business transaction.
        """
        # Per-event handlers keyed by ``event.name`` (e.g. "OrderPlaced").
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        # Wildcard handlers (NotificationService) run for EVERY event.
        self._wildcards: list[Handler] = []
        # Append-only record of published events (test inspection, no mocks).
        self._published: list[DomainEvent] = []
        self._raise = raise_on_error

    # -- subscription ------------------------------------------------------
    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Handle one event type (use ``event_cls.__name__`` as the name).

        Args:
            event_name: The ``DomainEvent.name`` to listen for.
            handler: Callable ``(event) -> None`` invoked on each publish.
        """
        self._handlers[event_name].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """Handle every event (the Notification context does this).

        Args:
            handler: Callable ``(event) -> None`` invoked for every published
                event, after any per-event handlers for that event.
        """
        self._wildcards.append(handler)

    # -- publishing --------------------------------------------------------
    def publish(self, *events: DomainEvent) -> None:
        """Record + synchronously dispatch each event, in order.

        Args:
            *events: Domain facts to record and fan out, in publish order.

        Raises:
            HandlerErrors: If any handler failed and the bus was constructed
                with ``raise_on_error=True``. Raised AFTER all handlers ran,
                carrying every underlying error.
        """
        errors: list[BaseException] = []
        for ev in events:
            # Record first so the stream reflects attempted truth even if a
            # handler later raises; handlers observe committed UoW state.
            self._published.append(ev)
            # Per-event handlers first, then wildcards -- deterministic order.
            targets = [*self._handlers.get(ev.name, []), *self._wildcards]
            for h in targets:
                try:
                    h(ev)
                except BaseException as e:  # noqa: BLE001 - collected below
                    # Collect (don't short-circuit): one failing subscriber
                    # must not starve the remaining subscribers.
                    errors.append(e)
        if errors and self._raise:
            raise HandlerErrors(errors)

    # -- inspection (tests) -------------------------------------------------
    def published(self, name: str | None = None) -> list[DomainEvent]:
        """All published events, optionally filtered by event name.

        Args:
            name: When given, only events with this ``DomainEvent.name``.

        Returns:
            A defensive copy in publish order.
        """
        if name is None:
            return list(self._published)
        return [e for e in self._published if e.name == name]

    def names(self) -> list[str]:
        """Event names in publish order (handy for stream assertions).

        Returns:
            List of ``event.name`` strings, oldest first.
        """
        return [e.name for e in self._published]

    def clear(self) -> None:
        """Discard the recorded stream (test isolation between scenarios)."""
        self._published.clear()
