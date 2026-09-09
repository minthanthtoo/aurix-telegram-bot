# AuriX AI gateway

Status: **live and verified 2026-09-09 (Asia/Rangoon)**

The AI product uses the existing 9Router as its model provider while giving
customers a stable AuriX hostname and a small server-side policy boundary:

```text
Browser
  -> https://ai.aurix-mart.tech/
  -> Caddy
  -> aurix-ai:10000
  -> 9router:20128/chat/completions
  -> configured model route

The AuriX AI ledger is separate from 9Router's provider ledger. It uses the
existing AuriX PostgreSQL database when `AURIX_AI_DATABASE_URL` is configured,
otherwise it uses the persistent local SQLite path.
```

The canonical implementation is in [`aurix_ai/`](../aurix_ai/). The root
`ai_router.py`, `ai_api_keys.py`, and `ai_web_api.py` files remain compatibility
shims for older imports; new code should use the package modules directly.

The old IP-derived 9Router hostname remains an infrastructure/admin alias. It
is not used as the customer-facing AuriX address.

## Modes

The server owns the system instructions and accepts only these modes:

- `english`: general answers in clear English.
- `translate`: English ↔ Lisu translation with uncertainty notes when useful.
- `lisu_assistant`: experimental Lisu-script conversation; native-speaker review
  is recommended.

The browser cannot replace the system instruction. Conversation history is
limited and is treated as untrusted user content. Lisu quality requires native
speaker review before official or sensitive use; a model-generated
back-translation is not independent validation.

The browser records a visible mode-change event after the first message is
submitted in the newly selected mode. When a mode changes, the visible
transcript remains available to the user and only a small recent handoff is
sent as untrusted reference context. This lets translator mode resolve a phrase
such as “translate it” without carrying over the previous mode's instructions.
Translator mode returns only the translation by default; the assistant modes
are conversational and do not require the user to provide a coding task.
Lisu-assistant mode is instructed to answer only in Lisu script and avoid
romanized or mixed-language filler; generated Lisu still requires
native-speaker review. The currently verified route for this behavior is
`ag/gemini-3.7-flash-high`, which returned `gemini-3.7-flash-tiered` during
the live comparison. The prior `ag/gemini-3.1-pro-low` route was rejected
because it repeatedly returned task-gating and English fallback text.

## API

`GET /api/healthz`, `GET /api/modes`, `GET /api/models`, and
`GET /api/auth/config` are public. The browser authenticates with Telegram:

- Inside Telegram, the Mini App sends signed `initData` to
  `POST /api/auth/miniapp`.
- In a normal HTTPS browser, the Telegram Login Widget sends its signed user
  payload to `POST /api/auth/telegram`.
- AuriX verifies the signature with the server-only `TELEGRAM_BOT_TOKEN`, then
  issues an opaque `HttpOnly; Secure; SameSite=Lax` session cookie.
- `POST /api/chat` uses that cookie; no AuriX access token is pasted into the
  browser.
- `POST /api/auth/logout` revokes the process-local session.

For the standalone Login Widget, configure the domain once in BotFather for
`@aurix_outline_vpn_bot` with `/setdomain ai.aurix-mart.tech`. Mini App login
does not require the widget domain, but the bot must still be configured with
the HTTPS Mini App URL if it is launched from Telegram.

The legacy header is accepted only when the server explicitly sets
`AURIX_AI_LEGACY_TOKEN_ENABLED=1` during migration:

```text
Authorization: Bearer <AURIX_AI_ACCESS_TOKEN>
```

Request:

```json
{
  "mode": "translate",
  "model_id": "gemini-3.7-flash-high",
  "message": "Translate this sentence.",
  "history": []
}
```

`GET /api/models` returns the server-approved comparison catalog. The browser
may submit one of those safe IDs as `model_id`; it cannot submit an arbitrary
9Router route. Omitting `model_id` uses the configured default. Changing the
model in the browser keeps the working conversation so the next answer can be
compared with the same context; the visible transcript and response ordering
remain unchanged.

Response includes the text, selected model ID/label, requested route, returned
model, provider usage when 9Router supplies it, and bounded-context metadata.
The returned model is recorded so a friendly alias cannot be mistaken for a
verified model identity. The browser reports when older context was trimmed;
this is expected behavior, not a short-input failure.

## External website API

The external integration boundary is AuriX, not 9Router. This repository does
not contain 9Router's source or a 9Router user/role system, and the gateway
therefore keeps the single `AURIX_AI_ROUTER_API_KEY` private. Other websites
must never receive that credential.

When either `AURIX_AI_API_KEYS_DB_PATH` or `AURIX_AI_DATABASE_URL` is
configured, the gateway exposes the external API. For production, set
`AURIX_AI_DATABASE_URL` to the same PostgreSQL URL used by
`COMMERCE_DATABASE_URL`; this creates component-scoped AI tables in the
existing AuriX database without touching 9Router tables.

```text
POST /v1/chat
POST /v1/chat/completions
Authorization: Bearer ak_live_...
```

`/v1/chat` uses the native AuriX request shape. The standard compatibility
route uses `model` and `messages[]`, does not require `conversation_id`, and
returns an OpenAI-style `chat.completion` response. It supports SSE streaming,
tool/function-call messages, image message parts, embeddings, and
OpenAI-compatible audio routes when the selected 9Router model advertises the
capability. AuriX never executes tools or stores audio bytes.

Native request body:

```json
{
  "mode": "translate",
  "model_id": "gemini-3.7-flash-high",
  "message": "How are you?",
"history": [],
"user_id": "site-user-123",
"conversation_id": "chat-456"
}
```

Each consuming website gets a separate account and one or more revocable
AuriX keys. Accounts hold the allowed modes, allowed model IDs, and a
requests-per-minute limit shared by all keys belonging to that account.
Successful and failed request metadata is recorded
without storing prompts or bearer tokens. Optional site-owned `user_id` and
`conversation_id` values are stored for per-user/conversation attribution only;
they are not authentication credentials. The key is hashed in the configured
AI database and the plaintext is printed only by the issue command, once.

The consuming website remains the source of truth for transcripts. It may send
up to 100 history items and an optional `context_summary`; AuriX keeps the
newest complete user/assistant turns that fit its 48 KiB bounded working
context, reports trimming in the response `context` object, and treats all
site-provided history and summaries as untrusted data.

There is deliberately no public self-registration or browser key-management
endpoint in this patch. Provision accounts from the host/container:

```sh
python /app/aurix_ai_keys.py create-account "Example site" \
  --modes translate,english \
  --models gemini-3.7-flash-high,claude-sonnet-4-6 \
  --requests-per-minute 60 \
  --owner-type external_site --owner-id example-site
python /app/aurix_ai_keys.py list-accounts
python /app/aurix_ai_keys.py issue-key acct_... --label production --expires-in-days 90
python /app/aurix_ai_keys.py update-account acct_... --requests-per-minute 120
python /app/aurix_ai_keys.py list-keys --account-id acct_...
python /app/aurix_ai_keys.py usage --account-id acct_...
python /app/aurix_ai_keys.py usage --format 9router > aurix-usage.json
python /app/aurix_ai_keys.py revoke-key key_...
```

Store the issued token in the consuming site's backend secret manager. Do not
put it in browser JavaScript. Browser-direct integrations require an additional
CORS allowlist and short-lived user tokens; they are intentionally not enabled
by this patch. The current `ai.aurix-mart.tech` hostname serves both
`/v1/chat` and `/v1/chat/completions`;
an `api.aurix-mart.tech` DNS/Caddy alias may be added later without changing
the API contract.

Keys expire after 90 days by default; use `--no-expiry` only for a controlled
server-to-server integration with an established rotation process. Issue a new
key before revoking the old one to perform zero-downtime rotation. Responses
and errors include an `X-Request-ID`/`request_id` for support correlation.

## Account usage and admin reporting

Every authenticated `/v1/chat` or `/v1/chat/completions` request creates a
prompt-free usage record with
the account, key, mode, model, result status, request ID, and token counters.
The gateway normalizes common provider fields (`prompt_tokens`/`input_tokens`,
`completion_tokens`/`output_tokens`, and `total_tokens`). If 9Router does not
return usage, the request is still counted and `usage_reported_requests` shows
that token data was unavailable.

With `AURIX_AI_ADMIN_TOKEN` configured, an operator can query the current UTC
month's account totals. When `ADMIN_TELEGRAM_IDS` is configured, the same
admin routes also accept an authenticated Telegram session for those existing
AuriX admin IDs; the bearer token remains useful for automation.

```text
GET /api/admin/accounts
GET /api/admin/usage?account_id=acct_...
Authorization: Bearer <admin-token>
```

The usage report includes requests, successful/failed requests, input tokens,
output tokens, cached tokens, provider model, optional cost, and recent
prompt-free request records. Optional `from`, `to`, and `limit` query
parameters support an ISO-8601 time window and up to 1,000 recent records.
`GET /api/admin/usage?format=9router` and the CLI `--format 9router` produce a
read-only `9router.usageHistory.v1` export. It omits bearer keys and never
writes to 9Router's database. The admin token is separate from every
consuming site key and is not accepted by `/v1/chat`.

For reconciliation, save a 9Router usage export and run:

```sh
python /app/aurix_ai_reconcile.py --router-report router-usage.json
```

The result compares request and token totals by model and reports matched
AuriX request IDs when the router export preserves the private correlation
metadata. A mismatch is diagnostic; AuriX remains the customer ledger and
9Router remains the provider/router ledger.

## Configure and run on the existing 9Router host

The AI container must join the existing `9router-net` network. Keep the secret
environment file root-readable only; copy the example and fill in the exact
9Router API key and model route obtained from the router itself.

```sh
install -d -m 0750 /etc/aurix-ai
install -o root -g root -m 0600 deploy/aurix-ai.env.example /etc/aurix-ai/aurix-ai.env
docker build -f deploy/aurix-ai.Dockerfile -t aurix-ai:local .
install -o root -g root -m 0644 deploy/aurix-ai.service /etc/systemd/system/aurix-ai.service
systemctl daemon-reload
systemctl enable --now aurix-ai
```

Before enabling the service, verify that the configured model route is present
and that an authenticated direct 9Router request returns the expected model.
Do not print the API key or prompt contents in a diagnostic log.

## Live Caddy route

The Caddy configuration routes the AuriX AI hostname to the live container:

```caddyfile
ai.aurix-mart.tech {
  import security_headers
  reverse_proxy aurix-ai:10000
}
```

The live deployment backed up the active Caddyfile, validated the configuration,
recreated the Caddy container so the read-only bind-mounted file was refreshed,
and verified these HTTPS endpoints without `-k`:

```sh
curl -fsS https://ai.aurix-mart.tech/api/healthz
curl -fsS https://ai.aurix-mart.tech/api/modes
```

An authenticated `/api/chat` request returned `PUBLIC_AURIX_AI_OK` with the
current configured route and an unauthenticated request returned 401.
The old `.sslip.io` hostname remains available as an infrastructure/admin
compatibility alias and is not used in customer documentation.

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `AURIX_AI_ROUTER_BASE_URL` | Private 9Router origin, normally `http://9router:20128` |
| `AURIX_AI_ROUTER_API_KEY` | Server-only 9Router credential |
| `AURIX_AI_MODEL` | Exact verified model route; required, no default alias |
| `TELEGRAM_BOT_TOKEN` | Server-only bot credential used to verify Telegram signatures |
| `AURIX_TELEGRAM_BOT_USERNAME` | Public bot username used by the Login Widget |
| `ADMIN_TELEGRAM_IDS` | Comma-separated existing AuriX Telegram admin IDs allowed to view AI reports |
| `AURIX_AI_SESSION_MAX_AGE_SECONDS` | Maximum age of Telegram-authenticated sessions |
| `AURIX_AI_LEGACY_TOKEN_ENABLED` | Default `0`; temporary migration fallback for the old header |
| `AURIX_AI_ACCESS_TOKEN` | Legacy migration token; not used by the browser when the fallback is disabled |
| `AURIX_AI_ALLOW_ANONYMOUS` | Must remain `0` for production |
| `AURIX_AI_API_KEYS_DB_PATH` | Persistent SQLite registry; used only when `AURIX_AI_DATABASE_URL` is empty |
| `AURIX_AI_DATABASE_URL` | Optional existing AuriX PostgreSQL URL; takes precedence over the SQLite path |
| `AURIX_AI_ADMIN_TOKEN` | Separate operator bearer token for account usage reports; leave empty to disable admin endpoints |
| `AURIX_AI_TIMEOUT_SECONDS` | Upstream timeout, bounded to 5–180 seconds |
| `AURIX_AI_MAX_OUTPUT_TOKENS` | Upstream output limit, bounded to 64–8192 |
| `AURIX_AI_MAX_REQUESTS_PER_MINUTE` | Per-process client rate limit |

The rate limiter is process-local and intended for the current single-container
deployment. A shared store is required before running multiple AI replicas;
set `AURIX_AI_DATABASE_URL` to the existing AuriX PostgreSQL database before
scaling out. The database has versioned component-scoped migrations and must be
backed up with the rest of AuriX state.
