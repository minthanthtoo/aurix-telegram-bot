# Refactor Phase 7: Runtime Composition Facade

Phase 7 makes the executable application module a compatibility facade.

## Delivered

- `runtime_composition.py` owns validated settings, injectable factories,
  database/storage/Outline composition, service construction, and readiness
  reconciliation.
- `runtime.py` owns Telegram `getMe`, webhook convergence, signal handling,
  polling lifecycle, and hosted-connection shutdown.
- `app.py` is now a small executable facade that preserves the historical
  imports and `main()` entrypoint for deployments, tests, and scripts.
- Runtime composition tests exercise required-setting failure and a complete
  dependency-injected startup path without touching Telegram, Outline,
  Supabase, or a real database.

## Dependency direction

`app` -> `runtime` -> `runtime_composition` -> adapters/transports -> domain
services -> repositories. Neither runtime module imports `app`; this removes the last reverse
dependency risk while retaining `python -u app.py` as the deployment command.
