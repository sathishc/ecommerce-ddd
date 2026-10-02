"""Fulfillment bounded context — the Pickup aggregate (doorstep return collection).

Responsibility:
    Model the doorstep collection of a return: the platform's courier visits
    the customer's address and takes possession of the goods. Since the
    customer holds no return label, the courier's on-site scan + photo is
    the customer's proof of collection and the possession-transfer event
    that gates refund + restock (RT5/S4).

Lifecycle / state machine (PK3/PK4)::

    Scheduled --assign_courier()--> EnRoute --confirm_pickup(evidence)--> PickedUp (terminal)
        |                         |  ^
        +--mark_no_access()--+   +--+  (nobody home -> NoAccess, then reschedule() back
              (NoAccess,           to Scheduled; up to max_attempts, default 2, before
               re-scheduled)       the Return is flagged for manual handling)

    ``PickedUp`` fires only on the courier's on-site scan with mandatory
    photo evidence (PK5). ``NoAccess`` never fails the return outright — it
    re-schedules (PK4). Pickup itself is free (no pickup charge in v1).
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
    """Aggregate root (identity = pickup_id): one doorstep collection.

    Role:
        Track one approved Return's courier visit from scheduling through
        possession transfer (or repeated no-access re-scheduling).

    Members:
        _pickup_id: Unique collection identity.
        _return_id: The single approved Return this pickup serves (PK1).
        _customer_address: Doorstep where collection happens.
        _scheduled_slot: Next courier visit window (PK2: must be future;
            area checks live in the courier context, out of scope).
        _courier_ref: Assigned courier; set by ``assign_courier``.
        _evidence: Collection proof (photo + optional signature), captured
            at ``confirm_pickup`` (PK5).
        _attempts: No-access count so far (PK4).
        _max_attempts: Allowed no-access re-schedules before manual handling.
        _status: Scheduled | EnRoute | PickedUp (terminal) | NoAccess.

    Invariants:
        PK1: a Pickup belongs to exactly one approved Return.
        PK2: scheduledSlot within the courier's service area (modelled as: a
            slot must be provided and future-dated; area checks are external).
        PK3: PickedUp is terminal; fires only on the courier's on-site scan
            (possession actually transferred).
        PK4: NoAccess re-schedules rather than failing — up to N attempts
            before the Return is flagged for manual handling.
        PK5: evidence of collection is MANDATORY — ``confirm_pickup``
            requires captured evidence (photo + optional signature).

    State transitions:
        __init__: (new) -> Scheduled.
        assign_courier: Scheduled/NoAccess -> EnRoute.
        confirm_pickup: EnRoute -> PickedUp (terminal; requires evidence).
        mark_no_access: Scheduled/EnRoute/NoAccess -> NoAccess (counts attempts).
        reschedule: NoAccess -> Scheduled.
    """

    prefix = "pku"

    def __init__(self, return_id: str, customer_address: Address,
                 scheduled_slot: datetime, max_attempts: int = MAX_ATTEMPTS,
                 now: datetime | None = None) -> None:
        """Schedule a doorstep collection for one approved Return (PK1/PK2).

        Args:
            return_id: RMA this pickup serves (exactly one per PK1).
            customer_address: Doorstep where the courier collects.
            scheduled_slot: First visit window (PK2: must be in the future).
            max_attempts: Allowed no-access re-schedules (PK4, >= 1).
            now: Reference time for the future-slot check (injectable for
                deterministic tests; defaults to wall clock).

        Raises:
            InvariantViolation: PK1 if no return_id; PK2 if the slot is not
                in the future; PK4 if max_attempts < 1.

        Events:
            PickupScheduled(pickup_id, return_id).
        """
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
        """Return the aggregate identity (the pickup id).

        Returns:
            str: this pickup's unique id.
        """
        return self._pickup_id

    @property
    def pickup_id(self) -> str:
        """Return the pickup id.

        Returns:
            str: identity of this collection.
        """
        return self._pickup_id

    @property
    def return_id(self) -> str:
        """Return the served RMA id (PK1).

        Returns:
            str: the single return this pickup belongs to.
        """
        return self._return_id

    @property
    def status(self) -> str:
        """Return the current collection state (PK3/PK4).

        Returns:
            str: Scheduled | EnRoute | PickedUp | NoAccess.
        """
        return self._status

    @property
    def evidence(self) -> PickupEvidence | None:
        """Return the captured collection evidence, if any (PK5).

        Returns:
            Optional[PickupEvidence]: photo + optional signature; None until
            ``confirm_pickup``.
        """
        return self._evidence

    @property
    def attempts(self) -> int:
        """Return the no-access attempt count so far (PK4).

        Returns:
            int: missed-visit count; exhausting ``max_attempts`` raises
            NoAccessExhausted on the next ``mark_no_access``.
        """
        return self._attempts

    # -- behavior -------------------------------------------------------------
    def assign_courier(self, courier_ref: str) -> None:
        """Dispatch a courier: Scheduled/NoAccess -> EnRoute.

        Args:
            courier_ref: Courier identity (default courier in v1).

        Raises:
            InvalidStateTransition: if already EnRoute or terminal PickedUp.

        Events:
            None (PickupScheduled already fired at scheduling).
        """
        if self._status not in (SCHEDULED, NO_ACCESS):
            raise InvalidStateTransition(f"can only assign a courier before pickup, not from {self._status}")
        self._courier_ref = courier_ref
        self._status = EN_ROUTE

    def confirm_pickup(self, evidence: PickupEvidence) -> None:
        """Confirm possession transfer at the door: EnRoute -> PickedUp (PK3/PK5).

        The courier's on-site scan with mandatory photo evidence. Terminal:
        only this gates the Returns settlement (RT5) and Inventory restock
        (S4). The scan + photo is the customer's proof of collection since
        they hold no return label.

        Args:
            evidence: Collection proof (photo_ref required, signature
                optional) captured at the doorstep.

        Raises:
            InvalidStateTransition: PK3 unless currently EnRoute.
            InvariantViolation: PK5 if evidence is missing or has no photo.

        Events:
            PickupConfirmed(pickup_id, return_id, evidence.photo_ref).
        """
        if self._status != EN_ROUTE:
            raise InvalidStateTransition(f"PK3: a pickup is confirmed from EnRoute, not {self._status}")
        if evidence is None or not evidence.photo_ref:
            raise InvariantViolation("PK5: evidence (photo) is mandatory for a doorstep pickup")
        self._evidence = evidence
        self._status = PICKED_UP
        self.raise_event(PickupConfirmed(self._pickup_id, self._return_id, evidence.photo_ref))

    def mark_no_access(self, reason: str, reschedule_slot: datetime) -> None:
        """Record a missed doorstep (nobody home): -> NoAccess, re-scheduled (PK4).

        Never fails the return outright; the visit is re-scheduled up to
        ``max_attempts``. Past the limit the Return must be flagged for
        manual handling.

        Args:
            reason: Why access failed (e.g. "nobody home").
            reschedule_slot: Next visit window.

        Raises:
            InvalidStateTransition: PK4 if already terminal PickedUp.
            NoAccessExhausted: PK4 when attempts exceed max_attempts.

        Events:
            None (re-scheduling is a query-side concern; PickupScheduled
            already fired).
        """
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
        """Book the next visit after a no-access: NoAccess -> Scheduled (PK4/PK2).

        Args:
            new_slot: Next visit window (PK2: must be in the future).
            now: Reference time for the future-slot check (injectable for
                tests; defaults to wall clock).

        Raises:
            InvalidStateTransition: unless currently NoAccess.
            InvariantViolation: PK2 if the new slot is not in the future.

        Events:
            None.
        """
        if self._status != NO_ACCESS:
            raise InvalidStateTransition(f"only a NoAccess pickup re-schedules, not {self._status}")
        ref_now = now if now is not None else datetime.now()
        if new_slot <= ref_now:
            raise InvariantViolation("PK2: re-scheduled slot must be in the future")
        self._scheduled_slot = new_slot
        self._status = SCHEDULED
