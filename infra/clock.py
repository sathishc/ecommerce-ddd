"""Testability seam for wall-clock access (DIP + local-first Blocks philosophy).

Pattern implemented: Dependency Inversion via a ``Clock`` port. The domain
previously called ``datetime.now()`` directly (e.g. ``Pickup`` slot
validation in ``domain/pickup.py``), which made tests time-bombed: a hardcoded
``NOW`` in the past eventually fails once wall-clock passes it. Services and
aggregates should take a ``Clock`` instead so production and tests inject
different adapters.

AWS <-> local mapping: ``SystemClock`` is the production adapter (real wall
clock, like CloudWatch time in AWS); ``FixedClock`` is the local/test adapter
(frozen, manually advanceable time -- no AWS, no flakiness). The container
(``infra/container.py``) wires ``SystemClock`` by default and ``FixedClock``
via ``build_test_container()``.

How it supports atomicity/testability: time is read through ``now()`` inside
the Unit-of-Work boundary, so a test can freeze ``now``, run checkout/return
flows, then ``advance(days=31)`` to cross the 30-day return window (RT1)
deterministically. ``today()`` is derived from ``now()`` so subclasses only
override one method (Template Method, no duplication).
"""
from __future__ import annotations

import abc
from datetime import date, datetime


class Clock(abc.ABC):
    """Interface (DIP): time source port. Production uses SystemClock, tests use FixedClock.

    Contract: ``now()`` must return the current datetime from the caller's
    frame of reference (wall clock in prod, frozen value in tests). Subclasses
    must not perform I/O beyond reading the clock.
    """

    @abc.abstractmethod
    def now(self) -> datetime:
        """Return the current datetime.

        Returns:
            The current datetime (naive, local frame) per this clock's policy.

        Raises:
            NotImplementedError: If a subclass fails to implement the port.
        """

    def today(self) -> date:
        """Return the current date, derived from :meth:`now`.

        Derived (Template Method) so subclasses only override ``now()`` --
        one source of truth for "current time", no drift between ``now`` and
        ``today``.

        Returns:
            The date component of :meth:`now`.
        """
        return self.now().date()


class SystemClock(Clock):
    """Production clock adapter: the real wall clock.

    Used by ``build_container()``. No state, no test controls -- delegates
    straight to ``datetime.now()`` so domain slot/window checks see live time.
    """

    def now(self) -> datetime:
        """Return wall-clock time.

        Returns:
            ``datetime.now()`` at call time.
        """
        return datetime.now()


class FixedClock(Clock):
    """Test clock adapter: frozen time, manually advanceable.

    Spy-friendly: tests construct ``FixedClock(at)``, inject it via
    ``build_test_container(at)``, then ``advance()``/``set()`` to simulate the
    passage of time (e.g. crossing the RT1 30-day return window) without
    sleeping or touching the wall clock.
    """

    def __init__(self, at: datetime) -> None:
        """Freeze time at a fixed instant.

        Args:
            at: The instant ``now()`` returns until advanced.
        """
        self._at = at

    def now(self) -> datetime:
        """Return the frozen instant.

        Returns:
            The datetime passed to the constructor or last set via
            :meth:`advance`/:meth:`set`.
        """
        return self._at

    def advance(self, **kwargs) -> None:
        """Move time forward, e.g. ``clock.advance(days=31)``.

        Args:
            **kwargs: Keyword arguments forwarded to ``timedelta`` (days,
                hours, minutes, ...). Positive deltas move forward.
        """
        from datetime import timedelta

        # Local import keeps module import cheap (timedelta only needed here).
        self._at = self._at + timedelta(**kwargs)

    def set(self, at: datetime) -> None:
        """Jump to an absolute time.

        Args:
            at: The new frozen instant returned by subsequent ``now()`` calls.
        """
        self._at = at
