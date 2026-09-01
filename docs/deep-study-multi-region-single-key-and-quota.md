# Multi-region Outline with one customer key: selection and quota study

Status: architecture decision for the Phase-8 AuriX snapshot  
Date: 2026-08-29 (Asia/Rangoon)  
Decision scope: whether the Telegram bot can provide multi-region automatic selection through one Outline key, and whether quota can move between regions

## Architecture judgment against the required intention

### Required product intention

The extracted conversations consistently define the product as **reliable connectivity**, not the sale of an Outline key. The intended workflow is:

```text
customer buys one entitlement
→ receives one simple connection identity
→ AuriX selects suitable infrastructure
→ endpoint failure does not alter payment, expiry, or remaining quota
→ replacement infrastructure is hidden as much as the client permits
→ customer regains connectivity with minimal action
```

Telegram is intended to be the account, purchase, support, preference, notification, and operator-control surface. It is not intended to carry VPN traffic or make local client networking decisions.

### Overall verdict

**The proposed architecture is correct for V3 if its promise is “one entitlement, centrally selected region, and controlled reassignment.” It is insufficient if its promise is “one static key with seamless best-region roaming.”**

Fit by requirement:

| Required outcome | Fit | Judgment |
|---|---:|---|
| Payment/subscription survives endpoint failure | Strong | Business truth remains separate from credentials and endpoints |
| Multiple Outline servers and regions | Strong after V3 work | Endpoint-scoped clients, assignments, and credentials are the correct model |
| Automatic region selection at initial provisioning | Strong | Control plane can select from policy, health, capacity, and customer preference |
| “Fastest region” selection | Weak without client evidence | Server probes cannot measure the customer's ISP path |
| One ordinary static key across regions | Fails | Static `ss://` configuration identifies one host/port/secret |
| One stable customer-facing profile | Feasible, conditional | Requires a separately hosted dynamic profile and compatible clients |
| Seamless failover of an active tunnel | Fails with stock client/control plane | Backend cannot move an existing client session |
| Recovery on next manual reconnect | Good with dynamic profile | Official client fetches dynamic configuration during `connect()` |
| Exact globally shared byte quota | Fails natively | Server counters are independent, rolling, and periodically enforced |
| Commercial remaining-quota preservation | Good with new ledger | AuriX can commit source usage and derive the destination allowance |
| Operation during Telegram outage | Strong if decoupled | Existing VPN traffic continues; profile hosting must not depend on bot polling |
| Operation during dynamic-profile outage | Weak unless separately hardened | A stock client that cannot fetch dynamic configuration cannot start a new connection |
| No custom client requirement | Acceptable for V3 assisted migration | Not sufficient for true client-observed selection or automatic failover |

### Required promise correction

Safe V3 customer wording:

> AuriX assigns you to an available regional Outline server and can replace that assignment while preserving your subscription and remaining allowance. A reconnect or replacement import may be required.

Unsafe V3 wording:

> One Outline key automatically finds the fastest region and fails over seamlessly with an exact shared quota.

The unsafe wording requires client-side probing, selection, reconnection, and verified telemetry. That is V11 behavior.

### Corrected V3 architecture

```text
Telegram control surface
  → entitlement/payment service
  → endpoint selector
  → one persisted ACTIVE assignment
  → one endpoint-scoped Outline credential
  → native server limit derived from AuriX remaining allowance

Independent workers
  → endpoint observations
  → usage checkpoints/commits
  → provision/revoke/migrate/reconcile

Optional independent profile service
  → stable revocable customer URL
  → returns only the verified active assignment
  → changes take effect on client connect/reconnect
```

The dynamic profile service is optional in V3 and must not run only inside the Telegram long-polling process. Otherwise a bot deployment or outage becomes a new-connection outage for VPN customers.

### Workflow judgment

#### Purchase and first connection — passes

```text
payment verified
→ entitlement created
→ eligible endpoint selected and reserved
→ endpoint-scoped key created with quota limit
→ assignment committed
→ static key or stable profile delivered
→ paid clock starts only after successful provisioning
```

This matches the present durable paid workflow once assignment is inserted before remote provisioning.

#### Customer-selected region — passes

Treat region as a preference constrained by eligibility and capacity, not as an unconditional promise. Persist it on the profile/entitlement. A region change is a migration, not an in-place edit to a remote key.

#### Automatic initial region — passes with honest inputs

The selector may use endpoint state, fresh management evidence, capacity, cost, and stable hashing. Without client telemetry, it should call its result `recommended` or `assigned`, not `fastest`.

#### Endpoint degradation — partially passes

V4 can stop new assignments and initiate a controlled migration from server-side evidence. It cannot know that a particular Myanmar ISP/customer can reach the destination. For V3/V4, require reconnect and optionally customer confirmation; retain bounded rollback where quota/security policy permits.

#### Active endpoint failure — constrained

If the source server is unreachable, AuriX may be unable to read final usage or revoke the old credential. The workflow must choose one explicit policy:

1. **Conservative:** hold a reserve from the destination quota until source state is reconciled.
2. **Availability-first:** issue bounded emergency allowance and accept audited overspend.
3. **Strict:** wait for operator/source recovery, sacrificing immediate restoration.

There is no architecture that simultaneously guarantees immediate recovery and exact quota when the failed source cannot report final usage.

#### Dynamic profile refresh — passes only as reconnect-based recovery

The profile service may atomically point the stable customer URL at a verified destination. Standard clients fetch during connection. Existing sessions and clients unable to reach the profile host are not automatically moved.

#### Quota exhaustion — passes with tolerance, not byte precision

AuriX should treat committed usage as commercial truth, set the active server's limit as a safety guard, and delete exhausted credentials. Product terms need a small measurement/enforcement tolerance because Outline evaluates rolling metrics asynchronously.

#### Control-plane outage — requires two availability rules

1. Existing credentials and tunnels continue while Telegram/database workers are unavailable.
2. Dynamic profile delivery must be independently available or the customer must retain a documented static fallback.

### Final architecture decision

Adopt this split contract:

```text
V3 contract
  one entitlement
  one active regional assignment
  deterministic initial selection
  transferable remaining allowance
  assisted migration
  static key baseline
  dynamic stable profile optional

V4 contract
  health-aware allocation and drain
  bounded centrally initiated reassignment
  reconnect still required

V11 contract
  client-observed path measurements
  automatic endpoint choice
  verified reconnect/failover
  genuinely minimal customer action
```

This preserves the required business intention now without claiming client behavior the current Outline stack cannot provide.

## Executive answer

### Can the Telegram bot solve multi-region automatic selection with one Outline key?

**Not with one ordinary static `ss://` Outline key, and not by the Telegram bot alone.**

A static Outline key contains one server hostname, one port, one cipher, and one secret. After the user imports it, Telegram is not in the VPN data path. The bot cannot rewrite that imported key, observe the user's real network path, force the Outline client to reconnect, or make the client race several regions.

AuriX can nevertheless give the customer **one stable profile link** by adding an authenticated dynamic-access-key/config service. That stable `ssconf://` or HTTPS link can return the currently assigned region's Outline configuration whenever a compatible client fetches it. This removes repeated copy/paste during migrations, but it is not seamless live failover:

- AuriX chooses the assignment centrally from server health, capacity, policy, and user preference.
- The Outline client fetches the selected configuration when it starts/reconnects.
- An existing tunnel does not become another region merely because the backend changed the response.
- The control plane still lacks the customer's end-to-end reachability and latency evidence unless the customer reports it or a client supplies telemetry.

Therefore:

```text
V3: one entitlement + one active regional credential
    + optional stable dynamic profile
    + controlled reconnect/migration

V4: health/capacity-aware central reassignment

V11: true client-verified automatic endpoint selection/failover
```

### Are quota limits transferable?

**The entitlement's remaining quota can be transferred by AuriX accounting; an Outline server's native counter cannot be transferred.**

Outline calculates and enforces each access key's data limit from metrics local to that server. A new key on another server begins with a separate usage history. If a 100 GiB entitlement consumed 35 GiB in Singapore, AuriX may create the Japan replacement with a 65 GiB server-side limit. Outline itself does not carry the 35 GiB counter to Japan.

The safe V3 model is consequently:

```text
subscription quota             = 100 GiB
usage committed before move    =  35 GiB
destination safety limit       =  65 GiB
```

This is a migration of **remaining allowance**, not a transfer of an Outline counter.

Do not keep full-quota replicas active on two servers. Two independent 100 GiB limits permit approximately 200 GiB in aggregate, and a polling bot notices the overspend only after traffic has occurred.

## What “one key” can mean

The phrase has three materially different meanings.

| Meaning | Feasible? | Auto-selection quality | Quota consequence |
|---|---:|---|---|
| One static Outline `ss://` key generated by one server | Yes, current behavior | None; it targets one host/port | One server-local counter |
| One static key whose secret/port is cloned across servers behind DNS | Technically possible with careful server configuration | Weak and nondeterministic; DNS/client caching is not health-aware failover | Every server has an independent counter; unsafe without central accounting |
| One stable AuriX dynamic profile URL that returns a selected Outline config | Yes, with new service and compatible clients | Central selection on fetch/reconnect; not instant in-session failover | AuriX can carry remaining entitlement quota into each replacement credential |

The recommended interpretation is the third: **one stable customer-facing profile, not one simultaneously valid native server credential**.

## Outline semantics that govern the answer

### Static access URLs are endpoint-specific

The official server constructs `accessUrl` from the access key's server hostname, port, method, and password. The Management API exposes key creation, per-key limits, and transfer metrics on that particular server. See the official [Outline Server API](https://github.com/Jigsaw-Code/outline-server/blob/de566bfc9ff4429ae839e304b4d7e03b703ce415/src/shadowbox/server/api.yml) and [access URL construction](https://github.com/Jigsaw-Code/outline-server/blob/de566bfc9ff4429ae839e304b4d7e03b703ce415/src/shadowbox/server/manager_service.ts).

The current AuriX adapter follows that model exactly: `create_key` returns one server's `accessUrl`, `transfer_metrics` reads one server, and `set_data_limit` changes one key on that server ([`outline_adapter.py`](../outline_adapter.py#L86)).

### Dynamic access keys provide indirection, not autonomous roaming

The official client recognizes static `ss://` keys and dynamic `ssconf://`/HTTPS keys. A dynamic key stores a configuration location, and the client fetches and parses the configuration during `connect()`. See the official [client key parser](https://github.com/Jigsaw-Code/outline-apps/blob/8aa8d4f799816050b3fb88d75be899697c2191cf/client/web/app/outline_server_repository/config.ts), [connect path](https://github.com/Jigsaw-Code/outline-apps/blob/8aa8d4f799816050b3fb88d75be899697c2191cf/client/web/app/outline_server_repository/server.ts), and [configuration reference](https://github.com/Jigsaw-Code/outline-apps/blob/8aa8d4f799816050b3fb88d75be899697c2191cf/client/config.md).

The configuration format's `first-supported` option is often misunderstood. It selects the first configuration type the application can parse; it is a compatibility mechanism, not a health probe or fastest-region race. The official implementation skips unsupported options and returns the first supported parser result ([implementation](https://github.com/Jigsaw-Code/outline-apps/blob/8aa8d4f799816050b3fb88d75be899697c2191cf/client/go/outline/configregistry/config_first_supported.go)).

Thus, a dynamic key lets AuriX change what is fetched. It does not by itself create latency-based multi-region failover.

### Data limits and transfer metrics are server-local

Outline's transfer endpoint returns `bytesTransferredByUserId` for the server answering the Management API request. The server enforces access-key limits using a 30-day Prometheus `increase(...)` query grouped by access-key ID ([metrics implementation](https://github.com/Jigsaw-Code/outline-server/blob/de566bfc9ff4429ae839e304b4d7e03b703ce415/src/shadowbox/server/manager_metrics.ts), [limit enforcement](https://github.com/Jigsaw-Code/outline-server/blob/de566bfc9ff4429ae839e304b4d7e03b703ce415/src/shadowbox/server/server_access_key.ts)).

Consequences:

1. The same key ID on two servers does not imply a shared counter.
2. The same password on two servers does not imply a shared counter.
3. A destination server cannot infer source-server consumption.
4. The 30-day reading is a trailing-window observation, not an immutable lifetime billing ledger.
5. Server-local limits remain valuable as periodically enforced safety brakes, but the AuriX entitlement ledger must become authoritative across migrations.
6. Reusing an old remote key ID on the same endpoint can encounter that ID's retained trailing-window metrics; each migration generation therefore needs a fresh remote ID, while retries of the same generation reuse its deterministic ID.

### Current Server API capability matrix

Fact-checked against official `outline-server` commit [`de566bfc`](https://github.com/Jigsaw-Code/outline-server/tree/de566bfc9ff4429ae839e304b4d7e03b703ce415) and official `outline-apps` commit [`8aa8d4f`](https://github.com/Jigsaw-Code/outline-apps/tree/8aa8d4f799816050b3fb88d75be899697c2191cf) on 2026-08-29:

| Proposed capability | Native Outline Server API? | Verified behavior |
|---|---:|---|
| Get one server's identity/version/config | Yes | `GET /server` |
| Configure hostname used in newly generated keys | Yes | API changes generated-key hostname; DNS must be configured independently |
| Configure default port for new keys | Yes | Server-scoped setting |
| Create/list/get/delete keys | Yes | Full key material and static `accessUrl` are returned |
| Create with caller-selected key ID | Yes | `PUT /access-keys/{id}`; an existing ID returns conflict, so timeout recovery still requires `GET` reconciliation |
| Supply password, method, port, and initial limit | Yes | Allows carefully cloning credential material onto another server; it does not link their state |
| Rename key | Yes | Metadata only |
| Set/remove per-key byte limit | Yes | Limit is local to that server/key |
| Set/remove a default limit for all keys | Yes | Server-local default |
| Read per-key transferred bytes | Yes | `GET /metrics/transfer`, local to the queried server |
| Stable bandwidth/device/session telemetry | No | Richer `/experimental/server/metrics` exists but is explicitly unstable; there is no stable active-session API |
| Per-key wall-clock expiry | No | AuriX must schedule deletion/revocation |
| Reset a key's usage counter | No | Removing/reapplying a limit does not erase the Prometheus history |
| Transfer quota/usage to another key or server | No | Must be calculated and committed by AuriX |
| Multi-server or region registry | No | Each Management API controls one server; region/provider metadata is external |
| Cross-server create/move/copy transaction | No | AuriX must orchestrate independent APIs and reconcile partial failure |
| Health-aware region selection | No | No fleet selector or customer-path probe |
| Capacity reservation | No | Must be modeled in AuriX; experimental metrics may contribute evidence |
| Host dynamic access-key content | No | Dynamic profile hosting is a separate HTTPS control-plane service |
| Consume `ssconf://`/HTTPS dynamic keys | Client feature | Official Outline clients fetch dynamic configuration during `connect()` |
| Seamless in-session region failover | No | Neither the Management API nor standard client config provides this fleet behavior |

### Native quota precision correction

The current server implementation evaluates limits from:

```text
sum(increase(shadowsocks_data_bytes{...}[720h])) by (access_key)
```

and schedules normal limit enforcement every hour. Creating/changing a limit also triggers an asynchronous evaluation, but this is still not a transactional byte cutoff. Traffic can exceed the nominal limit between metric collection and enforcement. Because the query is a rolling 30-day window, an undeleted raw Outline key can become eligible again after old usage ages out.

Therefore, AuriX must not advertise byte-exact enforcement. Its current practice of observing exhaustion and deleting the key is stronger for one-time subscription quotas than leaving an over-limit key present indefinitely, but an explicit overrun tolerance remains necessary.

### API support is broader than the current AuriX adapter

The current `OutlineClient` implements server info, basic transfer metrics, key CRUD/reconciliation, rename, and per-key limit operations. It does **not** currently expose:

- caller-supplied password, method, or port during key creation;
- server hostname/default-port management;
- server-wide default-limit management;
- metrics-sharing settings;
- experimental server metrics.

That subset is sufficient for the current single-server product. V3 should add only the official operations required by a concrete workflow; cloning credentials or depending on experimental telemetry should not be added merely because the upstream API permits them.

## What the current code can and cannot do

### Current strengths

- Paid provisioning uses deterministic key identity and ambiguity reconciliation ([`commerce_worker.py`](../commerce_worker.py#L336)).
- Paid and free keys are created with native Outline limits ([`commerce_worker.py`](../commerce_worker.py#L355), [`entitlements.py`](../entitlements.py#L137)).
- Quota enforcement records the latest server metric and schedules idempotent revocation ([`commerce_worker.py`](../commerce_worker.py#L711)).
- The native limit blocks traffic even if the Telegram maintenance pass is delayed.

These are strong single-server safety properties.

### Current blockers

1. Runtime creates one process-global `OutlineClient` and injects it into every service ([`runtime.py`](../runtime.py#L87)).
2. Free and paid records store only `outline_key_id`; neither identifies endpoint or region ([`free_repository.py`](../free_repository.py#L41), [`commerce_repositories.py`](../commerce_repositories.py#L109)).
3. `outline_key_id` is globally unique locally even though key IDs are meaningful only with an endpoint.
4. Maintenance fetches one metrics map and applies it to both free and paid keys.
5. Current quota enforcement compares one raw server reading directly with the full entitlement quota. It has no committed cross-credential usage ledger.
6. AuriX returns and stores the server's static `accessUrl`; it does not host dynamic profiles.
7. Telegram supplies no client-side connectivity measurement or reconnect control.

Simply changing `OUTLINE_API_URL` into a list would therefore be incorrect. It could associate a key ID with the wrong server, interpret a missing metric as zero, reset apparent consumption during migration, or delete a key from the wrong endpoint.

## Candidate designs

### Design A — bot chooses once and sends one static regional key

Flow:

```text
purchase/claim
→ selector chooses eligible region
→ create key there
→ Telegram sends ss:// key
```

Advantages:

- smallest V3 implementation;
- works with existing Outline clients;
- native server quota remains exact for the assigned endpoint;
- no new public profile service.

Limits:

- changing region requires a replacement key and customer import;
- bot cannot recover an active tunnel automatically;
- “auto-selection” means selection at provisioning, not continuous failover.

This should be the V3 baseline even if dynamic profiles are added later.

### Design B — clone one static credential across regions and use DNS

Servers can be configured with matching Shadowsocks method, password, and port, while a shared hostname resolves to different regional IPs.

Why it is not recommended:

- DNS selection is not a reliable latency/health policy;
- client and resolver caches delay changes;
- active TCP/UDP sessions do not migrate;
- every server independently permits its configured limit;
- key revocation and rotation must converge across all replicas;
- one leaked credential has a larger blast radius;
- attribution is `(endpoint, key_id)`, not `key_id`;
- partial replication failures create difficult-to-detect access inconsistencies.

This trades a visible replacement workflow for hidden distributed-state problems.

### Design C — one stable AuriX dynamic profile, one active assignment

Flow:

```text
Telegram sends stable authenticated profile URL once
→ client fetches profile
→ AuriX resolves entitlement's active assignment
→ profile service returns one Outline tunnel config
→ client connects to that region
```

On migration:

```text
create destination credential but keep it unpublished
→ verify destination management state
→ stop resolving and disable source credential
→ settle and commit final source usage
→ update destination limit to the final remaining allowance
→ atomically change active assignment/profile generation
→ ask client to reconnect or let next reconnect fetch it
→ reconcile source deletion and destination presence
```

Advantages:

- stable customer-facing link;
- transport details remain replaceable;
- region changes do not require another secret in Telegram history;
- profile can return a clear quota-expired or maintenance error;
- natural bridge toward V10's protocol-agnostic profile model.

Requirements and risks:

- highly available HTTPS profile endpoint independent of Telegram polling;
- long random bearer token or signed request, token hashing at rest, rotation, revocation, and rate limits;
- `Cache-Control: no-store` or carefully bounded expiry and generation/ETag handling;
- explicit client compatibility tests on every supported platform/version;
- no Outline Management API URL, certificate pin, or administrative secret in client output;
- reconnection semantics must be documented honestly.

This is the recommended customer-experience enhancement after the core V3 assignment model is proven.

### Design D — custom/managed client performs verified selection

A managed client can probe candidate routes from the customer's actual network, choose a working/fast endpoint, report bounded evidence, and reconnect. This is the first design that can provide genuine client-observed automatic selection.

It also introduces software distribution, updates, privacy, telemetry, platform VPN APIs, support, and safety obligations. It belongs to V11, not V3.

## Correct quota architecture

### Separate commercial truth from server enforcement

Use three layers:

```text
Entitlement ledger
  granted_bytes
  committed_usage_bytes
  remaining_bytes

Credential usage observations
  endpoint_id + remote_key_id
  observed_window_bytes
  last_observed_at
  source/freshness/confidence

Server safety limit
  limit on the currently active Outline credential
```

The invariant is:

```text
remaining_bytes = max(0, granted_bytes - committed_usage_bytes)
```

The server limit is derived from `remaining_bytes`; it is not the commercial source of truth.

### Recommended single-active migration transaction

For strict quota preservation, prepare the destination privately, then use a short break-before-publish cutover:

1. Lock the entitlement/migration intent so only one migration can run.
2. Mark the source assignment `DRAINING`; stop new profile resolution to it.
3. Create a fresh destination credential, but do not publish its secret/profile yet.
4. Verify the destination management record and intended configuration.
5. Disable/delete the source key to stop additional usage.
6. Allow a bounded metric-settlement interval, then persist the freshest source checkpoint.
7. Increase committed entitlement usage idempotently.
8. Compute `remaining = granted - committed`.
9. If remaining is zero, delete the unpublished destination and terminate.
10. Set the destination's native limit to `remaining` and verify the management record.
11. Atomically activate/publish the destination assignment and profile generation.
12. Notify the user to reconnect when the client cannot refresh automatically.
13. Reconcile both endpoints until the source is confirmed absent and destination present.

The unpublished credential is not a security boundary by itself; its access URL must remain encrypted and inaccessible to customers until cutover. If availability requires both credentials to be customer-usable at once, reserve a small explicit overlap allowance and accept a bounded overspend risk. Never silently set the full original quota on both endpoints.

### Why summing raw snapshots is unsafe

Do not calculate:

```text
global usage = latest Singapore metric + latest Japan metric
```

as the permanent ledger without qualification. Each value is a trailing 30-day query and may later fall as old samples leave the window. It may also be missing or stale during management outages. AuriX needs immutable migration checkpoints and idempotent committed deltas, with monotonicity/reset detection per `(endpoint_id, remote_key_id)`.

For the present plans, a simpler safe rule is available: keep one active credential, commit the source reading exactly once when closing it, and use only the current active credential's usage against its assigned remaining allowance.

### Failure policy

| Failure | Safe behavior |
|---|---|
| Source metrics unavailable | Do not claim an exact transfer; retry or migrate with a conservative reserved deduction |
| Source cannot be disabled | Do not activate a full-limit destination; require bounded emergency policy/operator action |
| Destination create times out | Reconcile by deterministic `(endpoint_id, credential_id)` before retrying |
| Profile changes but destination is unverified | Roll profile generation back to the known-good assignment |
| Destination succeeds but DB commit fails | Reconcile and adopt/delete the orphan deterministically |
| Two migration workers race | Unique active-assignment constraint plus leased migration job prevents double activation |
| Metrics become smaller | Treat as reset/window change; never reduce committed usage |
| Quota reaches zero during migration | Publish an entitlement-expired profile error and revoke all credentials |

## Required V3 data model changes

Minimum records:

```text
endpoints
  id, provider_id, region_id, transport_id, state, secret_ref

connectivity_assignments
  id, entitlement_id, endpoint_id, state, profile_generation

credentials
  id, assignment_id, endpoint_id, remote_key_id,
  encrypted_config, server_limit_bytes, state

usage_observations
  endpoint_id, credential_id, observed_bytes,
  observed_at, window_kind, freshness, source

usage_commits
  id, entitlement_id, credential_id, committed_bytes,
  idempotency_key, reason, committed_at

migrations
  id, entitlement_id, source_assignment_id,
  destination_endpoint_id, state, lease, attempt

dynamic_profiles (optional V3.x)
  entitlement_id, token_hash, generation, state, expires_at
```

Required uniqueness/invariants:

- remote identity is unique on `(endpoint_id, remote_key_id)`, not globally on `remote_key_id`;
- at most one `ACTIVE` assignment per entitlement in V3;
- every credential belongs to exactly one endpoint and assignment;
- a credential's deterministic remote ID includes the assignment/migration generation, so retries converge but a later return to the same endpoint does not inherit an old ID's metric window;
- every usage commit has an idempotency key;
- committed usage never decreases;
- active server limit never exceeds entitlement remaining quota except an explicit audited overlap reserve;
- profile resolution returns only a verified active assignment.

## Selector responsibility

The bot/control plane can select centrally using evidence it actually owns:

```text
eligible endpoint
= ACTIVE
+ accepts assignments
+ fresh management probe
+ key/transfer/connection headroom
+ customer-allowed region
+ provider/region diversity policy
```

Initial deterministic policy:

1. Honor an explicit customer region when healthy and eligible.
2. Otherwise choose the nearest configured region class only if location inference is consented and trustworthy.
3. Prefer the lowest normalized load among eligible endpoints.
4. Break ties by stable hash of entitlement ID to avoid oscillation.
5. Keep the assignment sticky until a real drain, failure, customer request, or policy migration.

Do not call this “fastest region” without measurements from the customer's network. Server-side probes show server health, not whether a Myanmar ISP can reach that endpoint well.

## Implementation sequence for this repository

### V3.0 — correct multi-region core

1. Add migration version 2 for endpoint, assignment, credential, observation, usage-commit, and migration tables.
2. Backfill the current server and every current key with an explicit endpoint ID.
3. Replace global key-ID uniqueness with endpoint-scoped identity.
4. Compose endpoint-scoped Outline clients in `runtime.py`.
5. Route paid provisioning through a persisted assignment before calling Outline.
6. Move free/trial provisioning to the same durable workflow.
7. Poll and attribute metrics independently per endpoint.
8. Implement one-active-assignment quota-preserving migration.
9. Add a second endpoint in the same region, then a second region.
10. Expose Telegram commands for region preference, status, refresh, and confirmed manual migration.

### V3.x — stable dynamic profile

1. Add a small HTTPS profile service, not a Telegram callback URL.
2. Issue one revocable customer profile token.
3. Resolve only the active verified assignment.
4. Test config refresh/reconnect behavior on Android, iOS, Windows, macOS, and Linux versions AuriX supports.
5. Keep static replacement delivery as a fallback until compatibility is proven.

### V4 — health-aware controlled movement

- health state machine with hysteresis;
- capacity reservation before movement;
- bounded migration batches and cooldowns;
- destination verification and automatic rollback;
- evidence explaining every selection decision.

### V11 — genuine client-side automatic selection

- client-observed connectivity and latency;
- privacy-bounded telemetry;
- candidate probing/racing;
- verified reconnect;
- safe profile/config update channel.

## Acceptance tests

The feature is not complete until these pass against real Outline servers and supported clients:

1. A static key imported for region A does not silently claim to roam.
2. A dynamic profile moved from A to B resolves B after reconnect on every supported client.
3. An existing session's behavior during profile change is measured and documented.
4. A 100 GiB entitlement using 35 GiB on A receives no more than approximately 65 GiB on B, within the declared accounting tolerance.
5. Two concurrent migration attempts cannot produce two active assignments.
6. Ambiguous destination creation does not produce duplicate credentials.
7. Source-management outage cannot reset quota to the full grant.
8. Same remote key IDs on different endpoints are attributed correctly.
9. Destination failure before cutover preserves the known-good profile.
10. Failure after cutover either converges to B or rolls back to A.
11. Quota exhaustion publishes a clear dynamic-profile error and revokes all active credentials.
12. Profile tokens are unguessable, revocable, rate-limited, and absent from logs.

## Final decision

For AuriX V3:

```text
Use one entitlement.
Use one active endpoint assignment.
Use one endpoint-scoped Outline credential at a time.
Transfer remaining allowance through AuriX accounting.
Optionally expose one stable dynamic profile link.
Do not clone a full-quota credential across regions.
Do not promise seamless or fastest-region failover from Telegram alone.
```

The Telegram bot remains the customer and operator control surface. The endpoint selector, dynamic-profile service, durable migration worker, and quota ledger are the systems that make multi-region operation correct.
