"""Domain errors. Invariants raise these; they are the model's teeth."""


class DomainError(Exception):
    """Base class for all domain-layer failures."""


class InvariantViolation(DomainError):
    """An aggregate invariant (S1..RT6, C1..CO7, O1..O5, PM1..PM4, PK1..PK5, SH1..SH4) was broken."""


class InvalidStateTransition(DomainError):
    """A state machine does not permit this transition from the current state."""


class StockInsufficient(DomainError):
    """Stock.reserve requested more than is available (invariant S2)."""


class CouponValidationError(DomainError):
    """A coupon fails validation (invariants CO2/CO3)."""


class NoAccessExhausted(DomainError):
    """Doorstep pickup missed its re-schedule attempts (invariant PK4)."""
