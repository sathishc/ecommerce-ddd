"""Payment bounded context — a GENERIC charge (no ecommerce knowledge).

Responsibility:
    Charge a payment instrument through authorize -> capture -> refund/void.
    This context deliberately knows nothing about orders, shipments, or
    returns: the Order/Returns contexts translate into its language (Money +
    opaque instrument ref) via an anti-corruption layer (see
    ``domain/orchestration.py``).

Lifecycle / state machine (PM2/PM3/PM4)::

    Authorized --capture()--> Captured --refund(full)--> Refunded (terminal)
        |                         |
        +--void()--> Voided        +--refund(partial)--> Captured (accrues refunds[])
        (terminal)                 (stays Captured until sum(refunds) == captured)

    Capture-at-handoff: authorization happens at order placement, but money
    moves only when the carrier takes the parcel (Fulfillment handoff calls
    ``capture``). Cancel-before-shipment calls ``void`` (no money moved);
    cancellation after shipment is impossible — only the Returns path calls
    ``refund``. A partial return keeps the Payment Captured while accruing
    ``refunds[]``; it flips to Refunded only on full settlement (PM4).
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
    """Child entity of Payment: one recorded partial/full refund.

    Role:
        Audit trail entry for money returned to the instrument. The running
        sum over these entries is bounded by PM1 (never exceeds captured).

    Members:
        amount: Money returned by this entry.
        reason: Why (e.g. "return <rma_id>").
    """

    amount: Money
    reason: str


class Payment(Aggregate):
    """Aggregate root: one charge on one instrument (identity = payment_id).

    Role:
        Own the money movement for a single authorization: capture it once
        at shipment handoff, then refund it (partially or fully) through
        Returns settlements.

    Members:
        _payment_id: Unique charge identity.
        _amount: Authorized (and capturable) amount (Money); the PM1 ceiling
            for the sum of all refunds.
        _instrument_ref: Opaque instrument reference (card token, etc.).
        _status: Authorized | Captured | Refunded | Voided (PM2/PM3/PM4).
        _refunds: Accrued refund entries (PM1/PM4 audit trail).

    Invariants:
        PM1: sum(refunds) <= captured amount.
        PM2: capture only from Authorized.
        PM3: Captured / Voided / Refunded are terminal (except the PM4
            Captured -> Refunded flip on full settlement).
        PM4: a *partial* refund keeps the Payment Captured while accruing
            refunds[]; it flips to Refunded only when sum(refunded) ==
            captured (full settlement).

    State transitions:
        __init__: (new) -> Authorized.
        capture: Authorized -> Captured.
        void: Authorized -> Voided (terminal).
        refund: Captured -> Captured (partial) | Captured -> Refunded (full).

    Deliberately knows NOTHING about orders — the Order context speaks to it
    through an ACL (see orchestration.py).
    """

    prefix = "pay"

    def __init__(self, amount: Money, instrument_ref: str) -> None:
        """Authorize a charge (no money moves yet; capture is at handoff).

        Args:
            amount: Amount to authorize (Money, >= 0; equals the frozen
                order total via the Order -> Payment ACL).
            instrument_ref: Opaque payment instrument reference.

        Raises:
            InvariantViolation: if amount < 0 or no instrument ref given.

        Events:
            PaymentAuthorized(payment_id, amount).
        """
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
        """Return the aggregate identity (the payment id).

        Returns:
            str: this payment's unique id.
        """
        return self._payment_id

    @property
    def payment_id(self) -> str:
        """Return the payment id.

        Returns:
            str: identity of this charge.
        """
        return self._payment_id

    @property
    def amount(self) -> Money:
        """Return the authorized/captured amount (PM1 ceiling).

        Returns:
            Money: amount authorized at placement, captured at handoff.
        """
        return self._amount

    @property
    def status(self) -> str:
        """Return the current charge state (PM2/PM3/PM4).

        Returns:
            str: Authorized | Captured | Refunded | Voided.
        """
        return self._status

    @property
    def refunded(self) -> Money:
        """Return the running sum of all refunds (PM1: <= captured).

        Returns:
            Money: total refunded so far (zero if none).
        """
        cur = self._amount.currency
        return Money(sum(r.amount.minor for r in self._refunds), cur)

    # -- behavior -------------------------------------------------------------
    def capture(self) -> None:
        """Capture the authorized amount: Authorized -> Captured (PM2/PM3).

        Called at carrier handoff (authorize-then-capture): this is the
        moment money actually moves. Must run exactly once.

        Raises:
            InvalidStateTransition: PM2 if not Authorized (e.g. double
                capture, or capture after void/refund).

        Events:
            PaymentCaptured(payment_id).
        """
        if self._status != AUTHORIZED:
            raise InvalidStateTransition(f"PM2: capture only from Authorized, not {self._status}")
        self._status = CAPTURED
        self.raise_event(PaymentCaptured(self._payment_id))

    def void(self, reason: str) -> None:
        """Void an authorized-but-uncaptured payment (cancel-before-shipment).

        Args:
            reason: Why the authorization is voided (order cancel reason).

        Raises:
            InvalidStateTransition: PM3 unless currently Authorized (a
                Captured payment must go through ``refund``, never void).

        Events:
            PaymentVoided(payment_id, reason).
        """
        if self._status != AUTHORIZED:
            raise InvalidStateTransition(f"PM3: can only void an Authorized payment, not {self._status}")
        self._status = VOIDED
        self.raise_event(PaymentVoided(self._payment_id, reason))

    def refund(self, amount: Money, reason: str) -> None:
        """Refund captured money, partially or fully (PM1/PM4).

        Each Returns settlement calls this once with its RT3 pro-rata total.
        Partial refunds accrue in ``refunds[]`` and leave the status
        Captured; only when the running sum reaches the captured amount does
        the status flip to Refunded (terminal, PM4).

        Args:
            amount: Money to return (> 0; PM1: refunded-so-far + amount must
                not exceed the captured amount).
            reason: Why (e.g. "return <rma_id>").

        Raises:
            InvalidStateTransition: PM3 unless currently Captured.
            InvariantViolation: PM1 if amount <= 0 or the refund would exceed
                the captured amount.

        Events:
            PaymentRefunded(payment_id, amount, reason).
        """
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
        # PM4: full settlement -> terminal; partial stays Captured.
        if self.refunded == self._amount:
            self._status = REFUNDED  # PM4: full settlement -> terminal

    # PM3 terminal guard: no method transitions out of Captured/Refunded/Voided
    # except capture->(none) and the PM4 flip above.
