"""Application layer: orchestration across bounded contexts in one atomic UoW.

Role in the DDD layering (``domain`` <- ``application`` <- ``infra``):

- ``domain/`` owns invariants and emits past-tense domain events; it never
  does I/O and never knows about repositories, buses, or transactions.
- ``application/`` is the *only* place allowed to cross bounded-context
  boundaries (the context-map ACLs from ``domain-model.md`` §2 / README).
  It loads aggregates via ``infra.unit_of_work`` repositories, invokes
  domain behavior, persists, and drains events into the UoW outbox.
- ``infra/`` supplies the technical adapters (repositories, UoW,
  CommandBus/EventBus, Clock, Blocks) and the composition root.

Atomicity contract (see ``infra/unit_of_work.py``):

- Every public application call runs inside exactly one
  ``with uow_factory() as uow:`` block. ``__enter__`` snapshots all
  repositories; ``__exit__`` commits (publishes the collected outbox via
  the EventBus) on success or restores snapshots + drops the outbox on
  any exception. So a failed checkout never leaves a dangling stock
  reservation, coupon redemption, or half-published event.
- ``uow.collect(*aggregates)`` drains each aggregate's pending events
  into the commit outbox; events reach subscribers (e.g.
  NotificationService) only after the whole multi-context mutation
  commits — the transactional-outbox guarantee.

Contents:

- ``application.services``: aggregate-level API (callers pass loaded
  aggregates: ``CheckoutAppService``, ``ShippingAppService``,
  ``CancelAppService``, ``ReturnAppService``).
- ``application.command_handlers``: ID-based front door for the
  ``CommandBus`` (callers pass immutable ``domain.commands`` intents
  carrying IDs; handlers do the ``uow.<repos>.get`` loading themselves).
  Both front doors share identical UoW semantics and event flow.
"""
