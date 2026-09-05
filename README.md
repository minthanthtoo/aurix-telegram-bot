# AuriX Telegram Bot

Telegram-first paid-concierge MVP with public free/trial access:

- catalog-backed 30-day plans: 50 GB for 3,000 MMK and 100 GB for 6,000 MMK;
- every tracked Telegram account can claim a 300 MB key once per rolling 24 hours;
- 3 GB / 30-day free entitlement, renewable every rolling 30 days;
- owner-configurable promo seasons with decimal-GB quota, gift duration,
  UTC start/end, campaign/daily/hourly capacity, one claim per account, and
  automatic restoration of regular plans when a gift or season ends;
- Telegram order creation with a compact KBZPay → WavePay → AYA Pay → UABPay → CB Pay QR chooser, in-place method switching, and receipt-screenshot submission (optional vision LLM extracts candidate transaction IDs);
- receipt-backed wallet top-ups with preset/custom amounts, all five local QR
  methods, exact-amount verification, and immutable ledger credits/captures;
- Telegram-ID allowlisted staff approval/rejection;
- idempotent Outline key provisioning, quota application, reconciliation, and revocation;
- durable SQLite jobs, notifications, and audit events for a single staging process;
- multi-server, server-scoped free/promo/paid allocation with admin-declared
  key, traffic, tier, and plan capacity;
- admin-only transfer/capacity summary from Outline metrics;
- quota observations fail closed and queue a hard Outline key deletion at `used >= limit`;
- TLS certificate fingerprint pinning for the Outline Management API;
- Small vetted `cryptography` and optional PostgreSQL-driver dependencies.

The bot is designed to run as one persistent process. The recommended Render
topology is one paid Background Worker with persistent-disk SQLite; the
controlled free profile stores all bot state in Supabase PostgreSQL instead.
Neither profile is safe to scale beyond one instance. Independent worker
processes, automated payment-provider verification, referrals, affiliates, and
resellers are intentionally not enabled until their deployment or evidence
gates pass. Multi-server allocation is supported, but provider mutation remains
disabled by default and separate from the Telegram runtime. See
[`docs/MVP_STATUS.md`](docs/MVP_STATUS.md) for the current comparison with the
final architecture and
[`docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md`](docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md)
for the canonical fleet design and operations guide. The missing-key,
quota-preserving recovery procedure is documented separately in
[`docs/MANAGED_KEY_REPAIR_RUNBOOK.md`](docs/MANAGED_KEY_REPAIR_RUNBOOK.md).

Every branch is checked by the repository CI workflow before it is suitable for
deployment: Python compilation, Ruff, the full test suite, shell syntax, and
secret-free diff hygiene are required.

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
- `OUTLINE_SERVERS_JSON` — optional multi-server array of `{id,label,provider,region,transport,provider_resource_id,api_url,cert_sha256}`. `provider_resource_id` is the numeric DigitalOcean Droplet ID (never an IP address); keep the complete value only in the host's secret environment. `provider`, `region`, and `transport` are non-secret registry dimensions; current transport support is `outline`.
- `OUTLINE_DEFAULT_SERVER_ID` — fallback for legacy records; new free, promo,
  and paid issuance uses fresh health and declared capacity across the pool.
- `OUTLINE_PROVIDER_RESOURCE_ID` — optional numeric DigitalOcean Droplet ID for
  the legacy single-server configuration; use `provider_resource_id` inside
  `OUTLINE_SERVERS_JSON` after switching to a fleet.
- `AURIX_SERVER_HEALTH_MAX_AGE_SECONDS` — maximum age of inventory telemetry
  accepted for new admission (default `900`).
- `AURIX_ENDPOINT_FAILURE_THRESHOLD` / `AURIX_ENDPOINT_RECOVERY_THRESHOLD` —
  bounded health hysteresis (defaults `3` failures / `2` successful probes);
  failed nodes are blocked from new issuance until recovery evidence is stable.
- `AURIX_KEY_REPAIR_CACHED_USAGE_MAX_AGE_SECONDS` — maximum age of a persisted,
  explicitly observed per-key usage sample that may be reused during missing
  key repair (default `900`; older or never-observed usage requires owner review).
- `OWNER_TELEGRAM_ID` — preferred immutable owner/recovery Telegram numeric ID
- `ADMIN_TELEGRAM_IDS` — legacy one-time comma-separated administrator bootstrap
- `AURIX_CONTROL_GROUP_ID` — optional trusted numeric `-100...` bootstrap override. Normally the owner connects the group from **Owner Controls → Choose Control Group**; Telegram supplies and AuriX persists the numeric ID without invite-link parsing.
- `ADMIN_SCOPE_CLEANUP_IDS` — optional one-time comma-separated IDs whose old Telegram admin command scopes must be deleted after an administrator is removed
- `TRIAL_TELEGRAM_IDS` — legacy allowlist; leave empty for public daily 300 MB and monthly 3 GB claims
- `COMMERCE_DATABASE_URL` — PostgreSQL URL for all bot state when using the hosted PostgreSQL profile; empty uses SQLite. Existing SQLite state can be copied with the guarded `deploy/migrate_sqlite_to_postgres.py` cutover tool.
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — server-side credentials for the private receipt-evidence bucket. Never use the publishable/anon key here.
- `SUPABASE_RECEIPTS_BUCKET` — private bucket name (default `payment-receipts`)
- `RECEIPT_STORAGE_REQUIRED` — set to `1` in hosted deployments so a receipt cannot enter review until its object is stored
- `RECEIPT_LLM_BASE_URL`, `RECEIPT_LLM_MODEL`, `RECEIPT_LLM_API_KEY` — OpenAI-compatible vision endpoint. Local/manual-review development may leave these empty; hosted production profiles require them through `RECEIPT_VISION_REQUIRED=1`.
- `RECEIPT_VISION_REQUIRED` — set to `1` for production profiles so preflight
  refuses to start without all three receipt-vision settings; keep `0` only for
  local/manual-review development.
- `RECEIPT_LLM_FALLBACK_MODELS` — optional comma-separated routes on the same gateway. A fallback runs only when the primary fails or omits critical receipt fields; QR/payment-request negatives do not waste a fallback call.
- `RECEIPT_LLM_SELECTION_MODE` — `first_acceptable` (default, lowest cost), `rank_all` (score every configured model), or `consensus` (require two agreeing acceptable outputs; disagreement is flagged for manual review). The latter modes spend more model quota and never approve payments.
- `PAYMENT_RECIPIENTS_JSON` — required server-side merchant profiles for all five payment methods. Each profile contains accepted recipient `names` and/or account/phone `accounts`; values are never shown in customer messages or diagnostics.

When loading `.env` with a shell or systemd `EnvironmentFile`, keep this value
on one line and quote the complete JSON (the example below uses single quotes).
Without shell quoting, JSON property quotes are stripped before the bot starts,
causing the fail-closed receipt recipient check and the deployment preflight to
reject an otherwise valid profile.
- `ALLOW_TEXT_PAYMENT_REFERENCES` — defaults to `0`; keep disabled for screenshot-only payments. Enable only for legacy staging tests.
- `AURIX_MAINTENANCE_INTERVAL_SECONDS` — independent housekeeping interval (default `60`).
- `AURIX_LATENCY_LOG` — set to `1` temporarily to log bounded Telegram, Outline, Supabase Storage, Postgres, handler, and maintenance timings.
- `AURIX_FLEET_REGISTRATION_ENABLED`, `AURIX_FLEET_REGISTRATION_URL`, and
  `AURIX_FLEET_ENROLLMENT_KEY` — optional HTTPS one-time node-enrollment
  callback; enable only together with the worker's
  `AURIX_FLEET_AUTO_REGISTRATION_ENABLED` gate and pinned SSH trust files.

Optional DigitalOcean fleet creation belongs to a separate operator worker.
It is off by default and requires explicit allowlists, node/day/cooldown limits,
and a monthly budget. The final MVP envelope is assisted scaling at 75%/90%
key or declared-traffic utilization,
up to three Singapore 1 GB nodes, one creation per 24 hours, and an $18/month
node ceiling. Do not put `DIGITALOCEAN_API_TOKEN` in the normal bot
service. The default path remains assisted and stops before activation; an
owner-approved zero-touch enrollment callback is available when its HTTPS,
one-time-token, and pinned-SSH gates are configured. The exact gates and
verification paths are documented in
the [fleet runbook](docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md).
The exact second-node installation and canary sequence is in
[`docs/NODE2_INSTALL_AND_CANARY.md`](docs/NODE2_INSTALL_AND_CANARY.md).
After node-two verification, `AURIX_INFRASTRUCTURE_QUEUE_ENABLED=1` exposes a
single idempotent **Prepare next node** action in the admin Capacity panel. That
button records intent only; the separate worker and its mutation gate remain the
final authority. Configure `AURIX_SCALE_REGION`, `AURIX_SCALE_DROPLET_SIZE` and
`AURIX_SCALE_DROPLET_IMAGE` only with values inside the worker allowlists.
When provider mutations are enabled, the worker also requires
`AURIX_DIGITALOCEAN_SSH_KEY_IDS` (comma-separated DigitalOcean SSH-key IDs or
fingerprints) so every new Droplet is reachable by the pinned automation key.
The private key itself stays in `AURIX_FLEET_SSH_KEY`/the worker environment;
provider creation never uses a password or an untrusted first connection.

Provider inventory is also audited for stale, billable AuriX Droplets. The
worker records a candidate only after two persisted observations separated by
`AURIX_ORPHAN_CLEANUP_MIN_AGE_SECONDS` (default `3600`) and protects every
resource referenced by the endpoint registry or an unfinished infrastructure
job. Candidate discovery is read-only by default. To opt into destructive
cleanup, set `AURIX_ORPHAN_CLEANUP_ENABLED=1`, keep
`AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=1`, and set
`AURIX_ORPHAN_CLEANUP_CONFIRMATION=DELETE-UNREGISTERED-AURIX-NODES` in the
separate root-only infrastructure-worker environment. The exact phrase is an
intentional second gate; never place it in the normal Telegram bot service.

Customer-facing payment cards live in `assets/payment_qr/`. They intentionally
contain the merchant QR, but no visible account names or phone numbers. A QR can
still reveal its recipient inside the wallet app. Treat replacements as payment
credentials and scan-compare the source and final payload before deployment.

Do not expose `OUTLINE_API_URL`, `OUTLINE_SERVERS_JSON`, bot token, DB, or generated access URLs. Firewall every Outline Management API so only the bot host can reach it.

## Deploy on Render

AuriX has two Render profiles. Pick one; do not combine their storage settings.

| Profile | Render file | Service | State | Use it for |
| --- | --- | --- | --- | --- |
| Durable MVP (recommended) | [`render.yaml`](render.yaml) | One paid Background Worker (`starter` / `0.5c-512mb`) | SQLite on a 1 GB persistent disk | Real users and payments |
| Free pilot | [`render-free.yaml`](render-free.yaml) | One Free Web Service | Supabase PostgreSQL; Render filesystem is disposable | Controlled testing only |

The paid worker does not have a public URL because Telegram long polling only
needs outbound network access. The free profile wraps the same bot in a small
HTTP server so Render can check `/healthz`. Render Free can sleep, restart, and
has no persistent disk; PostgreSQL is therefore mandatory for that profile.
See Render's official [Free service limitations](https://render.com/docs/free)
and [persistent disk documentation](https://render.com/docs/disks).

### 1. Prepare the external services

Before opening Render:

1. Push the repository to GitHub, GitLab, or Bitbucket.
2. Create a Supabase project in or near Singapore.
3. In Supabase Storage, create a **private** bucket named
   `payment-receipts`. Both profiles use it for receipt screenshots.
4. Send `/whoami` to the bot and save the returned numeric Telegram ID for
   `OWNER_TELEGRAM_ID`. Optionally set the trusted AuriX group numeric ID in
   `AURIX_CONTROL_GROUP_ID` for safe bootstrap and owner-reviewed sync previews.
5. Stop every other process using this bot token. Only one long-polling
   `getUpdates` consumer may run reliably.
6. Collect the complete secret Outline Management API URL and its certificate
   fingerprint. The API URL must include the secret path, not just the IP and
   port.
7. Generate the access-URL encryption key once and keep it permanently:

   ```sh
   python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

To calculate the Outline certificate fingerprint, replace only the host and
management port:

```sh
openssl s_client -connect OUTLINE_HOST:OUTLINE_PORT </dev/null 2>/dev/null \
  | openssl x509 -outform DER \
  | openssl dgst -sha256
```

### 2. Add the common environment variables

Render Blueprints prompt for variables marked `sync: false`. Paste values
without surrounding quotes.

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `OWNER_TELEGRAM_ID` | Owner numeric Telegram ID; cannot be removed from the bot UI |
| `ADMIN_TELEGRAM_IDS` | Optional legacy bootstrap; manage admins from Owner Controls afterward |
| `AURIX_CONTROL_GROUP_ID` | Optional trusted negative `-100...` group ID |
| `OUTLINE_API_URL` | Complete secret HTTPS management URL, including its path |
| `OUTLINE_CERT_SHA256` | The 64-character SHA-256 certificate digest |
| `AURIX_ACCESS_URL_KEY` | The Fernet key generated above; never regenerate it after keys are stored |
| `SUPABASE_URL` | Project URL such as `https://PROJECT_REF.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side service-role secret, never the anon/publishable key |
| `SUPABASE_RECEIPTS_BUCKET` | `payment-receipts` |
| `RECEIPT_STORAGE_REQUIRED` | `1` |
| `ALLOW_TEXT_PAYMENT_REFERENCES` | `0` |
| `TRIAL_TELEGRAM_IDS` | Leave blank for public free and trial claims |

`RECEIPT_LLM_BASE_URL`, `RECEIPT_LLM_MODEL`, and `RECEIPT_LLM_API_KEY` are
optional, but must be either all configured or all blank. Without them, receipt
screenshots still enter manual review. LLM extraction never verifies a payment.
The standalone Antigravity OAuth proxy is compatible: set the base URL to its
authenticated `/v1` endpoint, use a model ID returned by `/v1/models`, and use
the gateway API key—not a Google/OAuth access or refresh token. Keep the gateway
behind TLS and authentication. Bot diagnostics mask infrastructure hosts and
request IDs with retained prefixes/suffixes.

`PAYMENT_RECIPIENTS_JSON` must contain the real receiving identities before AI
triage is enabled. Example shape (real values belong only in the private host
environment): `{"kbzpay":{"names":["MERCHANT NAME"],"accounts":["1234"]},...}`.

### 3A. Recommended: paid Background Worker with persistent disk

1. In Render, select **New → Blueprint** and connect this repository.
2. Use the default Blueprint path `render.yaml`.
3. Confirm Render proposes exactly one service with these settings:

   ```text
   Type: Background Worker
   Region: Singapore
   Plan: starter / 0.5c-512mb
   Instances: 1
   Disk: 1 GB mounted at /var/data
   Build Command: pip install -r requirements.txt
   Start Command: python deploy/render_preflight.py --live && python -u app.py
   ```

4. Enter the common secrets above. Keep these Blueprint values unchanged:

   ```text
   AURIX_STORAGE_MODE=disk
   DATABASE_PATH=/var/data/bot.db
   COMMERCE_DATABASE_URL=
   ```

5. Apply the Blueprint and watch the first deploy. Do not add `PORT`; a
   Background Worker does not serve HTTP.

This is the recommended MVP topology. The disk preserves claims, orders,
wallets, encrypted keys, Telegram update deduplication, jobs, and audit events
across restarts. Keep exactly one instance because this release is a
single-poller, single-worker application.

### 3B. Optional: $0 Web Service with Supabase PostgreSQL

Use this only for a controlled pilot. Render Free has an ephemeral filesystem,
can restart at any time, and normally spins down after 15 minutes without
inbound traffic. A sleeping bot cannot receive Telegram long-poll updates until
an HTTP request wakes the service.

Create it from `render-free.yaml` as a Blueprint, or enter these exact fields
when creating a Web Service manually:

```text
Runtime: Python
Region: Singapore
Plan: Free
Instances: 1
Build Command: pip install -r requirements.txt
Start Command: python deploy/render_preflight.py --live && python -u deploy/render_web.py
Health Check Path: /healthz
```

Set the common variables, plus:

```text
AURIX_STORAGE_MODE=postgres
COMMERCE_DATABASE_URL=<Supabase session-pooler PostgreSQL URL>
```

Do not set `DATABASE_PATH` and do not use SQLite on the Free profile.

In Supabase, click **Connect → Session pooler** and copy the complete URI. A
persistent Render process should use the IPv4-compatible session pooler rather
than the direct IPv6-only database endpoint on a Free Supabase project. Keep
`sslmode=require`. The shape is:

```text
postgresql://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

Do not type that example literally: copy the project-specific value from
Supabase. The username belongs before the first `:` and the hostname must not
contain `@`. If the database password contains `@`, `/`, `:`, `#`, `%`, or other
URL-reserved characters, use the percent-encoded password supplied by the
dashboard or URL-encode it. A malformed value such as a hostname beginning
with `sup@aws-...` causes `failed to resolve host` during startup. See the
official [Supabase connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres).

After deployment, open:

```text
https://YOUR-SERVICE.onrender.com/healthz
```

A healthy response has HTTP 200 and `"status": "ok"`. An external uptime
monitor can check this endpoint, but it does not turn the Free profile into a
durable or production-grade service.

#### Optional zero-touch node enrollment

The same web entrypoint exposes `POST /fleet/register` only when
`AURIX_FLEET_REGISTRATION_ENABLED=1`. A provider-created node receives a
short-lived, single-use enrollment token in cloud-init; after Outline and SSH
start, it posts its local `access.txt` identity and SSH host key to this
endpoint. The callback stores an encrypted payload, and the infrastructure
worker activates the node only after the provider-observed IP, Outline
management URL, and pinned SSH host key all match. It never accepts a host key
implicitly and never sends the enrollment encryption key to the VM.

To opt in, configure these values in the worker and web-service environments
(keep the encryption key identical and private):

```text
AURIX_FLEET_REGISTRATION_ENABLED=1
AURIX_FLEET_AUTO_REGISTRATION_ENABLED=1
AURIX_FLEET_REGISTRATION_URL=https://YOUR-SERVICE.onrender.com/fleet/register
AURIX_FLEET_ENROLLMENT_KEY=<Fernet key>
AURIX_FLEET_ENV_FILE=/etc/aurix-bot/aurix.env
AURIX_FLEET_SSH_KEY=/etc/aurix-fleet/automation_ed25519
AURIX_FLEET_KNOWN_HOSTS=/etc/aurix-fleet/known_hosts
AURIX_FLEET_CONTROL_PLANE_SOURCE=<control-plane CIDR>
```

The callback returns only a sanitized status and job ID. A rejected, expired,
replayed, or malformed request does not activate a node; the worker keeps the
provisioning job in `awaiting_verification` for a later retry. For production,
use a durable PostgreSQL control-plane database and keep Render's health check
on `/healthz`.

### 4. Verify the first deployment

A healthy log contains:

```text
Render preflight passed: single-worker persistent disk configuration is valid
Bot authorized: @your_bot_username
Outline connected: version ...
```

The free profile reports `hosted PostgreSQL configuration is valid` instead.
This warning is expected when receipt vision is intentionally disabled:

```text
WARNING: receipt vision extraction is disabled; screenshots require manual transaction entry.
```

Then test, in order:

1. `/start` and `/whoami`.
2. `/admin` from the allowlisted account.
3. `/claim`, then `/usage`.
4. `/trial` and confirm a second monthly trial is refused.
5. Create one paid test order, submit a synthetic receipt screenshot, review
   it from `/admin`, and verify that approval provisions exactly one key.
6. Check `/myorders`, `/wallet`, `/myvpn`, `/capacity`, `/reconcile`, and
   `/enforcement`.

Do not accept real payments until this sequence passes.

### 5. Common Render failures

| Log or symptom | Cause and fix |
| --- | --- |
| Render asks for **Start Command** | Paid worker: `python deploy/render_preflight.py --live && python -u app.py`. Free Web Service: `python deploy/render_preflight.py --live && python -u deploy/render_web.py`. |
| `failed to resolve host 'sup@aws-...'` | The Supabase URI was assembled incorrectly. Copy **Connect → Session pooler**, preserve the username/host boundary, and URL-encode the password. |
| Cannot resolve `db.PROJECT_REF.supabase.co` | The direct endpoint is IPv6 by default. Use the Supabase session-pooler URI on port `5432`. |
| `Render preflight failed` | Fix the named variable; the preflight deliberately exits before starting with unsafe storage, key, or payment settings. |
| `Telegram getMe failed` | Replace the invalid BotFather token or fix outbound network access. |
| Repeated Telegram `Conflict` / `getUpdates` | Another VPS, local terminal, or Render service is using the same bot token. Stop it. |
| Outline certificate/readiness failure | Recheck the complete management URL, TLS fingerprint, API reachability, and Outline firewall. |
| Receipt upload failure | Confirm the bucket is private and exists, and that `SUPABASE_SERVICE_ROLE_KEY` is the server-side service-role secret. |
| Slow first request on Free | The Web Service probably cold-started after sleeping. Paid services do not have the Free idle-spin-down behavior. |
| SQLite data disappears | SQLite was used without the paid `/var/data` disk. Restore from backup and use the paid profile, or switch the whole app to the PostgreSQL profile. |

The deployment preflight never prints secret values. Do not paste Render or
Supabase secrets into issues, screenshots, Git commits, or support messages.
For backup, rollback, and the full acceptance runbook, see
[`docs/RENDER_DEPLOYMENT.md`](docs/RENDER_DEPLOYMENT.md).

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
- choose a payment method from the order, scan the single QR card, then tap **I’ve Paid** and send the receipt screenshot
- `/paid <order-id>` — compatibility path for sending a receipt directly
- `/trial`
- `/wallet`
- `/topup` — choose 3,000/6,000/10,000/20,000 MMK or enter a custom
  1,000–1,000,000 MMK amount, then pay through one of the five QR methods
- `/walletpay <order-id>`
- `/myorders` — in-place Open/Completed/Cancelled/Rejected/All tabs with
  pagination; buttons refresh the current message instead of adding chat noise
- `/order <order-id>` — full detail for an order you own
- `/cancelorder <order-id>` — cancel an untouched order
- `/status`
- `/myvpn`
- `/renew`
- sending the exact active promo code claims that campaign; `/start`, `/plans`,
  and `/myvpn` provide one-tap Redeem and Copy buttons while it is available

Admin-only (available to allowlisted administrators through the `/admin`
inline panel; only `/admin` is advertised in the administrator command menu):

- `/admin`
- `/receiptsystem` (Manual/AI-Assisted policy, health and isolated actual-receipt test)
- `/receiptmode manual|assisted` (confirmed; AI never proves payment receipt)
- `/receipttest` (diagnostic only; cannot create an order, credit, subscription or key)
- `/promo` (admin; view the active/scheduled campaign and copy a setup example)
- `/setpromo CODE QUOTA_GB DAYS COUNT campaign|daily|hourly FROM_UTC TO_UTC`
  (admin; confirmed mutation; decimal GB means `1 GB = 1,000,000,000 bytes`)
- `/stoppromo <code>` / `/resumepromo <code>` (admin; confirmed season control)
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
- `/migrations` (admin; monitor endpoint credential migrations and retries)
- `/retry <order-id>` (admin; requeue one reviewed terminal worker failure)
- `/ledger <telegram-id>` (admin; inspect wallet balance and immutable events)
- `/refund <order-id> [reason]` (admin; issue a wallet reversal and revoke access)

Owner-only endpoint operations are available from **Capacity → server policy**:
**Start drain** stops new assignments while existing keys continue to work;
**Resume admission** reopens a drained endpoint; **Retire when empty** is
accepted only after local orders/keys, pending setup, and the latest remote
inventory are empty. The equivalent confirmed command is
`/serverstate <server-id> active|draining|retired`. These controls never delete
or rebuild a provider VM.

The owner can also open **Capacity → server policy → Migrate active keys**. The
bot lists only active, registry-bound credentials, shows only healthy admitting
targets, and asks for confirmation. The worker rechecks source usage, creates a
replacement with only the remaining quota, persists the local cutover, notifies
the customer, and retries old-key deletion until verified. **Migrations** in the
admin panel shows pending, failed, and source-delete-pending operations without
exposing any management URL or access-key secret.

Owner-only `/owner` provides Staff & Access, group-sync preview and receipt
controls. A new administrator must first open the bot and use `/whoami`; the
owner then uses `/addadmin ID` (or the Staff panel) with a durable confirmation.
Removal is immediate, invalidates pending confirmations and removes the private
Telegram command scope. The owner can select a group through Telegram's native
chat picker. AuriX requires the bot to already be a member, verifies that the
AuriX owner is also the group creator, stores the numeric chat ID, and imports
human administrators only when no active administrators exist.
`AURIX_CONTROL_GROUP_ID` remains an optional server bootstrap override. Invite
links are not parsed, bot accounts are not imported, and an established owner
is never silently replaced. Later group-role changes are preview-only until
owner review. Telegram provides member count and administrator data, but not a
full ordinary-member directory.

Promo codes and Outline credentials intentionally use different UX. Promo cards
make **Redeem** the primary action; the visible code remains long-press
selectable and a Copy Code control is reserved for share/admin views. Outline
keys use native readable **Copy Outline Key** buttons. `/myvpn` shows one copy
row per plan and hides raw `ss://` credentials by default; **Show Keys as Text**
is the compatibility fallback for older Telegram clients.

Promo `COUNT` is the number of gifts available in each selected window:
`campaign` shares one count across the entire season, `daily` resets at 00:00
UTC, and `hourly` resets at the start of every UTC hour. An account can still
claim only once for a given promo code. Stopping a season does not delete an
already-issued key, but it immediately removes the promo lock so daily,
monthly, paid, renewal, and replacement actions return. Those actions also
return automatically when the gift expires or reaches quota. After a promo has
claims, its quota, duration, frequency, and start are immutable; use a new code
for a materially new season so issued entitlements and audit history stay
truthful.

Creating an order is idempotent while the customer already has an order in `awaiting_payment` or `payment_submitted`: repeated `/buy`, Upgrade-button, renewal, and top-up requests return that open order instead of inserting duplicates. Once an order is approved, another purchase creates an independent entitlement, so a customer may hold multiple paid keys for separate people, devices, or plans at the same time. `/myvpn` stays compact and links to an in-place, five-per-page Active/Ended/All paid-key browser; each numbered key opens a focused quota/expiry view with one-tap copy. Untouched orders expire after 24 hours and can be cancelled by the customer; orders with payment activity remain protected for staff review. Customers can inspect only their own orders; allowlisted admins can use `/order <order-id>` to inspect any order’s payment, receipt-review, wallet reservation, subscription, and provisioning trail. When several historical orders are open, an uncaptioned receipt is refused as ambiguous; the order-specific **Upload Receipt** button binds the next screenshot to that order for ten minutes, or the customer can use `/paid <order-id>` in the caption.

The customer-facing order stage is derived consistently from the underlying records:
`awaiting_payment`, `review_pending`, `payment_verified`, `wallet_reserved`,
`activation_pending`, `fulfilled`, `rejected`, or `cancelled`. Screenshot submission
always enters `review_pending`, even when the vision model cannot extract a
transaction ID. Staff can reject an individual receipt and keep the order open
for a replacement; rejecting the order itself closes it.

Order, plan, wallet, VPN, and receipt messages include contextual inline buttons. Customers can open an order, request receipt-upload guidance, pay from wallet, refresh status, or return to their order list without copying IDs. Admin queues link directly to order and receipt review; every high-impact admin action requires a fresh, single-use confirmation click after the current state is displayed.

The admin **Capacity** panel reconciles each configured Outline server, counts every
remote access key (including keys created outside AuriX), and shows observed
30-day transfer plus experimental current/peak bandwidth when Outline 1.12+
provides it. Owners set a maximum key count, protected headroom, monthly traffic
budget, and per-plan slot allocations with inline controls. These are declared
business limits—Outline does not publish a maximum-user capacity. New paid
orders reserve a server/plan slot for 24 hours; submitted receipts retain the
reservation through review, and provisioning/revocation stays pinned to that
server. Existing keys are never silently migrated when limits change.

Administrative confirmations are stored in the hosted database (with an
in-memory fallback only for lightweight test doubles), bound to the requesting
administrator and private chat, expired after five minutes, and rejected when
the reviewed order or receipt state changes. Cancelled and consumed tokens
cannot be replayed after a restart.

## Test

```sh
python3 -m pip install --requirement requirements-dev.txt
ruff check app.py commerce.py commerce_models.py commerce_repositories.py commerce_service.py commerce_worker.py entitlements.py free_repository.py migrations.py observability.py outline_adapter.py persistence.py ports.py repositories.py receipt_llm.py runtime.py supabase_storage.py telegram_transport.py telegram_admin.py telegram_admin_panels.py telegram_callbacks.py telegram_commands.py telegram_maintenance.py deploy test_*.py
PYTHONWARNINGS=error::ResourceWarning coverage run -m unittest discover
coverage report
```

The CI and refactor invariants, schema fingerprint, staging smoke checks, and
backup/rollback gate are documented in
[`docs/REFACTOR_PHASE0_BASELINE.md`](docs/REFACTOR_PHASE0_BASELINE.md).
The shared SQLite connection lifecycle and Phase 1 dependency rules are in
[`docs/REFACTOR_PHASE1.md`](docs/REFACTOR_PHASE1.md).
The repository protocols and numbered migration contract are in
[`docs/REFACTOR_PHASE2.md`](docs/REFACTOR_PHASE2.md).
The free/trial entitlement and persistence boundaries are in
[`docs/REFACTOR_PHASE3.md`](docs/REFACTOR_PHASE3.md).
The paid-commerce model, repository, service, and facade boundaries are in
[`docs/REFACTOR_PHASE4.md`](docs/REFACTOR_PHASE4.md).
The external adapter ports and reliable-worker boundary are in
[`docs/REFACTOR_PHASE5.md`](docs/REFACTOR_PHASE5.md).
The Telegram presentation and administrator-operation boundary are in
[`docs/REFACTOR_PHASE6.md`](docs/REFACTOR_PHASE6.md).
The runtime composition and executable compatibility facade are in
[`docs/REFACTOR_PHASE7.md`](docs/REFACTOR_PHASE7.md).
The decomposed Telegram feature-module boundaries are in
[`docs/REFACTOR_PHASE8.md`](docs/REFACTOR_PHASE8.md).

## Operational notes

Outline byte limits use trailing 30-day transfer accounting. Fresh keys prevent old usage affecting a new entitlement. Time expiry belongs to this bot: a bounded maintenance pass (60 seconds by default) checks expiry and quota independently. Outline has no documented pause endpoint, so AuriX marks access unavailable locally and queues `DELETE /access-keys/{id}`; a known-key 404 is converged state. The maintenance pass queues one Telegram warning at 25%, 10%, and 5% remaining quota per key, then removes the key at the observed limit. Warning delivery is retried durably and does not gate enforcement. Existing sessions must be acceptance-tested against the deployed Outline version before promising an immediate disconnect.

Customers can use `/usage` or the **Usage** button to see each of their free,
trial, and paid keys with Outline-reported bytes used, configured limit,
remaining bytes, percentage, expiry, and local state. These figures come from
`GET /metrics/transfer`: they are trailing-30-day transfer accounting, not live
speed or a calendar-month usage ledger. A customer can never query another
customer's key statistics. In a multi-node fleet, these customer views query
only the endpoint(s) that own the customer's keys; an unrelated node outage is
therefore shown in admin health without blocking key retrieval.

Telegram displays persisted timestamps in `Asia/Yangon` as `DD Mon YYYY, HH:MM
MMT` by default, while the database and audit events remain UTC. Set
`AURIX_DISPLAY_TIMEZONE` to another IANA timezone when the operator or customer
base requires a different display zone; an invalid display-only value safely
falls back to UTC and does not prevent startup.

Outline key names are operator-readable and use UTC start time: `<username-or-telegram-id>-<tier>-<duration>-YYYYMMDDHHMM`, for example `min_user-FREE300MB-24hr-202608280520`. Telegram usernames are sanitized; accounts without a username fall back to their numeric Telegram ID. Renaming a key does not change its access URL.

Receipt images are evidence, not proof. AuriX stores each new raw image in a private Supabase Storage bucket and stores only its bucket/path, checksum, MIME type, size, extraction result, and review state in the database. Telegram file metadata remains as a compatibility fallback for older evidence. The upload is completed before the order enters `payment_submitted`; failed uploads remain retryable and are never shown in the admin review queue. Provider-aware AI triage checks completion, selected provider, exact amount/currency, the provider's visible reference label, timestamp relative to the original upload, and the configured merchant recipient. Clear mismatches are reject candidates; missing or ambiguous fields remain manual review. The LLM never approves a payment or credits a wallet. Staff tap **Verify Payment**, compare the candidate fields with the actual receiving account, and confirm; **Approve** appears only after verification. The underlying typed commands remain compatibility/recovery tools, not the primary workflow. In public mode, the commerce service itself rejects approval without verified evidence or a wallet reservation; the legacy text-only approval path exists only for explicit test fixtures.
Configure the bucket's lifecycle/retention rule separately after confirming the business and payment-record retention policy; the application does not silently delete evidence.

New orders, submitted receipts, receipt rejections, and newly opened managed-key
repairs create deduplicated, durable alerts for every active owner/admin who
has that event enabled. Each
staff account controls its own choices through `/notifications`; customer
confirmations and critical VPN-enforcement alerts are unaffected. Receipt images
remain private and are opened on demand from the alert rather than copied into
persistent admin chat history. Telegram distinguishes photos from image documents,
so AuriX preserves that media type and retries the alternate review method for
older evidence. `/receipts` remains the durable recovery queue.

The maintenance sender claims due alerts with a short delivery lease before
calling Telegram. A crashed sender leaves the row retryable after the lease
expires; a second healthy worker cannot claim the same row during that window.
This prevents normal concurrent duplicate sends while preserving at-least-once
delivery semantics.

Wallet events are immutable. A wallet top-up accepts an exact receipt amount only, credits the balance exactly once after human verification, and never provisions a VPN subscription. A subsequent wallet purchase records `reserve → capture`; rejection releases a reservation exactly once. Capture does not deduct the balance a second time. Receipt SHA-256 and Telegram file identity are rejected across different orders before vision processing; a low-threshold perceptual fingerprint now flags resized/re-encoded near-duplicates for manual review without hard-rejecting legitimate provider templates. Normalized provider transaction IDs remain the authoritative duplicate check at verification, and the database enforces canonical provider/reference uniqueness atomically (legacy case/spacing collisions stop startup for manual reconciliation). AI triage fails closed when a merchant profile is missing and always leaves financial approval to staff. `/wallet` shows the current projection and recent ledger events, while `/reconcile` reports balance mismatches and impossible order/job combinations.

Short-lived conversational prompts (custom top-up amount, staff receipt
verification details, receipt diagnostics, and owner admin entry) are also
stored as bounded, ten-minute workflow markers. A restart therefore cannot
silently lose the context of a reply; expired markers are ignored and pruned by
maintenance. The markers contain only IDs and action names, never screenshots,
access URLs, or credentials.

The receipt parser uses an OpenAI-compatible `/chat/completions` endpoint with a
vision-capable model. Configure the three `RECEIPT_LLM_*` variables only after
testing that endpoint with a synthetic receipt. If they are unset, unreachable,
or return invalid JSON, the bot records the screenshot for manual review and
does not guess a transaction ID.

For an OpenAI-compatible vision gateway, obtain the gateway API key from the
proxy owner, query its authenticated `/v1/models` endpoint, choose a
vision-capable route, and configure a private authenticated `/v1` base URL.
The public dashboard URL alone is insufficient: unauthenticated model requests
correctly return HTTP 401. Never put OAuth tokens in the bot environment.

SQLite fits one-process MVP on persistent local storage. Do not deploy DB onto an ephemeral filesystem. Run one bot process only; Telegram long polling and this SQLite workflow are not designed for replicas.

Setting `COMMERCE_DATABASE_URL` stores free/trial claims, Telegram update
deduplication, commerce, jobs, notifications, and audit state in PostgreSQL. It
does not make Telegram long polling or notification delivery replica-safe; keep
one bot process until the independent worker/webhook gate is completed.

**Production DB path:** Mount a persistent volume (e.g., `/var/lib/aurix-bot/`) and set `DATABASE_PATH=/var/lib/aurix-bot/bot.db`. Do not use `/tmp`, container layers, or Render free-tier disk — data is lost on restart.

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

Production multi-node fleets are declarative and health-gated. See
[`docs/FLEET_CICD_AND_DISASTER_RECOVERY.md`](docs/FLEET_CICD_AND_DISASTER_RECOVERY.md)
for CI/CD, one-command reconciliation, encrypted endpoint recovery, and the
safe automated-expansion contract.
