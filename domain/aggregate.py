"""Aggregate base: identity + the domain-event outbox (in-memory).

In a production system the outbox would be a transactional outbox table;
the in-memory list keeps the invariants honest and testable.
"""
from __future__ import annotations

import itertools
import uuid

from .events import DomainEvent

_counters: dict[type, itertools.count] = {}


def _next_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Aggregate:
    """Base class for all aggregate roots.

    - Exactly one root per aggregate; all mutation goes through root methods.
    - Children never mutate the parent's invariants directly.
    - pending_events is drained via pop_events() (the in-memory outbox).
    """

    prefix: str = "agg"

    def __init__(self) -> None:
        self._pending: list[DomainEvent] = []
        self._lock: str | None = None

    # -- identity -----------------------------------------------------------
    def aggregate_id(self) -> str:
        raise NotImplementedError

    # -- event outbox -------------------------------------------------------
    def raise_event(self, ev: DomainEvent) -> None:
        if self._lock is not None:
            raise RuntimeError(
                f"{type(self).__name__} is locked by another aggregate "
                f"({self._lock}); cross-aggregate mutation is forbidden"
            )
        self._pending.append(ev)

    def pop_events(self) -> list[DomainEvent]:
        evs, self._pending = self._pending, []
        return evs

    def peek_events(self) -> list[DomainEvent]:
        return list(self._pending)

    # -- cross-aggregate safety (used by orchestration) ----------------------
    def _enter(self) -> None:
        assert self._lock is None
        self._lock = f"{type(self).__name__}:{id(self)}"

    def _exit(self) -> None:
        self._lock = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.aggregate_id()}>"
