"""Fulfillment bounded context — the Shipment aggregate (outbound leg).

Responsibility:
    Model one physical dispatch of a Paid order's lines to its destination:
    creation, handoff to the carrier (which triggers payment capture),
    in-transit tracking, and terminal carrier-confirmed delivery.

Lifecycle / state machine (SH3)::

    Created --hand_to_carrier()--> HandedToCarrier --update_tracking()--> InTransit
        |                               |                                      |
        +-------------------------------+--------------------------------------+
                                        +--------confirm_delivered()----------> Delivered (terminal)

    ``confirm_delivered`` fires only on carrier confirmation and drives the
    derived ``OrderDelivered``; the delivered lines then seed the Return
    aggregate (SH4) if the customer opens an RMA.
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
    """Child entity of Shipment: one product + quantity being moved.

    Role:
        The physical units of one product in this dispatch. In v1 (no split
        shipments) these mirror the order's lines exactly (SH1) and later
        seed the Return's bounds (SH4/RT2).

    Members:
        product_id: Product being moved.
        quantity: Units moved (>= 1 per SH1).

    Invariants:
        SH1: quantity >= 1; together the lines cover exactly one Paid order.
    """

    product_id: str
    quantity: int


class Shipment(Aggregate):
    """Aggregate root (identity = shipment_id): one physical dispatch.

    Role:
        Carry one Paid order's goods to its destination with carrier +
        tracking, and become the returnable unit if the customer later
        opens an RMA.

    Members:
        _shipment_id: Unique dispatch identity.
        _order_id: Order this shipment fulfills (SH1: exactly one Paid order).
        _lines: ShipmentLine list (SH1: exactly the order's lines in v1).
        _destination: Where the goods go (copied from the order).
        _carrier: Carrier name, set at handoff.
        _tracking_number: Set once at handoff; immutable afterwards (SH2).
        _scans: Carrier tracking scans appended in transit.
        _status: Created | HandedToCarrier | InTransit | Delivered (SH3).

    Invariants:
        SH1: a shipment covers exactly the lines of one Paid Order (no split
            shipments in v1).
        SH2: trackingNumber immutable once set at handoff.
        SH3: Delivered is terminal; fires only on carrier confirmation.
        SH4: a Shipment becomes a returnable unit — its lines seed the Return.

    State transitions:
        __init__: (new) -> Created.
        hand_to_carrier: Created -> HandedToCarrier (sets carrier+tracking).
        update_tracking: HandedToCarrier -> InTransit (appends a scan).
        confirm_delivered: HandedToCarrier/InTransit -> Delivered (terminal).
    """

    prefix = "sht"

    def __init__(self, order_id: str, lines: list[tuple[str, int]], destination: Address) -> None:
        """Create a shipment for one Paid order's lines (SH1).

        Args:
            order_id: Order being fulfilled (must be Paid; enforced by
                ``Order.confirm_shipped`` in orchestration).
            lines: (product_id, quantity) pairs — exactly the order's lines
                in v1 (no split shipments).
            destination: Delivery address (copied from the order).

        Raises:
            InvariantViolation: SH1 if no lines or any quantity < 1.

        Events:
            ShipmentCreated(shipment_id, order_id).
        """
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
        """Return the aggregate identity (the shipment id).

        Returns:
            str: this shipment's unique id.
        """
        return self._shipment_id

    @property
    def shipment_id(self) -> str:
        """Return the shipment id.

        Returns:
            str: identity of this dispatch.
        """
        return self._shipment_id

    @property
    def order_id(self) -> str:
        """Return the fulfilled order's id (SH1).

        Returns:
            str: the single order this shipment covers.
        """
        return self._order_id

    @property
    def status(self) -> str:
        """Return the current dispatch state (SH3).

        Returns:
            str: Created | HandedToCarrier | InTransit | Delivered.
        """
        return self._status

    @property
    def lines(self) -> list[ShipmentLine]:
        """Return a copy of the shipment lines.

        Returns:
            list[ShipmentLine]: the moved lines (SH1/SH4 source).
        """
        return list(self._lines)

    @property
    def tracking_number(self) -> str | None:
        """Return the carrier tracking number, if handoff happened (SH2).

        Returns:
            Optional[str]: immutable once set at ``hand_to_carrier``.
        """
        return self._tracking_number

    @property
    def scans(self) -> list[str]:
        """Return the carrier tracking scans appended in transit.

        Returns:
            list[str]: scan history (empty until ``update_tracking``).
        """
        return list(self._scans)

    def as_returnable(self) -> list[tuple[str, int]]:
        """Expose the lines as a returnable unit for the Returns context (SH4).

        The customer may return up to these quantities (RT2 bound).

        Returns:
            list[tuple[str, int]]: (product_id, quantity) pairs seeding the Return.
        """
        return [(l.product_id, l.quantity) for l in self._lines]

    # -- behavior -------------------------------------------------------------
    def hand_to_carrier(self, carrier: str, tracking_number: str) -> None:
        """Hand the parcel to the carrier: Created -> HandedToCarrier (SH2/SH3).

        This handoff is the capture-at-handoff trigger: orchestration
        commits stock (S3) and captures payment here.

        Args:
            carrier: Carrier name (single carrier in v1).
            tracking_number: Carrier tracking number (SH2: immutable once set).

        Raises:
            InvalidStateTransition: SH3 unless currently Created.
            InvariantViolation: if tracking_number is empty.

        Events:
            ShipmentShipped(shipment_id, tracking_number).
        """
        if self._status != CREATED:
            raise InvalidStateTransition(f"SH3: can only hand from Created, not {self._status}")
        if not tracking_number:
            raise InvariantViolation("a handoff needs a tracking number")
        self._carrier = carrier
        self._tracking_number = tracking_number   # SH2: immutable after this
        self._status = HANDED
        self.raise_event(ShipmentShipped(self._shipment_id, tracking_number))

    def update_tracking(self, scan: str) -> None:
        """Record a carrier scan: HandedToCarrier -> InTransit.

        Args:
            scan: Non-empty carrier scan description.

        Raises:
            InvalidStateTransition: unless currently HandedToCarrier.
            InvariantViolation: if scan is empty.

        Events:
            ShipmentShipped(shipment_id, scan).
        """
        if self._status != HANDED:
            raise InvalidStateTransition(f"only a HandedToCarrier shipment tracks, not {self._status}")
        if not scan:
            raise InvariantViolation("a tracking scan must be non-empty")
        self._status = IN_TRANSIT
        self._scans.append(scan)   # SH2: the tracking number set at handoff stays immutable
        self.raise_event(ShipmentShipped(self._shipment_id, scan))

    def confirm_delivered(self) -> None:
        """Confirm carrier delivery: HandedToCarrier/InTransit -> Delivered (SH3).

        Terminal; fires only on carrier confirmation. Orchestration derives
        ``OrderDelivered`` from this, which opens the RT1 return window.

        Raises:
            InvalidStateTransition: SH3 unless HandedToCarrier or InTransit.

        Events:
            ShipmentDelivered(shipment_id).
        """
        if self._status not in (HANDED, IN_TRANSIT):
            raise InvalidStateTransition(f"SH3: can only deliver from {HANDED}/{IN_TRANSIT}, not {self._status}")
        self._status = DELIVERED
        self.raise_event(ShipmentDelivered(self._shipment_id))
