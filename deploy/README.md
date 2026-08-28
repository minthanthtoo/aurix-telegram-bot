# AuriX staging deployment

This runbook targets the supplied DigitalOcean staging Droplet:

```text
Public IPv4: 139.59.122.170
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
  root@139.59.122.170 'id; uname -a; free -h; df -h /'
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
ADMIN_TELEGRAM_IDS=replace-with-owner-telegram-id
# Leave empty for public daily 300 MiB and monthly 3 GiB claims.
TRIAL_TELEGRAM_IDS=
OUTLINE_API_URL=replace-with-installer-output
OUTLINE_CERT_SHA256=replace-with-installer-output
AURIX_ACCESS_URL_KEY=replace-with-a-persistent-fernet-key
DATABASE_PATH=/var/lib/aurix-bot/bot.db
# Optional: set a reachable PostgreSQL URL for hosted commercial state.
COMMERCE_DATABASE_URL=
# Optional OpenAI-compatible vision endpoint for receipt parsing.
RECEIPT_LLM_BASE_URL=
RECEIPT_LLM_MODEL=
RECEIPT_LLM_API_KEY=
ALLOW_TEXT_PAYMENT_REFERENCES=0
```

Never paste this file into chat, Git, ordinary logs, or a support screenshot.
Generate `AURIX_ACCESS_URL_KEY` once and preserve it across restarts; it encrypts
stored Outline access URLs used for `/myvpn` and notification redelivery. Losing
the key makes old stored URLs unreadable and requires a controlled re-provision.
The bot validates the Outline certificate fingerprint before sending each
management request. The first live check should call pinned `GET /server` and
record only the non-secret Outline version.

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

Use `/start`, `/claim`, `/trial`, `/plans`, `/buy`, send a receipt screenshot,
`/receipts`, `/receipt`, `/verify`, `/approve`, `/myvpn`, and `/capacity` with an owner test
account. Verify the Outline key inventory before and after provisioning, quota
exhaustion, and expiry. Confirm the receiving-account transaction manually;
LLM extraction is not payment proof and must never supply the credited amount.
Revoke any installer-created or untracked
staging keys before opening the bot to additional users.

## Current deployment evidence

The application and fake-Outline regression tests pass locally. Deployment to
`139.59.122.170` is not asserted until SSH succeeds and the installed Outline
version, bot token, admin ID, firewall, and persistent disk are verified.
