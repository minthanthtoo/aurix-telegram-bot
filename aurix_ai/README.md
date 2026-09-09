# AuriX AI package

This package is the canonical implementation of the AuriX AI gateway. It is
intentionally independent from the VPN and commerce modules.

```text
aurix_ai/router.py    model catalog, mode policy, context window, 9Router client
aurix_ai/api_keys.py  AI accounts, bearer-key auth, usage ledger, migrations
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

The root `ai_router.py`, `ai_api_keys.py`, and `ai_web_api.py` files are
compatibility shims for existing scripts and imports. New code should import
from `aurix_ai.router`, `aurix_ai.api_keys`, and `aurix_ai.web_api` directly.
