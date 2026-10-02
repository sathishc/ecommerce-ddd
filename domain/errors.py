"""Domain errors — the teeth of the model.

Bounded context: cross-cutting. Every bounded context (Catalog, Inventory,
Shop, Order Management, Payment, Fulfillment, Returns) raises these instead
of returning error codes, so invariant breaches (C1-C4, CO1-CO7, P1-P3,
S1-S4, O1-O5, PM1-PM4, PK1-PK5, SH1-SH4, RT1-RT6) can never pass silently.

Responsibility: a tiny, dependency-free exception hierarchy. ``DomainError``
is the single catch point for callers (application handlers, tests);
subclasses distinguish *invariant* failures from *state-machine* and
*resource* failures so handlers can map them (retry vs reject vs void).

Key rule: raising is part of the ubiquitous language — e.g. a coupon that
does not validate is REJECTED via ``CouponValidationError`` (C3/CO2/CO3),
never silently ignored.
"""


class DomainError(Exception):
    """Base class for all domain-layer failures.

    Catch this to handle any business-rule breach uniformly. Never raised
    directly — always raise a specific subclass so callers can tell an
    invariant breach from a bad state transition.
    """


class InvariantViolation(DomainError):
    """An aggregate invariant (S1..RT6, C1..CO7, O1..O5, PM1..PM4, PK1..PK5, SH1..SH4) was broken.

    Raised for guard-clause failures: bad constructor args (empty SKU,
    negative price/quantity), illegal mutation (quoting an empty cart,
    over-returning), or cross-aggregate misuse. The message always cites
    the invariant id (e.g. ``"C1: quantity must be >= 1"``) for traceability
    back to ``domain-model.md``.
    """


class InvalidStateTransition(DomainError):
    """A state machine does not permit this transition from the current state.

    Raised when mutating a terminal/closed aggregate — e.g. adding to a
    ``CheckedOut``/``Abandoned`` cart (C4), editing a ``Discontinued``
    product. Distinct from ``InvariantViolation`` so orchestration can
    report "wrong lifecycle step" separately from "bad value".
    """


class StockInsufficient(DomainError):
    """Stock.reserve requested more than is available (invariant S2).

    Separate subclass (not plain ``InvariantViolation``) so Inventory
    callers can catch the all-or-nothing reservation failure explicitly
    and translate it to ``StockInsufficient`` events / order expiry.
    """


class CouponValidationError(DomainError):
    """A coupon fails validation (invariants CO2/CO3).

    Raised by ``Coupon.validate`` (cart preview) and ``Coupon.redeem``
    (order-placement commit) for: inactive status, lapsed validity window
    or exhausted usage limit (CO2), duplicate redemption by the same
    customer (CO3), or a cart below ``min_order_value``. Callers treat this
    as REJECT (C3) — never fall back to "no discount".
    """


class NoAccessExhausted(DomainError):
    """Doorstep pickup missed its re-schedule attempts (invariant PK4).

    Raised when a ``Pickup`` in ``NoAccess`` exceeds the configured retry
    budget (v1 default N=2) and must be flagged for manual handling instead
    of re-scheduling again.
    """
