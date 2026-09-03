# AuriX staging deployment

For multi-node bootstrap, reconciliation, encrypted endpoint recovery,
automated expansion boundaries, and full revival, use the canonical
[fleet CI/CD and disaster-recovery guide](../docs/FLEET_CICD_AND_DISASTER_RECOVERY.md).

This runbook targets the supplied DigitalOcean staging Droplet:

```text
Public IPv4: 157.245.63.95
Region: SGP1
Image: Ubuntu 24.04 LTS x64
Size: 1 vCPU / 1 GB RAM / 25 GB disk
```

The public IP is infrastructure metadata, not an Outline management credential.
Do not invent an `OUTLINE_API_URL`; the Outline installer emits the secret path
and certificate fingerprint after installation.

## 1. Verify access before making changes

From the operator workstation, use the approved SSH key and a short timeout:

```sh
ssh -i /Users/min/.ssh/digitalocean_1994 \
  -o BatchMode=yes -o ConnectTimeout=10 \
  root@157.245.63.95 'id; uname -a; free -h; df -h /'
```

If this times out, fix the Droplet firewall, cloud firewall, SSH service, or
source-network route first. Do not repeatedly retry and do not change firewall
rules blindly.

## 2. Install the data plane and create the service account

Use the current official Outline installation procedure and record its exact
release/version. Capture the emitted `apiUrl` and `certSha256` into a root-only
file; both are sensitive, and `apiUrl` contains the management secret path.

Then create an unprivileged bot account and persistent state directory:

```sh
apt-get update
apt-get install -y python3-venv
useradd --system --home-dir /opt/aurix-bot --create-home --shell /usr/sbin/nologin aurix
install -d -o aurix -g aurix -m 0750 /opt/aurix-bot /var/lib/aurix-bot
install -d -o root -g aurix -m 0750 /etc/aurix-bot
python3 -m venv /opt/aurix-venv
```

Copy the repository files to `/opt/aurix-bot` and install the unit:

```sh
install -o root -g root -m 0644 deploy/aurix-bot.service /etc/systemd/system/aurix-bot.service
/opt/aurix-venv/bin/pip install --requirement /opt/aurix-bot/requirements.txt
```

## 3. Configure secrets outside Git

Create `/etc/aurix-bot/aurix.env` with mode `0640` and owner `root:aurix`:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-a-staging-bot-token
OWNER_TELEGRAM_ID=replace-with-owner-telegram-id
ADMIN_TELEGRAM_IDS=
# Optional bootstrap override. Prefer Owner Controls → Choose Control Group.
AURIX_CONTROL_GROUP_ID=
# Optional one-time cleanup list for Telegram command scopes of removed admins.
ADMIN_SCOPE_CLEANUP_IDS=
# Leave empty for public daily 300 MiB and monthly 3 GiB claims.
TRIAL_TELEGRAM_IDS=
OUTLINE_API_URL=replace-with-installer-output
OUTLINE_CERT_SHA256=replace-with-installer-output
# For multiple servers, replace the two lines above with root-only
# OUTLINE_SERVERS_JSON and choose a legacy fallback ID.
# Enable after normalizing allocations so all plan+tier slots fit the saleable
# key capacity (max_keys minus reserved_keys).
# AURIX_FLEET_STRICT_ALLOCATION_VALIDATION=1
# OUTLINE_SERVERS_JSON=[{"id":"sg-a","label":"Singapore A","provider_resource_id":"<droplet-id>","api_url":"https://host:port/secret","cert_sha256":"64hex"}]
# OUTLINE_DEFAULT_SERVER_ID=sg-a
# For the legacy single-server form, optionally record its numeric Droplet ID.
# OUTLINE_PROVIDER_RESOURCE_ID=<droplet-id>
# Optional provider identity metadata. Use actual DigitalOcean Droplet IDs,
# never public IP addresses. Attaching tags to existing Droplets requires the
# separately approved droplet:update scope; IDs are the current safe bridge.
# AURIX_MANAGED_DROPLET_IDS=<sg-a-droplet-id>,<sg-b-droplet-id>
# AURIX_MANAGED_DROPLET_TAG=aurix-vpn-node
# Leave off until node-two verification is complete; this only exposes the
# owner/admin intent button and does not itself create infrastructure.
# AURIX_INFRASTRUCTURE_QUEUE_ENABLED=0
AURIX_SERVER_HEALTH_MAX_AGE_SECONDS=900
AURIX_ACCESS_URL_KEY=replace-with-a-persistent-fernet-key
DATABASE_PATH=/var/lib/aurix-bot/bot.db
# Optional: set a reachable PostgreSQL URL for hosted commercial state.
COMMERCE_DATABASE_URL=
# Required for hosted receipt evidence. Use the Supabase project URL and a
# server-only service-role key; do not use an anon/publishable key.
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=replace-with-service-role-key
SUPABASE_RECEIPTS_BUCKET=payment-receipts
# Optional encrypted recovery mirror. Use a separate private bucket, and do
# not configure this together with AURIX_BACKUP_OBJECT_STORE_*.
# AURIX_BACKUP_SUPABASE_BUCKET=aurix-recovery
# AURIX_BACKUP_SUPABASE_PREFIX=production
# AURIX_BACKUP_STORAGE_TIMEOUT_SECONDS=45
# AURIX_BACKUP_STORAGE_MAX_MB=512
RECEIPT_STORAGE_REQUIRED=1
# Optional OpenAI-compatible vision endpoint for receipt parsing. For the
# standalone Antigravity proxy use https://your-gateway.example/v1, the gateway
# API key, and antigravity/<vision-model-id> returned by GET /v1/models.
RECEIPT_LLM_BASE_URL=
RECEIPT_LLM_MODEL=
RECEIPT_LLM_API_KEY=
RECEIPT_LLM_FALLBACK_MODELS=
# Optional model selection: first_acceptable (low cost), rank_all, or
# consensus (requires at least one fallback model; disagreements stay manual).
RECEIPT_LLM_SELECTION_MODE=first_acceptable
PAYMENT_RECIPIENTS_JSON='{"kbzpay":{"names":["MERCHANT NAME"],"accounts":["1234"]},"wavepay":{"names":["MERCHANT NAME"],"accounts":["09123456789"]},"ayapay":{"names":["MERCHANT NAME"],"accounts":["1234"]},"uabpay":{"names":["MERCHANT NAME"],"accounts":["09123456789"]},"cbpay":{"names":["MERCHANT NAME"],"accounts":[]}}'
ALLOW_TEXT_PAYMENT_REFERENCES=0
```

Never paste this file into chat, Git, ordinary logs, or a support screenshot.
Generate `AURIX_ACCESS_URL_KEY` once and preserve it across restarts; it encrypts
stored Outline access URLs used for `/myvpn` and notification redelivery. Losing
the key makes old stored URLs unreadable and requires a controlled re-provision.
The bot validates the Outline certificate fingerprint before sending each
management request. The first live check should call pinned `GET /server` and
record only the non-secret Outline version.

Create a private Supabase Storage bucket named `payment-receipts` before the
first hosted deploy. Keep Storage object policies closed to public reads; the
bot uses the service-role key server-side and gives admins only short-lived
signed URLs when rendering receipt evidence. The database stores the object
path and checksum, never the image bytes. If an upload fails, the order remains
open for retry and the evidence is hidden from `/receipts` until storage is
confirmed. Configure a Supabase lifecycle/retention rule only after the
business and payment-record retention policy is approved; the bot does not
silently delete evidence.

High-impact administrator actions use database-backed, single-use five-minute
confirmations. The confirmation includes the current order/receipt state and
is rejected if that state changes before the click. This remains safe across a
Render restart; do not enable multiple workers until PostgreSQL is configured
and the challenge/concurrency tests pass.

On this 1-GB staging Droplet, leave `COMMERCE_DATABASE_URL` empty unless a
separate PostgreSQL service is already provisioned and its resource budget is
known. When set, the free-claim SQLite database remains at `DATABASE_PATH`,
while orders, payments, subscriptions, jobs, notifications, and audit state use
PostgreSQL.

## 4. Firewall and service checks

Before live customer testing:

- restrict SSH to the operator source IP or a private access path;
- restrict the Outline management port to the bot host/private path;
- allow assigned Outline access-key TCP and UDP ports from clients;
- ensure the staging bot token and admin allowlist are not shared with production;
- capture a pre-test `GET /access-keys` inventory;
- enable and start the service, then inspect logs without printing URLs.

```sh
systemctl daemon-reload
systemctl enable --now aurix-bot
systemctl --no-pager --full status aurix-bot
journalctl -u aurix-bot -n 100 --no-pager
```

Install the guarded infrastructure worker separately. It has its own root-only
environment containing the scoped DigitalOcean token and defaults to no
provider mutations:

```sh
install -d -o root -g root -m 0700 /etc/aurix-infrastructure /var/lib/aurix-infrastructure
install -o root -g root -m 0644 deploy/aurix-infrastructure-worker.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/aurix-infrastructure-worker.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aurix-infrastructure-worker.timer
```

The worker only makes a node customer-eligible after a separate activation
gate. By default it stops at `awaiting_verification`. An optional
`AURIX_INFRASTRUCTURE_AUTO_ACTIVATION_ENABLED=1` gate may be used when the
new resource is already declared in `AURIX_FLEET_NODES_JSON` with the exact
provider resource ID and public IP, and the pinned `AURIX_FLEET_KNOWN_HOSTS`
trust file is available. The worker then runs the normal fleet reconciler and
records a durable `endpoint_activated` event. It never uses `ssh-keyscan`,
`StrictHostKeyChecking=accept-new`, cloud-init secrets, or a merely active VM
as proof of identity. If any prerequisite is missing, the job remains
`awaiting_verification` for a later pass.

Do not set `AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=1` until provider inventory,
budget, Outline installation, firewall restrictions and canary tests have passed.

Before declaring a release production-ready, run the source-controlled
acceptance audit. It is intentionally a gate: warnings for allocation policy,
DNS, canaries, or sustained observation keep the result non-passing.

```sh
python deploy/production_acceptance.py \
  --env-file /etc/aurix-bot/aurix.env \
  --verify-archives --live
```

Use `/start`, `/claim`, `/trial`, `/plans`, `/buy`, send a receipt screenshot,
`/receipts`, `/receipt`, `/verify`, `/approve`, `/myvpn`, and `/capacity` with an owner test
account. Verify the Outline key inventory before and after provisioning, quota
exhaustion, and expiry. Confirm the receiving-account transaction manually;
LLM extraction is not payment proof and must never supply the credited amount.
Revoke any installer-created or untracked
staging keys before opening the bot to additional users.

## Current deployment evidence

The application regression suite and pinned Outline readiness checks must pass
before every deployment to `157.245.63.95`. The GitHub-gated workflow below
records the deployed commit and retains rollback releases.

## GitHub-gated automatic deployment

The DigitalOcean bot does not update merely because a commit exists locally or
on GitHub. Install the repository's timer once to make GitHub `main` the release
source. The timer polls every two minutes and deploys only when the `safety-net`
GitHub Actions check has completed successfully for that exact commit.

Each accepted commit is built under `/opt/aurix-releases/<full-commit-sha>` with
its own virtual environment. The deployer compiles the release, runs the full
test suite, validates the production configuration and live Telegram,
Supabase, receipt-vision, and database dependencies, then atomically changes
`/opt/aurix-current`. It restarts one `aurix-bot` process and requires fresh
`Bot authorized` and `Outline connected` startup evidence. A failed release
restores the previous symlink and restarts the previous version.

One-time bootstrap, run from a checked-out, already-tested release:

```sh
install -d -o root -g root -m 0755 /var/lib/aurix-deploy /opt/aurix-releases
install -d -o root -g root -m 0755 /opt/aurix-bot/deploy
install -o root -g root -m 0755 \
  deploy/digitalocean_autodeploy.py \
  /opt/aurix-bot/deploy/digitalocean_autodeploy.py
ln -sfn /opt/aurix-venv /opt/aurix-bot/.venv
ln -sfn /opt/aurix-bot /opt/aurix-current
install -o root -g root -m 0644 deploy/aurix-bot.service \
  /etc/systemd/system/aurix-bot.service
install -o root -g root -m 0644 deploy/aurix-autodeploy.service \
  /etc/systemd/system/aurix-autodeploy.service
install -o root -g root -m 0644 deploy/aurix-autodeploy.timer \
  /etc/systemd/system/aurix-autodeploy.timer
systemctl daemon-reload
systemctl restart aurix-bot
systemctl enable --now aurix-autodeploy.timer
systemctl start aurix-autodeploy.service
```

Production deployment is deliberately blocked unless private receipt storage
and the vision parser are configured and reachable. Keep these in
`/etc/aurix-bot/aurix.env`, never Git:

```dotenv
RECEIPT_STORAGE_REQUIRED=1
SUPABASE_URL=https://PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=server-side-secret
SUPABASE_RECEIPTS_BUCKET=payment-receipts
# Optional: use a separate private Supabase bucket for encrypted recovery.
# AURIX_BACKUP_SUPABASE_BUCKET=aurix-recovery
# AURIX_BACKUP_SUPABASE_PREFIX=production
RECEIPT_LLM_BASE_URL=https://vision-gateway.example/v1
RECEIPT_LLM_MODEL=vision-model-id
RECEIPT_LLM_API_KEY=gateway-secret
```

If using the optional Supabase recovery backend, bootstrap its separate private
bucket once before enabling the off-site requirements:

```sh
/opt/aurix-current/.venv/bin/python /opt/aurix-current/deploy/recovery_storage.py \
  ensure --env-file /etc/aurix-bot/aurix.env
```

Operational checks:

```sh
systemctl list-timers aurix-autodeploy.timer
journalctl -u aurix-autodeploy -n 100 --no-pager
readlink -f /opt/aurix-current
cat /var/lib/aurix-deploy/deployed-sha
```

Do not run a Render service, a local bot, or a second VPS with the same Telegram
token. Git auto-deployment changes code delivery; it does not make Telegram
long polling multi-instance safe.

For multi-server admission, per-tier/per-plan capacity, partial-outage behavior,
and the separately gated DigitalOcean provisioning controller, follow
[`docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md`](../docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md).
Provider mutation must not run inside `aurix-bot`; keep its token in a distinct
operator worker environment and leave it disabled until the documented budget
and verification gates are approved.
