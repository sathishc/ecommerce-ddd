"""Serializers: aggregates / value objects -> plain JSON-safe dicts.

One function per aggregate. All Money renders as ``{"minor": int,
"currency": str, "display": "19.99 USD"}`` so the React client can format
without float math. Dates/datetimes render as ISO strings.
"""
from __future__ import annotations


def money_to_dict(m) -> dict:
    if m is None:
        return {"minor": 0, "currency": "USD", "display": "0.00 USD"}
    return {"minor": m.minor, "currency": m.currency, "display": str(m)}


def address_to_dict(a) -> dict | None:
    if a is None:
        return None
    return {"line1": a.line1, "city": a.city,
            "postal_code": a.postal_code, "country": a.country}


def quote_to_dict(q) -> dict:
    return {
        "subtotal": money_to_dict(q.subtotal),
        "discount": {"amount": money_to_dict(q.discount.amount), "rule": q.discount.rule},
        "tax": money_to_dict(q.tax),
        "shipping_fee": money_to_dict(q.shipping_fee),
        "total": money_to_dict(q.total),
    }


def breakdown_to_dict(b) -> dict:
    return {
        "goods": money_to_dict(b.goods),
        "tax": money_to_dict(b.tax),
        "shipping": money_to_dict(b.shipping),
        "total": money_to_dict(b.total),
        "is_full": b.is_full,
    }


def product_to_dict(p) -> dict:
    return {
        "product_id": p.product_id, "sku": p.sku, "name": p.name,
        "description": getattr(p, "_fields", None) and p._fields.description or "",
        "weight_grams": p.weight_grams,
        "price": money_to_dict(p.price), "status": p.status,
    }


def stock_to_dict(s, product=None) -> dict:
    return {
        "product_id": s.product_id,
        "product_name": product.name if product is not None else s.product_id,
        "available": s.available, "reserved": s.reserved,
    }


def coupon_to_dict(c) -> dict:
    value = c._value
    value_dict = money_to_dict(value) if not isinstance(value, int) else {"raw": value}
    return {
        "coupon_id": c.aggregate_id(), "code": c.code, "type": c._type,
        "value": value_dict, "status": c.status,
        "redemptions": c.redemptions, "usage_limit": c._usage_limit,
        "valid_from": c._valid_from.isoformat(), "valid_to": c._valid_to.isoformat(),
        "min_order_value": money_to_dict(c._min_order_value) if c._min_order_value else None,
    }


def cart_to_dict(cart, quote=None) -> dict:
    lines = [
        {"product_id": pid, "quantity": qty, "unit_price": money_to_dict(unit),
         "line_total": money_to_dict(unit.__class__(unit.minor * qty, unit.currency))}
        for pid, qty, unit in cart.lines
    ]
    try:
        q = quote if quote is not None else cart.quote()
        quote_dict = quote_to_dict(q)
    except Exception:
        quote_dict = None
    return {
        "cart_id": cart.aggregate_id(), "customer_ref": cart.customer_ref,
        "status": cart.status, "applied_coupon_code": cart.applied_coupon_code,
        "destination": address_to_dict(cart.destination),
        "lines": lines, "quote": quote_dict,
    }


def order_to_dict(o) -> dict:
    return {
        "order_id": o.order_id, "status": o.status,
        "customer_ref": o.customer_ref if o._customer_ref else None,
        "destination": address_to_dict(o._destination),
        "lines": [{"product_id": l.product_id, "quantity": l.quantity,
                   "unit_price": money_to_dict(l.unit_price),
                   "line_total": money_to_dict(l.line_total)} for l in o.lines],
        "discount": money_to_dict(o._discount),
        "shipping_fee": money_to_dict(o._shipping_fee),
        "money_total": money_to_dict(o._money_total),
        "coupon_code": o.coupon_code, "payment_ref": o._payment_ref,
        "reservation_ids": dict(o.reservation_ids), "rma_ids": list(o.rma_ids),
        "delivered_at": o.delivered_at.isoformat() if o.delivered_at else None,
    }


def payment_to_dict(p) -> dict:
    return {
        "payment_id": p.payment_id, "amount": money_to_dict(p.amount),
        "instrument_ref": p._instrument_ref, "status": p.status,
        "refunded": money_to_dict(p.refunded),
        "refunds": [{"amount": money_to_dict(r.amount), "reason": r.reason}
                    for r in p._refunds],
    }


def shipment_to_dict(s) -> dict:
    return {
        "shipment_id": s.shipment_id, "order_id": s.order_id,
        "status": s.status,
        "lines": [{"product_id": l.product_id, "quantity": l.quantity} for l in s.lines],
        "destination": address_to_dict(s._destination),
        "carrier": s._carrier, "tracking_number": s.tracking_number,
        "scans": list(s.scans),
    }


def pickup_to_dict(p) -> dict:
    return {
        "pickup_id": p.pickup_id, "return_id": p.return_id,
        "status": p.status,
        "customer_address": address_to_dict(p._customer_address),
        "scheduled_slot": p._scheduled_slot.isoformat() if p._scheduled_slot else None,
        "courier_ref": p._courier_ref, "attempts": p.attempts,
        "evidence": ({"photo_ref": p.evidence.photo_ref,
                      "signature_ref": p.evidence.signature_ref}
                     if p.evidence else None),
    }


def rma_to_dict(r) -> dict:
    return {
        "rma_id": r.rma_id, "order_id": r.order_id, "status": r.status,
        "customer_id": r.customer_id,
        "destination": address_to_dict(r.destination),
        "lines": [{"product_id": l.product_id, "quantity": l.quantity}
                  for l in r.lines],
        "reason": r._reason,
        "requested_at": r._requested_at.isoformat() if r._requested_at else None,
        "pickup_id": r._pickup_id,
        "settlement": breakdown_to_dict(r.settlement) if r.settlement else None,
        "is_complete": r.is_complete,
    }
