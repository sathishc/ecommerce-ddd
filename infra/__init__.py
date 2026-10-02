"""Infrastructure layer: technical adapters behind domain/application ports.

Local-first Blocks philosophy (same code, local or AWS -- see
``infra/README.md`` and ``infra/blocks.py``):

- ``blocks``: Block ports (KVStore/Table/S3/SES/SSM/CloudWatch/X-Ray/SQS)
  with local in-memory adapters selected by ``BLOCKS_BACKEND=local|aws``.
- ``clock``: ``Clock`` port (``SystemClock`` prod / ``FixedClock`` tests).
- ``repositories``: per-aggregate Repository ABCs + in-memory impls (DIP/ISP).
- ``unit_of_work``: ``UnitOfWork`` ABC + ``InMemoryUnitOfWork``
  (snapshot/rollback + transactional outbox -- publishes only on commit).
- ``event_bus``/``command_bus``: sync pub/sub facts bus + point-to-point
  intent bus (deterministic, spy-friendly, no mocks).
- ``handlers``: ``NotificationService`` wildcard subscriber (SES/Logs/Metrics).
- ``container``: composition root (``build_container``/``build_test_container``).

Dependency rule: ``infra`` adapts ``application``/``domain`` -- domain never
imports this package.
"""