# Refactor Phase 4: Paid Commerce Modules

Phase 4 turns the former paid-commerce monolith into explicit model,
persistence, and application-service modules.

## Delivered

- `commerce_models.py` owns immutable value objects, constants, identifiers,
  normalization, and Outline key naming.
- `commerce_repositories.py` owns SQLite and pooled PostgreSQL adapters and
  their schema bootstrap.
- `commerce_service.py` owns the paid-order, payment, receipt, wallet,
  subscription, job, and notification workflows.
- `commerce.py` is a small compatibility facade, preserving the original
  imports used by the app, tests, and deployment entry point.
- `CommerceService` now declares its dependency on the Phase 2 repository
  protocol.

## Invariants

Order and payment state transitions, transaction boundaries, idempotency keys,
job retry behavior, and encrypted access-URL handling were moved without
semantic changes. The frozen SQLite/PostgreSQL schema parity test remains the
database contract gate.
