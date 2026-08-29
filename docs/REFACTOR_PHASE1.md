# Refactor Phase 1 — Persistence Seam and Module Rules

Phase 1 introduces a shared persistence seam without moving business workflows.

## Implemented boundary

`persistence.py` owns SQLite connection construction and lifecycle:

```text
app.Database -------------> persistence.open_sqlite_connection
commerce.CommerceDatabase -> persistence.open_sqlite_connection
PostgresCommerceDatabase -> psycopg pool (unchanged)
```

`Database.connect()` and `CommerceDatabase.connect()` remain compatible public APIs:
they return `sqlite3.Connection` instances with the same row factory, foreign-key
enforcement, timeout, and commerce busy-timeout settings. The shared connection type
now closes on context-manager exit after SQLite commits or rolls back the transaction.

## Dependency rules

1. `persistence.py` may depend only on the Python standard library.
2. Extracted domain modules may depend on persistence helpers, never on Telegram,
   Render, or Supabase transport code. The existing `commerce.py` storage import is a
   documented legacy dependency for the later adapter-extraction phase.
3. Transport modules may call domain services but cannot issue business SQL directly.
4. The PostgreSQL pool remains a separate hosted adapter until Phase 2 establishes
   numbered migrations and repository interfaces.
5. New extraction work keeps compatibility facades in the existing modules until all
   callers and tests are migrated in one reviewed phase.

## Proven invariants

- Context-managed SQLite writes commit on success and roll back on exceptions.
- Handles close deterministically after either outcome.
- SQLite foreign-key behavior and Commerce's 30-second busy timeout are retained.
- The PostgreSQL adapter is unchanged.
- The complete test suite passes with `ResourceWarning` promoted to an error.

Phase 1 deliberately does not alter schema, SQL, entitlement logic, commerce state,
Telegram routing, worker behavior, or external service calls.
