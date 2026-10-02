"""Testability seam for wall-clock access (DIP).

The domain previously called ``datetime.now()`` directly (e.g. Pickup slot
validation), which made tests time-bombed: a hardcoded ``NOW`` in the past
eventually fails. Services and aggregates should take a ``Clock`` instead.
"""
from __future__ import annotations

import abc
from datetime import date, datetime


class Clock(abc.ABC):
    """Interface (DIP): time source. Production uses SystemClock, tests use FixedClock."""

    @abc.abstractmethod
    def now(self) -> datetime:
        """Current datetime."""

    def today(self) -> date:
        """Current date (derived so subclasses only override now())."""
        return self.now().date()


class SystemClock(Clock):
    """Production clock: the real wall clock."""

    def now(self) -> datetime:
        return datetime.now()


class FixedClock(Clock):
    """Test clock: frozen time, manually advanceable."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, **kwargs) -> None:
        """Move time forward, e.g. ``clock.advance(days=31)``."""
        from datetime import timedelta

        self._at = self._at + timedelta(**kwargs)

    def set(self, at: datetime) -> None:
        """Jump to an absolute time."""
        self._at = at
