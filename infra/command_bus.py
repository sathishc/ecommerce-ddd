"""Command Bus: dispatch user-initiated Commands to exactly one handler.

Pattern implemented: Command Bus (point-to-point intent dispatch). Each
``Command`` (a user intent from ``domain/commands.py`` such as ``PlaceOrder``)
is routed to exactly one registered handler, which runs one atomic
Unit-of-Work and returns a result. Unhandled commands raise
``UnregisteredCommand`` rather than silently dropping the intent.

Mirrors ``infra/event_bus.py`` on purpose:
- ``EventBus``  = pub/sub over facts (0..N handlers, fire-and-forget).
- ``CommandBus`` = point-to-point over intents (exactly 1 handler, returns a
  result, raises when unhandled). Synchronous dispatch keeps tests
  deterministic, same as events.

AWS <-> local mapping: this in-memory bus is the local stand-in for an
API-Gateway/SQS front door in AWS -- same ``register``/``dispatch`` contract,
so swapping in an SQS-backed dispatcher later needs no handler changes (DIP).

How it supports atomicity/testability: ``dispatch`` records every command in
``_dispatched`` (spy-friendly inspection via ``dispatched()``/``names()``)
and delegates atomicity to the handler's UoW -- a failed handler rolls back
state *and* publishes no events (see ``infra/unit_of_work.py``), so tests can
assert ``orders.list() == []`` and an unchanged event stream after failure.

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
    """No handler registered for this command type.

    Raised by :meth:`CommandBus.dispatch` when ``command.command_name`` has no
    binding. Fail-fast (instead of a silent no-op) so missing wiring in
    ``infra/container.py`` surfaces immediately in tests.
    """


class CommandBus:
    """In-memory command dispatcher (local stand-in for SQS/API-Gateway).

    Contract: exactly one handler per command type (last registration wins);
    ``dispatch`` runs it synchronously and returns its result. Every
    dispatched command is recorded for test inspection -- no mocks needed.
    """

    def __init__(self) -> None:
        # Keyed by command class name (``command.command_name``), not the class
        # object itself, so pickled/recreated commands still route correctly.
        self._handlers: dict[str, Handler] = {}
        # Append-only audit trail of dispatched commands (spy-friendly).
        self._dispatched: list[Command] = []

    # -- registration ------------------------------------------------------
    def register(self, command_cls: Type[Command], handler: Handler) -> None:
        """Bind one handler to one command type (second registration wins).

        Args:
            command_cls: The ``Command`` subclass to route.
            handler: Callable taking the command and returning its result.
                Runs inside its own atomic UoW (see application handlers).
        """
        self._handlers[command_cls.__name__] = handler

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, command: Command) -> Any:
        """Run the command's handler and return its result.

        Args:
            command: The user intent to handle (e.g. ``PlaceOrder(...)``).

        Returns:
            Whatever the registered handler returns (order/payment tuples,
            aggregates, breakdowns, ...).

        Raises:
            UnregisteredCommand: If no handler was registered for
                ``command.command_name``.
            DomainError: Propagated from the handler when invariants fail;
                the handler's UoW guarantees rollback + no event publication.
        """
        try:
            handler = self._handlers[command.command_name]
        except KeyError:
            # ``from None`` hides the KeyError chain: the domain-level error
            # (missing wiring) is what callers should see.
            raise UnregisteredCommand(
                f"no handler registered for {command.command_name!r}"
            ) from None
        # Record BEFORE handling so even a failing command leaves an audit
        # trail; state rollback is the handler/UoW's job, not the bus's.
        self._dispatched.append(command)
        return handler(command)

    # -- inspection (tests) -------------------------------------------------
    def dispatched(self, name: str | None = None) -> list[Command]:
        """Return dispatched commands, optionally filtered by command name.

        Args:
            name: When given, only commands with this ``command_name``.

        Returns:
            A defensive copy (mutating it never affects the bus record).
        """
        if name is None:
            return list(self._dispatched)
        return [c for c in self._dispatched if c.command_name == name]

    def names(self) -> list[str]:
        """Command names in dispatch order (handy for flow assertions).

        Returns:
            List of ``command_name`` strings, oldest first.
        """
        return [c.command_name for c in self._dispatched]

    def clear(self) -> None:
        """Discard the dispatch record (test isolation between scenarios)."""
        self._dispatched.clear()
