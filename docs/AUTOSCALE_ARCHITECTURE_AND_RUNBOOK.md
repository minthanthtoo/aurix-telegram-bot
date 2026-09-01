# AuriX Outline Fleet Architecture and Autoscale Runbook

Status: canonical implementation and operating guide  
Audience: future maintainers and AI agents  
Last reviewed: 2026-09-02  
Scope: AuriX Telegram commerce control plane and independently managed Outline VPN Droplets

## 1. Non-negotiable decision

AuriX performs application-layer fleet scaling. Customer VPN nodes are ordinary,
individually managed DigitalOcean Droplets. They are never members of a
resource-utilization Autoscale Pool and customer traffic is never distributed
blindly through a load balancer.

Every credential is bound to one durable endpoint assignment:

```text
subscription or free entitlement
  -> endpoint assignment
  -> endpoint-scoped Outline credential
  -> direct ss:// access URL
```

DigitalOcean's API is a server factory. AuriX remains the authority for capacity,
placement, drain, migration, and retirement.

## 2. Why generic Droplet Autoscale is unsafe

DigitalOcean Autoscale Pool members are ephemeral. Scale-in destroys the newest
member after a short shutdown window, and a template update may replace existing
members. Outline stores access-key state and transfer counters on each node. A
pool cannot know that a quiet node still owns paid credentials.

A DigitalOcean Load Balancer does not repair this mismatch. Even where TCP/UDP
forwarding is technically possible, independently managed Outline nodes do not
share credential state or counters. Reassigning a client to another backend can
make a valid credential unknown, duplicate quota, or attribute traffic to the
wrong customer.

Allowed use of generic autoscaling:

- stateless web/control-plane replicas after worker leases are safe;
- disposable builders or monitoring workers containing no customer credentials.

Forbidden use:

- any node holding an active customer Outline key;
- any automatic scale-down policy controlling VPN nodes;
- a load balancer as a substitute for endpoint assignment.

## 3. Current production facts and constraints

At the time of this guide, production uses one SGP1 Droplet for the AuriX bot,
Outline, 9Router, Caddy, and Watchtower. The commercial database is hosted
PostgreSQL. This is a migration source, not the desired final security boundary.

The legacy code has two credential tables (`keys` and `paid_vpn_keys`) and one
process-global Outline client. Historical Outline key IDs are only unique inside
one server. Multi-node identity is therefore the pair:

```text
(endpoint_id, outline_key_id)
```

All existing records must be backfilled to the configured bootstrap endpoint
before a second endpoint accepts assignments.

## 4. System topology

```text
Telegram
  -> AuriX application services
       -> PostgreSQL commercial and connectivity truth
       -> durable provisioning/infrastructure jobs
       -> endpoint-scoped Outline client factory
       -> DigitalOcean provider client (infrastructure worker only)

Customer device
  -> assigned Outline node directly
  -> Internet
```

The bot/control plane must not carry VPN traffic. A Telegram, PostgreSQL, or
worker outage must not interrupt an already-established Outline credential.

For production scale, place the infrastructure worker and its DigitalOcean token
outside the public VPN/9Router host. Keep the token out of Telegram handlers and
out of all VPN nodes.

## 5. Sources of truth

| Fact | Authority |
|---|---|
| Order, price and payment | AuriX PostgreSQL |
| Entitlement duration and quota | Subscription/free entitlement row |
| Endpoint eligibility and assignment | AuriX endpoint registry |
| Remote key existence and raw transfer | Assigned Outline server |
| Normalized usage observation | Endpoint capacity/usage snapshot |
| Droplet existence and provider lifecycle | DigitalOcean, reconciled into AuriX |
| Infrastructure intent and budget decision | AuriX infrastructure job/audit |

Never mark a remote effect complete merely because an API request was sent.
Persist intent, execute, observe, verify, then commit converged state.

## 6. Required data model

### `vpn_endpoints`

One independently managed Outline installation.

Required fields:

```text
id, code, provider, provider_resource_id, region
state, accepts_new_assignments
management_url_ciphertext or secret reference
certificate_sha256, outline_version
public_address, private_address
max_active_keys, reserved_transfer_bytes
created_at, verified_at, last_healthy_at, retired_at
```

States:

```text
PROVISIONING -> BOOTSTRAPPING -> VERIFYING -> ACTIVE
ACTIVE -> DEGRADED -> ACTIVE
ACTIVE/DEGRADED -> DRAINING -> RETIRED
any pre-active state -> QUARANTINED or FAILED
```

Only `ACTIVE` plus `accepts_new_assignments=true` is allocatable.

### `endpoint_assignments`

Pins one commercial/free intent to one endpoint.

```text
id, endpoint_id
subscription_id or free_key_id
status, reason
reserved_quota_bytes, plan_code
assigned_at, released_at
```

Retries use the existing assignment. They do not silently select another node.
Reassignment is an explicit, audited state transition.

### `endpoint_capacity_snapshots`

Durable observations, never authoritative payment state:

```text
endpoint_id, observed_at
healthy, active_key_count
observed_transfer_bytes
cpu_percent, memory_percent, peak_mbps
management_latency_ms, last_error
```

### `endpoint_plan_limits`

Admin-owned allocation policy:

```text
endpoint_id, plan_code, enabled
max_active_assignments
reservation_weight_bytes
```

### `infrastructure_jobs`

Durable provider operations:

```text
id, operation, endpoint_id
status, attempts, next_attempt_at, locked_at
provider_resource_id, provider_action_id
request_fingerprint, last_error
created_at, completed_at
```

Provider operations include `provision`, `verify`, `drain`, and `destroy`.
Destroy remains disabled until the endpoint has zero active assignments.

## 7. Database invariants and migration

1. Insert a bootstrap endpoint for the existing configured Outline server.
2. Add `endpoint_id` to both legacy credential tables.
3. Backfill every existing credential to that endpoint.
4. Replace global `outline_key_id` uniqueness with
   `UNIQUE(endpoint_id, outline_key_id)`.
5. Add assignment identity to provisioning jobs.
6. Do not activate multi-node selection while any credential lacks an endpoint.
7. Back up PostgreSQL and run the migration against a restored copy first.

SQLite is a development profile. PostgreSQL is required for production
concurrency, job leases, and scale-controller leadership.

## 8. Allocation policy

Allocation is deterministic, explainable, and transactional.

Eligibility gates:

```text
endpoint.state == ACTIVE
accepts_new_assignments
fresh health snapshot
region/product policy matches
active-key limit remains
plan-specific slots remain
transfer reservation remains
```

Stable selection order:

```text
greatest verified transfer headroom
-> greatest plan-specific slot headroom
-> lowest paid-customer concentration
-> stable endpoint code
```

The transaction locks eligible endpoint rows, chooses one endpoint, inserts the
assignment/reservation, and attaches it to the provisioning job before any
Outline request starts.

One Telegram user may buy several subscriptions. Allocation is per entitlement,
not per user. Keys belonging to one user may reside on different endpoints.

## 9. Capacity and economics

Do not treat user count or advertised quota as physical load by itself.

Maintain three separate views:

1. Endpoint operations: active keys, CPU, memory, peak Mbps, latency, packet or
   connection failures, and health freshness.
2. Commercial reservations: configurable per-plan slots and conservative
   reservation weights for 300 MB, 3 GB, 50 GB, 100 GB, and future plans.
3. DigitalOcean team economics: team-wide accrued outbound allowance, projected
   egress, overage, monthly infrastructure budget, and node creation limits.

DigitalOcean pools Droplet transfer allowance and usage at the team level. AuriX
must not claim that each Droplet owns an isolated billing allowance. Endpoint
transfer is still measured for quality and placement.

Start with conservative admin-set weights. Replace them only with measured
P90/P95 usage and peak-concurrency evidence.

## 10. Usage and quota enforcement

Outline usage is server-local. All observations are endpoint-scoped:

```text
(endpoint_id, outline_key_id, bytes, observed_at)
```

Maintenance iterates endpoints independently. Failure of endpoint B must not
reuse endpoint A's empty or stale metric map. Enforcement addresses the client
belonging to the credential's persisted assignment.

Telegram `/myvpn` uses an endpoint-scoped fresh observation when available and
the last durable per-key observation as its fallback. As the fleet grows, move
interactive refreshes fully behind the maintenance snapshot worker so Telegram
latency remains bounded; edit the same panel when the refresh completes.

When migrating a remaining entitlement, commit source usage once and apply only
the remaining allowance to the destination. Never keep two full-quota replicas
active unless a bounded, audited overlap budget has been explicitly approved.

## 11. DigitalOcean provisioning workflow

Provisioning is asynchronous and reconciled:

1. Validate budget, region, size/image allowlists, cooldown, and global node cap.
2. Insert one infrastructure intent and acquire a region leadership lock.
3. Call `POST /v2/droplets` with a stable intent tag and no permanent secret in
   user data.
4. Persist returned Droplet/action IDs before polling.
5. Reconcile ambiguous responses by tag and creation window; quarantine duplicate
   candidates instead of selecting one silently.
6. Wait for provider action completion and addresses.
7. Apply a tag-based DigitalOcean Cloud Firewall.
8. Bootstrap over SSH with a dedicated provisioning key, or use a short-lived,
   single-use enrollment token. Never place provider/API secrets in user data.
9. Install a pinned Outline release and retrieve its management URL/pin through
   the protected bootstrap channel.
10. Encrypt the management capability, probe server info, create/delete a test
    key, and read metrics.
11. Mark `ACTIVE` only after verification.

User data remains available from the Droplet metadata service. It may contain
public bootstrap configuration, package pins, and a short-lived one-time token,
but no DigitalOcean token, permanent SSH private key, Outline management URL,
Telegram token, database URL, or encryption key.

Provider token permissions must be limited to the required Droplet, action, tag,
firewall, image/size/region, and project reads/writes. The token belongs only to
the infrastructure worker.

## 12. Firewall policy

Use stable tags such as `aurix-vpn-node` and `aurix-env-production`.

Inbound policy:

- actual configured Outline access TCP/UDP ports: public;
- Outline management port: control-plane addresses only;
- SSH: infrastructure worker and emergency operator only;
- everything else: denied.

Outbound policy must allow package installation, provider registration, DNS,
time synchronization, and VPN customer traffic. Record firewall IDs and intended
rules in infrastructure events. Probe external reachability after changes.

## 13. Scale-out controller

The controller first runs in recommendation-only mode.

Trigger candidates:

```text
no eligible endpoint can reserve an approved entitlement
or healthy ready capacity is below the configured warm buffer
```

Mandatory guards:

- maximum nodes total and per region;
- maximum creations per day;
- monthly infrastructure budget;
- one active scale intent per region;
- cooldown longer than measured bootstrap time;
- approved size/image/region lists;
- database lock and unique request fingerprint;
- owner-confirmation mode until live evidence permits automation.

Scale-out never occurs inside a Telegram callback or payment transaction. The
user request records durable intent; the worker performs the provider effect.

## 14. Drain, migration, and scale-down

Automatic provider scale-down is disabled initially.

Safe retirement:

```text
ACTIVE -> DRAINING
stop new assignments
wait for natural expiry where practical
create replacement intent for remaining customers
observe final source usage
apply only remaining quota at destination
deliver replacement and record acknowledgement/support result
revoke and verify source credential
confirm zero active assignments and no unknown remote keys
owner-confirm destruction
destroy Droplet and verify provider absence
RETIRED
```

No CPU, RAM, nighttime traffic, or low connection count may directly trigger
Droplet destruction.

## 15. Failure and recovery rules

- Bot unavailable: existing VPN credentials continue working.
- PostgreSQL unavailable: no allocation, payment, or infrastructure mutation.
- One management API unavailable: endpoint becomes degraded; other bot functions
  and endpoints continue.
- Worker crashes after remote create: observe deterministic key ID on the pinned
  endpoint before retrying.
- DigitalOcean create timeout: reconcile by stored intent/tag; never blindly
  create again.
- Bootstrap failure: quarantine the node; never allocate customers.
- Capacity data stale: stop new allocation to that endpoint.
- Unknown remote key: quarantine from capacity accounting and alert an operator.
- Duplicate Droplet candidates: quarantine all but an explicitly reconciled one.
- Budget data unavailable: automatic creation fails closed.

## 16. Admin UX

The owner/admin endpoint panel should provide:

```text
Fleet overview
Endpoint detail and health freshness
Capacity by plan
Assigned credentials/customers
Infrastructure job history
Probe endpoint
Enable/stop allocation
Provision endpoint
Drain endpoint
Retire endpoint (zero-assignment guard)
```

Lists and pagination edit the existing Telegram message. Mutations require the
existing admin challenge/confirmation mechanism. Destruction requires owner
authorization even when normal server administration is delegated.

## 17. Rollout gates

### Gate 0: recoverability and security

- core application committed and pushed;
- production/local release identity documented;
- database backup and restore test;
- current 16-key inventory reconciled;
- management port restricted;
- secrets separated from VPN/9Router host where practical.

### Gate 1: endpoint-aware single node

- bootstrap endpoint backfilled;
- composite credential identity active;
- endpoint-scoped clients and snapshots;
- degraded startup works;
- all 164+ tests and migration tests pass.

### Gate 2: manual two-node proof

- second SGP endpoint manually provisioned;
- paid and free allocation tested;
- usage and revocation proven independently;
- drain without data corruption exercised.

### Gate 3: admin-triggered provider automation

- scoped token stored only with infrastructure worker;
- API create/bootstrap/verify is idempotent;
- firewall and budget guards tested;
- no customer allocation before verification.

### Gate 4: bounded automatic scale-out

- recommendation decisions compared with operator decisions;
- bootstrap time and failure rate measured;
- P90/P95 usage and peak load available;
- owner enables automatic creation within explicit budget.

### Gate 5: controlled retirement

- assisted migration and remaining-quota accounting proven;
- zero-assignment deletion invariant tested;
- provider destruction requires owner confirmation.

## 18. Required test matrix

- two fake endpoints use independent API URLs and certificate pins;
- identical external key IDs coexist on different endpoints;
- allocation never selects inactive, draining, stale, or full endpoints;
- concurrent reservations do not oversubscribe the final slot;
- provisioning retries stay pinned;
- one endpoint's metrics never enforce another endpoint's key;
- free/promo timeout reconciles without consuming duplicate claim capacity;
- ambiguous Droplet create produces one active endpoint or quarantined duplicates;
- bootstrap failure never makes an endpoint allocatable;
- budget/cooldown/max-node guards fail closed;
- drain blocks new assignments but preserves active customers;
- destruction fails while any assignment or unknown key remains;
- control plane starts when one or every Outline management API is unavailable;
- customer/admin pagination edits the current Telegram message.

## 19. Instructions for future AI agents

1. Read this document, `FINAL_ARCHITECTURE.md`, migrations, runtime composition,
   worker code, and tests before changing fleet behavior.
2. Inspect Git status and preserve unrelated user changes.
3. Treat provider/model/API output as untrusted external state.
4. Never expose management URLs, certificate secrets, access URLs, provider
   tokens, database URLs, or receipt credentials in logs or chat.
5. Never create or destroy a real Droplet merely to test code.
6. Use fake provider/Outline adapters for automated tests.
7. Backfill and verify the current endpoint before enabling multi-node selection.
8. Do not weaken certificate pinning or job idempotency to make a test pass.
9. Do not add a load balancer or Autoscale Pool to customer VPN traffic.
10. Do not claim automatic scale is production-ready until its rollout gate is
    evidenced in audit records and live acceptance results.

## 20. Production enablement checklist

```text
[ ] Git baseline pushed and release commit known
[ ] Supabase backup and restore verified
[ ] Existing endpoint and every key backfilled
[ ] Management API/firewall restricted
[ ] Dedicated infrastructure-worker boundary
[ ] Scoped DigitalOcean token configured
[ ] Allowed regions/sizes/images configured
[ ] Monthly budget and node caps configured
[ ] Recommendation-only mode reviewed
[ ] Second node acceptance completed
[ ] Owner explicitly enables automatic scale-out
[ ] Automatic destruction remains disabled
```

## 21. Code map and present activation state

Implementation owners:

| Concern | Code |
|---|---|
| Endpoint schema and legacy backfill | `migrations.py`, `connectivity.EndpointRegistry.backfill_free_assignments` |
| Encrypted endpoint registry and health | `connectivity.EndpointRegistry` |
| Paid placement and endpoint-pinned retries | `commerce_worker.CommerceWorkerMixin._provision` |
| Free/trial/promo placement | `entitlements.ClaimService` |
| Endpoint-scoped usage, inventory, quota, deletion | `connectivity.py`, `entitlements.py`, `commerce_worker.py` |
| Guarded DigitalOcean intent/provider lifecycle | `connectivity.DigitalOceanClient`, `connectivity.FleetController` |
| Admin fleet and per-plan slot controls | `/capacity`, `telegram_commands.py`, `telegram_callbacks.py` |
| Degraded control-plane startup | `runtime.py` |
| Regression and provider-fake coverage | `test_connectivity.py`, `test_app.py`, `test_commerce.py`, `test_runtime.py` |

Current defaults deliberately permit endpoint-aware operation on the existing
server while keeping real provider mutation off:

```text
AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=0
AURIX_ENDPOINT_ACTIVATION_ENABLED=0
DIGITALOCEAN_API_TOKEN absent on the bot host where possible
no automatic destruction path
```

The provider controller can record a guarded intent, submit an allowed Droplet,
persist provider/action IDs, reconcile asynchronous activation, and require a
real Outline probe before endpoint activation. It does not pretend that an
installer, enrollment exchange, Cloud Firewall, budget policy, or second-node
acceptance has occurred. Those are deployment gates, not facts established by
unit tests. Keep provider mutations disabled until Gate 0–2 evidence is stored.

Before deploying these migrations to Supabase:

1. create a restorable database backup;
2. run the application migration against a restored copy;
3. verify every existing `keys` and `paid_vpn_keys` row has `endpoint_id`;
4. verify every active row has one active `endpoint_assignments` row;
5. compare Outline inventory with `(endpoint_id, outline_key_id)` records;
6. start one application worker and inspect `/capacity`;
7. only then permit a second endpoint to accept assignments.
