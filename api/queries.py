"""CQRS read side: pure queries over the container repositories.

Every function takes the shared ``Container`` and returns JSON-safe dicts
(via ``api.serializers``). No UoW is opened, no aggregate is mutated, no
event is emitted — reads are side-effect free by construction.
"""
from __future__ import annotations

from api import serializers as s


def _safe_list(repo):
    try:
        return repo.list()
    except Exception:
        return []


def _safe_get(repo, _id):
    try:
        return repo.get(_id)
    except KeyError:
        return None


# -- catalog / inventory -------------------------------------------------
def list_products(c) -> list[dict]:
    return [s.product_to_dict(p) for p in _safe_list(c.products)]


def get_product(c, product_id) -> dict | None:
    p = _safe_get(c.products, product_id)
    return s.product_to_dict(p) if p else None


def list_stocks(c) -> list[dict]:
    out = []
    for stk in _safe_list(c.stocks):
        prod = _safe_get(c.products, stk.product_id)
        out.append(s.stock_to_dict(stk, prod))
    return out


def list_coupons(c) -> list[dict]:
    return [s.coupon_to_dict(x) for x in _safe_list(c.coupons)]


# -- shop ----------------------------------------------------------------
def list_carts(c) -> list[dict]:
    return [s.cart_to_dict(x) for x in _safe_list(c.carts)]


def get_cart(c, cart_id) -> dict | None:
    cart = _safe_get(c.carts, cart_id)
    return s.cart_to_dict(cart) if cart else None


# -- orders / payments / fulfillment -------------------------------------
def list_orders(c) -> list[dict]:
    return [s.order_to_dict(x) for x in _safe_list(c.orders)]


def get_order(c, order_id) -> dict | None:
    o = _safe_get(c.orders, order_id)
    return s.order_to_dict(o) if o else None


def get_payment(c, payment_id) -> dict | None:
    p = _safe_get(c.payments, payment_id)
    return s.payment_to_dict(p) if p else None


def order_payment(c, order_id) -> dict | None:
    o = _safe_get(c.orders, order_id)
    if not o or not o._payment_ref:
        return None
    return get_payment(c, o._payment_ref)


def list_shipments(c) -> list[dict]:
    return [s.shipment_to_dict(x) for x in _safe_list(c.shipments)]


def get_shipment(c, shipment_id) -> dict | None:
    x = _safe_get(c.shipments, shipment_id)
    return s.shipment_to_dict(x) if x else None


# -- returns / pickups ----------------------------------------------------
def list_returns(c) -> list[dict]:
    return [s.rma_to_dict(x) for x in _safe_list(c.returns)]


def get_return(c, rma_id) -> dict | None:
    x = _safe_get(c.returns, rma_id)
    return s.rma_to_dict(x) if x else None


def list_pickups(c) -> list[dict]:
    return [s.pickup_to_dict(x) for x in _safe_list(c.pickups)]


def get_pickup(c, pickup_id) -> dict | None:
    x = _safe_get(c.pickups, pickup_id)
    return s.pickup_to_dict(x) if x else None


# -- ops dashboard (one read-model for the frontend header) ----------------
def dashboard(c) -> dict:
    orders = _safe_list(c.orders)
    by_status: dict[str, int] = {}
    revenue_minor = 0
    currency = "USD"
    for o in orders:
        by_status[o.status] = by_status.get(o.status, 0) + 1
        try:
            if o.status not in ("Cancelled", "Expired"):
                revenue_minor += o.money_total.minor
                currency = o.money_total.currency
        except Exception:
            pass
    return {
        "counts": {
            "products": len(_safe_list(c.products)),
            "carts": len(_safe_list(c.carts)),
            "orders": len(orders),
            "shipments": len(_safe_list(c.shipments)),
            "returns": len(_safe_list(c.returns)),
            "pickups": len(_safe_list(c.pickups)),
            "coupons": len(_safe_list(c.coupons)),
        },
        "orders_by_status": by_status,
        "revenue": {"minor": revenue_minor, "currency": currency,
                    "display": f"{revenue_minor / 100:.2f} {currency}"},
    }
