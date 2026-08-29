# Refactor Phase 2: Persistence Contracts and Migration History

Phase 2 establishes explicit persistence boundaries without changing the
application's public imports or business behavior.

## Delivered

- `RepositoryDatabase` and `HostedRepositoryDatabase` structural protocols
  define the transaction lifecycle required by services.
- `schema_migrations` records immutable, component-scoped migration history.
- Existing free-access and commerce schemas are adopted as version 1.
- SQLite and PostgreSQL initializers use the same numbered migration registry.
- Startup rejects renamed, duplicate, non-positive, or unknown migration
  versions instead of silently accepting incompatible schema history.
- Schema parity fingerprints and migration regression tests cover the new
  metadata table.

## Compatibility rule

The legacy initializers remain the version-1 bootstrap during the refactor.
All schema changes after this phase must be represented by a new immutable
entry in `migrations.py`; an applied migration's version or name must never be
edited.

The caller owns the surrounding transaction. Migration statements must be
idempotent so concurrent application startup can safely converge on the same
recorded version.
