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
    """Which backend to use: ``local`` (default) or ``aws``."""
    return os.environ.get("BLOCKS_BACKEND", "local").lower()


# -- KVStore (DynamoDB key-value with conditional writes) ---------------------

class KVStore(abc.ABC):
    """Interface: key-value storage with conditional writes."""

    @abc.abstractmethod
    def get(self, key: str) -> str | None: ...

    @abc.abstractmethod
    def put(self, key: str, value: str) -> None: ...

    @abc.abstractmethod
    def put_if_not_exists(self, key: str, value: str) -> bool:
        """Conditional write. Returns True if written, False if key existed."""

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...


class LocalKVStore(KVStore):
    """Local (dev/test) KVStore: plain dict, no AWS account needed."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        self._data[key] = value

    def put_if_not_exists(self, key: str, value: str) -> bool:
        if key in self._data:
            return False
        self._data[key] = value
        return True

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class DynamoKVStore(KVStore):
    """AWS backend for KVStore (DynamoDB). Lazily imports boto3."""

    def __init__(self, table_name: str) -> None:
        self._table_name = table_name
        self._table = None

    def _tbl(self):
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError("DynamoKVStore needs boto3 (pip install boto3)") from e
        if self._table is None:
            self._table = boto3.resource("dynamodb").Table(self._table_name)
        return self._table

    def get(self, key: str) -> str | None:
        r = self._tbl().get_item(Key={"pk": key}).get("Item")
        return r["v"] if r else None

    def put(self, key: str, value: str) -> None:
        self._tbl().put_item(Item={"pk": key, "v": value})

    def put_if_not_exists(self, key: str, value: str) -> bool:
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
        self._tbl().delete_item(Key={"pk": key})


def make_kv_store(name: str = "default") -> KVStore:
    """Factory honouring BLOCKS_BACKEND (Blocks-style conditional loading)."""
    if backend_name() == "aws":
        return DynamoKVStore(name)
    return LocalKVStore()


# -- DistributedTable (DynamoDB structured data + indexes) --------------------

class DistributedTable(abc.ABC):
    """Interface: structured rows keyed by (pk, sk)."""

    @abc.abstractmethod
    def put(self, pk: str, sk: str, item: dict) -> None: ...

    @abc.abstractmethod
    def get(self, pk: str, sk: str) -> dict | None: ...

    @abc.abstractmethod
    def query(self, pk: str) -> list[dict]:
        """All rows for a partition key, ordered by sort key."""


class LocalTable(DistributedTable):
    """Local DistributedTable: nested dicts, no AWS needed."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}

    def put(self, pk: str, sk: str, item: dict) -> None:
        self._data.setdefault(pk, {})[sk] = dict(item)

    def get(self, pk: str, sk: str) -> dict | None:
        row = self._data.get(pk, {}).get(sk)
        return dict(row) if row is not None else None

    def query(self, pk: str) -> list[dict]:
        return [dict(v) for _, v in sorted(self._data.get(pk, {}).items())]


# -- FileBucket (S3 file storage + presigned URLs) -----------------------------

class FileBucket(abc.ABC):
    """Interface: blob storage (doorstep-pickup photo evidence lives here)."""

    @abc.abstractmethod
    def put(self, key: str, content: bytes) -> str:
        """Store bytes, return the reference callers keep (photo_ref)."""

    @abc.abstractmethod
    def get(self, ref: str) -> bytes | None: ...


class LocalFileBucket(FileBucket):
    """Local FileBucket: in-memory (no S3, no credentials)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, content: bytes) -> str:
        self._blobs[key] = bytes(content)
        return key

    def get(self, ref: str) -> bytes | None:
        blob = self._blobs.get(ref)
        return bytes(blob) if blob is not None else None


# -- EmailClient (SES transactional email) ------------------------------------

@dataclass
class Email:
    """Value object: one outbound email."""

    to: str
    subject: str
    body: str


class EmailClient(abc.ABC):
    """Interface: transactional email (Notification context)."""

    @abc.abstractmethod
    def send(self, to: str, subject: str, body: str) -> None: ...

    @property
    @abc.abstractmethod
    def sent(self) -> list[Email]:
        """Outbox visible to tests (local) / send log."""


class LocalEmailClient(EmailClient):
    """Local EmailClient: captures sends in memory instead of calling SES."""

    def __init__(self) -> None:
        self._sent: list[Email] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self._sent.append(Email(to, subject, body))

    @property
    def sent(self) -> list[Email]:
        return list(self._sent)


# -- AppSetting (SSM Parameter Store config/secrets) ---------------------------

class AppSetting(abc.ABC):
    """Interface: configuration values and secrets."""

    @abc.abstractmethod
    def get(self, name: str, default: str = "") -> str: ...


class LocalAppSetting(AppSetting):
    """Local AppSetting: explicit dict first, then env vars, then default."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str, default: str = "") -> str:
        return self._values.get(name, os.environ.get(name, default))


# -- Observability (Logger / Metrics / Tracer) ---------------------------------

class Logger(abc.ABC):
    """Interface: structured logging with correlation ids (CloudWatch Logs)."""

    @abc.abstractmethod
    def info(self, msg: str, **fields) -> None: ...

    @abc.abstractmethod
    def error(self, msg: str, **fields) -> None: ...

    @property
    @abc.abstractmethod
    def records(self) -> list[dict]:
        """Log records visible to tests."""


class StdLogger(Logger):
    """Local Logger: stdout + in-memory records."""

    def __init__(self, quiet: bool = True) -> None:
        self._records: list[dict] = []
        self._quiet = quiet

    def _emit(self, level: str, msg: str, fields: dict) -> None:
        self._records.append({"level": level, "msg": msg, **fields})
        if not self._quiet:
            print(f"[{level}] {msg} {fields}")

    def info(self, msg: str, **fields) -> None:
        self._emit("INFO", msg, fields)

    def error(self, msg: str, **fields) -> None:
        self._emit("ERROR", msg, fields)

    @property
    def records(self) -> list[dict]:
        return list(self._records)


class Metrics(abc.ABC):
    """Interface: custom application metrics (CloudWatch)."""

    @abc.abstractmethod
    def increment(self, name: str, value: int = 1) -> None: ...

    @abc.abstractmethod
    def count(self, name: str) -> int: ...


class LocalMetrics(Metrics):
    """Local Metrics: counters in memory."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def increment(self, name: str, value: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + value

    def count(self, name: str) -> int:
        return self._counters.get(name, 0)


@dataclass
class Span:
    """One traced unit of work (X-Ray span in AWS)."""

    name: str
    fields: dict = field(default_factory=dict)


class LocalTracer:
    """Local Tracer: spans in memory; doubles as a test spy."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def trace(self, name: str, **fields) -> Span:
        span = Span(name, dict(fields))
        self.spans.append(span)
        return span


# -- AsyncJob (SQS + Lambda fire-and-forget background work) -------------------

class AsyncJob(abc.ABC):
    """Interface: fire-and-forget background work."""

    @abc.abstractmethod
    def enqueue(self, job_name: str, payload: dict) -> None: ...

    @abc.abstractmethod
    def pending(self) -> list[dict]:
        """Jobs not yet processed (visible to tests)."""


class LocalAsyncJob(AsyncJob):
    """Local AsyncJob: in-memory queue drained synchronously in tests."""

    def __init__(self) -> None:
        self._queue: list[dict] = []

    def enqueue(self, job_name: str, payload: dict) -> None:
        self._queue.append({"job": job_name, **payload})

    def pending(self) -> list[dict]:
        return list(self._queue)

    def drain(self) -> list[dict]:
        """Pop all jobs (a test worker would process them here)."""
        jobs, self._queue = self._queue, []
        return jobs
