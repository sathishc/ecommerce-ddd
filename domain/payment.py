"""Payment context — a GENERIC charge (no ecommerce knowledge).

Invariants:
  PM1  sum(refunds) <= captured amount
  PM2  capture only from Authorized
  PM3  Captured / Voided / Refunded are terminal
  PM4  a *partial* refund keeps the Payment in Captured while accruing
       refunds[]; it flips to Refunded only when sum(refunds) == captured
"""
from __future__ import annotations

from dataclasses import dataclass

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, InvalidStateTransition
from .events import PaymentAuthorized, PaymentCaptured, PaymentRefunded, PaymentVoided
from .value_objects import Money

AUTHORIZED, CAPTURED, REFUNDED, VOIDED = "Authorized", "Captured", "Refunded", "Voided"
TERMINAL = {CAPTURED, REFUNDED, VOIDED}


@dataclass
class _Refund:
    amount: Money
    reason: str


class Payment(Aggregate):
    """Aggregate root: one charge on one instrument (identity = payment_id).

    Deliberately knows NOTHING about orders — the Order context speaks to it
    through an ACL (see orchestration.py).
    """

    prefix = "pay"

    def __init__(self, amount: Money, instrument_ref: str) -> None:
        super().__init__()
        if amount.minor < 0:
            raise InvariantViolation("amount must be >= 0")
        if not instrument_ref:
            raise InvariantViolation("a payment needs an instrument reference")
        self._payment_id = _next_id(self.prefix)
        self._amount = amount
        self._instrument_ref = instrument_ref
        self._status = AUTHORIZED
        self._refunds: list[_Refund] = []
        self.raise_event(PaymentAuthorized(self._payment_id, amount))

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        return self._payment_id

    @property
    def payment_id(self) -> str:
        return self._payment_id

    @property
    def amount(self) -> Money:
        return self._amount

    @property
    def status(self) -> str:
        return self._status

    @property
    def refunded(self) -> Money:
        """PM1: running sum of all refunds (<= captured by construction)."""
        cur = self._amount.currency
        return Money(sum(r.amount.minor for r in self._refunds), cur)

    # -- behavior -------------------------------------------------------------
    def capture(self) -> None:
        """PM2: capture only from Authorized (money moves)."""
        if self._status != AUTHORIZED:
            raise InvalidStateTransition(f"PM2: capture only from Authorized, not {self._status}")
        self._status = CAPTURED
        self.raise_event(PaymentCaptured(self._payment_id))

    def void(self, reason: str) -> None:
        """An authorized-but-uncaptured payment is voided (cancel-before-shipment)."""
        if self._status != AUTHORIZED:
            raise InvalidStateTransition(f"PM3: can only void an Authorized payment, not {self._status}")
        self._status = VOIDED
        self.raise_event(PaymentVoided(self._payment_id, reason))

    def refund(self, amount: Money, reason: str) -> None:
        """PM1/PM4: partial or full refund. A full settlement flips to Refunded."""
        if self._status != CAPTURED:
            raise InvalidStateTransition(f"PM3: can only refund a Captured payment, not {self._status}")
        if amount.minor <= 0:
            raise InvariantViolation("refund amount must be > 0")
        if self.refunded.plus(amount) > self._amount:
            raise InvariantViolation(
                f"PM1: refund {amount} would exceed captured {self._amount} "
                f"(already refunded {self.refunded})"
            )
        self._refunds.append(_Refund(amount, reason))
        self.raise_event(PaymentRefunded(self._payment_id, amount, reason))
        if self.refunded == self._amount:
            self._status = REFUNDED  # PM4: full settlement -> terminal

    # PM3 terminal guard: no method transitions out of Captured/Refunded/Voided
    # except capture->(none) and the PM4 flip above.
