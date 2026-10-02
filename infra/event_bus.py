"""Event-driven core: publish/subscribe over immutable domain events.

Design (testability first):
- Handlers are plain callables ``(event) -> None`` registered per event name
  (or for every event). No framework, no threads: synchronous dispatch keeps
  tests deterministic.
- The bus records every published event (``published``) so tests can assert
  the exact Notification-context stream without mocks.
- Failures in one handler never block the others; they are collected and
  re-raised as a single ``HandlerErrors`` after dispatch (or swallowed when
  the bus is constructed with ``raise_on_error=False``).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from domain.events import DomainEvent

Handler = Callable[[DomainEvent], None]


class HandlerErrors(Exception):
    """One or more event handlers failed; carries each underlying error."""

    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__(f"{len(errors)} event handler(s) failed")
        self.errors = errors


class EventBus:
    """In-memory pub/sub (local stand-in for EventBridge/SQS + Realtime)."""

    def __init__(self, raise_on_error: bool = True) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcards: list[Handler] = []
        self._published: list[DomainEvent] = []
        self._raise = raise_on_error

    # -- subscription ------------------------------------------------------
    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Handle one event type (use ``event_cls.__name__`` as the name)."""
        self._handlers[event_name].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        """Handle every event (the Notification context does this)."""
        self._wildcards.append(handler)

    # -- publishing --------------------------------------------------------
    def publish(self, *events: DomainEvent) -> None:
        """Record + synchronously dispatch each event, in order."""
        errors: list[BaseException] = []
        for ev in events:
            self._published.append(ev)
            targets = [*self._handlers.get(ev.name, []), *self._wildcards]
            for h in targets:
                try:
                    h(ev)
                except BaseException as e:  # noqa: BLE001 - collected below
                    errors.append(e)
        if errors and self._raise:
            raise HandlerErrors(errors)

    # -- inspection (tests) -------------------------------------------------
    def published(self, name: str | None = None) -> list[DomainEvent]:
        """All published events, optionally filtered by event name."""
        if name is None:
            return list(self._published)
        return [e for e in self._published if e.name == name]

    def names(self) -> list[str]:
        """Event names in publish order (handy for stream assertions)."""
        return [e.name for e in self._published]

    def clear(self) -> None:
        self._published.clear()
