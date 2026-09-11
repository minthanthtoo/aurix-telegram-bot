# AuriX AI package

This package is the canonical implementation of the AuriX AI gateway. It is
intentionally independent from the VPN and commerce modules.

```text
aurix_ai/router.py    model catalog, mode policy, context window, 9Router client
aurix_ai/api_keys.py  AI accounts, bearer-key auth, usage ledger, migrations
aurix_ai/conversations.py durable owner-scoped conversations, turns, and attempts
aurix_ai/web_api.py   HTTP routes, Telegram session auth, external API boundary
aurix_ai/__main__.py  package entrypoint
```

Shared infrastructure is kept explicit at repository level:

```text
telegram_web_app.py   Telegram signature verification shared by web products
migrations.py         component-scoped migration runner
persistence.py        SQLite connection helper
web/ai-app/           AI browser assets
```

Run locally or in the container with:

```sh
python -m aurix_ai
```

The browser conversation routes use the configured AI database when the API-key
store is enabled. If no API-key database is configured, they use the durable
session database instead. Submitted turns are persisted before inference;
duplicate client submission IDs are idempotent, and process restarts mark
in-flight attempts as interrupted rather than silently resending them. The
conversation database is component-scoped and does not modify VPN/commerce
tables. `POST /api/conversations/{id}/turns` returns a durable attempt before
generation completes; `GET /api/conversations/{id}/attempts/{id}/events` is a
reconnectable SSE view of partial output, and the cancel endpoint makes a
running attempt terminal without allowing a late provider result to overwrite
it. The bounded worker is process-local by design; startup marks abandoned
attempts as interrupted instead of resending them.
`POST /api/conversations/{id}/attempts/{id}/retry` creates a fresh attempt for
the same submitted turn only when the latest attempt failed, was cancelled, or
was interrupted. It reuses the server-frozen source and context snapshot, so a
changed browser draft cannot alter a retry; completed attempts require a new
turn instead.

The API console reads the same account ledger without exposing bearer secrets.
Usage summaries can be filtered by account, key, model, endpoint, outcome, and
site user; request rows are paginated independently of the full-data summary.
Administrator account/key mutations write non-secret audit events containing
the actor, target, outcome, timestamp, and HTTP request correlation ID.

The root `ai_router.py`, `ai_api_keys.py`, and `ai_web_api.py` files are
compatibility shims for existing scripts and imports. New code should import
from `aurix_ai.router`, `aurix_ai.api_keys`, and `aurix_ai.web_api` directly.
