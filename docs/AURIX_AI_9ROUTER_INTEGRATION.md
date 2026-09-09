# AuriX AI / 9Router integration contract

Status: implemented locally on 2026-09-09. 9Router source and database are
not modified by this integration.

## Ownership

```text
Customer/site identity, API keys, permissions, quotas, usage ledger: AuriX
Provider connections, routing, provider telemetry, provider cost: 9Router
```

AuriX calls 9Router through its private OpenAI-compatible endpoint. External
sites receive only AuriX `ak_live_...` keys. The 9Router credential never
leaves the AuriX service.

## Persistence

Set `AURIX_AI_DATABASE_URL` to the existing AuriX `COMMERCE_DATABASE_URL` for
hosted production. AuriX creates component-scoped `ai_api_keys` migrations in
that PostgreSQL database; it does not use or alter 9Router's database tables.

If the URL is empty, `AURIX_AI_API_KEYS_DB_PATH` selects the single-host SQLite
fallback. The fallback requires its own persistent volume and backup.

Accounts may be linked to existing AuriX identities with `owner_type` and
`owner_id`, for example `owner_type=telegram` and the Telegram numeric ID.
The link is deliberately application-level so external sites remain separate
from Telegram VPN customers unless the operator explicitly links them.

## Usage compatibility

AuriX records request status, model, provider model, input/output/total/cache
tokens, cost when supplied, AuriX request ID, and the returned 9Router request
ID. It sends private `X-AuriX-Request-ID` and `X-AuriX-Account-ID` headers to
9Router for correlation by router versions that preserve request metadata.

The following is a read-only export; it never inserts into 9Router:

```text
GET /api/admin/usage?format=9router
python /app/aurix_ai_keys.py usage --format 9router
```

The format is `9router.usageHistory.v1` and uses 9Router-compatible fields
such as `timestamp`, `provider`, `model`, `promptTokens`,
`completionTokens`, `cost`, `status`, `tokens`, and `meta`.

To compare against a saved 9Router `/api/usage/logs` or equivalent JSON array:

```sh
python /app/aurix_ai_reconcile.py --router-report router-usage.json
```

The report compares requests and token totals by model and counts matched
AuriX request IDs. Missing correlation metadata is reported as a mismatch; it
is not silently guessed.

## Admin access

Automations may use `AURIX_AI_ADMIN_TOKEN`. Existing AuriX Telegram admins may
also use their authenticated Telegram session when their IDs are listed in
`ADMIN_TELEGRAM_IDS`. The admin endpoints are separate from consuming site
keys and are not available to ordinary API callers.

## Scaling rule

The process-local rate limiter is valid for one AI replica. Before scaling to
multiple replicas, use the shared AuriX PostgreSQL ledger and add a shared
distributed rate limiter. Do not create one 9Router key per external site and
do not write AuriX customer rows into 9Router's internal database.
