# AuriX Render deployment handoff

This runbook deploys the current AuriX MVP without requiring Codex and Render
to be reachable at the same time. Prepare and verify the repository first,
close any local copy of the bot, change to the network that can reach Render,
then perform only the dashboard steps in this document.

The deployment is intentionally one paid Render background worker with one
persistent disk and one SQLite database. Do not change it to a web service,
free instance, multiple instances, or ephemeral storage.

## 1. What this release contains

- Public 300 MiB claim once per rolling 24 hours.
- Public 3 GiB claim once per rolling 30 days.
- Paid 50 GiB / 30-day plan for 3,000 MMK.
- Paid 100 GiB / 30-day plan for 6,000 MMK.
- Screenshot-only payment submission, optional LLM field extraction, and
  mandatory human verification against the receiving account.
- Customer order, wallet, VPN, and usage views with inline buttons.
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

### Optional receipt-vision route

Automatic transaction-ID extraction requires all three values:

- `RECEIPT_LLM_BASE_URL`: base URL of an OpenAI-compatible API.
- `RECEIPT_LLM_MODEL`: a vision-capable model available at that endpoint.
- `RECEIPT_LLM_API_KEY`: its credential.

All three may instead remain blank. The bot will still accept screenshots and
an admin can manually type the verified transaction ID and amount. Model output
is untrusted evidence only and can never approve or credit a payment.

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
| `OUTLINE_API_URL` | Full secret HTTPS management URL |
| `OUTLINE_CERT_SHA256` | 64 hexadecimal characters |
| `AURIX_ACCESS_URL_KEY` | One persistent Fernet key |
| `DATABASE_PATH` | Keep Blueprint value `/var/data/bot.db` |

Keep these Blueprint defaults:

| Variable | MVP setting |
| --- | --- |
| `TRIAL_TELEGRAM_IDS` | blank (public free/trial access) |
| `COMMERCE_DATABASE_URL` | blank (single SQLite database) |
| `ALLOW_TEXT_PAYMENT_REFERENCES` | `0` |
| `RECEIPT_LLM_*` | all configured, or all blank |

Do not add `PORT`; a background worker does not serve HTTP. Do not remove the
disk merely to use a free plan: doing so loses claims, order state, wallets,
audit history, encrypted keys, dedupe records, and Telegram update offsets on
every replacement/restart.

## 7. Read the first deploy log

A healthy startup contains these lines, without secret values:

```text
Render preflight passed: persistent single-worker configuration is valid
Bot authorized: @your_bot_username
Outline connected: version ...
```

Expected non-fatal warning when the LLM values are blank:

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
6. Open Plans, select 50 GiB, and press the same upgrade action twice. Verify
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
burning 300 MiB merely to test the boundary. Confirm the bot records the reason,
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
per transferred GiB.
