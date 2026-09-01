# Refactor Phase 3: Free and Trial Entitlements

Phase 3 extracts the free/trial domain from the Telegram transport while
preserving every legacy import from `app.py`.

## Delivered

- `entitlements.py` owns claim policy, quota warnings, expiry, remote
  termination reconciliation, and free/trial usage projections.
- `free_repository.py` owns the SQLite schema and operational persistence used
  by those entitlements.
- The service depends on the Phase 2 repository protocol rather than the
  concrete SQLite class.
- `app.py` remains a compatibility facade for `ClaimService`, `ClaimResult`,
  `Database`, constants, and `OutlineError`, so existing deployments and tests
  do not need an import migration.
- Boundary tests pin those compatibility identities.

## Dependency direction

`app` composes the Telegram transport, `entitlements` contains policy, and
`free_repository` implements persistence. Neither extracted module imports the
Telegram application.
