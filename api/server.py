"""Stdlib HTTP API: CQRS write endpoint + read-model query endpoints.

Routes
------
Writes (command side — mutate via the CommandBus, one atomic UoW each)::

    POST /api/commands   {"type": "AddToCart", "payload": {...}} -> {"ok": true, "result": {...}}
    POST /api/seed       (re)seed demo catalog/coupons

Reads (query side — pure ``api.queries`` over repositories, never mutate)::

    GET /api/health /api/dashboard /api/commands (command catalogue)
    GET /api/products [/api/products/<id>]  GET /api/stocks  GET /api/coupons
    GET /api/carts [/api/carts/<id>]  GET /api/orders [/api/orders/<id>]
    GET /api/orders/<id>/payment  GET /api/shipments [/api/shipments/<id>]
    GET /api/returns [/api/returns/<id>]  GET /api/pickups [/api/pickups/<id>]
    GET /api/events?limit=50 (committed domain-event stream, newest last)

Errors: ``{"ok": false, "error": "...", "kind": "domain"|"not_found"|"bad_request"}``
with status 422 / 404 / 400. CORS is wide open for the Vite dev server.
"""
from __future__ import annotations

import argparse
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import command_adapter as ca
from api import queries as q
from api import serializers as s
from infra.container import build_container

CONTAINER = None
CONTAINER_LOCK = threading.Lock()


def get_container(seed: bool = False):
    global CONTAINER
    with CONTAINER_LOCK:
        if CONTAINER is None:
            CONTAINER = build_container()
            if seed:
                from api.seed import seed as seed_demo
                seed_demo(CONTAINER)
        return CONTAINER


def event_to_dict(ev) -> dict:
    d = {"name": getattr(ev, "name", type(ev).__name__)}
    for k, v in vars(ev).items():
        if k.startswith("_"):
            continue
        d[k] = str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
    return d


def serialize_result(cmd_type: str, result):
    c = get_container()
    if result is None:
        return None
    if cmd_type == "PlaceOrder" and isinstance(result, tuple):
        order, payment = result
        return {"order": s.order_to_dict(order), "payment": s.payment_to_dict(payment)}
    if cmd_type in ("OpenCart", "AddToCart", "UpdateCartLine", "RemoveCartLine",
                    "ApplyCoupon", "RemoveCoupon", "CloseCart"):
        return {"cart": s.cart_to_dict(result)}
    if cmd_type == "PublishProduct":
        return {"product": s.product_to_dict(result)}
    if cmd_type == "CancelOrder":
        return {"order": s.order_to_dict(result)}
    if cmd_type == "HandToCarrier":
        return {"shipment": s.shipment_to_dict(result)}
    if cmd_type in ("RequestReturn", "RejectReturn"):
        return {"return": s.rma_to_dict(result)}
    if cmd_type == "SchedulePickup":
        return {"pickup": s.pickup_to_dict(result)}
    if cmd_type == "SettleReturn":
        return {"breakdown": s.breakdown_to_dict(result)}
    # fallback: try generic serializers
    for fn, key in ((s.order_to_dict, "order"), (s.cart_to_dict, "cart"),
                    (s.product_to_dict, "product"), (s.shipment_to_dict, "shipment"),
                    (s.payment_to_dict, "payment"), (s.pickup_to_dict, "pickup"),
                    (s.rma_to_dict, "return")):
        try:
            return {key: fn(result)}
        except Exception:
            continue
    return {"raw": str(result)}


class Handler(BaseHTTPRequestHandler):
    server_version = "EcommerceCQRS/1.0"

    # -- helpers ---------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *args):  # quieter logs
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # -- reads (CQRS query side) ------------------------------------------
    def do_GET(self):
        c = get_container()
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)

        def one(idx):
            return parts[idx] if len(parts) > idx else None

        try:
            if parts == ["api", "health"]:
                return self._send(200, {"ok": True, "service": "ecommerce-cqrs"})
            if parts == ["api", "dashboard"]:
                return self._send(200, {"ok": True, "dashboard": q.dashboard(c)})
            if parts == ["api", "commands"]:
                return self._send(200, {"ok": True, "commands": ca.COMMANDS})
            if parts[:2] == ["api", "products"]:
                pid = one(2)
                if pid:
                    prod = q.get_product(c, pid)
                    return self._send(200, {"ok": True, "product": prod}) if prod \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no product {pid!r}"})
                return self._send(200, {"ok": True, "products": q.list_products(c)})
            if parts == ["api", "stocks"]:
                return self._send(200, {"ok": True, "stocks": q.list_stocks(c)})
            if parts == ["api", "coupons"]:
                return self._send(200, {"ok": True, "coupons": q.list_coupons(c)})
            if parts[:2] == ["api", "carts"]:
                cid = one(2)
                if cid:
                    cart = q.get_cart(c, cid)
                    return self._send(200, {"ok": True, "cart": cart}) if cart \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no cart {cid!r}"})
                return self._send(200, {"ok": True, "carts": q.list_carts(c)})
            if parts[:2] == ["api", "orders"]:
                oid, sub = one(2), one(3)
                if oid and sub == "payment":
                    pay = q.order_payment(c, oid)
                    return self._send(200, {"ok": True, "payment": pay}) if pay \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no payment for order {oid!r}"})
                if oid:
                    order = q.get_order(c, oid)
                    return self._send(200, {"ok": True, "order": order}) if order \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no order {oid!r}"})
                return self._send(200, {"ok": True, "orders": q.list_orders(c)})
            if parts[:2] == ["api", "shipments"]:
                sid = one(2)
                if sid:
                    shp = q.get_shipment(c, sid)
                    return self._send(200, {"ok": True, "shipment": shp}) if shp \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no shipment {sid!r}"})
                return self._send(200, {"ok": True, "shipments": q.list_shipments(c)})
            if parts[:2] == ["api", "returns"]:
                rid = one(2)
                if rid:
                    rma = q.get_return(c, rid)
                    return self._send(200, {"ok": True, "return": rma}) if rma \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no return {rid!r}"})
                return self._send(200, {"ok": True, "returns": q.list_returns(c)})
            if parts[:2] == ["api", "pickups"]:
                pid = one(2)
                if pid:
                    pku = q.get_pickup(c, pid)
                    return self._send(200, {"ok": True, "pickup": pku}) if pku \
                        else self._send(404, {"ok": False, "kind": "not_found",
                                              "error": f"no pickup {pid!r}"})
                return self._send(200, {"ok": True, "pickups": q.list_pickups(c)})
            if parts == ["api", "events"]:
                limit = int((query.get("limit") or ["200"])[0])
                events = c.bus.published()
                tail = events[-limit:]
                return self._send(200, {"ok": True, "events": [event_to_dict(e) for e in tail],
                                        "total": len(events)})
            return self._send(404, {"ok": False, "kind": "not_found",
                                    "error": f"no route {parsed.path!r}"})
        except Exception as e:  # pragma: no cover - defensive
            return self._send(500, {"ok": False, "kind": "server", "error": str(e)})

    # -- writes (CQRS command side) ----------------------------------------
    def do_POST(self):
        c = get_container()
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            body = self._read_json()
        except Exception:
            return self._send(400, {"ok": False, "kind": "bad_request",
                                    "error": "invalid JSON body"})
        try:
            if parts == ["api", "seed"]:
                from api.seed import seed as seed_demo
                return self._send(200, {"ok": True, "seed": seed_demo(c)})
            if parts == ["api", "commands"]:
                cmd_type = body.get("type", "")
                payload = body.get("payload", {})
                try:
                    cmd = ca.build_command(cmd_type, payload)
                except KeyError:
                    return self._send(400, {"ok": False, "kind": "bad_request",
                                            "error": f"unknown command {cmd_type!r}"})
                except Exception as e:
                    return self._send(400, {"ok": False, "kind": "bad_request",
                                            "error": f"bad payload: {e}"})
                before = len(c.bus.published())
                try:
                    result = c.commands.dispatch(cmd)
                except KeyError as e:
                    return self._send(404, {"ok": False, "kind": "not_found",
                                            "error": str(e)})
                except Exception as e:
                    # DomainError / InvariantViolation / CouponValidationError etc.
                    return self._send(422, {"ok": False, "kind": "domain",
                                            "error": f"{type(e).__name__}: {e}"})
                new_events = [event_to_dict(e) for e in c.bus.published()[before:]]
                return self._send(200, {"ok": True, "type": cmd_type,
                                        "result": serialize_result(cmd_type, result),
                                        "events": new_events})
            return self._send(404, {"ok": False, "kind": "not_found",
                                    "error": f"no route {parsed.path!r}"})
        except Exception as e:  # pragma: no cover - defensive
            return self._send(500, {"ok": False, "kind": "server", "error": str(e)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ecommerce CQRS API (stdlib http.server)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--seed", action="store_true", help="seed demo catalog/coupons on startup")
    ap.add_argument("--no-seed", dest="seed", action="store_false")
    ap.set_defaults(seed=True)
    args = ap.parse_args(argv)
    get_container(seed=args.seed)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CQRS API on http://{args.host}:{args.port} (seed={args.seed})")
    print("  writes: POST /api/commands {type, payload} | reads: GET /api/<resource>")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
