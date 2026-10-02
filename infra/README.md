# Ecommerce domain on AWS Blocks (local-first) — analysis + implementation

## 1. Code analysis (what was already good, what was missing)

**Good:** `domain/` is a clean DDD model — aggregates own their invariants
(C1..CO7, O1..O5, PM1..PM4, PK1..PK5, SH1..SH4, S1..S4, RT1..RT6), value
objects are immutable, every mutation emits past-tense domain events, and
`orchestration.py` keeps cross-context ACLs explicit. 52 tests lock this in.

**Gaps this change closes:**
1. No persistence abstraction — services took raw `dict[str, Stock]`; you
   could not swap in DynamoDB without rewriting them (DIP violation).
2. No atomicity — `CheckoutService` hand-rolled compensation; a crash between
   `reserve` and `pay` left partial state (no Unit-of-Work).
3. `Outbox.publish` was fire-and-forget with no subscriber model — the
   Notification context had no code (no event-driven architecture).
4. `datetime.now()` hard-coded in `Pickup` made 3 tests time-bombed (they
   failed once wall-clock passed the hardcoded `NOW`).

## 2. What was added (Blocks philosophy: same code, local or AWS)

```
infra/blocks.py        Python port of AWS Blocks, local-first
  KVStore -> DynamoDB           LocalKVStore (dict) / DynamoKVStore (boto3, lazy)
  DistributedTable -> DynamoDB  LocalTable / (same conditional pattern)
  FileBucket -> S3              LocalFileBucket (pickup photo evidence)
  EmailClient -> SES            LocalEmailClient (captures, Notification ctx)
  AppSetting -> SSM params      LocalAppSetting (dict + env)
  Logger/Metrics/Tracer         StdLogger / LocalMetrics / LocalTracer
  AsyncJob -> SQS+Lambda        LocalAsyncJob (in-memory queue)
  Backend chosen by BLOCKS_BACKEND=local|aws, like Blocks' conditional exports.
infra/clock.py         Clock ABC (SystemClock / FixedClock) — fixes the time bomb
infra/event_bus.py     EventBus: sync pub/sub, records stream, spy-friendly
infra/repositories.py  Repository ABCs per aggregate + InMemory impls (DIP/ISP)
infra/unit_of_work.py  UnitOfWork ABC + InMemoryUoW: snapshot/rollback + outbox
infra/handlers.py      NotificationService: subscribes to ALL events (pub/sub)
infra/container.py     Composition root: build_container() / build_test_container()
application/services.py Checkout/Shipping/Cancel/Return services on UoW + repos
tests/test_infra_blocks.py  17 tests: Blocks local, DI, UoW atomicity, events, e2e
```

SOLID: services depend on `UoWFactory`/`Clock`/Block ABCs (DIP), repos are
narrow per-aggregate interfaces (ISP) with one swappable impl (OCP/LSP), each
module has one reason to change (SRP). Events publish only on UoW commit
(transactional outbox); tests use `FixedClock` + in-memory Blocks + handler
spies — no mocks, no AWS account.

Real TS reference: [@aws-blocks/blocks v0.6.0](https://github.com/aws-devtools-labs/aws-blocks)
(`bb-kv-store`, `bb-async-job`, `bb-email-client`, `bb-file-bucket`, …) —
the table in `infra/blocks.py` maps each Block to its AWS service and local port.

## 3. Local testing

```bash
python3 -m pytest -q          # 79 passed: 62 domain + 17 infra/blocks
BLOCKS_BACKEND=local python3 -m pytest tests/test_infra_blocks.py -q
```

## 4. Usage

```python
from infra.container import build_container
c = build_container()  # local; build_container(backend="aws") deploys same code
order, payment = c.checkout.place_and_pay(cart, "card-1")
shipment = c.shipping.ship(order, "UPS", "TRK-1")
rma = c.returns_svc.request_return(order, [(pid, 1)], "broken", c.clock.now())
```

## 5. Fixes to existing code

- `domain/pickup.py`: `__init__`/`reschedule` accept injectable `now`
  (default = wall clock; backward compatible).
- `tests/test_aggregates.py`: `NOW = datetime.now()` (was a hardcoded 2026-09-28
  that expired — the 3 `TestPickup` failures).
