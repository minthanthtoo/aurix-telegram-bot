# AuriX Render deployment handoff

This runbook deploys the current AuriX MVP without requiring Codex and Render
to be reachable at the same time. Prepare and verify the repository first,
close any local copy of the bot, change to the network that can reach Render,
then perform only the dashboard steps in this document.

The deployment is intentionally one paid Render background worker with one
persistent disk and one SQLite database. Do not change it to a web service,
free instance, multiple instances, or ephemeral storage.

## 1. What this release contains

- Public 300 MB claim once per rolling 24 hours.
- Public 3 GB claim once per rolling 30 days.
- Paid 50 GB / 30-day plan for 3,000 MMK.
- Paid 100 GB / 30-day plan for 6,000 MMK.
- Screenshot-only payment submission, production-gated LLM field extraction, and
  mandatory human verification against the receiving account.
- Private Supabase Storage receipt objects with database-only metadata and
  short-lived admin signed URLs; failed uploads remain retryable.
- Customer order, wallet, VPN, and usage views with inline buttons.
- Multiple independent paid entitlements per customer, each with its own key,
  quota, expiry, and provisioning job.
- Admin receipt review, approval/rejection, refund, retry, ledger, capacity,
  consistency, and quota/expiry enforcement views.
- Per-key Outline quota application, usage lookup, expiry/quota deletion,
  retry/escalation records, and encrypted access URLs.

Outline transfer totals and its data limit are rolling 30-day values. The bot's
30-day paid/trial expiry is a separate wall-clock rule. Deleting an Outline key
prevents later authentication, but the Management API has no documented
per-key command that guarantees an already-established transport is killed
immediately.

## 2. Files that make Render work

- `render.yaml` creates a Singapore background worker, Starter plan, one
  instance, and a 1 GB disk mounted at `/var/data`.
- `deploy/render_preflight.py` validates critical settings without printing
  their values before the bot starts.
- `requirements.txt` installs the encryption and optional PostgreSQL drivers.
- `app.py`, `commerce.py`, and `receipt_llm.py` are the runtime.
- `/var/data/bot.db` is the only SQLite state location on Render.

The source archive in `dist/` deliberately excludes `.env`, databases, Git
history, Python caches, and the archived design conversation. It is a backup of
the deployable source, not a backup of live customer state.

`render.yaml` is the durable paid-worker profile. `render-free.yaml` is an
experimental Web Service profile for a $0 pilot that stores all AuriX tables in
Supabase PostgreSQL and exposes `/healthz` for an external monitor. Use the
free profile only for controlled testing; it is subject to Render sleep/restart
and Supabase Free project limits.

## 3. Collect the six required values offline

Never add these values to Git, the source archive, screenshots, or support
messages.

### Telegram bot token

Use the token from BotFather. If it has ever been exposed publicly, revoke it
in BotFather and use the replacement.

### Numeric admin Telegram ID

Send `/whoami` to the existing AuriX bot before stopping it and record the
numeric result. A numeric ID is stable; a username is not. For multiple admins,
use comma-separated numbers without spaces, for example `123456789,987654321`.

### Complete Outline Management API URL

Copy the full URL shown by Outline Manager, including its secret path and final
slash. Treat the complete URL as a password. Do not use only the IP/port and do
not use an Outline client access key URL.

### Outline certificate SHA-256 fingerprint

From a trusted terminal, substitute only the Outline host and management port:

```sh
openssl s_client -connect OUTLINE_HOST:OUTLINE_PORT </dev/null 2>/dev/null \
  | openssl x509 -outform DER \
  | openssl dgst -sha256
```

Enter only the hexadecimal digest in Render. Colons and a `SHA2-256(stdin)=`
prefix are accepted by the runtime, but a plain 64-character lowercase digest
is easiest to audit. Repeat the command from another trusted network if the
certificate changed unexpectedly.

### Persistent AuriX encryption key

Generate this exactly once:

```sh
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store it in a password manager. Replacing it later makes previously stored
Outline access URLs unreadable; it is not a value to rotate casually.

### Receipt-vision route

Automatic transaction-ID extraction requires all three values:

- `RECEIPT_LLM_BASE_URL`: base URL of an OpenAI-compatible API.
- `RECEIPT_LLM_MODEL`: a vision-capable model available at that endpoint.
- `RECEIPT_LLM_API_KEY`: its credential.

For local/manual-review development all three may remain blank. Hosted Render
profiles set `RECEIPT_VISION_REQUIRED=1`; preflight then refuses to start until
all three are configured, the five merchant profiles are present, and the live
startup canary reaches the gateway. Model output is untrusted evidence only and
can never approve or credit a payment.

## 4. Verify and publish the source repository

Run locally from the repository root before switching networks:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest -v
python3 -m py_compile app.py commerce.py receipt_llm.py deploy/render_preflight.py
git status --short
```

Confirm `.env`, `*.db`, `data/`, and `dist/` are not staged. Then commit the
source files and push to a private GitHub, GitLab, or Bitbucket repository:

```sh
git add .env.example .gitignore README.md app.py commerce.py receipt_llm.py \
  requirements.txt render.yaml deploy docs assets brand test_app.py \
  test_commerce.py test_mvp.py
git diff --cached --check
git commit -m "Prepare AuriX MVP for Render"
git push
```

Do not use `git add -A` until you have reviewed the archived conversations and
other local files. They are not required by the worker.

## 5. Stop every old poller

Telegram permits only one reliable `getUpdates` consumer for this bot token.
Before the Render worker starts, stop local terminals, systemd services, Docker
containers, and VPS processes that run this bot. Examples:

```sh
sudo systemctl disable --now aurix-bot 2>/dev/null || true
ps aux | grep '[p]ython.*app.py'
```

Do not run a local smoke test with the production token after Render is live.
The application clears a pre-existing Telegram webhook at startup and then
uses long polling.

## 6. Create the Render Blueprint

After saving this guide, close Codex if your VPN route cannot reach both Codex
and Render. Switch to the Render-capable connection and perform these steps:

1. Open the Render dashboard.
2. Choose **New +**, then **Blueprint**.
3. Connect the private Git repository and select its deployment branch.
4. Render will read `render.yaml` and propose `aurix-telegram-bot`.
5. Confirm it is a **Background Worker**, region **Singapore**, plan
   **Starter**, instance count **1**, with a 1 GB disk at `/var/data`.
6. Enter each prompted secret. Do not paste surrounding quotes.
7. Create/apply the Blueprint and watch the first deploy log.

Required environment values:

| Variable | Required value/rule |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `ADMIN_TELEGRAM_IDS` | At least one positive numeric Telegram ID |
| `ADMIN_SCOPE_CLEANUP_IDS` | Optional one-time comma-separated IDs of removed admins whose old Telegram command scopes must be deleted |
| `OUTLINE_API_URL` | Full secret HTTPS management URL |
| `OUTLINE_CERT_SHA256` | 64 hexadecimal characters |
| `AURIX_ACCESS_URL_KEY` | One persistent Fernet key |
| `DATABASE_PATH` | Keep Blueprint value `/var/data/bot.db` |
| `SUPABASE_URL` | HTTPS URL of the Singapore Supabase project |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only service-role secret; never publish it |
| `SUPABASE_RECEIPTS_BUCKET` | Private bucket, normally `payment-receipts` |
| `RECEIPT_STORAGE_REQUIRED` | `1` |

Keep these Blueprint defaults:

| Variable | MVP setting |
| --- | --- |
| `TRIAL_TELEGRAM_IDS` | blank (public free/trial access) |
| `COMMERCE_DATABASE_URL` | blank (single SQLite database) |
| `SUPABASE_RECEIPTS_BUCKET` | `payment-receipts` (create it as private) |
| `RECEIPT_STORAGE_REQUIRED` | `1` |
| `ALLOW_TEXT_PAYMENT_REFERENCES` | `0` |
| `RECEIPT_VISION_REQUIRED` | `1` for hosted production |
| `RECEIPT_LLM_*` | all configured when vision is required |

Do not add `PORT`; a background worker does not serve HTTP. Do not remove the
disk merely to use a free plan: doing so loses claims, order state, wallets,
audit history, encrypted keys, dedupe records, and Telegram update offsets on
every replacement/restart.

## 7. Read the first deploy log

A healthy startup contains these lines, without secret values:

```text
Render preflight passed: single-worker persistent disk configuration and live dependencies are valid
Bot authorized: @your_bot_username
Outline connected: version ...
```

Local/manual-review development may show this warning when the LLM values are blank:

```text
WARNING: receipt vision extraction is disabled; screenshots require manual transaction entry.
```

Treat these as hard failures:

- `Render preflight failed`: fix the named environment-variable rule.
- `Telegram getMe failed`: bad token or outbound connectivity.
- `Outline readiness check failed`: wrong URL/path, wrong fingerprint,
  unreachable management port, or management firewall rejecting Render.
- repeated Telegram `Conflict` / `getUpdates` errors: another bot process is
  still running.
- `database is locked`: more than one instance/process is writing SQLite.

If Outline only allows the former VPS IP, the Render worker will not connect.
Allow the Render service's actual outbound network path before retrying. Keep
the management endpoint as restricted as your hosting arrangement allows; do
not publish the secret management URL.

## 8. First-run acceptance test

Use one owner/admin Telegram account and perform the sequence in order. Avoid
real customers until every expected state is visible.

1. Send `/start`; verify the customer button menu is clean.
2. Send `/whoami`; verify it matches `ADMIN_TELEGRAM_IDS`.
3. Send `/admin`; verify admin buttons appear only for the allowlisted ID.
4. Send `/claim`; verify a human-readable `FREE300MB-24hr` Outline key is
   created and `/usage` shows limit, used, remaining, expiry, and state.
5. Send `/trial`; verify a `TRIAL3GB-30d` key and no second monthly claim.
6. Open Plans, select 50 GB, and press the same upgrade action twice. Verify
   `/myorders` shows one open order, not duplicates.
7. Send a synthetic receipt screenshot. Verify the order becomes
   `review_pending`. If LLM parsing is enabled, treat its transaction ID as a
   suggestion only.
8. From `/admin`, open Receipts and the receipt detail. Check the receiving
   account independently, then record the real transaction ID and exact amount.
9. Approve the verified order. Verify activation begins only after Outline key
   creation and quota-setting succeed.
10. Use `/myvpn`, `/usage`, `/wallet`, `/order`, and `/myorders`; verify customer
    ownership boundaries and consistent stages.
11. Use `/capacity`, `/reconcile`, `/failed`, and `/enforcement`; the consistency
    scan should report no actionable issues.
12. Exercise reject and refund with test orders. A refund is recorded as AuriX
    wallet credit plus access revocation; it does not claim an external bank
    reversal occurred.

For quota enforcement acceptance, use a disposable small test key rather than
burning 300 MB merely to test the boundary. Confirm the bot records the reason,
hides the URL immediately, deletes the remote key, reports the result to the
customer/admin, and never resurrects the key after a lower rolling metric.

## 9. Backups, redeploys, and rollback

- Preserve `AURIX_ACCESS_URL_KEY` separately from Render.
- Use Render disk snapshots/backups suitable for your plan and test restoration
  before relying on them.
- Keep one worker instance during redeploys. Never run an old release and a new
  release simultaneously with the same bot token and database.
- A source rollback does not roll back SQLite schema/data. Take a disk snapshot
  before a risky future migration and restore only with the worker stopped.
- The `dist/*.tar.gz` archive can restore source files but cannot restore the
  Render disk or secret environment values.

## 10. Current release boundary

This configuration is a durable single-process MVP, not the final horizontally
scaled production architecture. Long polling, SQLite, one Outline server,
manual payment verification, and at-least-once Telegram notification delivery
remain deliberate limits. Dead-lettered notifications and terminal jobs are
visible to admins for manual recovery; they are not silently retried forever.

Before broad public launch, separately validate provider/payment/legal rules,
support ownership, privacy/retention, real backup restoration, measured Myanmar
network quality, actual quota cutoff timing on Outline 1.12.3, and server cost
per transferred GB.

## 11. Optional $0 pilot profile (Supabase + UptimeRobot)

This section is separate from the persistent MVP above. It is useful when a
paid Render worker is not yet available, but it is not a no-downtime or
customer-payment guarantee.

### Create Supabase storage

1. Create a Supabase project from the [Supabase dashboard](https://supabase.com/dashboard).
2. Choose the Free plan for testing.
3. In **Project Settings → Database**, copy a PostgreSQL connection string
   that includes SSL (`sslmode=require`). Keep it private.
4. Do not put the connection string in GitHub. Paste it into Render as the
   secret `COMMERCE_DATABASE_URL`.

The free profile uses the same PostgreSQL schema for free claims, trial claims,
Telegram update deduplication, orders, wallets, payment evidence, jobs,
notifications, audit events, and paid subscriptions. On first startup the bot
creates/migrates these tables automatically.

Supabase Free currently provides 500 MB of database space and pauses a project
after one week of inactivity. Render Free may also restart or sleep the Web
Service. Treat this profile as a test environment and keep an export before
any destructive experiment.

### Create the free Render service

Use **New → Blueprint**, connect the AuriX repository, and select
`render-free.yaml` as the Blueprint file if Render asks for a non-default file.
Do not replace `render.yaml`; that is the paid persistent profile.

Confirm:

```text
Service type: Web Service
Plan: Free
Region: Singapore
Instances: 1
Health check: /healthz
```

The free profile's start command is:

```text
python deploy/render_preflight.py --live && python -u deploy/render_web.py
```

`render_web.py` binds `0.0.0.0:$PORT`, starts `app.py` as a child process, and
returns HTTP 200 only while the bot process is alive. If the bot exits, the web
process exits as well so Render can restart it.

### Configure UptimeRobot

Create an HTTP monitor for:

```text
https://YOUR-FREE-SERVICE.onrender.com/healthz
```

Use the Free UptimeRobot interval of five minutes and expect HTTP 200. This
helps prevent the 15-minute idle sleep window, but it cannot prevent Render
maintenance/restarts, Supabase pauses, quota exhaustion, or network failure.

### Free-profile environment differences

Set:

```text
AURIX_STORAGE_MODE=postgres
COMMERCE_DATABASE_URL=<Supabase SSL PostgreSQL URL>
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server-only service-role key>
SUPABASE_RECEIPTS_BUCKET=payment-receipts
RECEIPT_STORAGE_REQUIRED=1
RECEIPT_VISION_REQUIRED=1
```

Do not set `/var/data/bot.db` in this profile; there is no persistent Render
disk. Leave `TRIAL_TELEGRAM_IDS` blank and keep
`ALLOW_TEXT_PAYMENT_REFERENCES=0`. The remaining Telegram, Outline, Fernet, LLM,
and merchant-profile variables are the same as the paid profile.

Create `payment-receipts` as a private bucket before testing. New receipt
objects are stored under `orders/{order-id}/{evidence-id}.{extension}`. Only
the object path/checksum/metadata enter PostgreSQL; admins receive a short-lived
signed URL. Failed uploads remain retryable and are excluded from `/receipts`.
Configure object lifecycle/retention in Supabase only after the payment-record
retention policy is approved; this release does not silently delete evidence.

### Latency workflow

Keep the Render service, Supabase project, and Outline management server in
Singapore when the customer base is primarily in Myanmar or nearby APAC. This
reduces network round trips, but it does not remove connection setup or remote
API work. The worker now keeps a small PostgreSQL connection pool and polls
Telegram continuously while a dedicated maintenance thread runs quota, expiry,
job, and notification work. Free and paid enforcement share one Outline metrics
snapshot per pass instead of making duplicate management API requests.

For a short diagnostic window, set `AURIX_LATENCY_LOG=1` in Render. The logs
then report Telegram, Outline, Postgres checkout/transaction, update-handler,
and maintenance timings without request payloads or credentials. Return it to
`0` after measuring. `AURIX_MAINTENANCE_INTERVAL_SECONDS=60` is the default;
increase it only to reduce maintenance load, not to repair command latency.

For a long-lived Render worker, use the Supabase **Session** pooler (port
`5432`) or a direct connection when IPv6 is available. Use the Transaction
pooler (port `6543`) for short-lived/serverless workloads; the application has
prepared statements disabled for compatibility.

### Free-profile acceptance boundary

Before any real use, test `/start`, `/claim`, `/trial`, `/usage`, `/myorders`,
receipt review, `/reconcile`, and a restart. Verify the data remains in
Supabase. Do not accept real payments until the service survives a forced
restart and the Supabase project has an appropriate backup/export process.

Never use a Telegram channel as the database. If desired, add an encrypted
append-only audit copy later, but keep PostgreSQL authoritative for balances,
orders, entitlement, and access state.
