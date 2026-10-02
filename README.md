# Ecommerce Order Platform — DDD Backend + CQRS API + React Frontend

Single-seller shop for physical products. Stock is reserved at order time.
Payment is **authorize at placement, capture at shipment**. Returns are
customer-initiated and collected via **doorstep courier pickup**. Discounts are
**single-coupon**, with a **pro-rata refund (goods + tax + shipping)** on return.

This repo is a clean DDD implementation in Python: aggregates own their
invariants, application services / command handlers cross bounded contexts
inside one atomic Unit-of-Work, and infra follows a local-first Blocks
philosophy (same code runs locally or on AWS). On top sits a stdlib-only
CQRS HTTP API (`api/`) and a React demo frontend (`web/`) that walks the
full order + return flows through commands (writes) and queries (reads).

Full ubiquitous language, context map, and invariants live in
[`domain-model.md`](./domain-model.md) (v1 locked).

## Architecture

```
domain/          Pure domain — no I/O, no AWS, no DB. Aggregates, value
                 objects, domain events, commands, errors.
application/     Orchestration across contexts. Command handlers (one per
                 user intent) + thin app services. One atomic UoW per call.
infra/           Technical adapters: repositories, Unit-of-Work, CommandBus,
                 EventBus, Clock, local-first Blocks (KVStore/Table/S3/SES/
                 SSM/CloudWatch/X-Ray/SQS ports), DI container.
api/             CQRS HTTP layer (stdlib only): JSON → Command writes on
                 POST /api/commands, repository read-models on GET /api/….
web/             React + Vite demo frontend; src/cqrs/ splits commands.js
                 (writes) from queries.js (reads).
tests/           94 tests: aggregates, happy path, returns, commands, infra, api.
```

Dependency rule: `domain` ← `application` ← `infra`. Domain never imports
application or infra.

### Bounded contexts

| Context | Responsibility | Aggregates |
|---|---|---|
| Catalog | Sellable products | `Product` |
| Inventory | Stock, reservations | `Stock` |
| Shop (Cart, Checkout, Pricing) | Cart, single-coupon discounting, pricing, checkout | `Cart`, `Coupon` |
| Order Management | Order lifecycle, frozen totals | `Order` |
| Payment | Generic authorize / capture / refund | `Payment` |
| Fulfillment | Shipments + doorstep return pickups | `Shipment`, `Pickup` |
| Returns | Customer RMAs, refund + restock | `Return` |
| Notification | Email/SMS on domain events | — (subscriber) |

Key relationships: Shop → Order (checkout produces `PlaceOrder`);
Order → Payment is downstream-via-events (Payment never learns "Order");
Order → Inventory (conformist, but Inventory publishes
`StockInsufficient` / `StockReleased` / `StockCommitted` as the safety valve);
Order ↔ Fulfillment (partnership); Returns is downstream-conformist to Order.
`Money` is the only shared kernel.

## Key business rules

- **Cart:** qty ≥ 1, one line per product, total always derived
  (lines − discount + tax + shipping), at most **one coupon**, checked-out
  carts are immutable.
- **Order:** total frozen at placement (catalog price changes never affect
  existing orders). `Placed → Paid → Shipped → Delivered → Refunded`
  (or `Cancelled`/`Expired`). Only `Delivered` can be returned; `Refunded`
  is terminal. No double-charge.
- **Stock:** `available ≥ 0` always; all-or-nothing reservation per order
  (no partials); commit on shipment, release on cancel/expiry; restock only
  after courier possession (`PickupConfirmed`), never double-restocked.
- **Payment (generic):** `Authorized → Captured | Refunded | Voided`;
  Σ refunds ≤ captured; partial refunds stay `Captured` until fully refunded.
- **Coupon:** one per order, one redemption per customer, re-validated at
  placement (cart apply is only a preview), percentage capped at 100% of
  subtotal. A **complete** return un-redeems the coupon; a partial return
  keeps it burned.
- **Shipment:** one shipment per order (no split shipments in v1), tracking
  number immutable, `Delivered` terminal on carrier confirmation.
- **Pickup (doorstep):** one per approved return, `Scheduled → EnRoute →
  PickedUp | NoAccess`; `PickedUp` requires courier scan + **photo evidence**
  (+ optional signature); `NoAccess` re-schedules (up to 2 attempts).
- **Return/RMA:** only against `Delivered` orders within 30 days; qty ≤
  shipped qty; refund is **pro-rata** (see below); complete return triggers
  `Coupon.unredeem`; pickup is free.

### Refund rule (pro-rata, incl. shipping)

```
returnFraction = returnedLineValue / originalLineSubtotal   # 1.0 = full return
goodsRefund    = returnedLineValue × (1 − discountShare)
taxRefund      = goodsRefund × taxRate
shippingRefund = shippingFee × returnFraction               # full fee on full return
totalRefund    = goodsRefund + taxRefund + shippingRefund
```

## Order flows

**Happy path:**
```
OpenCart → AddToCart×N → ApplyCoupon → PlaceOrder
  → ReserveStock → AuthorizePayment
  → HandToCarrier (commit stock + CapturePayment) → ConfirmDelivery
```

**Return path (doorstep pickup):**
```
RequestReturn → ApproveReturn → SchedulePickup
  → ConfirmPickup (door, scan + photo) → ReturnGoodsReceived
  → SettleReturn (pro-rata) → PaymentRefunded + StockRestocked + Order→Refunded
  → (if complete return) CouponUnredeemed
```

Commands are user intents (`AddToCart`, `PlaceOrder`, `RequestReturn`, … in
`domain/commands.py`, dispatched via `CommandBus` to exactly one handler in
`application/command_handlers.py`). System follow-ons (reserve → authorize →
capture → restock → un-redeem) are purely event-driven — intentionally **not**
commands, since no external actor initiates them.

## Getting started

Backend: Python 3.10+ (tested on 3.14), no third-party dependencies.
Frontend (`web/`): Node 18+ and npm.

```bash
git clone <this-repo> && cd ecommerce-domain
python3 -m pytest -q
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`.

```bash
python3 -m pytest -q                        # all 94 tests
python3 -m pytest tests/test_api.py -q      # API layer only (7 tests)
BLOCKS_BACKEND=local python3 -m pytest tests/test_infra_blocks.py -q
```

### Run the demo (backend + frontend)

```bash
python3 -m api.server --port 8000 --seed   # backend + demo catalog/coupons
cd web && npm install && npm run dev       # React frontend on :5173 (/api proxied)
```

Open http://localhost:5173. The Shop tab walks `OpenCart → AddToCart →
ApplyCoupon → PlaceOrder`; Orders covers `HandToCarrier` (commit + capture),
`ConfirmDelivery`, and `CancelOrder`; Returns covers `RequestReturn →
SchedulePickup → ConfirmPickup` (scan + photo) `→ SettleReturn` (pro-rata);
Events shows the committed event stream. State is in-memory — restarting the
backend resets the demo (re-seed with `POST /api/seed` or `--seed`).

## Usage

Two equivalent front doors (same behavior, same UoW atomicity):

**1. CommandBus (recommended — dispatchable intents):**

```python
from infra.container import build_container
from domain.commands import OpenCart, AddToCart, ApplyCoupon, PlaceOrder, HandToCarrier, ConfirmDelivery
from domain.value_objects import Address, Money

c = build_container()
cmd = c.commands
h = c.command_handlers  # or cmd.dispatch(...)

cart = h.open_cart(OpenCart(customer_ref="cust-1"))
h.add_to_cart(AddToCart(cart_id=cart.cart_id, product_id=product_id, quantity=2))
h.apply_coupon(ApplyCoupon(cart_id=cart.cart_id, coupon_code="SAVE20"))
order, payment = h.place_order(PlaceOrder(
    cart_id=cart.cart_id,
    instrument_ref="card-1",
    destination=Address("1 Main St", "Springfield", "12345", "US"),
))
shipment = h.hand_to_carrier(HandToCarrier(order_id=order.order_id, carrier="UPS", tracking_number="TRK-1"))
```

**2. Application services (direct calls):**

```python
from infra.container import build_container

c = build_container()
order, payment = c.checkout.place_and_pay(cart, "card-1")
shipment = c.shipping.ship(order, "UPS", "TRK-1")
c.shipping.mark_delivered(shipment, order)
rma = c.returns_svc.request_return(order, [(product_id, 1)], "broken")
pickup = c.returns_svc.schedule_pickup(rma, slot)
c.returns_svc.complete_pickup(rma, pickup, evidence)  # courier scan + photo
breakdown = c.returns_svc.settle(rma, order)          # pro-rata refund
```

For deterministic tests / scripts: `build_test_container()` injects a
`FixedClock` and fresh in-memory everything. `build_container(backend="aws")`
selects AWS-backed Blocks with no application-code changes.

## Infra: local-first Blocks

`infra/blocks.py` is a Python port of the AWS Blocks philosophy — each Block
is an ABC with a local impl (default) and an AWS impl selected by
`BLOCKS_BACKEND=local|aws`:

| Block | AWS service | Local impl |
|---|---|---|
| KVStore / DistributedTable | DynamoDB | in-memory dict |
| FileBucket | S3 | in-memory (pickup photo evidence) |
| EmailClient | SES | captures sends (Notification ctx) |
| AppSetting | SSM Parameter Store | dict + env |
| Logger / Metrics / Tracer | CloudWatch / X-Ray | stdout+memory / counters / spans |
| AsyncJob | SQS + Lambda | in-memory queue |

Plus: `Clock` (`SystemClock`/`FixedClock`), `EventBus` (sync pub/sub,
transactional outbox — events publish only on UoW commit),
per-aggregate `Repository` ABCs + in-memory impls, `InMemoryUnitOfWork`
(snapshot/rollback), `NotificationService` (subscribes to all events),
and `container.py` — the composition root (`build_container` /
`build_test_container`).

## Project layout

```
domain-model.md            Ubiquitous language, contexts, aggregates, events (source of truth)
domain/                    value_objects, product, stock, coupon, cart, order,
                           payment, shipment, pickup, return_rma,
                           commands, events, errors, aggregate, orchestration
application/               command_handlers.py (Command → UoW → Events),
                           services.py (Checkout/Shipping/Cancel/Return)
infra/                     blocks.py, clock.py, event_bus.py, repositories.py,
                           unit_of_work.py, command_bus.py, handlers.py,
                           container.py
api/                       CQRS HTTP layer (stdlib only): server.py (routes),
                           command_adapter.py (JSON → Command), queries.py
                           (read-models), serializers.py, seed.py (demo data)
web/                       React + Vite demo frontend (commands vs queries split
                           in src/cqrs/); proxies /api to the backend in dev
tests/                     test_aggregates, test_happy_path, test_returns,
                           test_commands, test_infra_blocks, test_api, conftest
pytest.ini / .gitignore
```

## API + frontend (CQRS demo)

The backend exposes the domain over HTTP with a strict CQRS split — same
`Container`/`CommandBus` as the tests, no new dependencies (stdlib
`http.server` only):

- **Writes:** `POST /api/commands` with `{"type": "AddToCart", "payload": {...}}`
  — one domain `Command` → exactly one handler → one atomic UoW. Returns the
  resulting DTO plus the events committed by that UoW.
- **Reads:** pure `api/queries.py` over the repositories; never mutate, never
  open a UoW.
- Errors are JSON envelopes: domain violations → 422 (`{"ok": false, "kind":
  "domain", ...}`), missing aggregates → 404 (`"kind": "not_found"`), unknown
  commands → 400 (`"kind": "bad_request"`).

### Write endpoint

`POST /api/commands` accepts all 17 user intents (`GET /api/commands` lists
them): `PublishProduct`, `OpenCart`, `AddToCart`, `UpdateCartLine`,
`RemoveCartLine`, `ApplyCoupon`, `RemoveCoupon`, `PlaceOrder`, `CancelOrder`,
`CloseCart`, `HandToCarrier`, `ConfirmDelivery`, `RequestReturn`,
`RejectReturn`, `SchedulePickup`, `ConfirmPickup`, `SettleReturn`.

### Read endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/dashboard` | counts, orders-by-status, revenue read-model |
| `GET /api/products` (+ `/:id`) | catalog with `Money` as `{minor, currency, display}` |
| `GET /api/stocks` | availability + reserved per product (with product names) |
| `GET /api/coupons` | code, type, window, usage, redemptions |
| `GET /api/carts` (+ `/:id`) | lines + derived `quote` (subtotal − discount + tax + shipping) |
| `GET /api/orders` (+ `/:id`) | frozen totals, status, lines, coupon, payment ref |
| `GET /api/orders/:id/payment` | payment status + accrued refunds |
| `GET /api/shipments` (+ `/:id`) | carrier, tracking, scans |
| `GET /api/returns` (+ `/:id`) | RMA lines, status, pro-rata settlement once settled |
| `GET /api/pickups` (+ `/:id`) | courier slot, status, photo evidence |
| `GET /api/events?limit=N` | committed domain-event stream (transactional outbox), newest last |
| `POST /api/seed` | (re)seed demo data, idempotent |

### curl walkthrough (happy path)

```bash
B=localhost:8000
CART=$(curl -s -X POST $B/api/commands -H 'Content-Type: application/json' \
  -d '{"type":"OpenCart","payload":{"customer_ref":"demo-1"}}' |
  python3 -c "import sys,json; print(json.load(sys.stdin)['result']['cart']['cart_id'])")
P1=$(curl -s $B/api/products |
  python3 -c "import sys,json; print(json.load(sys.stdin)['products'][0]['product_id'])")
curl -s -X POST $B/api/commands -H 'Content-Type: application/json' \
  -d "{\"type\":\"AddToCart\",\"payload\":{\"cart_id\":\"$CART\",\"product_id\":\"$P1\",\"quantity\":2}}"
curl -s -X POST $B/api/commands -H 'Content-Type: application/json' \
  -d "{\"type\":\"ApplyCoupon\",\"payload\":{\"cart_id\":\"$CART\",\"coupon_code\":\"SAVE20\"}}"
ORD=$(curl -s -X POST $B/api/commands -H 'Content-Type: application/json' -d \
  "{\"type\":\"PlaceOrder\",\"payload\":{\"cart_id\":\"$CART\",\"instrument_ref\":\"card-1\",\
  \"destination\":{\"line1\":\"1 Main St\",\"city\":\"Springfield\",\"postal_code\":\"12345\",\"country\":\"US\"}}}" |
  python3 -c "import sys,json; print(json.load(sys.stdin)['result']['order']['order_id'])")
curl -s -X POST $B/api/commands -H 'Content-Type: application/json' \
  -d "{\"type\":\"HandToCarrier\",\"payload\":{\"order_id\":\"$ORD\",\"carrier\":\"UPS\",\"tracking_number\":\"TRK-1\"}}"
SHP=$(curl -s $B/api/shipments |
  python3 -c "import sys,json; print(json.load(sys.stdin)['shipments'][0]['shipment_id'])")
curl -s -X POST $B/api/commands -H 'Content-Type: application/json' \
  -d "{\"type\":\"ConfirmDelivery\",\"payload\":{\"shipment_id\":\"$SHP\",\"order_id\":\"$ORD\"}}"
curl -s $B/api/orders/$ORD   # Delivered · frozen total · payment Captured
```

Return path from there: `RequestReturn` (lines as `[[product_id, qty]]`) →
`SchedulePickup` (ISO `slot`) → `ConfirmPickup` (`evidence.photo_ref`) →
`SettleReturn` (pro-rata `goods + tax + shipping` breakdown).

### Seed data (`--seed` / `POST /api/seed`)

4 products (Gadget Pro $100, Gadget Mini $50, Braided Cable $14.99, Desk
Stand $29.99) with stock, plus coupons `SAVE20` (20% off), `FLAT5` ($5 off),
`FREESHIP` (free shipping).

### Frontend (`web/`)

React 18 + Vite. `src/cqrs/` mirrors the backend split: `commands.js` (all
mutations as dispatched intents) vs `queries.js` (all reads as GETs,
re-fetched after each command). Vite proxies `/api` → `127.0.0.1:8000` in
dev (`npm run dev`, :5173); `npm run build` produces a static `dist/`.

| Tab | Demonstrates |
|---|---|
| **Shop** | catalog query → `OpenCart` → `AddToCart` → `ApplyCoupon` → live quote → `PlaceOrder` (authorize, not capture) |
| **Orders** | order read-models + payment status; `HandToCarrier` (commit + capture), `ConfirmDelivery` (opens return window), `CancelOrder` (void + release, pre-shipment only) |
| **Returns** | `RequestReturn` (Delivered-only picklist) → `SchedulePickup` → `ConfirmPickup` (scan + photo) → `SettleReturn` with goods/tax/shipping breakdown |
| **Events** | committed event stream, polls every 3s — the Notification-context view |

## Assumptions (v1 locked)

Single seller / single warehouse / single carrier; no split shipments; no
exchanges (return is refund-only, re-order is a fresh order); tax is a
pluggable zero-rate modifier; customer identity is an opaque external ref;
return window 30 days; pickup free, `NoAccess` re-schedules up to 2 attempts.
See `domain-model.md` §6 for the full list.
