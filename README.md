# AuriX Telegram Bot

Telegram-first paid-concierge MVP with public free/trial access:

- catalog-backed 30-day plans: 50 GiB for 3,000 MMK and 100 GiB for 6,000 MMK;
- every tracked Telegram account can claim a 300 MiB key once per rolling 24 hours;
- 3 GiB / 30-day free entitlement, renewable every rolling 30 days;
- Telegram order creation and receipt-screenshot submission (optional vision LLM extracts candidate transaction IDs);
- immutable wallet ledger records verified credits and order captures;
- Telegram-ID allowlisted staff approval/rejection;
- idempotent Outline key provisioning, quota application, reconciliation, and revocation;
- durable SQLite jobs, notifications, and audit events for a single staging process;
- admin-only transfer/capacity summary from Outline metrics;
- quota observations fail closed and queue a hard Outline key deletion at `used >= limit`;
- TLS certificate fingerprint pinning for the Outline Management API;
- Small vetted `cryptography` and optional PostgreSQL-driver dependencies.

The paid flow is designed for a single persistent staging Droplet. PostgreSQL,
independent worker processes, automated payment-provider verification, referrals,
affiliates, resellers, and multi-node allocation are intentionally not enabled
until their deployment or evidence gates pass. See
[`docs/MVP_STATUS.md`](docs/MVP_STATUS.md) for the current comparison with the
final architecture.

## Configure

Create a bot with [@BotFather](https://t.me/BotFather). Obtain Outline Management API URL from your server install output. Compute its certificate SHA-256 fingerprint:

```sh
openssl s_client -connect OUTLINE_HOST:OUTLINE_PORT </dev/null 2>/dev/null \
  | openssl x509 -outform DER \
  | openssl dgst -sha256
```

Copy `.env.example` to a secret file outside Git, then export values:

```sh
set -a
. ./.env
set +a
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

Required variables:

- `TELEGRAM_BOT_TOKEN`
- `OUTLINE_API_URL` — full secret management URL, such as `https://host:port/SecretPath`
- `OUTLINE_CERT_SHA256` — pinned server certificate fingerprint
- `AURIX_ACCESS_URL_KEY` — persistent Fernet key used to encrypt stored access URLs

Optional:

- `DATABASE_PATH` — default `data/bot.db`
- `ADMIN_TELEGRAM_IDS` — comma-separated Telegram numeric IDs for staff commands
- `TRIAL_TELEGRAM_IDS` — legacy allowlist; leave empty for public daily 300 MiB and monthly 3 GiB claims
- `COMMERCE_DATABASE_URL` — PostgreSQL URL for hosted commercial state; empty uses staging SQLite
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — server-side credentials for the private receipt-evidence bucket. Never use the publishable/anon key here.
- `SUPABASE_RECEIPTS_BUCKET` — private bucket name (default `payment-receipts`)
- `RECEIPT_STORAGE_REQUIRED` — set to `1` in hosted deployments so a receipt cannot enter review until its object is stored
- `RECEIPT_LLM_BASE_URL`, `RECEIPT_LLM_MODEL`, `RECEIPT_LLM_API_KEY` — optional OpenAI-compatible vision endpoint. If absent/unavailable, receipts stay in manual review.
- `ALLOW_TEXT_PAYMENT_REFERENCES` — defaults to `0`; keep disabled for screenshot-only payments. Enable only for legacy staging tests.
- `AURIX_MAINTENANCE_INTERVAL_SECONDS` — independent housekeeping interval (default `60`).
- `AURIX_LATENCY_LOG` — set to `1` temporarily to log bounded Telegram, Outline, Supabase Storage, Postgres, handler, and maintenance timings.

Do not expose `OUTLINE_API_URL`, bot token, DB, or generated access URLs. Firewall Outline Management API so only bot host can reach it.

## Commands

The normal customer menu uses persistent Telegram buttons. The default slash-command list intentionally excludes staff operations.

Customer:

- `/start`
- `/help`
- `/whoami` — show the Telegram ID used for admin allowlisting
- `/claim`
- `/plans`
- `/buy <plan-code>`
- `/replace <plan-code>` — replace an untouched open order with another plan
- `/paid <order-id>` (then send the receipt screenshot)
- `/trial`
- `/wallet`
- `/walletpay <order-id>`
- `/myorders` — recent order/payment/review status
- `/order <order-id>` — full detail for an order you own
- `/cancelorder <order-id>` — cancel an untouched order
- `/status`
- `/myvpn`
- `/renew`

Admin-only (shown through `/admin` only after allowlisting):

- `/admin`
- `/orders` (admin)
- `/receipts` (admin)
- `/receipt <evidence-id>` (admin)
- `/verify <evidence-id> <transaction-id> <amount>` (admin; after checking the receiving account)
- `/rejectreceipt <evidence-id> [reason]` (admin; keep the order open for a replacement screenshot)
- `/approve <order-id>` (admin)
- `/reject <order-id>` (admin)
- `/capacity` (admin)
- `/reconcile` (admin; scan order, job, receipt, and wallet invariants)
- `/failed` (admin; review terminal provisioning/revocation failures)
- `/retry <order-id>` (admin; requeue one reviewed terminal worker failure)
- `/ledger <telegram-id>` (admin; inspect wallet balance and immutable events)
- `/refund <order-id> [reason]` (admin; issue a wallet reversal and revoke access)

To enable an administrator, send `/whoami` to the bot, place the returned numeric ID in `ADMIN_TELEGRAM_IDS` (comma-separated for multiple staff), and restart the bot. Telegram usernames are not accepted for authorization because they can change; only numeric IDs are used.

Creating an order is idempotent while the customer already has an order in `awaiting_payment` or `payment_submitted`: repeated `/buy`, Upgrade-button, and renewal requests return that open order instead of inserting duplicates. Once an order is approved, another `/buy` or `/renew` creates an independent entitlement, so a customer may hold multiple paid keys for separate devices or plans at the same time. `/myvpn` lists every active key and hides expired/revoked credentials. Untouched orders expire after 24 hours and can be cancelled by the customer; orders with payment activity remain protected for staff review. Customers can inspect only their own orders; allowlisted admins can use `/order <order-id>` to inspect any order’s payment, receipt-review, wallet reservation, subscription, and provisioning trail.

The customer-facing order stage is derived consistently from the underlying records:
`awaiting_payment`, `review_pending`, `payment_verified`, `wallet_reserved`,
`activation_pending`, `fulfilled`, `rejected`, or `cancelled`. Screenshot submission
always enters `review_pending`, even when the vision model cannot extract a
transaction ID. Staff can reject an individual receipt and keep the order open
for a replacement; rejecting the order itself closes it.

Order, plan, wallet, VPN, and receipt messages include contextual inline buttons. Customers can open an order, request receipt-upload guidance, pay from wallet, refresh status, or return to their order list without copying IDs. Admin queues link directly to order and receipt review; approval appears only after receipt verification, while rejection requires a separate confirmation click.

## Test

```sh
python3 -m unittest -v
```

## Operational notes

Outline byte limits use trailing 30-day transfer accounting. Fresh keys prevent old usage affecting a new entitlement. Time expiry belongs to this bot: a bounded maintenance pass (60 seconds by default) checks expiry and quota independently. Outline has no documented pause endpoint, so AuriX marks access unavailable locally and queues `DELETE /access-keys/{id}`; a known-key 404 is converged state. The maintenance pass queues one Telegram warning at 25%, 10%, and 5% remaining quota per key, then removes the key at the observed limit. Warning delivery is retried durably and does not gate enforcement. Existing sessions must be acceptance-tested against the deployed Outline version before promising an immediate disconnect.

Customers can use `/usage` or the **Usage** button to see each of their free,
trial, and paid keys with Outline-reported bytes used, configured limit,
remaining bytes, percentage, expiry, and local state. These figures come from
`GET /metrics/transfer`: they are trailing-30-day transfer accounting, not live
speed or a calendar-month usage ledger. A customer can never query another
customer's key statistics.

Outline key names are operator-readable and use UTC start time: `<username-or-telegram-id>-<tier>-<duration>-YYYYMMDDHHMM`, for example `min_user-FREE300MB-24hr-202608280520`. Telegram usernames are sanitized; accounts without a username fall back to their numeric Telegram ID. Renaming a key does not change its access URL.

Receipt images are evidence, not proof. AuriX stores each new raw image in a private Supabase Storage bucket and stores only its bucket/path, checksum, MIME type, size, extraction result, and review state in the database. Telegram file metadata remains as a compatibility fallback for older evidence. The upload is completed before the order enters `payment_submitted`; failed uploads remain retryable and are never shown in the admin review queue. The optional LLM output is untrusted and never approves a payment or credits a wallet. Staff must verify recipient, amount/currency, timestamp, and unique transaction ID against the receiving account, record that decision with `/verify`, and only then use `/approve`. In public mode, the commerce service itself rejects approval without verified evidence or a wallet reservation; the legacy text-only approval path exists only for explicit test fixtures.
Configure the bucket's lifecycle/retention rule separately after confirming the business and payment-record retention policy; the application does not silently delete evidence.

When a customer submits a receipt, each configured admin receives the screenshot
immediately with order/review controls. Telegram distinguishes photos from image
documents, so AuriX preserves that media type and retries the alternate send method
for evidence saved before this metadata was introduced. `/receipts` remains the
durable recovery queue if an admin was offline or the immediate notification failed.

Wallet events are immutable. An external verified receipt records `credit → reserve → capture`; a wallet purchase records `reserve → capture`; rejection releases a reservation exactly once. Capture does not deduct the balance a second time. `/wallet` shows the current projection and recent ledger events, while `/reconcile` reports balance mismatches and impossible order/job combinations.

The receipt parser uses an OpenAI-compatible `/chat/completions` endpoint with a
vision-capable model. Configure the three `RECEIPT_LLM_*` variables only after
testing that endpoint with a synthetic receipt. If they are unset, unreachable,
or return invalid JSON, the bot records the screenshot for manual review and
does not guess a transaction ID.

SQLite fits one-process MVP on persistent local storage. Do not deploy DB onto an ephemeral filesystem. Run one bot process only; Telegram long polling and this SQLite workflow are not designed for replicas.

Setting `COMMERCE_DATABASE_URL` adds PostgreSQL durability for commercial state,
but does not by itself make Telegram long polling or notification delivery
replica-safe; keep one bot process until the independent worker/webhook gate is
completed.

**Production DB path:** Mount a persistent volume (e.g., `/var/lib/aurix-bot/`) and set `DATABASE_PATH=/var/lib/aurix-bot/bot.db`. Do not use `/tmp`, container layers, or Render free-tier disk — data is lost on restart.

### Deploy on Render

[`render.yaml`](render.yaml) defines AuriX as a single Singapore background
worker with a 1 GB persistent disk mounted at `/var/data`. A worker is required
because Telegram long polling is a continuous outbound process and does not
serve an HTTP port. Render does not offer free background workers or free
persistent disks, so use at least the Starter plan. The disk also intentionally
prevents multiple instances and overlapping SQLite writers.

1. Push this repository to GitHub, GitLab, or Bitbucket, then choose **New →
   Blueprint** in Render and connect the repository.
2. Supply every environment variable marked `sync: false`. Generate
   `AURIX_ACCESS_URL_KEY` once with the command in `.env.example`; never replace
   it after keys have been stored, or existing encrypted access URLs become
   unreadable. `ADMIN_TELEGRAM_IDS` must contain your numeric Telegram ID.
3. Leave `TRIAL_TELEGRAM_IDS` empty for public 300 MiB daily and 3 GiB monthly
   claims. Leave `ALLOW_TEXT_PAYMENT_REFERENCES=0` for screenshot-only payment
   review. The three `RECEIPT_LLM_*` values may be blank together; screenshots
   then remain available for manual verification.
4. Deploy exactly one instance. Stop any local/VPS copy using the same bot token
   before starting Render, because two `getUpdates` consumers conflict.
5. Verify the deploy log contains `Bot authorized` and `Outline connected`, then
   test `/whoami`, `/claim`, `/usage`, and the admin `/enforcement` screen.

The worker first runs `deploy/render_preflight.py`. It fails the deploy without
printing secrets if the admin allowlist, Outline URL/pin, encryption key,
persistent database path, or screenshot-only payment policy is unsafe.

The persistent disk preserves the SQLite claim, order, wallet, audit, quota,
and termination records across restarts. Render disk snapshots are useful for
recovery, but deployment remains a single-process topology; moving all state to
PostgreSQL plus webhook/outbox workers is still the production scaling gate.

An experimental `render-free.yaml` profile is also included for a $0 Web
Service pilot using Supabase PostgreSQL and the `/healthz` wrapper monitored by
UptimeRobot. It is intentionally separate from `render.yaml`: Free Render has
no persistent disk and can sleep/restart, so use that profile only for
controlled testing.

For the complete offline-to-dashboard handoff, environment-variable table,
failure diagnosis, acceptance test, backup, and rollback procedure, use
[`docs/RENDER_DEPLOYMENT.md`](docs/RENDER_DEPLOYMENT.md).

For repeatable production diagnosis with Codex, use the Supabase and Render
MCP setup in [`codex-mcp.toml.example`](codex-mcp.toml.example) and follow the
correlation workflow in [`docs/MCP_DEBUGGING.md`](docs/MCP_DEBUGGING.md).

### Expiry and quota enforcement

Time expiry and data quota are independent hard stops. Each bounded maintenance
pass first checks Outline's rolling transfer metrics, then wall-clock expiry. When either
condition is observed, AuriX immediately hides the credential, writes a durable
termination event, sends `DELETE /access-keys/{id}`, and verifies the key with a
follow-up read. Failed deletes remain retryable; after ten failed cycles their
record changes to `escalated` and the admin receives another alert. `/usage`
shows the customer-visible state and `/enforcement` shows the operator audit.

Outline 1.12.3's own quota evaluator runs hourly, so the bot-side deletion loop
closes that possible delay. A verified API deletion proves that the credential
is absent and prevents new authenticated connections. It does not provide a
documented per-key command to kill a transport already established inside the
shared proxy process. AuriX therefore never reports “force disconnected” unless
that behavior has been separately proven in a live client test. Automatically
restarting Outline is intentionally excluded because it would interrupt every
customer on the server.

For the supplied staging host (`139.59.122.170`, Ubuntu 24.04, 1 vCPU/1 GB RAM),
follow [`deploy/README.md`](deploy/README.md). The server address alone is not
enough to deploy: SSH reachability, Outline installer output, a bot token, and
an admin Telegram ID are still required.
