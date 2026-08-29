# Refactor Phase 5: External Adapters and Reliable Workers

Phase 5 isolates network integrations and durable background work from the
synchronous domain/application services.

## Delivered

- `ports.py` defines structural contracts for Outline, receipt storage,
  receipt extraction, and notification delivery.
- `outline_adapter.py` owns the pinned-certificate Outline Management API HTTP
  client. `app.OutlineClient` remains the same class through a compatibility
  export.
- Supabase Storage and the receipt extractor satisfy explicit adapter ports.
- `commerce_worker.py` owns durable provisioning, expiry, revocation, quota,
  retry, capacity, and notification-outbox operations.
- `CommerceService` inherits the worker boundary so all existing commands and
  tests retain the same method surface.
- `observability.py` centralizes opt-in, secret-safe adapter latency records.

## Reliability contract

Worker state is committed before or after an external call according to the
existing operation's recovery strategy. Jobs use persisted attempt counts,
retry timestamps, stale-lock recovery, deterministic or reconciled Outline key
lookup, and terminal failure visibility. Notifications use a durable outbox,
bounded retry delay, and dead-letter state. External responses remain
untrusted and are validated by their concrete adapters or service workflows.

This refactor does not introduce a second worker process; Render's existing
maintenance loop remains the scheduler and invokes the same public methods.
