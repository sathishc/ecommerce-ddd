"""Aggregate base: identity + the domain-event outbox (in-memory).

Bounded context: shared domain kernel — used by every aggregate root
(``Cart``, ``Coupon``, ``Product``, ``Order``, ``Stock``, ``Payment``,
``Shipment``, ``Pickup``, ``Return``) across all bounded contexts.

Responsibility:
  - Give every root a stable identity (``aggregate_id()``) and a
    per-aggregate event outbox (``raise_event`` / ``pop_events``).
  - Enforce the "one root per aggregate" rule: all mutation goes through
    root methods; children never mutate a parent's invariants directly.
  - Provide the cross-aggregate concurrency guard (``_enter`` / ``_exit``
    lock + ``raise_event`` lock check) used by orchestration so two
    aggregates are never mutated as one atomic unit by accident.

Key invariants: the outbox is drained via ``pop_events()`` (transactional
outbox semantics — events publish only on UoW commit). In production the
in-memory list would be a transactional outbox table; the list keeps the
invariants honest and testable without I/O.
"""
from __future__ import annotations

import itertools
import uuid

from .events import DomainEvent

_counters: dict[type, itertools.count] = {}


def _next_id(prefix: str) -> str:
    """Generate a unique aggregate identity string.

    Args:
        prefix: Short per-aggregate namespace (e.g. ``"cart"``, ``"cpn"``,
            ``"prd"``) so ids are human-scannable in logs/events.

    Returns:
        A ``"<prefix>-<8 hex chars>"`` unique id (uuid4-based; globally
        unique without coordination — safe for tests and local runs).
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Aggregate:
    """Base class for all aggregate roots.

    Role: the DDD aggregate-root contract — exactly one root per
    aggregate; all mutation goes through root methods and every
    state change that outsiders care about is recorded as a domain
    event in the in-memory outbox.

    Aggregate membership: parent of ``Cart``, ``Coupon``, ``Product``
    (and Order/Stock/Payment/Shipment/Pickup/Return). Children such as
    ``_CartLine`` or ``_Redemption`` never subclass this.

    Invariants enforced:
      - Children never mutate the parent's invariants directly (they are
        plain dataclasses; only root methods touch them).
      - ``pending_events`` is drained via ``pop_events()`` (outbox).
      - While ``_lock`` is held (orchestration entered a sibling
        aggregate), ``raise_event`` refuses — cross-aggregate mutation
        in one step is forbidden.

    Example usage:
        class Cart(Aggregate):
            def add_product(self, ...):
                ...
                self.raise_event(CartItemAdded(...))
        events = cart.pop_events()  # drained by the UoW on commit
    """

    prefix: str = "agg"
    """ID namespace for this aggregate family; subclasses override."""

    def __init__(self) -> None:
        """Initialize an empty event outbox and an unlocked guard.

        No events, no lock held. Subclasses must call ``super().__init__()``
        before raising their creation event.
        """
        self._pending: list[DomainEvent] = []
        self._lock: str | None = None

    # -- identity -----------------------------------------------------------
    def aggregate_id(self) -> str:
        """Return this aggregate's stable identity string.

        Returns:
            The aggregate id (e.g. ``cart-<hex>``). Used by repositories,
            events, and the UoW to correlate loads/stores.

        Raises:
            NotImplementedError: Base implementation only — every concrete
                root (Cart/Coupon/Product/...) must override.
        """
        raise NotImplementedError

    # -- event outbox -------------------------------------------------------
    def raise_event(self, ev: DomainEvent) -> None:
        """Record a domain event in the in-memory outbox.

        Args:
            ev: The past-tense domain event to append (e.g.
                ``CartItemAdded``, ``CouponUnredeemed``).

        Raises:
            RuntimeError: If this aggregate is currently locked by
                orchestration (``_enter`` held by a sibling) — the
                concurrency guard against cross-aggregate mutation.

        Events emitted: none directly — this *buffers* the given event
            for later ``pop_events()`` draining on UoW commit.
        """
        if self._lock is not None:
            # Concurrency guard: orchestration locks one aggregate at a
            # time; an event raised on a locked sibling means two roots
            # are being mutated as one unit — forbid it loudly.
            raise RuntimeError(
                f"{type(self).__name__} is locked by another aggregate "
                f"({self._lock}); cross-aggregate mutation is forbidden"
            )
        self._pending.append(ev)

    def pop_events(self) -> list[DomainEvent]:
        """Drain and return all buffered domain events.

        Returns:
            The pending events in raise order; the outbox is left empty.
            The UoW calls this on commit (transactional-outbox drain).
        """
        evs, self._pending = self._pending, []
        return evs

    def peek_events(self) -> list[DomainEvent]:
        """Return a copy of the buffered events without draining.

        Returns:
            A shallow copy of the pending outbox (safe for assertions
            and Notification pub/sub previews; does not clear state).
        """
        return list(self._pending)

    # -- cross-aggregate safety (used by orchestration) ----------------------
    def _enter(self) -> None:
        """Mark this aggregate as exclusively entered by orchestration.

        Raises:
            AssertionError: If already locked (re-entrant entry) — signals
                an orchestration bug, not a domain failure.
        """
        assert self._lock is None
        self._lock = f"{type(self).__name__}:{id(self)}"

    def _exit(self) -> None:
        """Release the orchestration lock, allowing events again.

        Idempotent-by-contract: always clears to ``None``; called in a
        ``finally`` by orchestration so a failed step never deadlocks.
        """
        self._lock = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Return a short debug label (class + id)."""
        return f"<{type(self).__name__} {self.aggregate_id()}>"
