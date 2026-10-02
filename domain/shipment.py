"""Fulfillment context — the Shipment aggregate (outbound).

Invariants:
  SH1  a shipment covers exactly the lines of one Paid Order
  SH2  trackingNumber immutable once set
  SH3  Delivered is terminal; fires only on carrier confirmation
  SH4  a Shipment becomes a returnable unit — its lines seed the Return
"""
from __future__ import annotations

from dataclasses import dataclass

from .aggregate import Aggregate, _next_id
from .errors import InvariantViolation, InvalidStateTransition
from .events import ShipmentCreated, ShipmentDelivered, ShipmentShipped
from .value_objects import Address

CREATED, HANDED, IN_TRANSIT, DELIVERED = "Created", "HandedToCarrier", "InTransit", "Delivered"
TERMINAL = {DELIVERED}


@dataclass(frozen=True)
class ShipmentLine:
    """Child of Shipment: a product + quantity being moved."""

    product_id: str
    quantity: int


class Shipment(Aggregate):
    """Aggregate root (identity = shipment_id): one physical dispatch."""

    prefix = "sht"

    def __init__(self, order_id: str, lines: list[tuple[str, int]], destination: Address) -> None:
        super().__init__()
        if not lines:
            raise InvariantViolation("SH1: a shipment needs at least one line")
        for pid, qty in lines:
            if qty < 1:
                raise InvariantViolation(f"SH1: line quantity must be >= 1, got {qty}")
        self._shipment_id = _next_id(self.prefix)
        self._order_id = order_id
        self._lines = [ShipmentLine(pid, qty) for pid, qty in lines]
        self._destination = destination
        self._carrier: str | None = None
        self._tracking_number: str | None = None
        self._scans: list[str] = []
        self._status = CREATED
        self.raise_event(ShipmentCreated(self._shipment_id, order_id))

    # -- identity / read ----------------------------------------------------
    def aggregate_id(self) -> str:
        return self._shipment_id

    @property
    def shipment_id(self) -> str:
        return self._shipment_id

    @property
    def order_id(self) -> str:
        return self._order_id

    @property
    def status(self) -> str:
        return self._status

    @property
    def lines(self) -> list[ShipmentLine]:
        return list(self._lines)

    @property
    def tracking_number(self) -> str | None:
        return self._tracking_number

    @property
    def scans(self) -> list[str]:
        return list(self._scans)

    def as_returnable(self) -> list[tuple[str, int]]:
        """SH4: the lines that a customer may return (seed the Return)."""
        return [(l.product_id, l.quantity) for l in self._lines]

    # -- behavior -------------------------------------------------------------
    def hand_to_carrier(self, carrier: str, tracking_number: str) -> None:
        if self._status != CREATED:
            raise InvalidStateTransition(f"SH3: can only hand from Created, not {self._status}")
        if not tracking_number:
            raise InvariantViolation("a handoff needs a tracking number")
        self._carrier = carrier
        self._tracking_number = tracking_number   # SH2: immutable after this
        self._status = HANDED
        self.raise_event(ShipmentShipped(self._shipment_id, tracking_number))

    def update_tracking(self, scan: str) -> None:
        if self._status != HANDED:
            raise InvalidStateTransition(f"only a HandedToCarrier shipment tracks, not {self._status}")
        if not scan:
            raise InvariantViolation("a tracking scan must be non-empty")
        self._status = IN_TRANSIT
        self._scans.append(scan)   # SH2: the tracking number set at handoff stays immutable
        self.raise_event(ShipmentShipped(self._shipment_id, scan))

    def confirm_delivered(self) -> None:
        """SH3: terminal, fires only on carrier confirmation."""
        if self._status not in (HANDED, IN_TRANSIT):
            raise InvalidStateTransition(f"SH3: can only deliver from {HANDED}/{IN_TRANSIT}, not {self._status}")
        self._status = DELIVERED
        self.raise_event(ShipmentDelivered(self._shipment_id))
