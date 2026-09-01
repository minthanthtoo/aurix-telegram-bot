# AuriX Outline Fleet Architecture and Operations Runbook

Status: final MVP production decision, 2 September 2026
Scope: Telegram control plane, current single-host SQLite state, future PostgreSQL, individually managed
Outline servers on DigitalOcean, and guarded future node provisioning.

## 1. Decision

AuriX must not put customer Outline keys behind a generic DigitalOcean Droplet
Autoscale Pool or a conventional load balancer.

An Outline access key contains server-specific Shadowsocks connection material.
Outline creates, lists, limits, and deletes those keys through each server's own
Management API. The server implementation filters keys that reached their data
limit out of the active Shadowsocks configuration. See the official
[Management API documentation](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/README.md)
and [server key implementation](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/server/server_access_key.ts).

DigitalOcean Autoscale Pools are designed to add and remove interchangeable
application VMs using aggregate CPU/memory targets or a fixed size, commonly
with tagged load-balancer backends. That lifecycle is not interchangeable with
stateful, server-bound Outline credentials. See DigitalOcean's current
[Autoscale Pool guide](https://docs.digitalocean.com/products/droplets/how-to/use-autoscale-pools/).

Therefore:

- one `outline_servers` row represents one explicit Outline endpoint;
- one key is identified by `(server_id, outline_key_id)`, never key ID alone;
- every free, promo, paid order, subscription, and paid key is assigned to one
  endpoint;
- AuriX admits new work only against fresh, healthy, declared capacity;
- failed endpoints stop new issuance but do not stop Telegram/admin recovery;
- provider provisioning and endpoint activation are separate approval gates;
- scale-in is drain, revoke/expire, verify empty, then operator-approved destroy.

## 2. What is implemented now

The current code provides:

- environment-defined `OutlineServerPool` clients with pinned TLS fingerprints;
- non-secret endpoint metadata in `outline_servers`;
- paid per-plan slot policies in `server_plan_allocations`;
- free/promo slot policies in `server_tier_allocations`;
- shared maximum-key, reserved-headroom, and monthly traffic policies;
- fresh-health admission with `AURIX_SERVER_HEALTH_MAX_AGE_SECONDS`;
- order-time paid capacity reservation;
- deterministic least-utilized server selection;
- server-scoped free, promo, and paid usage/key lookup;
- partial-outage-safe quota enforcement;
- degraded startup when all Outline endpoints are unavailable;
- durable `infrastructure_jobs` and `infrastructure_events`;
- an opt-in, budget-guarded DigitalOcean controller in `infrastructure.py`.

The controller deliberately stops at `awaiting_verification`. It does not store
an Outline Management URL in the database and does not make a new VM customer-
eligible. An operator must install/verify Outline, add the pinned secret endpoint
to `OUTLINE_SERVERS_JSON`, restart the service, observe a healthy inventory, set
capacity, and only then allocate tiers/plans.

Not implemented or intentionally not automatic:

- no CPU-only scale trigger;
- no automatic scale-in or Droplet destruction;
- no permanent cloud/Telegram/database secrets in user data;
- no live provider token in the normal Telegram process;
- no automatic activation from a merely `active` Droplet state;
- no automatic migration of existing customer keys between servers.

The provider worker now has a bounded, lock-protected one-pass entrypoint and
systemd timer. It reconciles existing tagged/explicitly managed Droplets before
enforcing node and budget limits. Provider inventory and committed monthly
run-rate are safety inputs; month-to-date billing alone is not treated as the
monthly commitment.

## 3. Runtime topology

```text
Telegram customers/admins
          |
          v
single AuriX bot process on the current Singapore Droplet
  |       |                  |
  |       |                  +--> receipt storage / vision triage
  |       +--> persistent SQLite (current authority and audit)
  +--> OutlineServerPool
          |--> sg-a Management API (pinned TLS)
          |--> sg-b Management API (pinned TLS)
          +--> future verified endpoint

future separate operator infrastructure worker on the same trusted host
  |--> infrastructure_jobs/events
  +--> scoped DigitalOcean API token
```

Telegram long polling remains single-process. Multiple bot replicas using one
token are not a scaling mechanism.

### Final MVP database decision

Keep the current persistent SQLite database while there is exactly one bot and
one operator worker host. Supabase remains private receipt-object storage; its
PostgreSQL product is not required merely because the Outline data plane gains
more servers. Move business state to hosted PostgreSQL before adding a second
bot/control-plane host, failover writer, or independently hosted infrastructure
worker. Never run two SQLite writers on different machines or copy a live file
between them.

## 4. Authority and secrets

The configured AuriX database (persistent SQLite today; PostgreSQL after the
multi-writer migration gate) is authoritative for customer/business lifecycle
and capacity policy. Outline is authoritative for remote key existence and measured traffic.
DigitalOcean is authoritative for VM/action state. Each observation is
reconciled into AuriX; none is guessed.

`OUTLINE_SERVERS_JSON`, Management API paths, fingerprints, Telegram tokens,
Fernet key, database URL, Supabase service key, receipt LLM key, and DigitalOcean
token are secrets. Management URLs are credentials because their path grants
management access. Outline's guide explicitly treats the access configuration
as sensitive: [share management access](https://developers.google.com/outline/docs/guides/service-providers/share-management-access).

`outline_servers` stores only identifiers, labels, declared limits, health,
counts, and timestamps. It never stores access URLs or management paths.

## 5. Identity and collision rule

Outline key IDs are local to a server. Two endpoints may both return key `1`.
All application joins, metrics, retrieval, quota actions, and deletion must use:

```text
(server_id, outline_key_id)
```

Database uniqueness is composite for both `keys` and `paid_vpn_keys`. A global
unique constraint on `outline_key_id` is incorrect for a fleet.

## 6. Health model

Inventory reconciliation calls each configured endpoint independently:

1. pinned `GET /server`;
2. list access keys;
3. transfer metrics;
4. optional Outline 1.12 experimental server metrics;
5. persist count, transfer, bandwidth fields, status, and `last_synced_at`.

An endpoint is eligible only when all are true:

- `enabled = 1`;
- `health_status = healthy`;
- `last_synced_at` exists;
- observation age is within `AURIX_SERVER_HEALTH_MAX_AGE_SECONDS`;
- key, traffic, and tier/plan policy have capacity.

An error marks that endpoint unreachable while other endpoints continue. Missing
metrics are unknown, never zero. Quota enforcement skips keys on an unobserved
endpoint and retries after telemetry recovers.

Startup is intentionally degraded rather than fatal: Telegram/admin functions
remain available, issuance is rejected by the health gate, and maintenance keeps
probing. This prevents a VPN outage from removing the recovery interface.

## 7. Capacity model

Each server may declare:

- `max_keys`: maximum customer keys accepted by policy;
- `reserved_keys`: operational headroom not sold/issued;
- `monthly_traffic_bytes`: quota commitment budget;
- paid plan slots, e.g. `basic_50gb = 20`;
- free/promo tier slots: `FREE300MB`, `FREE3GB`, and `PROMO`.

When any explicit allocation exists for a tier/plan, unallocated servers are not
eligible for it. A zero allocation disables new issuance of that tier on the
server. Existing keys are not moved or revoked by allocation changes.

Paid orders reserve capacity before receipt submission. Cancellation/rejection
releases the reservation. Free/promo creation increments the reconciled remote
count in the same durable transaction after successful remote creation; the next
inventory pass replaces that estimate with observed truth.

Traffic commitment is conservative: active/pending quota commitments plus the
new requested quota must fit the declared server budget. It is not a cloud bill
forecast and must not be confused with observed trailing-30-day transfer.

## 8. Allocation algorithm

For each new entitlement:

1. load the active plan/tier and requested quota;
2. load only fresh healthy endpoints;
3. enforce explicit allocation membership when configured;
4. reject endpoints at tier/plan slot capacity;
5. reject endpoints at key capacity after reserved headroom;
6. reject endpoints whose committed quota would exceed traffic policy;
7. select the lowest utilization ratio, with `server_id` as deterministic tie
   breaker;
8. create the remote key;
9. persist the server-scoped key and business state atomically;
10. if persistence fails, delete the just-created remote key and leave the
    entitlement unconsumed.

No healthy candidate means “temporarily full/unavailable”; it does not fall back
to an unhealthy or stale endpoint.

## 9. Quota and expiry enforcement

Metrics are collected per endpoint. For each active/retry key:

1. select its server's metric map;
2. if that server is absent, do nothing and record the endpoint health error;
3. compare observed bytes with the exact stored quota;
4. persist a termination event;
5. delete through that key's server client;
6. use `GET key` when supported to verify absence;
7. persist `deleted_verified`, `delete_accepted`, `retrying`, or `escalated`;
8. notify the customer and staff transparently.

Outline's current implementation evaluates a 30-day transfer window and removes
over-limit keys from its live configuration. AuriX additionally deletes exhausted
or expired credentials so state converges and old keys cannot reappear. Usage
counters are not assumed resettable.

## 10. Why a DigitalOcean Load Balancer is not the key abstraction

DigitalOcean Network Load Balancers can forward TCP/UDP and require backend
routing configuration. They are useful for interchangeable transport backends,
not for independently generated Outline keys with server-specific secrets and
ports. See the [NLB backend guide](https://docs.digitalocean.com/products/networking/load-balancers/how-to/configure-droplets-for-nlb/).

Adding a load balancer does not replicate Shadowbox key state or traffic counters
between servers. AuriX must retain explicit assignment even if a later transport
supports a stable front door.

## 11. Provisioning state machine

```text
requested
  -> pending
  -> running (DigitalOcean POST accepted)
  -> creating (provider action incomplete)
  -> awaiting_verification (Droplet active; not customer eligible)
  -> operator installs/verifies Outline and updates secret environment
  -> inventory healthy
  -> owner declares capacity and allocations
  -> enabled for new issuance
```

Provider create is asynchronous; an accepted HTTP response is not proof the VM
is usable. DigitalOcean documents the create response/action workflow in its
[Droplets API](https://docs.digitalocean.com/products/droplets/reference/api/droplets/).

The API token should have only required read/action/create/tag scopes. Current
scope dependencies are listed in DigitalOcean's
[`droplet:create` scope reference](https://docs.digitalocean.com/reference/api/scopes/droplet/create/).

## 12. Provider mutation gates

All of these must be present before the separate worker may create a VM:

```dotenv
DIGITALOCEAN_API_TOKEN=
AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=0
AURIX_ALLOWED_REGIONS=sgp1
AURIX_ALLOWED_DROPLET_SIZES=s-1vcpu-1gb
AURIX_ALLOWED_DROPLET_IMAGES=ubuntu-24-04-x64
AURIX_MAX_VPN_NODES=3
AURIX_MAX_NODE_CREATIONS_PER_DAY=1
AURIX_NODE_CREATION_COOLDOWN_SECONDS=86400
AURIX_MAX_MONTHLY_INFRA_BUDGET_USD=18
AURIX_DROPLET_MONTHLY_COST_ESTIMATE_USD=6
AURIX_MANAGED_DROPLET_TAG=aurix-vpn-node
# The current least-privilege worker token cannot attach tags to existing
# Droplets (that requires droplet:update). Until a separately approved token
# change, set this to the actual Droplet IDs (not public IP addresses).
AURIX_MANAGED_DROPLET_IDS=<droplet-id-for-sg-a>,<droplet-id-for-sg-b>
AURIX_SCALE_PREPARE_UTILIZATION_PERCENT=75
AURIX_SCALE_URGENT_UTILIZATION_PERCENT=90
AURIX_SCALE_PREPARE_TRAFFIC_PERCENT=75
AURIX_SCALE_URGENT_TRAFFIC_PERCENT=90
AURIX_INFRASTRUCTURE_QUEUE_ENABLED=0
AURIX_SCALE_REGION=sgp1
AURIX_SCALE_DROPLET_SIZE=s-1vcpu-1gb
AURIX_SCALE_DROPLET_IMAGE=ubuntu-24-04-x64
```

Safe default is disabled. The budget is mandatory when mutation is enabled and
fails closed on invalid/unavailable billing data. Existing managed Droplets are
counted by provider inventory and the configured ID bridge until tags are
verified. `user_data` is not accepted; new nodes require an operator verification
step.

## 13. Scale-out decision

### Final MVP policy

- Mode: **assisted scaling**. AuriX calculates and displays fleet posture, but
  does not purchase or delete infrastructure from the Telegram process.
- Prepare threshold: 75% of declared saleable key capacity.
- Urgent threshold: 90%, one remaining saleable slot, or no healthy capacity.
- Envelope: Singapore `s-1vcpu-1gb`, maximum three VPN nodes, maximum one new
  node per 24 hours, and an $18/month node ceiling based on the current $6 plan.
- Scale-in: manual drain and verified-empty destruction only.
- Database: persistent SQLite remains valid for the one-host control plane;
  PostgreSQL is mandatory before a second writer/control host.

The live fleet reported 17 remote keys against 20 saleable slots on 2 September
2026, so its posture is **Prepare**. Provisioning and verifying the second node
is the next operator action; raising the first node's declared limit merely to
silence the warning is not acceptable capacity planning.

Do not scale on one CPU spike or one transient traffic observation. The admin panel may show the current posture
immediately; before a provider job is approved, confirm two or more consecutive
fresh observation windows show a real admission problem, such as:

- remaining declared key slots below protected headroom;
- committed traffic above an owner threshold;
- plan/tier slots exhausted across every healthy eligible endpoint;
- sustained bandwidth saturation where the metric is available;
- an endpoint unreachable long enough that surviving capacity cannot serve new
  demand.

Before queuing, estimate pending reservations, expiring keys in the next 24/72
hours, recent order arrival rate, current cloud spend, new-node lead time, and
manual verification availability. Early production should be recommend/approve,
not autonomous create.

## 14. Drain and scale-in

There is no automatic destroy path. The safe sequence is:

1. set every plan/tier allocation on the endpoint to zero;
2. mark it unavailable for new assignments;
3. wait for paid/free/promo keys to expire or deliberately replace them with
   customer communication;
4. verify no open order reservations, pending provisioning jobs, or active keys;
5. capture final inventory, traffic, and audit evidence;
6. remove the endpoint from secret configuration and restart;
7. verify remaining fleet health;
8. destroy the Droplet manually with a second operator confirmation.

Never let a generic autoscale pool choose a scale-in victim.

## 15. Admin workflow

Telegram owner/admins use **Capacity**:

- reconcile endpoint health and observed inventory;
- open one server panel;
- set maximum keys, reserved headroom, and monthly traffic policy;
- allocate paid plans and Daily 300 MB / Monthly 3 GB / Promo slots;
- refresh the same message in place.

When the owner has completed node verification and intentionally enables
`AURIX_INFRASTRUCTURE_QUEUE_ENABLED=1`, a **Prepare next node** button appears
only for Prepare/Urgent posture. The click creates one idempotent pending intent
using the configured Singapore region, Droplet size and image. It does not call
DigitalOcean; the separate worker still checks provider inventory, budget,
cooldown, node count and `AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED`.

Capacity changes affect only new issuance. A separate audit event records the
actor, server, old/current domain state, and selected limit.

## 16. Environment configuration

Single endpoint remains supported:

```dotenv
OUTLINE_API_URL=https://host:port/secret-path
OUTLINE_CERT_SHA256=64-hex-fingerprint
OUTLINE_PROVIDER_RESOURCE_ID=<numeric-droplet-id>
```

Fleet configuration replaces those variables:

```dotenv
OUTLINE_SERVERS_JSON=[{"id":"sg-a","label":"Singapore A","provider_resource_id":"<droplet-id>","api_url":"https://host:port/secret","cert_sha256":"64hex"},{"id":"sg-b","label":"Singapore B","provider_resource_id":"<droplet-id>","api_url":"https://host:port/secret","cert_sha256":"64hex"}]
OUTLINE_DEFAULT_SERVER_ID=sg-a
AURIX_SERVER_HEALTH_MAX_AGE_SECONDS=900
```

Keep the JSON in `/etc/aurix-bot/aurix.env` or the hosting provider's secret
environment. Never commit or paste it into logs.

## 17. Deployment checklist

Before merging/deploying a fleet change:

1. back up PostgreSQL and record the deployed commit;
2. run compile, Ruff, and the complete test suite;
3. verify migration history and composite key indexes;
4. verify every configured Management API fingerprint out of band;
5. restart one bot process only;
6. confirm Telegram authorization and inventory health;
7. set capacity before opening a new server to any tier;
8. create one test key on the intended server;
9. verify `/myvpn`, usage, quota delete, and expiry use that server;
10. confirm no Management URL/access URL appears in logs or database metadata.

The infrastructure worker is installed separately from the bot:

```sh
install -d -o root -g root -m 0700 /etc/aurix-infrastructure /var/lib/aurix-infrastructure
install -o root -g root -m 0644 deploy/aurix-infrastructure-worker.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/aurix-infrastructure-worker.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aurix-infrastructure-worker.timer
systemctl start aurix-infrastructure-worker.service
systemctl --no-pager --full status aurix-infrastructure-worker.timer
```

Keep `AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=0` during installation and canary
verification. A worker pass may reconcile an already-created provider action,
but it never makes a Droplet customer-eligible. Only the owner-approved,
post-verification procedure may enable new admission.

## 18. Failure matrix

| Failure | Required behavior |
| --- | --- |
| One Outline endpoint unavailable | Mark unreachable, stop new assignment there, continue other endpoints |
| All Outline endpoints unavailable | Bot/admin stays online; all new issuance pauses |
| Metrics unavailable for one endpoint | Do not interpret as zero; skip its quota decisions |
| Remote create succeeds, DB write fails | Compensating delete on the same endpoint |
| Delete fails | Durable retry/escalation and transparent notices |
| PostgreSQL unavailable | No allocation, payment commit, or provider mutation |
| DigitalOcean create timeout | Reconcile stored job/provider action; never blindly create another |
| Droplet becomes active | Await operator Outline verification; no customer allocation |
| Stale health snapshot | Endpoint excluded from new admission |
| Duplicate callback/job execution | Database uniqueness/idempotency returns existing state |

## 19. Evidence and closed decisions

Code-level uncertainty is closed by regression coverage for multi-server key-ID
collisions, server-scoped usage/deletion, partial outages, stale health, capacity
reservations, provider feature gates, budget failure, and the manual verification
stop.

The MVP decisions are no longer open-ended: use assisted scaling, the explicit
three-node/$18 envelope above, a 24-hour creation cooldown, no automatic destroy,
and SQLite until a second control-plane writer exists. The current 80% posture
means prepare node two.

The scoped DigitalOcean API token is installed only in the separate
infrastructure-worker environment and provider mutation remains off by default.
The existing second Droplet is not yet customer-eligible until Outline is
installed, its pinned Management API endpoint is registered, inventory is
healthy, and the owner declares allocations. A future non-Outline transport
must prove replicated identity/quota state before any load-balanced design is
reconsidered.

## 20. Definition of done (100% acceptance)

The implementation is complete when every item below has a recorded command,
timestamp, and audit/evidence reference. A green unit suite alone is not
completion.

### Code and repository

- CI passes compile, Ruff, migrations, provider-worker, Telegram, commerce,
  receipt, wallet, quota, and multi-server tests on the exact commit deployed.
- The working tree is clean; the deployed SHA is recorded in the release log.
- No secret, Management API path, access URL, receipt image, provider token, or
  customer key appears in Git, test output, journald, or diagnostics.
- SQLite integrity/backup or PostgreSQL backup has been verified immediately
  before the release.

### Control plane and storage

- Exactly one Telegram long-polling process is active for this bot token.
- The configured database survives a restart and a restore rehearsal; migrations
  are idempotent and schema fingerprints match the release.
- Private receipt storage accepts an object, rejects a duplicate checksum, and
  preserves review/audit metadata without exposing the object publicly.
- Owner/admin authorization, notification preferences, order state transitions,
  wallet ledger invariants, and in-place Telegram pagination are smoke-tested.

### Every Outline endpoint

- Each node has a secret Management URL and pinned SHA-256 fingerprint installed
  out of band; `GET /server`, `/access-keys`, and metrics all succeed.
- The endpoint is registered with a stable `server_id` and numeric provider ID,
  has declared key/traffic/tier/plan allocations, and is fresh/healthy in the
  last observation window.
- One canary key is created, copied, connected, measured, quota-limited, deleted,
  and verified absent through the same server-scoped identity tuple.
- Expired and `used >= limit` keys are hard-deleted with a durable termination
  event, retry/escalation path, and customer/staff notice.

### Fleet and provider safety

- The worker reports every explicitly managed Droplet as `managed`; each has a
  matching registered Outline endpoint before it is eligible for issuance.
- Provider mutation remains disabled through node installation and canary.
  Enabling it requires owner approval, allowlisted region/size/image, budget,
  cooldown, node-count, and two consecutive fresh capacity observations.
- A queued provision is an idempotent intent only; the worker is the sole actor
  allowed to call DigitalOcean and stops at `awaiting_verification`.
- Scale-in is proven by a drain rehearsal: allocations zero, no active keys or
  reservations, final inventory captured, endpoint removed, then manual destroy.

### Final live pilot

- Run the complete customer path: `/start` → free daily/monthly claim → each
  paid plan → wallet top-up → receipt upload → AI triage/manual decision → key
  delivery → `/myvpn`/usage → expiry/quota termination.
- Run the complete staff path: owner/admin panel, receipt accept/reject, refund or
  rejection notification, capacity/allocation edit, reconcile, and worker audit.
- Observe at least 24 hours of maintenance/worker logs with no unclassified
  errors, then keep a 72-hour rollback window and the last known-good release.

Until the second node and these live gates are evidenced, the project is
**code-complete and operationally staged**, not 100% production-ready.
