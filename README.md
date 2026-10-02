# Ecommerce Order Platform — Domain + Application + Infra (Python, DDD)

Single-seller shop for physical products. Stock is reserved at order time.
Payment is **authorize at placement, capture at shipment**. Returns are
customer-initiated and collected via **doorstep courier pickup**. Discounts are
**single-coupon**, with a **pro-rata refund (goods + tax + shipping)** on return.

This repo is a clean DDD implementation in Python: aggregates own their
invariants, application services / command handlers cross bounded contexts
inside one atomic Unit-of-Work, and infra follows a local-first Blocks
philosophy (same code runs locally or on AWS).

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
tests/           87 tests: aggregates, happy path, returns, commands, infra.
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

Requires Python 3.10+ (tested on 3.14), no third-party dependencies.

```bash
git clone <this-repo> && cd ecommerce-domain
python3 -m pytest -q
```

`pytest.ini` sets `pythonpath = .` and `testpaths = tests`.

```bash
python3 -m pytest -q                        # all 87 tests
BLOCKS_BACKEND=local python3 -m pytest tests/test_infra_blocks.py -q
```

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
tests/                     test_aggregates, test_happy_path, test_returns,
                           test_commands, test_infra_blocks, conftest
pytest.ini / .gitignore
```

## Assumptions (v1 locked)

Single seller / single warehouse / single carrier; no split shipments; no
exchanges (return is refund-only, re-order is a fresh order); tax is a
pluggable zero-rate modifier; customer identity is an opaque external ref;
return window 30 days; pickup free, `NoAccess` re-schedules up to 2 attempts.
See `domain-model.md` §6 for the full list.
