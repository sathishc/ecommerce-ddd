"""Command Bus: dispatch user-initiated Commands to exactly one handler.

Mirrors ``infra/event_bus.py`` on purpose:
- ``EventBus``  = pub/sub over facts (0..N handlers, fire-and-forget).
- ``CommandBus`` = point-to-point over intents (exactly 1 handler, returns a
  result, raises when unhandled). Synchronous dispatch keeps tests
  deterministic, same as events.

Usage::

    bus = CommandBus()
    bus.register(PlaceOrder, lambda cmd: checkout.handle_place_order(cmd))
    order, payment = bus.dispatch(PlaceOrder(cart_id=..., instrument_ref=...))
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Type

from domain.commands import Command

Handler = Callable[[Command], Any]


class UnregisteredCommand(Exception):
    """No handler registered for this command type."""


class CommandBus:
    """In-memory command dispatcher (local stand-in for SQS/API-Gateway)."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._dispatched: list[Command] = []

    # -- registration ------------------------------------------------------
    def register(self, command_cls: Type[Command], handler: Handler) -> None:
        """Bind one handler to one command type (second registration wins)."""
        self._handlers[command_cls.__name__] = handler

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, command: Command) -> Any:
        """Run the command's handler and return its result."""
        try:
            handler = self._handlers[command.command_name]
        except KeyError:
            raise UnregisteredCommand(
                f"no handler registered for {command.command_name!r}"
            ) from None
        self._dispatched.append(command)
        return handler(command)

    # -- inspection (tests) -------------------------------------------------
    def dispatched(self, name: str | None = None) -> list[Command]:
        if name is None:
            return list(self._dispatched)
        return [c for c in self._dispatched if c.command_name == name]

    def names(self) -> list[str]:
        return [c.command_name for c in self._dispatched]

    def clear(self) -> None:
        self._dispatched.clear()
