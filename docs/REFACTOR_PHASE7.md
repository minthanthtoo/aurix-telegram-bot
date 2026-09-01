# Refactor Phase 7: Runtime Composition Facade

Phase 7 makes the executable application module a compatibility facade.

## Delivered

- `runtime.py` owns environment reads, startup validation, Telegram `getMe`,
  database/storage/Outline composition, readiness checks, webhook convergence,
  signal handling, and hosted-connection shutdown.
- `app.py` is now a small executable facade that preserves the historical
  imports and `main()` entrypoint for deployments, tests, and scripts.
- Runtime composition tests exercise required-setting failure and a complete
  dependency-injected startup path without touching Telegram, Outline,
  Supabase, or a real database.

## Dependency direction

`app` -> `runtime` -> adapters/transports -> domain services -> repositories.
The runtime module does not import `app`; this removes the last reverse
dependency risk while retaining `python -u app.py` as the deployment command.
