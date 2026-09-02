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

GitHub CI compiles and lints fleet code, syntax-checks the remote bootstrap,
validates a sample manifest, and runs all tests. The existing DigitalOcean
autodeployer activates only a CI-successful `main` commit and rolls back failed
bot startup. It installs versioned fleet units and, when a fleet manifest is
configured, enables:

- `aurix-fleet-reconcile.timer`, every ten minutes and after boot;
- `aurix-fleet-backup.timer`, daily encrypted state capture;
- `aurix-infrastructure-worker.timer`, provider request reconciliation.

```bash
systemctl list-timers 'aurix-*'
systemctl status aurix-fleet-reconcile.service
journalctl -u aurix-fleet-reconcile.service -n 100 --no-pager
```

Logs expose node IDs, addresses, versions and counts, but not management paths,
fingerprints, access URLs, keys, or provider secrets.

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

1. provision Ubuntu 24.04 and restore the private `.env`, fleet SSH key,
   verified `known_hosts`, backup key, database, and encrypted node archives;
2. clone GitHub `main`, install requirements and versioned systemd units;
3. restore a replaced node's matching Outline backup before changing its address;
4. run `validate`, `check`, then `reconcile`;
5. verify `Outline inventory ready: N/N`, Telegram authorization, database and
   remote-key counts, then a canary connection before enabling sales.

Never put `.env`, SSH private keys, decrypted archives, receipts, or management
API URLs in Git, CI logs, Telegram, or issue trackers.
