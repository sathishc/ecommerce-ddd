"""API layer tests: command adapter, queries (read side), and HTTP smoke.

Covers the CQRS contract: writes go through ``build_command`` + the
``CommandBus`` (one atomic UoW each), reads come from ``api.queries``
without mutating state, and the stdlib HTTP server maps domain failures
to 422 / missing aggregates to 404 / unknown commands to 400.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from api import command_adapter as ca
from api import queries as q
from api import seed as seed_mod
from api import server as srv
from infra.container import build_test_container


@pytest.fixture
def container():
    c = build_test_container()
    seed_mod.seed(c)
    return c


def _post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# -- command adapter ------------------------------------------------------
def test_build_command_round_trips():
    cmd = ca.build_command("AddToCart", {"cart_id": "c", "product_id": "p", "quantity": 2})
    assert cmd.cart_id == "c" and cmd.quantity == 2
    dest = {"line1": "1 Main St", "city": "Springfield", "postal_code": "12345", "country": "US"}
    cmd = ca.build_command("PlaceOrder", {"cart_id": "c", "instrument_ref": "card",
                                          "destination": dest})
    assert cmd.destination.city == "Springfield"
    cmd = ca.build_command("RequestReturn", {"order_id": "o",
                                             "lines": [["p1", 2]], "reason": "x"})
    assert cmd.lines == (("p1", 2),)
    cmd = ca.build_command("SchedulePickup", {"rma_id": "r",
                                              "slot": "2026-10-05T10:00:00"})
    assert isinstance(cmd.slot, datetime)
    with pytest.raises(KeyError):
        ca.build_command("Nope", {})


def test_seed_is_idempotent(container):
    first = seed_mod.seed(container)
    second = seed_mod.seed(container)
    assert second["products"] == 0 and second["coupons"] == 0
    assert first["totals"] == second["totals"]


# -- queries are pure reads ------------------------------------------------
def test_queries_read_without_mutating(container):
    assert len(q.list_products(container)) == 4
    assert len(q.list_coupons(container)) == 3
    assert len(q.list_stocks(container)) == 4
    assert q.list_orders(container) == []
    dash = q.dashboard(container)
    assert dash["counts"]["products"] == 4


def test_write_then_read_full_path(container):
    products = q.list_products(container)
    p1 = next(p for p in products if p["sku"] == "WIDGET-1")
    cart = container.commands.dispatch(ca.build_command("OpenCart", {"customer_ref": "t"}))
    container.commands.dispatch(ca.build_command(
        "AddToCart", {"cart_id": cart.aggregate_id(),
                      "product_id": p1["product_id"], "quantity": 1}))
    cart_dto = q.get_cart(container, cart.aggregate_id())
    assert len(cart_dto["lines"]) == 1 and cart_dto["quote"]["total"]["minor"] > 0
    assert q.get_cart(container, "missing") is None


# -- HTTP smoke (real server, ephemeral port) -------------------------------
@pytest.fixture
def live_port(container):
    old = srv.CONTAINER
    srv.CONTAINER = container
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    srv.CONTAINER = old


def test_http_write_and_read(live_port):
    port = live_port
    status, body = _get(port, "/api/health")
    assert status == 200 and body["ok"]
    status, body = _post(port, "/api/commands",
                         {"type": "OpenCart", "payload": {"customer_ref": "web"}})
    assert status == 200 and body["result"]["cart"]["cart_id"]
    status, body = _get(port, "/api/dashboard")
    assert status == 200 and body["dashboard"]["counts"]["carts"] == 1


def test_http_error_mapping(live_port):
    port = live_port
    status, body = _post(port, "/api/commands", {"type": "Nope", "payload": {}})
    assert status == 400 and not body["ok"]
    status, body = _post(port, "/api/commands",
                         {"type": "AddToCart",
                          "payload": {"cart_id": "missing", "product_id": "x", "quantity": 1}})
    assert status == 404 and body["kind"] == "not_found"
    status, body = _get(port, "/api/carts/missing")
    assert status == 404
    status, body = _get(port, "/api/nope")
    assert status == 404


def test_http_rejects_domain_violation(live_port):
    port = live_port
    status, body = _post(port, "/api/commands",
                         {"type": "OpenCart", "payload": {"customer_ref": ""}})
    assert status == 422 and body["kind"] == "domain"
