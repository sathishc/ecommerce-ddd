"""CQRS write side: JSON payload -> immutable ``domain.commands``.

``build_command(type, payload)`` is the single translation point between the
HTTP layer and the ``CommandBus``. It parses ``Address`` / ``Money`` /
datetime fragments and returns the frozen dataclass the handlers expect.
Unknown types raise ``KeyError``; malformed payloads raise ``ValueError``.
"""
from __future__ import annotations

from datetime import date, datetime

from domain import commands as C
from domain.value_objects import Address, Money, PickupEvidence


def parse_address(d: dict | None) -> Address | None:
    if not d:
        return None
    return Address(str(d.get("line1", "")), str(d.get("city", "")),
                   str(d.get("postal_code", "")), str(d.get("country", "")))


def parse_money(d: dict | None):
    if not d:
        return None
    if "minor" in d:
        return Money(int(d["minor"]), str(d.get("currency", "USD")))
    return Money(int(round(float(d.get("major", 0)) * 100)), str(d.get("currency", "USD")))


def _parse_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _parse_date(v):
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    return date.fromisoformat(str(v))


def build_command(cmd_type: str, payload: dict):
    """Build a domain Command from its wire representation."""
    p = payload or {}
    if cmd_type == "PublishProduct":
        return C.PublishProduct(sku=p.get("sku", ""), name=p.get("name", ""),
                                price=parse_money(p.get("price")),
                                description=p.get("description", ""),
                                weight_grams=int(p.get("weight_grams", 0)))
    if cmd_type == "OpenCart":
        return C.OpenCart(customer_ref=p.get("customer_ref", ""))
    if cmd_type == "AddToCart":
        return C.AddToCart(cart_id=p.get("cart_id", ""), product_id=p.get("product_id", ""),
                           quantity=int(p.get("quantity", 1)))
    if cmd_type == "UpdateCartLine":
        return C.UpdateCartLine(cart_id=p.get("cart_id", ""), product_id=p.get("product_id", ""),
                                quantity=int(p.get("quantity", 1)))
    if cmd_type == "RemoveCartLine":
        return C.RemoveCartLine(cart_id=p.get("cart_id", ""), product_id=p.get("product_id", ""))
    if cmd_type == "ApplyCoupon":
        return C.ApplyCoupon(cart_id=p.get("cart_id", ""), coupon_code=p.get("coupon_code", ""))
    if cmd_type == "RemoveCoupon":
        return C.RemoveCoupon(cart_id=p.get("cart_id", ""))
    if cmd_type == "PlaceOrder":
        return C.PlaceOrder(cart_id=p.get("cart_id", ""),
                            instrument_ref=p.get("instrument_ref", ""),
                            reservation_ttl_days=int(p.get("reservation_ttl_days", 30)),
                            destination=parse_address(p.get("destination")))
    if cmd_type == "CancelOrder":
        return C.CancelOrder(order_id=p.get("order_id", ""), reason=p.get("reason", ""))
    if cmd_type == "CloseCart":
        return C.CloseCart(cart_id=p.get("cart_id", ""))
    if cmd_type == "HandToCarrier":
        return C.HandToCarrier(order_id=p.get("order_id", ""), carrier=p.get("carrier", ""),
                               tracking_number=p.get("tracking_number", ""))
    if cmd_type == "ConfirmDelivery":
        return C.ConfirmDelivery(shipment_id=p.get("shipment_id", ""),
                                 order_id=p.get("order_id", ""),
                                 at=_parse_date(p.get("at")))
    if cmd_type == "RequestReturn":
        lines = tuple((str(pid), int(qty)) for pid, qty in (p.get("lines") or []))
        return C.RequestReturn(order_id=p.get("order_id", ""), lines=lines,
                               reason=p.get("reason", ""))
    if cmd_type == "RejectReturn":
        return C.RejectReturn(rma_id=p.get("rma_id", ""), reason=p.get("reason", ""))
    if cmd_type == "SchedulePickup":
        return C.SchedulePickup(rma_id=p.get("rma_id", ""), slot=_parse_dt(p.get("slot")))
    if cmd_type == "ConfirmPickup":
        ev = p.get("evidence") or {}
        evidence = PickupEvidence(photo_ref=str(ev.get("photo_ref", "")),
                                  signature_ref=ev.get("signature_ref"))
        return C.ConfirmPickup(pickup_id=p.get("pickup_id", ""), evidence=evidence)
    if cmd_type == "SettleReturn":
        return C.SettleReturn(rma_id=p.get("rma_id", ""))
    raise KeyError(f"unknown command {cmd_type!r}")


COMMANDS = ["PublishProduct", "OpenCart", "AddToCart", "UpdateCartLine",
            "RemoveCartLine", "ApplyCoupon", "RemoveCoupon", "PlaceOrder",
            "CancelOrder", "CloseCart", "HandToCarrier", "ConfirmDelivery",
            "RequestReturn", "RejectReturn", "SchedulePickup", "ConfirmPickup",
            "SettleReturn"]
