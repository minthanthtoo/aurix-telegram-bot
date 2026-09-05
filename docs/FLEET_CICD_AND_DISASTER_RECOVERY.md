# AuriX Fleet CI/CD and Disaster Recovery

This is the canonical operating guide for every Outline endpoint. The Git
repository plus one private environment file and the encrypted state/database
backups are sufficient to rebuild the control plane and reconcile the fleet.
The private `.env` can carry base64 recovery copies of the fleet SSH key and
pinned `known_hosts`; the reconciler materializes them mode `0600`. Base64 is
not encryption, so this file belongs in an offline secret store, never Git. No
bot, database, provider, or receipt credential is copied to a VPN node.

## State ownership

| State | Authoritative location | Recovery source |
|---|---|---|
| Application and node bootstrap code | GitHub `main` after CI | Git clone |
| Node topology, ports and capacity | `AURIX_FLEET_NODES_JSON` in private `.env` | Encrypted/offline `.env` copy |
| Telegram, database and provider secrets | Control-plane `.env` only | Secret-manager/offline copy |
| Outline management identity and access keys | `/opt/outline` on each node | Encrypted fleet backups |
| Customer/order/key ownership | persistent SQLite/PostgreSQL | normal database backup |

An Outline key is bound to the node's persisted Shadowbox state. Reinstalling a
node creates a different server and cannot revive existing keys. Both the
commerce database and encrypted `/opt/outline` backups are therefore mandatory.
Legacy official installations rooted at `/root/shadowbox` are detected and
backed up in place; new installations use `/opt/outline`.

## Declarative node schema

`AURIX_FLEET_NODES_JSON` is an array. Each enabled object has:

- `id`, `label`, literal `host`, `api_port`, and public `keys_port`;
- `provider`, optional provider `provider_resource_id`, and `region`;
- SSH user/port (root/22 by default); the private key is global and never in JSON;
- `max_keys`, `reserved_keys`, and optional `monthly_traffic_bytes`;
- optional stable `dns_name` (a fully-qualified hostname managed by the DNS
  synchronizer; the SSH/management `host` remains a literal IP);
- exact `tier_slots` for `FREE300MB`, `FREE3GB`, `PROMO`;
- exact `plan_slots` for `basic_50gb`, `standard_100gb`;
- `enabled` and optional `swap_mb`.

Missing tier or known-plan allocations become zero. This is fail-closed: adding
a node does not sell capacity until allocation is explicit. A disabled node is
rejected because silently removing its API would orphan active keys. Set all
allocations to zero, drain the keys, and only then remove it from the manifest. See
[`../.env.fleet.example`](../.env.fleet.example) for valid test data.

## One-time trust seed

```bash
install -d -o root -g root -m 0700 /etc/aurix-fleet
ssh-keygen -t ed25519 -N '' -f /etc/aurix-fleet/automation_ed25519
install -o root -g root -m 0600 /dev/null /etc/aurix-fleet/known_hosts
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Install the public key as a root authorized key on each current node. Verify
each SSH host fingerprint through the provider console before appending it to
`known_hosts`; `ssh-keyscan` alone does not authenticate a new host. Put the
manifest and fleet variables from `.env.example` in
`/etc/aurix-bot/aurix.env`, mode `0600`. Keep offline copies of that file, the
backup key, and verified host fingerprints. Never commit them.

For portable revival, set `AURIX_FLEET_SSH_PRIVATE_KEY_B64` and
`AURIX_FLEET_KNOWN_HOSTS_B64` from those two files. The normal path variables
remain the materialization destinations. If inline copies are omitted, the
files must already exist.

## Validate and reconcile

```bash
cd /opt/aurix-current
.venv/bin/python deploy/fleet_reconcile.py validate --env-file /etc/aurix-bot/aurix.env
.venv/bin/python deploy/fleet_reconcile.py check --env-file /etc/aurix-bot/aurix.env
.venv/bin/python deploy/fleet_reconcile.py reconcile --env-file /etc/aurix-bot/aurix.env
```

For a fast, read-only incident check, run the sanitized Outline diagnostic:

```bash
.venv/bin/python deploy/outline_diagnostics.py \
  --env-file /etc/aurix-bot/aurix.env --allow-partial
```

It verifies the pinned management API, reports Outline version/key and metrics
counts, and tests the TCP data port advertised by up to eight keys per node.
The output contains only node IDs, public host/port metadata, status, latency,
and exception types; management paths, certificate fingerprints, access URLs,
and transfer amounts are never printed. `--allow-partial` is useful during a
node outage because it returns success when at least one endpoint remains
healthy while still showing every failed node.

Reconciliation takes a global lock and, for each node:

1. connects with an explicit SSH key and strict pinned `known_hosts`;
2. applies firewall/SSH/swap policy and reuses a healthy Outline installation;
3. on a blank VM only, verifies and executes the official installer pinned to a
   specific Git commit and SHA-256;
4. reads management identity through SSH, validates host, port, secret path and
   fingerprint, then proves `/server` and `/access-keys` over pinned TLS;
5. atomically rebuilds `OUTLINE_SERVERS_JSON` without logging secret URLs;
6. refreshes inventory and applies exact capacity only while every node is healthy;
7. restarts the bot when configuration changed. Failure restores the prior
   environment and restarts that known configuration.

It never deletes, migrates, or silently recreates existing keys. Unsafe partial
Outline state stops reconciliation and requires restore.

## CI/CD and timers

`.github/workflows/ci.yml` is the source-level gate for every push and pull
request. It runs the full Python suite on Python 3.12 and 3.13, compilation,
Ruff, deployment-script shell checks, and merge-marker/whitespace checks. A
provider worker or Render deployment must use a commit that passed this gate;
the workflow never receives production secrets and never mutates infrastructure.

GitHub CI compiles and lints fleet code, syntax-checks the remote bootstrap,
validates a sample manifest, and runs all tests. The existing DigitalOcean
autodeployer activates only a CI-successful `main` commit and rolls back failed
bot startup. It installs versioned fleet units and, when a fleet manifest is
configured, enables:

- `aurix-fleet-reconcile.timer`, every ten minutes and after boot;
- `aurix-fleet-backup.timer`, daily encrypted state capture;
- `aurix-infrastructure-worker.timer`, provider request reconciliation.
  When the standalone HTTPS callback is selected, the recovery script also
  installs/enables `aurix-fleet-registration.service` from the same release.

```bash
systemctl list-timers 'aurix-*'
systemctl status aurix-fleet-reconcile.service
journalctl -u aurix-fleet-reconcile.service -n 100 --no-pager
```

Logs expose node IDs, addresses, versions and counts, but not management paths,
fingerprints, access URLs, keys, or provider secrets.

### Stable endpoint DNS

When a node has a `dns_name`, the fleet reconciler can maintain its DNS-only A
or AAAA record from the manifest. Cloudflare is currently supported. Create a
least-privilege API token with DNS edit permission for the zone and set:

```dotenv
AURIX_DNS_PROVIDER=cloudflare
AURIX_DNS_ZONE_ID=...
AURIX_DNS_API_TOKEN=...
AURIX_DNS_TTL=300
AURIX_DNS_PROXIED=0
AURIX_DNS_REQUIRE=1
```

Preview changes without contacting the provider:

```bash
.venv/bin/python deploy/dns_records.py sync \
  --env-file /etc/aurix-bot/aurix.env --dry-run
```

After every successful fleet reconciliation, configured records are upserted
automatically. A direct sync is also available:

```bash
.venv/bin/python deploy/dns_records.py sync \
  --env-file /etc/aurix-bot/aurix.env
```

The synchronizer refuses proxied records because Outline/Shadowsocks is not
HTTP traffic. It never deletes unrelated DNS records and refuses ambiguous
duplicate records. The recovery audit validates the provider configuration and
every node's `dns_name`; a missing DNS configuration remains a warning until
the operator chooses to make endpoint continuity mandatory. Set
`AURIX_DNS_REQUIRE=1` to turn that warning into a fail-closed deployment and
recovery gate.

## Automated expansion

VM creation and endpoint activation are separate idempotent state machines. The
DigitalOcean worker creates only within its region, size, image, daily-rate and
monthly-budget allowlists. A new resource remains unallocatable until:

1. it is in the reviewed fleet manifest;
2. approved cloud-init/provider bootstrap seeds its automation public key and
   an independently verified SSH host fingerprint;
3. reconciliation installs/discovers Outline and pinned health passes;
4. capacity and all plan/tier slots are explicit;
5. the bot observes a fresh healthy inventory snapshot.

For a fully unattended provider hand-off, enable the separately gated
`AURIX_FLEET_AUTO_REGISTRATION_ENABLED` flow and expose the credential-free
HTTPS `POST /fleet/register` endpoint from Render (`deploy/render_web.py`) or
the standalone TLS service (`deploy/fleet_registration_server.py`). Cloud-init
contains only a short-lived one-time token. The callback encrypts the node's
Outline identity and SSH host key; the worker binds both to the provider's
current IP, runs the same pinned reconciler, and consumes the token only after
health/policy success. A callback or reconcile failure leaves the resource
waiting and rolls back the manifest/known-hosts files.

For providers lacking API or cloud-init, VM creation is not falsely described
as automatic. Once a blank trusted VM is reachable, the same provider-neutral
reconciler owns all remaining steps. Future provider adapters return resource
ID, literal IP, SSH fingerprint, lifecycle state and billing estimate; they do
not write the bot environment.

## Backup and complete revival

```bash
.venv/bin/python deploy/fleet_backup.py backup --node all --env-file /etc/aurix-bot/aurix.env
```

Backups default to `/var/lib/aurix-fleet/backups/<node-id>/`, mode `0600`, with
14 retained copies. A size limit prevents disk exhaustion. Metadata contains
hashes and timestamps, never management credentials.

The control-plane disk is not an offsite backup. Prefer S3-compatible object
storage for the offsite recovery store:

```dotenv
AURIX_BACKUP_OBJECT_STORE_URL=s3://bucket/aurix-production
AURIX_BACKUP_OBJECT_STORE_ENDPOINT=https://account.r2.cloudflarestorage.com
AURIX_BACKUP_OBJECT_STORE_REGION=auto
AURIX_BACKUP_OBJECT_STORE_ACCESS_KEY_ID=...
AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY=...
```

This shape works for Cloudflare R2, Backblaze B2 S3, DigitalOcean Spaces, and
AWS S3. If object storage is unavailable, use
`AURIX_FLEET_BACKUP_OFFSITE_DIR` and `AURIX_DATABASE_BACKUP_OFFSITE_DIR` as
absolute private mount or synced paths. Set
`AURIX_FLEET_BACKUP_REQUIRE_OFFSITE=1` and
`AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE=1` in production; then backups and
verification fail closed if the offsite target is missing.
`*_OFFSITE_RETENTION` controls offsite pruning and can be longer than local
retention. Object-store backups prune complete archive/metadata pairs after
each successful upload, including the Supabase backend, so a daily schedule
does not grow without bound. Retention values are positive integers capped at
3,650 copies; malformed values fail the backup rather than silently disabling
retention.

Supabase Storage is also supported when the project already hosts the bot's
receipt evidence. Create a second **private** bucket (for example
`aurix-recovery`) and configure the same server-side Supabase URL/key plus an
explicit backup bucket:

```dotenv
SUPABASE_URL=https://PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=server-side-secret
SUPABASE_RECEIPTS_BUCKET=payment-receipts
AURIX_BACKUP_SUPABASE_BUCKET=aurix-recovery
AURIX_BACKUP_SUPABASE_PREFIX=production
AURIX_BACKUP_STORAGE_TIMEOUT_SECONDS=45
AURIX_BACKUP_STORAGE_MAX_MB=512
```

The backup bucket must differ from `SUPABASE_RECEIPTS_BUCKET`; this prevents a
retention or cleanup mistake from mixing encrypted recovery archives with
payment evidence. Configure either this Supabase backend or the S3-compatible
backend, never both. Uploads are immutable (`x-upsert=false`), encrypted before
leaving the control plane, and verified by downloading/decrypting the newest
archive. The recovery audit treats an explicit backup bucket as configured, so
partial credentials fail closed rather than silently falling back to local
disk. Supabase's service-role key remains server-only and is never returned to
Telegram or included in readiness output.

On first setup, run the explicit bootstrap command once. It creates the bucket
only if it is missing and refuses a public bucket; it never runs implicitly
from the bot or backup timer. A full control-plane recovery runs the same
private-bucket check automatically before archive verification, so a missing
bucket does not require an operator handoff:

```bash
.venv/bin/python deploy/recovery_storage.py ensure \
  --env-file /etc/aurix-bot/aurix.env
```

Verify the recovery set after scheduled backups and before risky changes:

```bash
.venv/bin/python deploy/fleet_backup.py verify --node all --env-file /etc/aurix-bot/aurix.env
```

`verify` decrypts the newest local archive for each node, validates the tar
contents, checks metadata hashes, and repeats the same checks against the
offsite copy when configured. The local project reserves the ignored
`.fleet-backups/` directory for operator-side encrypted copies; encrypted
archives must still never be committed.

On a fresh control-plane host, a local node archive may not exist yet. In that
case verification accepts the newest authenticated off-site node archive
directly; when both copies exist, both are checked.

Run the full sanitized readiness audit before declaring recovery complete:

```bash
.venv/bin/python deploy/recovery_readiness.py \
  --env-file /etc/aurix-bot/aurix.env \
  --verify-archives
```

The readiness audit prints only variable names, statuses, and sanitized detail.
It fails when required bot/payment/vision secrets are missing, when database
recovery is not externalized, when fleet backup encryption/offsite settings are
invalid, or when provider/DNS automation is partially configured. Warnings mean
the system can run but is not yet a true no-manual rebuild.

Fleet reconciliation treats the explicit `--env-file` as authoritative over
inherited systemd or shell variables. This prevents stale endpoint, allocation,
or SSH-trust values from shadowing the reviewed recovery configuration.

Commerce database backups use the same fail-closed pattern. SQLite deployments
use an authenticated snapshot; PostgreSQL deployments use an encrypted custom
`pg_dump` archive through the same command and timer:

```bash
.venv/bin/python deploy/database_backup.py backup --env-file /etc/aurix-bot/aurix.env
.venv/bin/python deploy/database_backup.py verify --env-file /etc/aurix-bot/aurix.env
```

When `COMMERCE_DATABASE_URL` is set, `database_backup.py` dispatches to the
PostgreSQL wrapper. It passes credentials through a mode-0600 temporary
`.pgpass` file (never the process argument list), verifies archives with
`pg_restore --list`, and mirrors the encrypted archive/metadata pair to the
configured off-site backend. A restore is deliberately explicit:

```bash
.venv/bin/python deploy/database_backup.py restore \
  --env-file /etc/aurix-bot/aurix.env \
  --confirm-postgres
```

Add `--allow-existing` only for an intentional replacement of an existing
PostgreSQL database. The recovery script installs `postgresql-client` on a
fresh Ubuntu host when the PostgreSQL mode is selected. The
`aurix-database-backup.timer` is installed automatically for both SQLite and
PostgreSQL deployments.
Set `AURIX_DATABASE_BACKUP_REQUIRE_OFFSITE=1` after configuring object storage
or mounting/syncing `AURIX_DATABASE_BACKUP_OFFSITE_DIR`; otherwise the timer can
create local encrypted backups but readiness remains incomplete.

PostgreSQL verification is recovery-source aware: if a rebuilt control-plane
host has no local archive yet, `database_backup.py verify` verifies the newest
authenticated off-site archive directly. If a local archive exists, it is
verified as well, so local corruption cannot be hidden by a healthy mirror.
Verification checks both ciphertext and plaintext SHA-256 metadata before
running `pg_restore --list`.
Passing `--verify-archives` to the readiness audit therefore proves the actual
encrypted archive path rather than only checking that a database URL is
syntactically present.

On a fresh control-plane host, `recover_control_plane.sh` automatically
restores the newest authenticated local/off-site SQLite archive when
`DATABASE_PATH` is absent. The restore is atomic, creates the destination with
private permissions, and refuses to overwrite an existing database. To run a
deliberate operator restore, provide an exact path confirmation:

```bash
.venv/bin/python deploy/database_backup.py restore \
  --confirm-path /var/lib/aurix-bot/bot.db \
  --env-file /etc/aurix-bot/aurix.env
```

An existing database requires the additional `--allow-existing` flag and must
be restored only while the bot is stopped; the previous file is retained as a
timestamped rollback copy.

Restore is deliberately explicit and replaces current remote Outline state. It
validates every tar path, rejects links/devices/traversal, preserves a remote
rollback directory, restores, starts Shadowbox, and checks identity:

```bash
.venv/bin/python deploy/fleet_backup.py restore \
  --node sg-a --confirm-node sg-a \
  --archive /var/lib/aurix-fleet/backups/sg-a/TIMESTAMP.tar.gz.fernet \
  --env-file /etc/aurix-bot/aurix.env
```

Full control-plane revival order:

1. provision Ubuntu 24.04 and restore the recovery entrypoint plus private
   `.env`, fleet SSH key, verified `known_hosts`, backup key, and encrypted node
   archives from the offsite recovery store; the recovery entrypoint restores the
   SQLite database automatically when its destination is absent;
2. run the recovery entrypoint:

```bash
sudo deploy/recover_control_plane.sh --env-file /etc/aurix-bot/aurix.env
```

When invoked outside a Git checkout, the script securely clones the configured
HTTPS GitHub repository/branch into a private staging directory and removes the
staging copy after the release is built. It then builds a versioned
`/opt/aurix-current` release from that source, installs Python dependencies, runs live deploy preflight, verifies
SQLite database backups when SQLite is configured, verifies the newest encrypted
local/offsite node backups, runs fleet `validate` and `check`, installs systemd
units, enables deploy/fleet timers, and starts `aurix-bot`.

Continue revival with:

4. restore a replaced node's matching Outline backup before changing its address;
5. run `reconcile`;
6. verify `Outline inventory ready: N/N`, Telegram authorization, database and
   remote-key counts, then a canary connection before enabling sales.

This is control-plane recovery from source and private environment. The
zero-touch registration path still requires pre-authorized provider/DNS
credentials, the scoped worker mutation gate, a trusted HTTPS endpoint, and
the pinned SSH files; absent those gates recovery remains safely assisted.

For SQLite deployments, `DATABASE_PATH` is not enough for disaster recovery.
Configure `AURIX_DATABASE_BACKUP_OFFSITE_DIR` or move production commerce state
to `COMMERCE_DATABASE_URL` PostgreSQL. For endpoint continuity, configure
`AURIX_DNS_PROVIDER`, `AURIX_DNS_ZONE_ID`, and `AURIX_DNS_API_TOKEN` before
promising node replacement without reissuing customer keys.

Never put `.env`, SSH private keys, decrypted archives, receipts, or management
API URLs in Git, CI logs, Telegram, or issue trackers.
