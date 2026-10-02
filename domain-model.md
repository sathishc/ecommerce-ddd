# Ecommerce Order Platform — Domain Model

Single-seller shop · physical products · stock reserved at order time
**Payment: authorize at placement, capture at shipment · Customer-initiated returns via doorstep pickup · Single-coupon discounts · Pro-rata shipping refund · v1 final**

---

## 1. Ubiquitous Language

| Term | Definition | Synonyms to avoid |
|---|---|---|
| **Product** | A sellable catalog item identified by SKU; has a name, description, price, physical attributes | "Item" (too generic), "Sku" (that's its identifier) |
| **SKU** | Unique, immutable code identifying a Product | "Product ID" (use for identity, not business code) |
| **Price** | Money (amount + currency) at which a Product is sold; a value object on the Product | "Cost" (that's COGS), "Fee" |
| **Cart** | A customer's temporary, mutable collection of intended purchases, between browsing and checkout | "Basket" (pick one, use everywhere) |
| **Cart Line** | One entry in a Cart: a Product + Quantity | "Cart item" (fine, but avoid "OrderItem" here) |
| **Coupon** | A discount instrument with a code, a type, usage limits, and validity; applied to a Cart or Order | "Promo" (OK in UI), "Discount" (that's the *result*, not the instrument) |
| **Discount** | The computed Money reduction a Coupon produces for a particular Cart/Order total | "Coupon" (that's the instrument), "Promo code" |
| **Shipping Fee** | The Money charged to deliver an Order's goods to the destination; frozen at placement | "Delivery fee" (fine in UI), "Postage" |
| **Order** | A customer's request to buy, with a frozen total (lines − discount + tax + shipping); the central agreement once placed | "Purchase", "Sale" (describe the completed event, not the object) |
| **Order Line** | An immutable snapshot of a Cart Line: product ref, unit price, quantity | "OrderItem" |
| **Order Total** | lines − discount + tax + shipping; computed once at placement, then frozen | "Grand total" (fine in UI, not in code) |
| **Stock** | Quantity of a Product held at the (single) warehouse | "Inventory" (acceptable; pick one team-wide) |
| **Reservation** | A temporary hold on Stock for an unpaid Order, with an expiry | "Lock" (implies permanent) |
| **Shipment** | Physical movement of a Shipment Line to a destination, with carrier + tracking number | "Delivery" (that's the *event*), "Dispatch" |
| **Payment** | A charge on a payment instrument: Authorized → Captured → Refunded | "Transaction" (too generic, gateway-specific) |
| **Return (RMA)** | A customer-requested return of goods against a Delivered Order, collected at the customer's door, driving refund + restock | "Refund request" (the refund is the *outcome*; the RMA is the process) |
| **Pickup** | The doorstep collection of a return: a scheduled courier visit to the customer's address that takes possession of the goods, evidenced by a photo + optional signature | "Collection" (fine in UI), "Drop-off" (that's a different channel, not used here) |
| **Customer** | The person placing Orders; identity managed outside these contexts | "Buyer" (UI copy only) |

⚠️ **Flagged ambiguities:** "Cart vs Basket" (pick one), "Item" (Product? Cart Line? Order Line? Shipment Line? — every "item" must carry a prefix in code), "Stock vs Inventory" (pick one), "Delivery" (entity Shipment vs event `OrderDelivered`?), "Order vs Purchase" (object vs completed state), "Discount vs Coupon" (result vs instrument), "Return vs Pickup" (the whole process vs the doorstep collection step).

---

## 2. Bounded Contexts

| Bounded Context | Responsibility | Key Aggregates |
|---|---|---|
| **Catalog** | Author and maintain sellable Products | `Product` |
| **Inventory** | Maintain Stock per Product; issue and expire reservations | `Stock` |
| **Shop** (Cart, Checkout, **Pricing**) | Pre-order experience: cart manipulation, **single-coupon discounting**, pricing (incl. shipping), checkout orchestration | `Cart`, `Coupon` |
| **Order Management** | Own the Order lifecycle, frozen totals, all state transitions | `Order` |
| **Payment** | Charge payment instruments; authorize / capture / refund (generic — no ecommerce knowledge) | `Payment` |
| **Fulfillment** | Pick, pack, hand to carrier, track shipments; and the **doorstep return pickup** leg | `Shipment`, `Pickup` |
| **Returns** | Customer-initiated returns against delivered orders; drive refund + restock | `Return` |
| **Notification** | React to domain events; deliver email/SMS | — |

**Context map:**

```
   Customer ──► ┌──────────────┐  ProductCatalog  ┌─────────┐
                │    Shop      │◄─────────────────│ Catalog │  (OHS)
                │ Cart+Coupon  │  CartCheckedOut  │         │
                └──────┬───────┘──────────────────┴─────────┘
                       │ OrderPlaced (with computed discount + shipping)
                       ▼
   Payment ◄─────┐  ┌────────────────┐  ReserveStock  ┌──────────┐
   (generic,     │  │  Order Mgmt    │────────────────►│ Inventory│
   downstream)   │  │   (upstream)   │  StockInsufficient │        │
                  │  │                │◄───────────────│(conformist)│
                  │  │                │  StockReleased │          │
                  └─►│                │  StockCommitted│          │
                     │                │               └──────────┘
                     │ OrderPaid                 │
                     ▼                           │
               ┌────────────┐                     │
               │Fulfillment │  Shipment + Pickup  │
               └─────┬──────┘─────────────────────│
                     │ ShipmentShipped            │
                     ▼                            │
              Payment: capture                    │
              ShipmentDelivered                   │
                     │                            │
                     ▼                            │
               ┌──────────────┐  ReturnRequested  │
               │   Returns    │◄──────────────────┘  (customer)
               │  (RMA)       │  ReturnApproved
               └──────┬───────┘  PickupScheduled
                      │ PickupConfirmed (courier, at customer's door)
                      │ ReturnRefunded  →  Payment: refund (goods+tax+shipping, pro-rata)
                      │                   Inventory: restock
                      │                   Order: Refunded
                      ▼
              Notification ◄── all domain events (pub/sub)
```

- **Catalog → Shop / Order:** *Open Host Service / Consumer* — stable views of `ProductId`, `name`, `price`; Order snapshots values at placement, so it's resilient to later catalog changes.
- **Shop → Order:** *Customer/Supplier* — checkout produces `PlaceOrder` (carrying the coupon discount + shipping result); Shop is upstream.
- **Order → Payment:** *Downstream via events* — Payment is a **generic downstream** context; the Order context translates to its language (ACL) so Payment never learns "Order".
- **Order → Inventory:** Order is upstream; Inventory is a **conformist** to order semantics but *publishes* `StockInsufficient` / `StockReleased` / `StockCommitted` upstream — the safety valve of the whole model.
- **Order → Fulfillment:** *Partnership* — Order emits `OrderPaid`; Fulfillment builds `Shipment` **and** the return `Pickup`.
- **Order → Returns:** Returns is a **downstream conformist** to order semantics; it reads `Delivered` orders and coordinates a `Pickup` in Fulfillment to collect the goods at the customer's door. When a return settles, it drives `Refunded` back on Order plus restock on Inventory. Customer-initiated only.
- **All → Notification:** *Pub/sub on domain events*.
- **No Shared Kernels** except the `Money` value object, shared read-only across all contexts (currency-attached minor units).

---

## 3. Aggregates (root, members, invariants)

### Cart (root: `Cart`) — *Shop*
**Members:** `CartLine` (productId, unitPrice snapshot, quantity, lineTotal), `customerRef`, `appliedCouponId?` (at most one), `shippingFee` (Money, derived from destination), `status` (Open → CheckedOut), derived `total`
**Invariants:**
- C1: quantity ≥ 1 for every line; at most one line per Product (adding same product increases quantity)
- C2: `total` is always **derived** = lines − discount (if a coupon applies) + tax + shippingFee; never stored by hand
- C3: **at most one coupon** per Cart (confirmed single); applying a new one replaces the old; a coupon that doesn't validate is rejected, not silently ignored
- C4: a `CheckedOut` Cart is immutable
**Behavior:** `addProduct(id, qty)`, `changeQuantity(lineId, qty)`, `removeLine(lineId)`, `applyCoupon(code)` → Discount, `removeCoupon()`, `quote()` → `{subtotal, discount, tax, shippingFee, total}`, `checkout()` → emits `CartCheckedOut`
- Anemic check ✓: total is computed *inside* the aggregate; no external code does `cart.total = …`.

### Order (root: `Order`) — *Order Management*
**Members:** `OrderLine` (productId, **unitPrice snapshot**, quantity, lineTotal), `destination` (Address), `customerRef`, `discount` (Money, frozen from the coupon), `shippingFee` (Money, frozen), `moneyTotal` (frozen = lines − discount + tax + shipping), `reservationRefs`, `status` (Placed → Paid → Shipped → Delivered → **Refunded**; or Cancelled; or Expired), `rmaIds[]`
**Invariants:**
- O1: at least one line; Order exists only after checkout succeeded
- O2: `moneyTotal` is fixed at placement; **never recomputed later** — later catalog price changes don't alter existing orders
- O3: state machine: `Placed →(PaymentAuthorized)→ Paid →(shipped)→ Shipped →(delivered)→ Delivered →(return)→ Refunded`; `Placed` may be cancelled (no charge yet → void); `Paid` may be cancelled *before* shipment (refunds the authorization) but not after `Shipped`; only `Delivered` may enter `Refunded` (via Returns)
- O4: cannot be `Shipped` unless `Paid`; cannot be charged twice; `Refunded` is terminal (a return already settled the money)
- O5: all mutations via root methods — OrderLine is immutable after placement
**Behavior:** `place(snapshot, destination)`, `pay(authorizationRef)`, `confirmShipped(shipmentId)`, `deliver()`, `cancel(reason)`, `expire()`, `acceptReturn(rmaId, amount)`, `refundCompleted(amount)`

### Product (root: `Product`) — *Catalog*
**Members:** `sku`, `name`, `description`, `price` (Money), `weight`, `status` (Active / Discontinued)
**Invariants:** P1: SKU unique and immutable; P2: price ≥ 0; P3: price change is an *event* (`ProductPriceChanged`), never silent
**Behavior:** `updateCatalogFields(…)`, `changePrice(newPrice)`, `discontinue()`

### Stock (root: `Stock`) — *Inventory* (one per Product)
**Members:** `available` (int), `reserved` (int), `reservationHandles[]` → (`orderId`, quantity, expiry)
**Invariants:** S1: `available ≥ 0` always; S2: `reserve(qty)` fails (`StockInsufficient`) if `available < qty` — **all-or-nothing per order, no partial reservations**; S3: `commit(reservationId)` on fulfillment; `release(reservationId)` on cancel / expiry; S4: restock via `release` **only** through the Returns flow for delivered returns, and only after the goods are actually back (`PickupConfirmed`) — never double-restocked
**Behavior:** `reserve(orderId, qty, expiry)` → reservationRef, `commit(reservationRef)`, `release(reservationRef, reason)`, `expire(reservationRef)`, `restock(returnRef, qty)` (audited, from Returns), `adjust(qty)` (manual correction, audited)
- This is the anti-oversell spine of the model.

### Payment (root: `Payment`) — *Payment* (generic)
**Members:** `amount` (Money), `instrumentRef` (opaque), `status` (Authorized → Captured | Refunded | Voided), `refunds[]` (amount, reason, date)
**Invariants:** PM1: Σ refunded ≤ captured; PM2: capture only from `Authorized`; PM3: `Captured`, `Voided`, `Refunded` are terminal; PM4: a *partial* refund (from a partial return) keeps the Payment in `Captured` while accruing `refunds[]`, and the Payment flips to `Refunded` only when Σ refunded == captured (full settlement)
**Behavior:** `authorize(instrumentRef, amount)`, `capture()`, `refund(amount, reason)` (partial or full), `void()`

### Shipment (root: `Shipment`) — *Fulfillment*
**Members:** `shipmentLines[]` (productId, qty), `destination`, `carrier`, `trackingNumber`, `status` (Created → HandedToCarrier → InTransit → Delivered)
**Invariants:** SH1: a shipment covers exactly the lines of one `Paid` Order; SH2: `trackingNumber` immutable once set; SH3: `Delivered` is terminal, fires only on carrier confirmation; SH4: a Shipment becomes a returnable unit — its lines seed the `Return` aggregate
**Behavior:** `handToCarrier(carrier, trackingNumber)`, `updateTracking(scan)`, `confirmDelivered()`

### Pickup (root: `Pickup`) — *Fulfillment* (doorstep return collection)
**Members:** `pickupId`, `returnId`, `customerAddress`, `scheduledSlot` (datetime window), `courierRef`, `evidence?` (photo hash + optional signature, captured at collection), `status` (Scheduled → EnRoute → **PickedUp** | NoAccess)
**Invariants:** PK1: a Pickup belongs to exactly one approved `Return`; PK2: `scheduledSlot` is within the courier's service area; PK3: `PickedUp` is terminal and fires only on the courier's on-site scan (possession actually transferred); PK4: `NoAccess` (nobody home) re-schedules rather than failing — up to N attempts before the Return is flagged for manual handling; PK5: **evidence of collection is mandatory** — `confirmPickup()` requires captured `evidence` (photo + optional signature); since the customer holds no return label, the courier scan + photo is their proof of collection
**Behavior:** `schedule(returnId, slot)` → Pickup, `assignCourier(ref)`, `confirmPickup(evidence)` (courier scan + photo → possession transferred), `markNoAccess(reason)` → re-schedule

### Coupon (root: `Coupon`) — *Shop / Pricing*
**Members:** `code` (unique, case-insensitive), `type` (Percentage | FixedAmount | FreeShipping), `value` (pct or Money), `minOrderValue?` (Money, for threshold), `validFrom`, `validTo`, `usageLimit?` (max redemptions), `redemptions` (int), `status` (Active / Expired / Deactivated)
**Invariants:** CO1: `code` unique; CO2: a coupon may be applied only when `validFrom ≤ now ≤ validTo` and `redemptions < usageLimit`; CO3: **one redemption per customer** (idempotent by customerId — a customer cannot double-dip); CO4: `redemptions` increments only at `PlaceOrder` time (not at cart apply) — the cart application is a *preview* that must be re-validated at order placement, because another session could race; CO5: a percentage coupon caps its own discount at 100% of the (pre-tax) subtotal — a 150%-off coupon is impossible by construction; CO6: **one coupon per order** (single-coupon policy, confirmed) — the aggregate holds exactly one `appliedCouponId` reference at a time; CO7: a **complete** return (every shipped line returned, order fully settled) triggers `unredeem(customerId, orderId)` — the redemption count is decremented and the customer regains eligibility for that code; a **partial** return keeps the redemption burned
**Behavior:** `validate(customerId, cartTotal)` → `Discount | ValidationError`, `redeem(customerId, orderId)` (increments), `unredeem(customerId, orderId)` (decrements; idempotent; only on a full refund — the mirror of `redeem`), `deactivate()`, `expire()`
- This is the aggregate that makes coupons safe under concurrency: the *check* is on the Cart side, the *commit* is atomic on the Coupon side.

### Return / RMA (root: `Return`) — *Returns*
**Members:** `rmaId`, `orderId` (must be `Delivered`), `returnLines[]` (productId, qty ≤ shipped qty), `reason`, `requestedDate`, `pickupId?` (link to the `Pickup`), `refunds[]` (per settlement), `status` (Requested → Approved → **PickupScheduled → PickedUp → GoodsReceived** → **Refunded** | Rejected)
**Invariants:**
- RT1: may be opened only against a `Delivered` order, within the **return window** (30 days from delivery, configurable)
- RT2: `returnLines` qty ≤ the original shipped qty for that product (no over-returning)
- RT3: refund amount is **pro-rata** — goods + tax + **shipping** all scale with the fraction of the order's line value that is returned (see §5 refund rule); a full return (all lines) refunds the full shipping fee, a partial return refunds a proportional share
- RT4: `Refunded` is terminal
- RT5: the returned goods are **collected at the customer's door** via a `Pickup` (doorstep, confirmed) — the courier's on-site scan is the possession-transfer event (`PickedUp`); only after the goods are physically back are they reconciled to Inventory (`Stock.restock`) — never restocked before the courier actually has them
- RT6: a **complete** return (every shipped line returned) triggers `Coupon.unredeem(customerId, orderId)` — the customer regains the code; a **partial** return keeps the redemption burned
**Behavior:** `request(orderId, lines, reason)` → validates window + qty, `approve()` (creates + `Pickup.schedule`), `onPickupConfirmed()` (courier scan + evidence → `PickedUp` → `GoodsReceived`), `settle()` → computes pro-rata refund → Payment refund + Inventory restock + Order→Refunded (+ `Coupon.unredeem` if a complete return), `reject(reason)`
- The **customer does not ship back** — the platform's courier collects at the door. The `ReceiveReturnGoods` of a label-based model is replaced by the `Pickup` aggregate + `confirmPickup(evidence)`.

---

## 4. Entities vs Value Objects

**Entities** (identity + lifecycle; changes go through the aggregate root):
- `Cart`, `Order`, `Product`, `Stock`, `Payment`, `Shipment`, `Coupon`, `Return`, `Pickup`
- `CartLine`, `OrderLine`, `ShipmentLine`, `ReturnLine` (children)
- `Reservation` (child of Stock), `Refund` (child of Payment)

**Value Objects** (immutable, defined by attributes, safe to compare / copy):
- `Money` (amount, currency — **the one shared kernel**), `Sku`, `ProductId`, `Quantity`, `Address`, `CarrierInfo`, `TrackingNumber`, `UnitPrice`, `ShippingFee`, `ReservationId`, `OrderId`, `CustomerId`, `RmaId`, `PickupId`, `CouponCode`, `Discount` (the *result* of a coupon, Money + the rule that produced it), `DateRange`, `DatetimeWindow`, `ExpiryTimestamp`

**Rule of thumb applied:** if two instances with equal attributes are interchangeable → VO (`Money`, `Address`, `Price`, `Discount`). If deleting it loses history → Entity (`Payment`, `Return`, `Pickup`, `Reservation`). Note the deliberate splits: **Coupon** (entity — lifecycle + redemptions) vs **Discount** (VO — computed result), and **Return** (entity — the whole RMA) vs **Pickup** (entity — one doorstep collection step).

---

## 5. Domain Events & Commands

> **Command pattern (user-initiated intents):** every row below whose trigger
> has an external actor (customer, staff/courier, catalog admin) is modeled
> as an immutable `Command` in `domain/commands.py` (imperative name:
> `AddToCart`, `PlaceOrder`, `RequestReturn`) and dispatched via the
> `CommandBus` (`infra/command_bus.py`) to exactly one handler in
> `application/command_handlers.py`. Handlers load aggregates by ID inside one
> atomic UoW and emit the past-tense `Event`(s). System follow-ons (reserve →
> authorize → capture → restock → un-redeem, plus derived `OrderShipped` /
> `OrderDelivered` / `ReturnGoodsReceived`) stay purely event-driven — they
> have intentionally NO command because no external actor initiates them.

| Domain Event (past tense) | Triggering Command | Context of truth |
|---|---|---|
| `ProductPublished` | `PublishProduct` | Catalog |
| `CartCreated` | `OpenCart` | Shop |
| `CartItemAdded` / `CartItemChanged` / `CartLineRemoved` | `AddToCart` / `UpdateCartLine` / `RemoveCartLine` | Shop |
| `CouponApplied` / `CouponRemoved` | `ApplyCoupon` / `RemoveCoupon` (cart-level preview) | Shop |
| `CartCheckedOut` | `CheckoutCart` | Shop |
| `OrderPlaced` | `PlaceOrder` (carries cart snapshot + destination + validated discount + shipping) | Order Mgmt |
| `StockReserved` / `StockInsufficient` | `ReserveStock` | Inventory |
| `OrderPaymentAuthorized` | `AuthorizePayment` | Payment |
| `OrderPaid` | `ConfirmPayment` | Order Mgmt |
| `PaymentCaptured` / `PaymentVoided` / `PaymentRefunded` | `CapturePayment` (at shipment) / `VoidPayment` (cancel-before-capture) / `RefundPayment` | Payment |
| `ShipmentCreated` | `CreateShipment` | Fulfillment |
| `ShipmentShipped` | `HandToCarrier` | Fulfillment |
| `ShipmentDelivered` | `ConfirmDelivery` (carrier scan) | Fulfillment |
| `OrderShipped` / `OrderDelivered` | (derived from Shipment events) | Order Mgmt |
| `ReturnRequested` | `RequestReturn` (customer, against Delivered) | Returns |
| `ReturnApproved` | `ApproveReturn` | Returns |
| `PickupScheduled` | `SchedulePickup` (Fulfillment) | Fulfillment |
| `PickupConfirmed` (courier scan + evidence at customer's door) | `ConfirmPickup` (courier) | Fulfillment |
| `ReturnGoodsReceived` | (derived from `PickupConfirmed`) | Returns |
| `ReturnRefunded` | `SettleReturn` (pro-rata: goods + tax + shipping) | Returns |
| `CouponUnredeemed` | `UnredeemCoupon` (triggered by a full-refund `ReturnRefunded`) | Shop |
| `StockRestocked` | (from `ReturnRefunded`) | Inventory |
| `ReturnRejected` | `RejectReturn` | Returns |
| `OrderCancelled` / `StockReleased` / `PaymentVoided` | `CancelOrder` | Order Mgmt / Inventory / Payment |
| `OrderExpired` (reservation lapsed) | `ExpireOrder` | Order Mgmt / Inventory |
| `CartAbandoned` | `CloseCart` | Shop |

**Happy-path sequence (forward):**
```
OpenCart → AddToCart×N → ApplyCoupon → CheckoutCart
  → PlaceOrder → ReserveStock → AuthorizePayment
  → CreateShipment → HandToCarrier → PaymentCaptured
  → ShipmentDelivered → OrderDelivered
```

**Return path (doorstep pickup):**
```
[customer] RequestReturn → ApproveReturn → SchedulePickup
  → CourierEnRoute → ConfirmPickup (door, scan + photo) → ReturnGoodsReceived
  → SettleReturn (pro-rata: goods + tax + shipping)
     → PaymentRefunded + StockRestocked + Order→Refunded
     → (if complete return) CouponUnredeemed
```

**Refund / return rule (pro-rata, incl. shipping):**
```
originalLineSubtotal  = Σ (originalQty × originalUnitPrice)         # pre-discount, pre-tax, pre-shipping
originalDiscountShare = couponDiscount / originalLineSubtotal        # 0..1, 0 if no coupon

For a given return (returnedQty per line):
  returnedLineValue = Σ (returnedQty × originalUnitPrice)
  returnFraction    = returnedLineValue / originalLineSubtotal        # 0 < f ≤ 1;  f = 1 for a full return

  goodsRefund      = returnedLineValue × (1 − originalDiscountShare)  # net goods, pre-tax
  taxRefund        = goodsRefund × taxRate                           # proportional to the net goods
  shippingRefund   = shippingFee × returnFraction                    # proportional → full fee on a full return

  totalRefund      = goodsRefund + taxRefund + shippingRefund        # this amount hits Payment.refund
```
So a customer's **effective per-unit price stays identical to what they paid**: a 20%-off order returns 20% less, and the shipping fee scales with the share of the order's value that comes back (a full return refunds the full shipping fee; a half-return refunds half). Edge case: a free-shipping order has `shippingFee = 0`, so there's simply no shipping to refund — the rule holds without special-casing.

---

## 6. Assumptions / Open Questions

**Confirmed by your answers (all resolved):**
1. **Single seller** — one warehouse, no vendor dimension. ✓
2. **Customer-initiated returns** — the `Return` aggregate + Returns context; staff may *approve/reject*, but only a customer opens an RMA. ✓
3. **Payment: authorize-then-capture** — authorize at `PlaceOrder`, **capture when the carrier takes the parcel** (`HandToCarrier`); a cancel before shipment → `VoidPayment` (no money moved), and cancellation is impossible after shipment — only the return path settles the money. ✓
4. **Coupons** — the `Coupon` aggregate: **one per order** (single, confirmed), one redemption per customer, re-validated at placement, percentage capped at 100%. ✓
5. **Shipping fee** — a first-class, frozen order component; on returns it is **refunded pro-rata** with the share of line value returned (full fee on a full return). ✓
6. **Return channel** — **doorstep pickup** (confirmed): the platform's courier collects at the customer's door; the customer does *not* use a return label. The `Pickup` aggregate + `ConfirmPickup` (courier scan) is the possession-transfer event that gates restock. ✓

**Remaining assumptions I made** (flag before building — none are blocking):
- **Return window** = 30 days from delivery, configurable. (Assumed.)
- **Single carrier**, chosen at checkout; the platform models the hand-off, not carrier *routing*. (Assumed.)
- **No split shipments** — an Order ships as one Shipment. (If you later need split shipments, `Shipment` becomes a child collection under Order.)
- **No exchanges** — a return is refund-only; re-ordering is a fresh Order. (Assumed.)
- **Tax** = a pluggable line-total modifier in `Cart.quote()`; zero for now, rules TBD. (Assumed.)
- **Customer** is an opaque reference — profile/identity is a separate context, out of scope. (Assumed.)
- **Pickup no-access handling** — a missed doorstep (`NoAccess`) re-schedules up to N attempts before the Return is flagged for manual handling. (Assumed N = 2.)

**Final defaults applied (your "defaults" — v1 is locked):**
7. **Coupon re-entitlement** — a **complete** return (all lines back, order fully refunded) triggers `Coupon.unredeem`: the customer regains the code; a **partial** return keeps the redemption burned. (CO7, RT6, `CouponUnredeemed` event.)
8. **Doorstep evidence** — `Pickup.confirmPickup(evidence)` requires a **photo + optional signature** at the door; `PickupConfirmed` carries the evidence reference. (PK5 — the customer's proof of collection, since they hold no return label.)
9. **Pickup fee** — doorstep pickup is **free**; no pickup charge appears in the reconciliation or refund math.
