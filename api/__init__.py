"""HTTP + CQRS API over the DDD backend (stdlib only, no third-party deps).

CQRS layout
-----------
* **Write side (commands):** ``POST /api/commands`` with ``{"type": ..., "payload": ...}``.
  Each ``type`` is a ``domain.commands`` intent dispatched on the container's
  ``CommandBus`` to exactly one ``application.command_handlers`` method.
  Writes mutate aggregates inside one atomic UoW and return the resulting DTO.
* **Read side (queries):** ``GET /api/<resource>[/<id>]`` served by ``api.queries``
  straight from the in-memory repositories. Reads never mutate, never open a UoW.

Run::

    python3 -m api.server --port 8000 --seed
"""
