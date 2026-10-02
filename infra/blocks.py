"""Python port of the AWS Blocks local-first philosophy.

Mapping (Block -> AWS service -> local implementation used here):

| Block            | AWS service (deploy)        | Local impl in this module          |
|------------------|-----------------------------|------------------------------------|
| KVStore          | DynamoDB                    | LocalKVStore (dict)                |
| DistributedTable | DynamoDB                    | LocalTable (dict + scan/query)     |
| FileBucket       | S3                          | LocalFileBucket (dict, opt. fs dir)|
| EmailClient      | SES                         | LocalEmailClient (captures sends)  |
| AppSetting       | SSM Parameter Store         | LocalAppSetting (dict + env)       |
| Logger           | CloudWatch Logs             | StdLogger (stdout + memory)        |
| Metrics          | CloudWatch                  | LocalMetrics (counters in memory)  |
| Tracer           | X-Ray                       | LocalTracer (spans in memory)      |
| AsyncJob         | SQS + Lambda                | LocalAsyncJob (in-memory queue)    |

Backend selection mirrors Blocks' conditional exports: ``BLOCKS_BACKEND``
env var = ``local`` (default, no AWS account needed) or ``aws`` (boto3
SDK calls, lazily imported so local testing has zero AWS dependencies).
The ``aws`` variants are thin stubs that fail with a clear message when
boto3/credentials are absent — same interface, no caller changes (LSP).
"""
from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field


def backend_name() -> str:
    """Select the Blocks backend (Blocks-style conditional loading).

    Returns:
        ``"local"`` (default, in-memory, no AWS account) or ``"aws"``
        (boto3-backed), from the ``BLOCKS_BACKEND`` env var (lowercased).
        Mirrors Blocks' conditional exports: same code, local or cloud.
    """
    return os.environ.get("BLOCKS_BACKEND", "local").lower()


# -- KVStore (DynamoDB key-value with conditional writes) ---------------------

class KVStore(abc.ABC):
    """Port: key-value storage with conditional writes (DynamoDB in AWS).

    Contract: ``get`` returns None (not KeyError) when missing; ``put``
    upserts; ``put_if_not_exists`` is the atomic conditional write used for
    idempotency guards (True = written, False = key already existed).
    """

    @abc.abstractmethod
    def get(self, key: str) -> str | None:
        """Fetch a value.

        Args:
            key: The storage key.

        Returns:
            The stored value, or None when the key is absent.
        """
        ...

    @abc.abstractmethod
    def put(self, key: str, value: str) -> None:
        """Upsert a value (unconditional write).

        Args:
            key: The storage key.
            value: The value to store (overwrites any existing entry).
        """
        ...

    @abc.abstractmethod
    def put_if_not_exists(self, key: str, value: str) -> bool:
        """Atomic conditional write (DynamoDB ConditionExpression in AWS).

        Args:
            key: The storage key.
            value: The value to store only when the key is absent.

        Returns:
            True if written, False if the key already existed.
        """

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Delete a key (idempotent -- missing keys are a no-op).

        Args:
            key: The storage key to remove.
        """
        ...


class LocalKVStore(KVStore):
    """Local (dev/test) KVStore adapter: plain dict, no AWS account needed.

    Testability: zero credentials, zero network; ``put_if_not_exists`` is a
    single-threaded dict check (mirrors the DynamoDB conditional contract
    without distributed atomicity claims).
    """

    def __init__(self) -> None:
        # In-memory table: key -> value (local stand-in for a DynamoDB table).
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        """Return the value or None when absent (never raises).

        Args:
            key: The storage key.

        Returns:
            The stored value, or None.
        """
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        """Unconditional upsert into the dict.

        Args:
            key: The storage key.
            value: The value to store.
        """
        self._data[key] = value

    def put_if_not_exists(self, key: str, value: str) -> bool:
        """Local conditional write (single-threaded equivalent).

        Args:
            key: The storage key.
            value: The value to store only when absent.

        Returns:
            True if written, False if the key already existed.
        """
        if key in self._data:
            return False
        self._data[key] = value
        return True

    def delete(self, key: str) -> None:
        """Idempotent delete (missing keys are a no-op).

        Args:
            key: The storage key to remove.
        """
        self._data.pop(key, None)


class DynamoKVStore(KVStore):
    """AWS backend for KVStore (DynamoDB). Lazily imports boto3.

    Same ``KVStore`` contract as ``LocalKVStore`` (LSP): callers in
    application/infra code never branch on backend. boto3 is imported lazily
    inside ``_tbl`` so local runs (``BLOCKS_BACKEND=local``) have zero AWS
    dependencies and import cleanly without boto3 installed.
    """

    def __init__(self, table_name: str) -> None:
        """Point at a DynamoDB table (no network I/O until first use).

        Args:
            table_name: The DynamoDB table name (``pk``/``v`` schema).
        """
        self._table_name = table_name
        # Lazily resolved boto3 Table resource (None until first _tbl() call).
        self._table = None

    def _tbl(self):
        """Resolve (and cache) the boto3 DynamoDB Table resource.

        Returns:
            The boto3 Table for ``self._table_name``.

        Raises:
            RuntimeError: If boto3 is not installed (clear local-dev message
                instead of an ImportError traceback).
        """
        try:
            # Lazy import: local runs never touch boto3, so it stays an
            # optional dependency (Blocks philosophy: no cloud SDK locally).
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError("DynamoKVStore needs boto3 (pip install boto3)") from e
        if self._table is None:
            self._table = boto3.resource("dynamodb").Table(self._table_name)
        return self._table

    def get(self, key: str) -> str | None:
        """Fetch one item by partition key.

        Args:
            key: The ``pk`` value.

        Returns:
            The ``v`` attribute, or None when the item is absent.
        """
        r = self._tbl().get_item(Key={"pk": key}).get("Item")
        return r["v"] if r else None

    def put(self, key: str, value: str) -> None:
        """Unconditional put (overwrites any existing item).

        Args:
            key: The ``pk`` value.
            value: Stored under the ``v`` attribute.
        """
        self._tbl().put_item(Item={"pk": key, "v": value})

    def put_if_not_exists(self, key: str, value: str) -> bool:
        """Atomic conditional write via ``attribute_not_exists(pk)``.

        Args:
            key: The ``pk`` value.
            value: Stored under ``v`` only when the item is absent.

        Returns:
            True if written, False on ``ConditionalCheckFailedException``.

        Raises:
            ClientError: Any DynamoDB error OTHER than the expected
                conditional-check failure is re-raised (real failures surface).
        """
        # Local botocore import: keeps module import boto-free for local runs.
        from botocore.exceptions import ClientError  # type: ignore

        try:
            self._tbl().put_item(
                Item={"pk": key, "v": value},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def delete(self, key: str) -> None:
        """Delete one item by partition key (idempotent in DynamoDB).

        Args:
            key: The ``pk`` value to delete.
        """
        self._tbl().delete_item(Key={"pk": key})


def make_kv_store(name: str = "default") -> KVStore:
    """Factory honouring BLOCKS_BACKEND (Blocks-style conditional loading).

    Args:
        name: Table name for the AWS backend; ignored locally (dict store).

    Returns:
        ``DynamoKVStore`` when ``BLOCKS_BACKEND=aws``, else ``LocalKVStore``.
        Callers program against the ``KVStore`` ABC either way (DIP/LSP).
    """
    if backend_name() == "aws":
        return DynamoKVStore(name)
    return LocalKVStore()


# -- DistributedTable (DynamoDB structured data + indexes) --------------------

class DistributedTable(abc.ABC):
    """Port: structured rows keyed by (pk, sk) (DynamoDB composite key in AWS).

    Contract: ``put`` upserts one row; ``get`` returns a defensive copy or
    None; ``query`` returns ALL rows for a partition key ordered by sort key
    (mirrors a DynamoDB Query on ``pk`` with ``ScanIndexForward=True``).
    """

    @abc.abstractmethod
    def put(self, pk: str, sk: str, item: dict) -> None:
        """Upsert one row.

        Args:
            pk: Partition key (e.g. order id for order-line rows).
            sk: Sort key (e.g. "line#1").
            item: Row payload (stored by value, not by reference).
        """
        ...

    @abc.abstractmethod
    def get(self, pk: str, sk: str) -> dict | None:
        """Fetch one row.

        Args:
            pk: Partition key.
            sk: Sort key.

        Returns:
            A copy of the row, or None when absent.
        """
        ...

    @abc.abstractmethod
    def query(self, pk: str) -> list[dict]:
        """All rows for a partition key, ordered by sort key.

        Args:
            pk: Partition key to query.

        Returns:
            Row copies sorted by ``sk`` (empty list when ``pk`` unknown).
        """


class LocalTable(DistributedTable):
    """Local DistributedTable adapter: nested dicts, no AWS needed.

    In-memory ``pk -> {sk -> row}`` map. ``put`` copies the item and
    ``get``/``query`` return copies, so callers can never mutate stored rows
    by aliasing (same value-semantics a DynamoDB port gets from
    serialization for free).
    """

    def __init__(self) -> None:
        # Outer key = partition key; inner key = sort key (DynamoDB shape).
        self._data: dict[str, dict[str, dict]] = {}

    def put(self, pk: str, sk: str, item: dict) -> None:
        """Store a defensive copy of ``item`` at ``(pk, sk)``.

        Args:
            pk: Partition key.
            sk: Sort key.
            item: Row payload (copied via ``dict(item)`` on write).
        """
        self._data.setdefault(pk, {})[sk] = dict(item)

    def get(self, pk: str, sk: str) -> dict | None:
        """Return a copy of the row, or None when ``(pk, sk)`` is unknown.

        Args:
            pk: Partition key.
            sk: Sort key.

        Returns:
            A defensive copy, or None.
        """
        row = self._data.get(pk, {}).get(sk)
        return dict(row) if row is not None else None

    def query(self, pk: str) -> list[dict]:
        """Return all rows for ``pk`` ordered by ``sk``.

        Args:
            pk: Partition key.

        Returns:
            Defensive row copies sorted by sort key (``sorted`` on the inner
            dict items -- the local equivalent of DynamoDB sort-key order).
        """
        return [dict(v) for _, v in sorted(self._data.get(pk, {}).items())]


# -- FileBucket (S3 file storage + presigned URLs) -----------------------------

class FileBucket(abc.ABC):
    """Port: blob storage (S3 in AWS; doorstep-pickup photo evidence lives here).

    Contract: ``put`` stores bytes and returns the reference callers keep as
    ``photo_ref``/``PickupEvidence``; ``get`` resolves the reference back to
    bytes or None. References are opaque strings (S3 key locally = S3 key).
    """

    @abc.abstractmethod
    def put(self, key: str, content: bytes) -> str:
        """Store bytes, return the reference callers keep (photo_ref).

        Args:
            key: Blob key (e.g. "photo-1.jpg").
            content: Raw bytes to store.

        Returns:
            The reference to persist on the aggregate (the key, locally).
        """

    @abc.abstractmethod
    def get(self, ref: str) -> bytes | None:
        """Resolve a reference back to bytes.

        Args:
            ref: Reference previously returned by :meth:`put`.

        Returns:
            A copy of the stored bytes, or None when unknown.
        """
        ...


class LocalFileBucket(FileBucket):
    """Local FileBucket adapter: in-memory (no S3, no credentials).

    Copies bytes on both write and read so callers cannot mutate stored blobs
    through ``bytearray`` aliasing -- same value-semantics S3 gives via the
    network. An ``fs dir`` backing could be added later without changing the
    contract (OCP).
    """

    def __init__(self) -> None:
        # Key -> immutable bytes snapshot (local stand-in for an S3 bucket).
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, content: bytes) -> str:
        """Store a bytes-copy under ``key`` and return the key as reference.

        Args:
            key: Blob key.
            content: Bytes to store (copied via ``bytes(...)``).

        Returns:
            ``key`` -- the reference callers persist as photo evidence.
        """
        self._blobs[key] = bytes(content)
        return key

    def get(self, ref: str) -> bytes | None:
        """Return a copy of the blob, or None when the ref is unknown.

        Args:
            ref: Blob reference from :meth:`put`.

        Returns:
            A fresh ``bytes`` copy, or None.
        """
        blob = self._blobs.get(ref)
        return bytes(blob) if blob is not None else None


# -- EmailClient (SES transactional email) ------------------------------------

@dataclass
class Email:
    """Value object: one outbound email (immutable record of an SES send).

    Kept as a dataclass (not a raw tuple) so tests can assert on
    ``.to``/``.subject``/``.body`` with named access, and a future SES port
    can extend it (message-id) without changing call sites.
    """

    to: str
    subject: str
    body: str


class EmailClient(abc.ABC):
    """Port: transactional email (SES in AWS; Notification context sender).

    Contract: ``send`` is fire-and-forget (no return, raises only on real
    delivery failure); ``sent`` exposes the send log for test assertions.
    """

    @abc.abstractmethod
    def send(self, to: str, subject: str, body: str) -> None:
        """Send one transactional email.

        Args:
            to: Recipient address.
            subject: Email subject (e.g. "Order confirmed").
            body: Plain-text body.
        """
        ...

    @property
    @abc.abstractmethod
    def sent(self) -> list[Email]:
        """Outbox visible to tests (local) / send log.

        Returns:
            A defensive copy of sent emails, oldest first (spy-friendly:
            assert subjects without mocks or a real SES identity).
        """


class LocalEmailClient(EmailClient):
    """Local EmailClient adapter: captures sends in memory, never calls SES.

    The Notification context's test double AND local-dev outbox: ``send``
    appends an ``Email`` value object; ``sent`` returns copies so tests
    cannot corrupt the captured log.
    """

    def __init__(self) -> None:
        # Append-only capture log (local stand-in for the SES send history).
        self._sent: list[Email] = []

    def send(self, to: str, subject: str, body: str) -> None:
        """Capture (not deliver) one email.

        Args:
            to: Recipient address.
            subject: Email subject.
            body: Plain-text body.
        """
        self._sent.append(Email(to, subject, body))

    @property
    def sent(self) -> list[Email]:
        """Captured emails, oldest first (defensive copy).

        Returns:
            A shallow list copy (the ``Email`` values themselves are
            treated as immutable records).
        """
        return list(self._sent)


# -- AppSetting (SSM Parameter Store config/secrets) ---------------------------

class AppSetting(abc.ABC):
    """Port: configuration values and secrets (SSM Parameter Store in AWS).

    Contract: ``get`` never raises for missing keys -- it falls back to
    ``default`` -- so application code stays free of ``os.environ`` checks.
    """

    @abc.abstractmethod
    def get(self, name: str, default: str = "") -> str:
        """Read one setting.

        Args:
            name: Setting name (SSM parameter name / env var, e.g.
                "RETURN_WINDOW_DAYS").
            default: Fallback when the setting is absent.

        Returns:
            The configured value, or ``default``.
        """
        ...


class LocalAppSetting(AppSetting):
    """Local AppSetting adapter: explicit dict first, then env vars, default.

    Precedence (highest wins): constructor ``values`` dict > process env >
    ``default``. Lets ``build_container(settings_values={...})`` override per
    test (e.g. return-window days) without mutating global env state.
    """

    def __init__(self, values: dict[str, str] | None = None) -> None:
        """Seed explicit overrides (copied, so caller mutation is safe).

        Args:
            values: Explicit setting overrides (highest precedence).
        """
        self._values = dict(values or {})

    def get(self, name: str, default: str = "") -> str:
        """Resolve with dict > env > default precedence.

        Args:
            name: Setting name.
            default: Fallback when neither dict nor env provides it.

        Returns:
            The resolved string value.
        """
        return self._values.get(name, os.environ.get(name, default))


# -- Observability (Logger / Metrics / Tracer) ---------------------------------

class Logger(abc.ABC):
    """Port: structured logging with correlation ids (CloudWatch Logs in AWS).

    Contract: ``info``/``error`` accept a message plus arbitrary structured
    fields (e.g. ``event="OrderPlaced"``); ``records`` exposes the log for
    test assertions without scraping stdout.
    """

    @abc.abstractmethod
    def info(self, msg: str, **fields) -> None:
        """Log an informational line with structured fields.

        Args:
            msg: Human-readable message.
            **fields: Structured context (correlation ids, event names).
        """
        ...

    @abc.abstractmethod
    def error(self, msg: str, **fields) -> None:
        """Log an error line with structured fields.

        Args:
            msg: Human-readable message.
            **fields: Structured context (error detail, ids).
        """
        ...

    @property
    @abc.abstractmethod
    def records(self) -> list[dict]:
        """Log records visible to tests.

        Returns:
            Defensive copy of ``{"level", "msg", **fields}`` dicts, oldest
            first (spy-friendly: assert ``r["event"] == "OrderPlaced"``).
        """


class StdLogger(Logger):
    """Local Logger adapter: stdout + in-memory records (CloudWatch locally).

    ``quiet=True`` (default) keeps test output clean while still recording;
    ``quiet=False`` also echoes to stdout for local debugging.
    """

    def __init__(self, quiet: bool = True) -> None:
        """Create a logger with an empty in-memory record.

        Args:
            quiet: When True (default) suppress stdout echo (tests stay
                clean); records are always kept regardless.
        """
        # Append-only record (spy-friendly); echo is a side channel only.
        self._records: list[dict] = []
        self._quiet = quiet

    def _emit(self, level: str, msg: str, fields: dict) -> None:
        """Append one record and optionally echo to stdout.

        Args:
            level: "INFO" or "ERROR".
            msg: Human-readable message.
            fields: Structured context dict.
        """
        self._records.append({"level": level, "msg": msg, **fields})
        if not self._quiet:
            print(f"[{level}] {msg} {fields}")

    def info(self, msg: str, **fields) -> None:
        """Record an INFO line.

        Args:
            msg: Message. **fields: Structured context.
        """
        self._emit("INFO", msg, fields)

    def error(self, msg: str, **fields) -> None:
        """Record an ERROR line.

        Args:
            msg: Message. **fields: Structured context.
        """
        self._emit("ERROR", msg, fields)

    @property
    def records(self) -> list[dict]:
        """Recorded log lines, oldest first (defensive copy).

        Returns:
            List of ``{"level", "msg", **fields}`` dicts.
        """
        return list(self._records)


class Metrics(abc.ABC):
    """Port: custom application metrics (CloudWatch in AWS).

    Contract: ``increment`` accumulates counters (default +1);
    ``count`` reads them (0 when never incremented -- never raises).
    """

    @abc.abstractmethod
    def increment(self, name: str, value: int = 1) -> None:
        """Add to a counter (e.g. ``events.OrderPlaced``).

        Args:
            name: Metric name.
            value: Amount to add (default 1).
        """
        ...

    @abc.abstractmethod
    def count(self, name: str) -> int:
        """Read a counter.

        Args:
            name: Metric name.

        Returns:
            Accumulated total, or 0 when never incremented.
        """
        ...


class LocalMetrics(Metrics):
    """Local Metrics adapter: counters in memory (CloudWatch locally).

    Spy-friendly: ``count("events.total")`` asserts the Notification fan-out
    fired without scraping logs or hitting CloudWatch.
    """

    def __init__(self) -> None:
        # Metric name -> accumulated total (starts empty; missing reads as 0).
        self._counters: dict[str, int] = {}

    def increment(self, name: str, value: int = 1) -> None:
        """Accumulate ``value`` into ``name``.

        Args:
            name: Metric name.
            value: Amount to add.
        """
        self._counters[name] = self._counters.get(name, 0) + value

    def count(self, name: str) -> int:
        """Read the accumulated total (0 when never incremented).

        Args:
            name: Metric name.

        Returns:
            The counter value.
        """
        return self._counters.get(name, 0)


@dataclass
class Span:
    """One traced unit of work (X-Ray segment/span in AWS).

    Recorded by ``LocalTracer.trace``; tests assert ``tracer.spans`` is
    non-empty to prove services traced their UoW without touching X-Ray.
    """

    name: str
    fields: dict = field(default_factory=dict)


class LocalTracer:
    """Local Tracer adapter: spans in memory; doubles as a test spy (X-Ray locally)."""

    def __init__(self) -> None:
        # Public on purpose: tests read ``tracer.spans`` directly as a spy
        # (no accessor needed for an in-memory test double).
        self.spans: list[Span] = []

    def trace(self, name: str, **fields) -> Span:
        """Record one span and return it (callers may enrich ``.fields``).

        Args:
            name: Span name (e.g. service/UoW operation).
            **fields: Structured span attributes (copied, not aliased).

        Returns:
            The recorded ``Span`` (also appended to ``self.spans``).
        """
        span = Span(name, dict(fields))
        self.spans.append(span)
        return span


# -- AsyncJob (SQS + Lambda fire-and-forget background work) -------------------

class AsyncJob(abc.ABC):
    """Port: fire-and-forget background work (SQS + Lambda in AWS).

    Contract: ``enqueue`` buffers one job; ``pending`` exposes the backlog
    for tests (a local worker would drain it; in AWS, Lambda polling does).
    """

    @abc.abstractmethod
    def enqueue(self, job_name: str, payload: dict) -> None:
        """Buffer one background job.

        Args:
            job_name: Job type (e.g. "order.placed").
            payload: JSON-able payload (merged with ``{"job": job_name}``).
        """
        ...

    @abc.abstractmethod
    def pending(self) -> list[dict]:
        """Jobs not yet processed (visible to tests).

        Returns:
            Defensive copy of buffered ``{"job": ..., **payload}`` dicts.
        """


class LocalAsyncJob(AsyncJob):
    """Local AsyncJob adapter: in-memory queue drained synchronously in tests.

    ``enqueue`` appends ``{"job": name, **payload}``; ``drain`` pops ALL jobs
    atomically (swap-then-return, like the UoW commit) so a test worker sees
    each job exactly once.
    """

    def __init__(self) -> None:
        # FIFO backlog (local stand-in for the SQS queue depth).
        self._queue: list[dict] = []

    def enqueue(self, job_name: str, payload: dict) -> None:
        """Buffer one job at the tail.

        Args:
            job_name: Job type.
            payload: Payload merged under ``{"job": job_name, **payload}``.
        """
        self._queue.append({"job": job_name, **payload})

    def pending(self) -> list[dict]:
        """Buffered jobs in enqueue order (defensive copy).

        Returns:
            List of job dicts (mutating it never affects the queue).
        """
        return list(self._queue)

    def drain(self) -> list[dict]:
        """Pop all jobs (a test worker would process them here).

        Returns:
            All buffered jobs in order; the queue is empty afterwards.
            Swap-then-return makes the drain atomic against concurrent
            enqueues during processing.
        """
        jobs, self._queue = self._queue, []
        return jobs
