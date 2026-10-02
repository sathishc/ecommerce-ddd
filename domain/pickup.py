"""Fulfillment context — the Pickup aggregate (doorstep return collection).

Invariants:
  PK1  a Pickup belongs to exactly one approved Return
  PK2  scheduledSlot within the courier's service area (modelled as: a slot
       must be provided; area checks live in the courier context, out of scope)
  PK3  PickedUp is terminal; fires only on the courier's on-site scan
       (possession actually transferred)
  PK4  NoAccess re-schedules rather than failing — up to N attempts before
       the Return is flagged for manual handling
  PK5  evidence of collection is MANDATORY — confirmPickup requires captured
       evidence (photo + optional signature): the customer holds no return
       label, so the courier scan + photo is their proof of collection
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, InvalidStateTransition, NoAccessExhausted
from .events import PickupConfirmed, PickupScheduled
from .value_objects import Address, PickupEvidence

SCHEDULED, EN_ROUTE, PICKED_UP, NO_ACCESS = "Scheduled", "EnRoute", "PickedUp", "NoAccess"
TERMINAL = {PICKED_UP}
MAX_ATTEMPTS = 2  # PK4: default re-schedule attempts (configurable in v1)


class Pickup(Aggregate):
    """Aggregate root (identity = pickup_id): one doorstep collection."""

    prefix = "pku"

    def __init__(self, return_id: str, customer_address: Address,
                 scheduled_slot: datetime, max_attempts: int = MAX_ATTEMPTS,
                 now: datetime | None = None) -> None:
        super().__init__()
        if not return_id:
            raise InvariantViolation("PK1: a pickup must belong to a return")
        ref_now = now if now is not None else datetime.now()
        if scheduled_slot <= ref_now:
            raise InvariantViolation("PK2: scheduledSlot must be in the future")
        if max_attempts < 1:
            raise InvariantViolation("PK4: max_attempts must be >= 1")
        self._pickup_id = _next_id(self.prefix)
        self._return_id = return_id            # PK1: exactly one return
        self._customer_address = customer_address
        self._scheduled_slot = scheduled_slot
        self._courier_ref: str | None = None
        self._evidence: PickupEvidence | None = None
        self._attempts = 0
        self._max_attempts = max_attempts
        self._status = SCHEDULED
        self.raise_event(PickupScheduled(self._pickup_id, return_id))

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        return self._pickup_id

    @property
    def pickup_id(self) -> str:
        return self._pickup_id

    @property
    def return_id(self) -> str:
        return self._return_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def evidence(self) -> PickupEvidence | None:
        return self._evidence

    @property
    def attempts(self) -> int:
        return self._attempts

    # -- behavior -------------------------------------------------------------
    def assign_courier(self, courier_ref: str) -> None:
        if self._status not in (SCHEDULED, NO_ACCESS):
            raise InvalidStateTransition(f"can only assign a courier before pickup, not from {self._status}")
        self._courier_ref = courier_ref
        self._status = EN_ROUTE

    def confirm_pickup(self, evidence: PickupEvidence) -> None:
        """PK3/PK5: the possession-transfer event. Evidence is MANDATORY."""
        if self._status != EN_ROUTE:
            raise InvalidStateTransition(f"PK3: a pickup is confirmed from EnRoute, not {self._status}")
        if evidence is None or not evidence.photo_ref:
            raise InvariantViolation("PK5: evidence (photo) is mandatory for a doorstep pickup")
        self._evidence = evidence
        self._status = PICKED_UP
        self.raise_event(PickupConfirmed(self._pickup_id, self._return_id, evidence.photo_ref))

    def mark_no_access(self, reason: str, reschedule_slot: datetime) -> None:
        """PK4: nobody home -> re-schedule, up to N attempts, then flag for manual handling."""
        if self._status not in (SCHEDULED, EN_ROUTE, NO_ACCESS):
            raise InvalidStateTransition(f"PK4: a terminal pickup ({self._status}) cannot be re-scheduled")
        self._attempts += 1
        if self._attempts > self._max_attempts:
            raise NoAccessExhausted(
                f"PK4: pickup {self._pickup_id} missed {self._attempts} attempts "
                f"({reason}); return must be flagged for manual handling")
        self._status = NO_ACCESS
        self._scheduled_slot = reschedule_slot
        # (no event: re-scheduling is a query-side concern; PickupScheduled already fired)

    def reschedule(self, new_slot: datetime, now: datetime | None = None) -> None:
        if self._status != NO_ACCESS:
            raise InvalidStateTransition(f"only a NoAccess pickup re-schedules, not {self._status}")
        ref_now = now if now is not None else datetime.now()
        if new_slot <= ref_now:
            raise InvariantViolation("PK2: re-scheduled slot must be in the future")
        self._scheduled_slot = new_slot
        self._status = SCHEDULED
