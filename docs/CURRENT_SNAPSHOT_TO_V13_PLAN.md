# AuriX current snapshot to V13: canonical version plan

> Historical planning baseline. The endpoint-aware V3 foundation was implemented
> on 2026-09-02. Use `AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md` as the canonical
> current fleet architecture and activation runbook.

Status: re-audited implementation roadmap derived from the complete VPN conversations  
Baseline date: 2026-08-29 (Asia/Rangoon)  
Current network version: **V2 — hardened single-Outline entitlement platform**  
Current software baseline: **V2, refactor Phase 8 — modular monolith with compatibility facades**

## Decision summary

The current codebase should not jump directly to a multi-transport or autonomous system.

The recommended production path is:

```text
Current V2
  ↓
V3 — Multi-server, multi-region Outline
  ↓
V4 — Health-aware adaptive Outline
  ↓  only if measured Outline failures justify another transport
V9 — Outline + Xray hybrid
  ↓
V10 — Protocol-agnostic adaptive endpoint fabric
  ↓
V11 — Verified client-assisted adaptation
  ↓
V13 — Bounded resilient connectivity operating system
```

Versions V5–V8 are useful research and infrastructure branches. They are not mandatory sequential releases. V12 represents broad autonomous infrastructure control; its useful capabilities should be introduced behind V13 safety boundaries rather than launched as unrestricted automation.

The immediate product to build is **V3**.

The interfaces and database vocabulary introduced during V3 should be compatible with **V10/V13 concepts**, but only the Outline adapter should operate initially. The repository's new ports, repositories, worker boundary, migrations, runtime composition, and Telegram modules make this materially safer than it was in the previous snapshot; they do not themselves constitute multi-server support.

## Conversation source map

This plan consolidates the complete locally extracted message history, not only the short conclusions from each shared page:

| Conversation artifact | Planning contribution |
|---|---|
| [`V1–V13 VPN Versions Deep Exploration`](../chatgpt_conversation_6a9246bf.md) | Canonical version vocabulary, adaptive-system progression, safety boundaries, and V13 north star |
| [`V13+ VPN Market Future Judgment`](../chatgpt_conversation_6a924781.md) | Positioning, economics, local/global market constraints, and why resilient connectivity matters more than a generic VPN label |
| [`V13 Research: Outline VPN Resilience`](../chatgpt_conversation_6a924803.md) | Outline resilience mechanisms, private-distribution effects, threat model, and evidence gates for transport diversity |
| [`Outline on Render`](../chatgpt_conversation_6a8f6e31.md) | Original deployment constraints, separation of bot hosting from the VPN data plane, and the initial single-server operating model |

The resulting version assignments are exact:

- **V3** = multi-server, multi-region, single-transport Outline; this is the requested next version and the best near-term reliability-to-cost ratio.
- **V10** = protocol-agnostic domain model becomes authoritative; its vocabulary should influence V3 schema and interfaces now.
- **V9** = first production multi-transport system (Outline + Xray); it remains evidence-gated until V3/V4 show endpoint and region diversity are insufficient.

## Re-audit result

The repository changed substantially after the first roadmap. The current source was therefore inspected and validated again rather than inferred from the previous study.

Verified locally from the current snapshot:

```text
132 tests passing
67% branch coverage across configured application/deploy modules
Ruff correctness checks passing
all production Python modules compiling
```

The refactor delivered eight structural phases:

| Delivered foundation | Current implementation | Effect on version plan |
|---|---|---|
| CI and coverage gate | GitHub Actions, Python 3.13.4, Ruff, compile, tests, coverage | Removes the old “add CI” prerequisite |
| Persistence seam | Shared SQLite connection lifecycle and repository protocols | Makes endpoint repositories easier to add |
| Numbered migration registry | Component-scoped immutable migration history | V3 schema must be migration version 2+, not startup `ALTER` drift |
| Free/trial domain extraction | `entitlements.py` + `free_repository.py` | Policy is separated from Telegram, but provisioning is still synchronous/single-endpoint |
| Paid domain extraction | models, repositories, service, worker | V3 assignment can be inserted before worker execution without rewriting commerce |
| External ports | Outline, storage, extraction, notification protocols | Good dependency inversion; current VPN port is Outline-specific, not protocol-agnostic |
| Runtime composition | `runtime.py` + small `app.py` facade | Endpoint registry/factory belongs in composition, not Telegram |
| Telegram decomposition | command/callback/admin/maintenance modules | Endpoint admin panels can be added without growing one monolith |
| Receipt evidence hardening | private Supabase Storage adapter and immutable metadata | Completes an important V2 production-control concern |
| Admin mutation hardening | durable, state-bound, expiring, single-use confirmations | Stronger operator control for future drain/migrate actions |
| Entitlement improvements | multiple paid keys, quota warnings, maintenance heartbeat | Better commercial/operational V2, still no endpoint identity |

Important remaining facts:

- `runtime.py` still reads exactly one `OUTLINE_API_URL` and one certificate fingerprint, creates one `OutlineClient`, and injects it into both free and paid services.
- `ports.OutlineGateway` is an Outline API contract. It is not yet a generic endpoint-scoped `VpnAdapter`.
- `paid_vpn_keys` and free `keys` still store `outline_key_id` without endpoint, region, provider, assignment, or transport identity.
- Free/trial creation still performs the remote Outline call inside the entitlement database transaction.
- Notification outbox rows are selected and sent without an atomic delivery lease, so independent notification workers remain unsafe.
- Startup still exits when the one Outline management endpoint is unavailable.
- Long polling, one application process, and one maintenance scheduler remain the deployment model.
- The application/refactor files remain untracked in the current workspace Git repository; the branding-only Git history is not a recoverable application baseline.

## Canonical version ledger

| Version | Canonical meaning | Transport scope | Fleet scope | Automation level | Role in this plan |
|---|---|---|---|---|---|
| V0 | One private Outline server | Outline | One endpoint | Manual | Historical foundation |
| V1 | Outline plus Telegram distribution | Outline | One endpoint | Bot-assisted | Historical product shell |
| **V2** | Entitlement, commerce, key management, quota, jobs, audit | Outline | One endpoint | Durable provisioning/revocation | **Current snapshot** |
| **V3** | Multi-server, multi-region Outline | Outline | Several endpoints/regions | Deterministic allocation; manual migration | **Build next** |
| **V4** | Dynamic/health-aware Outline fleet | Outline | Several endpoints/regions/providers | Controlled endpoint selection and drain | Second production milestone |
| V5 | Xray/VLESS platform experiment | Xray/VLESS | Initially small | Manual | Optional research branch |
| V6 | VLESS + REALITY specialization | Xray/REALITY | Small specialized pool | Manual | Optional censorship-path experiment |
| V7 | Multi-transport Xray | Several Xray transports | Multi-endpoint | Transport selection inside Xray | Optional research branch |
| V8 | Multi-provider, multi-region Xray | Xray transport family | Multi-provider/region | Fleet operations | Optional infrastructure branch |
| **V9** | Outline + Xray hybrid | Two transport families | Multi-provider/region | Policy-assisted, initially manual fallback | First production multi-transport milestone |
| **V10** | Protocol-agnostic adaptive endpoint fabric | Adapter-based | Replaceable provider/region/endpoint fleet | Deterministic policy and scoring | First serious platform target |
| **V11** | Autonomous adaptation with client verification | Adapter-based | Measured fleet | Verified health-based reassignment | Later evidence-gated evolution |
| V12 | Fully autonomous resilient infrastructure | Adapter-based | Automatically provisioned/retired fleet | Broad automation | Research/high-risk stage, not direct launch target |
| **V13** | Resilient connectivity operating system | Adapter-based | Multi-failure-domain fabric | Bounded, observable, reversible automation | Long-term north star |

## Current snapshot: V2 hardened, modular Phase 8

### What exists

The current repository implements considerably more than the earlier V2 snapshot:

- Telegram customer and admin interfaces;
- public daily and monthly free entitlements;
- paid plan catalog and immutable order snapshots;
- private receipt-object storage, immutable receipt metadata, untrusted LLM extraction, and human verification;
- immutable customer wallet ledger;
- independent paid entitlements with multiple simultaneous keys, activation, expiry, refund, and revocation;
- one pinned Outline management client;
- deterministic paid-key creation and ambiguous-response reconciliation;
- durable provisioning jobs, notification retries, dead-letter state, and audit events;
- thresholded quota warnings, per-key quota observation, and hard key deletion;
- SQLite deployment and optional PostgreSQL repository;
- persisted maintenance heartbeat and stage isolation;
- durable, state-bound administrator confirmation challenges;
- shared persistence protocols and numbered migration history;
- explicit external adapter ports and a separated reliable-worker boundary;
- modular runtime, commerce, entitlement, Telegram, and adapter code with compatibility facades;
- CI and local validation covering 132 tests at 67% branch coverage.

### What makes it V2 rather than V3

The process loads one `OUTLINE_API_URL`, creates one `OutlineClient`, and injects it into every claim and paid-provisioning path. Keys have no endpoint assignment. There is no endpoint, region, provider, or transport registry.

Current topology:

```text
Customer
  ↓
Telegram + AuriX V2 control plane
  ↓
one Outline API
  ↓
one Outline server/IP/region
```

### V2 foundation status

Completed since the earlier roadmap:

- [x] CI runs compile, Ruff, tests, and branch coverage on deployment Python.
- [x] SQLite lifecycle and repository contracts are explicit.
- [x] A numbered migration registry exists for SQLite and PostgreSQL.
- [x] Entitlement, commerce, worker, adapter, runtime, and Telegram boundaries are extracted.
- [x] Private receipt storage and stronger admin confirmation controls are implemented.
- [x] Multiple paid entitlements, quota warnings, maintenance heartbeat, and failure visibility are covered by tests.

Still required before or as the first bounded part of V3:

1. Commit the current application, tests, docs, and deployment files as a recoverable baseline.
2. Add `requires-python = ">=3.13,<3.14"` and a reproducible lock/check strategy; local `uv` currently falls back to Python 3.12 when not explicitly constrained.
3. Move free/trial provisioning onto the same durable intent/job/reconcile pattern as paid provisioning.
4. Give notification delivery an atomic claim/lease before running multiple workers.
5. Permit degraded control-plane startup when Outline management is unavailable.
6. Convert future schema changes into actual migration version 2+ entries; version 1 currently adopts the legacy bootstrap rather than creating new endpoint structures.
7. Automate database backups and complete a restore drill, including receipt-object reconciliation.
8. Run the documented live one-server acceptance test with known users.
9. Capture real usage, connection success, support, and contribution-margin evidence.

Exit gate:

> One Outline endpoint can be operated, restored, reconciled, and measured without manual database repair, and the modular Phase-8 source is committed as a recoverable application baseline.

## V3 — Multi-server, multi-region Outline

### Product definition

V3 is the exact version requested for multiple Outline servers:

```text
AuriX control plane
  ├── Outline SG-01
  ├── Outline SG-02
  ├── Outline JP-01
  └── future Outline endpoints
```

It remains a **single-transport system**. Every endpoint uses Outline/Shadowsocks. Resilience comes from endpoint, server, IP, provider, and region diversity—not protocol diversity.

### Customer promise

Allowed claim:

> AuriX assigns customers to available independently managed regional Outline endpoints and can replace an assignment while preserving subscription state and remaining allowance. A reconnect or replacement import may be required.

Not allowed:

> One key always finds the fastest region, preserves a byte-exact global quota, fails over seamlessly, or provides an unblockable VPN.

### Domain model

Add:

```text
providers
regions
transports
endpoints
connectivity_profiles
endpoint_assignments
credentials
endpoint_capacity_snapshots
```

Minimum fields:

```text
Provider
  id, code, name, status

Region
  id, code, display_name, status

Transport
  id, code, adapter_type, capabilities, status

Endpoint
  id, provider_id, region_id, transport_id
  status, accepts_new_assignments
  management_secret_reference, certificate_fingerprint
  capacity_policy, created_at, retired_at

ConnectivityProfile
  id, customer_id, subscription_id
  preferred_region_policy, lifecycle_status

EndpointAssignment
  id, profile_id, endpoint_id
  role, status, assigned_at, released_at, reason

Credential
  id, profile_id, assignment_id, transport_id
  external_id, encrypted_secret, status
  created_at, revoked_at
```

`Transport` exists in V3 so the model does not need a destructive rewrite later. Only one production row/adapter—`outline`—is enabled.

### Code architecture

Introduce one generic boundary:

```python
class VpnAdapter:
    def provision(self, endpoint, credential_intent): ...
    def observe_credential(self, endpoint, external_id): ...
    def revoke(self, endpoint, external_id): ...
    def read_usage(self, endpoint): ...
    def probe_management(self, endpoint): ...
```

Implement only:

```text
OutlineAdapter(VpnAdapter)
```

Replace the process-global Outline client with:

```text
endpoint record
  ↓
secret/certificate lookup
  ↓
endpoint-scoped OutlineAdapter client
```

Keep the system a modular monolith. Do not introduce microservices.

### V3 work packages mapped to the current modules

The current refactor removes the need for another broad extraction. V3 should be implemented through narrow additions:

| Work package | Current seam | Required change |
|---|---|---|
| Migration 2 | `migrations.py` | Add immutable endpoint/provider/region/transport/assignment/credential schema for both dialects |
| Connectivity models | new `connectivity_models.py` | States, records, policy input/output, capability metadata |
| Connectivity repository | new `connectivity_repositories.py` | Endpoint registry, assignments, credential metadata, capacity snapshots, migration records |
| Generic VPN port | `ports.py` | Add endpoint-scoped `VpnAdapter`; retain `OutlineGateway` as the concrete Outline protocol if useful |
| Outline implementation | `outline_adapter.py` | Wrap client creation behind an endpoint/client factory using per-endpoint URL and pin references |
| Assignment service | new `connectivity_service.py` | Deterministic eligibility, selection, reservation, drain, and assisted migration intent |
| Worker routing | `commerce_worker.py` | Resolve the persisted assignment first, then invoke the selected endpoint adapter; retries stay pinned |
| Free/trial convergence | `entitlements.py` | Create durable credential intent/job rather than calling one global Outline client inside the DB transaction |
| Runtime composition | `runtime.py` | Compose endpoint repository, adapter registry/factory, and services; remove mandatory process-global Outline readiness |
| Operator UI | `telegram_admin_panels.py`, `telegram_callbacks.py`, `telegram_admin.py` | Add endpoint views and confirmed state-changing operations through the existing authorization boundary |
| Maintenance | `telegram_maintenance.py` | Iterate endpoint-scoped metrics/probes with stage isolation; do not reuse one global metrics snapshot across unrelated endpoints |
| Compatibility | `app.py`, `commerce.py` | Preserve current imports while callers migrate; do not put new domain logic in facades |

Recommended implementation order:

```text
WP0 recoverable Git baseline + live single-node proof
→ WP1 migration 2 and read-only endpoint registry
→ WP2 register/backfill current endpoint
→ WP3 endpoint-scoped Outline factory and generic credential identity
→ WP4 paid provisioning through persisted assignment
→ WP5 free/trial through durable assignment/job
→ WP6 second Outline endpoint in same region
→ WP7 deterministic allocation and capacity reservation
→ WP8 second region
→ WP9 drain and assisted migration
→ WP10 live failure exercises and V3 promotion
```

The first server backfill is mandatory. Existing credentials must not become “endpoint unknown” after migration.

### Allocation policy

Start deterministic and explainable:

```text
eligible endpoint
= ACTIVE
+ accepts new assignments
+ management evidence is fresh
+ requested region is allowed
+ key/customer/transfer headroom remains
```

Choose using stable ordering:

```text
greatest verified headroom
→ lowest concentration
→ stable endpoint ID tie-break
```

Do not use machine learning, latency prediction, or automatic migration.

### Endpoint states

```text
PROVISIONING
ACTIVE
DEGRADED
DRAINING
FAILED
RETIRED
```

In V3, operators control most transitions. A failed management request does not automatically move customers.

### Admin operations

Add commands or service operations for:

- endpoint list/detail;
- register endpoint;
- verify management connectivity and certificate pin;
- enable/stop new allocation;
- view assigned customers and credentials;
- drain endpoint;
- manually create a replacement assignment;
- record migration completion/failure;
- retire endpoint after zero active credentials.

### Migration model

With third-party Outline clients, migration is initially assisted:

```text
operator marks endpoint DRAINING/FAILED
→ no new customers allocated
→ destination credential created and verified but kept unpublished
→ source credential disabled and final usage observed
→ source usage committed to the entitlement ledger
→ destination limit updated to only the remaining quota
→ destination assignment published
→ customer receives replacement instructions/key
→ customer confirms or support verifies connection
→ old credential deletion verified
→ assignment closed
```

Outline counters and limits are server-local; V3 transfers the entitlement's remaining allowance, not the remote counter. Keep one active assignment per entitlement rather than cloning a full-quota credential across regions. A make-before-break exception needs an explicit small overlap reserve and an audited overspend bound. The detailed accounting and failure model is in [`deep-study-multi-region-single-key-and-quota.md`](deep-study-multi-region-single-key-and-quota.md).

Dynamic Outline access keys may provide one stable customer-facing profile and improve configuration replacement, but the Telegram bot alone cannot make a static key roam or force an active client to reconnect. V3 must not depend on dynamic-profile behavior until it is proven against every deployed client/server version.

### V3 test plan

- two fake endpoints receive independent clients and certificate pins;
- allocation never selects inactive, draining, or full endpoints;
- endpoint failure does not modify payment or subscription truth;
- retries remain bound to the selected endpoint unless an explicit reassignment occurs;
- ambiguous remote create produces one credential on one endpoint;
- credentials and usage cannot be attributed to the wrong endpoint;
- draining prevents new allocations but preserves existing connectivity;
- manual migration is idempotent;
- migration commits source usage once and gives the destination no more than the remaining entitlement quota;
- simultaneous source/destination validity cannot multiply the customer's quota silently;
- one region can fail without preventing allocations to another eligible region;
- live acceptance uses at least two Outline servers and two regions.

### V3 completion gate

> New customers are safely distributed across at least two Outline servers, at least two regions are represented, endpoint ownership is explicit in the database, and an operator can drain or migrate a cohort without changing orders, payments, or entitlements.

## V4 — Health-aware adaptive Outline

### Product definition

V4 keeps Outline as the only transport but makes endpoint selection responsive to measured health and capacity.

```text
Customer profile
  ↓
policy engine
  ↓
health/capacity-aware endpoint selector
  ↓
Outline endpoint
```

### New data model

Add:

```text
endpoint_health_observations
endpoint_state_transitions
allocation_decisions
migration_attempts
policy_versions
```

Each observation records:

```text
endpoint
signal type
network/region/ISP context when known
value/result
observed_at
expires_at
confidence
source
```

### Health dimensions

Do not collapse health into one Boolean. Track separately:

- management API reachability;
- server/process/resource status;
- data-plane TCP reachability;
- data-plane UDP behavior;
- authenticated connection success;
- regional/ISP-specific client success;
- latency and loss indicators;
- capacity/headroom;
- evidence freshness and confidence.

### Safe selection logic

Add:

- circuit breakers;
- minimum evidence/sample thresholds;
- hysteresis before state transitions;
- cooldown before recovery;
- capacity-aware load shedding;
- maximum migrations per time window;
- deterministic policy versions and decision audit.

### V4 migration behavior

Without an AuriX client, V4 can automate detection and replacement preparation, but customer application remains assisted.

Allowed automation:

```text
detect strong endpoint degradation
→ stop new allocation
→ notify operator
→ prepare bounded replacement cohort
```

Still gated/manual:

```text
issue replacement
→ customer imports/reconnects
→ verify success
→ revoke old credential
```

### V4 completion gate

> The system stops allocating to demonstrably bad or overloaded Outline endpoints without reacting to one transient probe, and controlled migration cannot overload a destination or create a fleet-wide movement storm.

## V5–V8 — Optional protocol research branches

These versions answer technical questions. They should run in isolated test pools and must not force the production roadmap forward.

### V5 — Xray/VLESS

Purpose:

- validate Xray operations and client compatibility;
- compare connection success, latency, stability, support burden, and cost with Outline;
- learn credential/config lifecycle differences.

This is not “Outline replacement.” It is a candidate second adapter.

Exit evidence:

> Xray/VLESS provides measurable value on target networks that cannot be obtained by adding or replacing Outline endpoints.

### V6 — VLESS + REALITY

Purpose:

- test one specialized censorship-resistance path;
- validate deployment, rotation, upgrades, client compatibility, and failure behavior;
- measure actual target-network success rather than theoretical strength.

Do not deploy REALITY everywhere by default.

### V7 — Multi-transport Xray

Purpose:

- compare several Xray transport modes;
- create transport capability metadata;
- test configuration explosion and client-support costs;
- determine whether transport diversity improves reliability enough to justify operations.

### V8 — Multi-provider, multi-region Xray

Purpose:

- test provider/ASN/region independence for the Xray branch;
- validate IaC, secret isolation, upgrades, monitoring, and endpoint retirement;
- prove that operational complexity does not exceed reliability gains.

### Research branch rule

V5–V8 findings feed V9. They do not replace V3/V4 business and endpoint foundations.

```text
Production: V2 → V3 → V4 ─────────────┐
                                       ├→ V9
Research:              V5 → V6 → V7/V8┘
```

## V9 — Outline + Xray hybrid

### Product definition

V9 is the first production multi-transport version:

```text
one business control plane
  ├── OutlineAdapter
  └── XrayAdapter
```

Customers buy an entitlement and connectivity service, not a named protocol.

### Entry gate

Do not enter V9 because Xray is interesting. Enter only when V3/V4 measurements show that independent Outline endpoints and regions are insufficient for an important customer/network segment.

Required evidence:

- repeated Outline failures attributable to transport/network behavior rather than one IP/server/provider;
- Xray test-pool success on the same network/time samples;
- acceptable client support and upgrade burden;
- explicit legal/provider/security review;
- capacity and staffing for two operational stacks.

### Architecture changes

- implement `XrayAdapter` behind the V3 generic interface;
- add adapter capability flags rather than forcing false equivalence;
- add transport-specific encrypted configuration payloads;
- add transport-specific health and usage normalizers;
- allow a connectivity profile to authorize one or more transports;
- preserve transport identity on every assignment, credential, observation, and decision;
- add separate upgrade/rotation/runbooks for each stack.

Capability examples:

```text
supports_usage_metrics
supports_server_quota
supports_deterministic_credentials
supports_udp
supports_remote_rotation
client_config_type
```

### Customer behavior

Without a custom client, V9 fallback is not seamless. Customers may need a different client or configuration import.

### V9 completion gate

> The same subscription can be fulfilled by Outline or Xray without changing order, payment, wallet, or subscription code, and both transports have live operational evidence and isolated failure domains.

## V10 — Protocol-agnostic adaptive endpoint fabric

### Product definition

V10 makes the generic connectivity model authoritative:

```text
stable business core
  ↓
entitlement
  ↓
connectivity profile
  ↓
policy engine
  ↓
endpoint selector
  ↓
transport adapter
  ↓
provider/region/endpoint
```

The V10 question is:

> Which eligible endpoint and transport should fulfill this profile under the current deterministic policy?

### V10 control-plane components

- connectivity-profile service;
- transport/provider/region/endpoint registries;
- capability-aware eligibility engine;
- endpoint scheduler;
- policy versioning;
- credential/configuration versioning;
- normalized but source-preserving observations;
- decision audit and replay;
- cost and concentration inputs;
- basic SLOs and operational dashboards.

### Scoring inputs

```text
reliability
latency
capacity/headroom
cost
provider independence
region independence
endpoint concentration
users per endpoint
transport eligibility
evidence freshness/confidence
```

The score must not hide hard constraints. Eligibility and safety filters run before ranking.

### V10 safety rule

V10 makes recommendations and controlled assignments. It does not yet autonomously alter large parts of the fleet.

### V10 completion gate

> Outline and Xray endpoints are selected through one explainable policy contract, every decision is reproducible from versioned inputs, and business modules contain no transport-specific logic.

## V11 — Verified client-assisted autonomous adaptation

### Product definition

The V11 question is:

> Which path is currently best for this customer's real network, and did the client successfully apply the change?

V11 requires the missing edge component: an AuriX client or tightly controlled edge agent.

The client does not replace the commerce core. It authenticates an account/device and consumes signed entitlement/configuration state. Current 50/100 GiB offers can continue as prepaid Telegram packages, but app-store auto-renewal, scheduled renewal, device plans, channel-specific prices, and provider lifecycle events require the extensions defined in [`deep-study-aurix-client-subscriptions-and-pricing.md`](deep-study-aurix-client-subscriptions-and-pricing.md).

### Client/control-plane protocol

The client must:

- authenticate independently of a raw VPN credential;
- request a signed, versioned configuration;
- validate before applying;
- keep a known-good rollback configuration;
- acknowledge application;
- run a bounded connection test;
- report success/failure with privacy-preserving network context;
- receive revocation and expiry state;
- tolerate control-plane unavailability using last-known-good configuration.

The server must track:

```text
desired_config_version
delivered_config_version
applied_config_version
verified_connection_version
rollback_version
```

### V11 adaptation loop

```text
observe
→ infer with confidence
→ choose within policy
→ issue versioned config
→ client validates/applies
→ client verifies connection
→ commit success or roll back
```

### V11 completion gate

> A configuration change is end-to-end observable and reversible; “command sent” is never treated as “customer connected,” and adaptation demonstrably reduces failures in replay and controlled live cohorts.

## V12 — Fully autonomous infrastructure research

### Definition

V12 expands automation from customer assignments into fleet lifecycle:

- provision endpoints;
- configure transports;
- rotate infrastructure credentials;
- scale capacity;
- rebalance assignments;
- drain and retire endpoints;
- replace blocked or unhealthy infrastructure.

### Why V12 is not a direct production target

Unbounded automation can amplify a mistaken health inference into:

- mass migration;
- destination overload;
- credential churn;
- provider cost spikes;
- fleet-wide outage;
- loss of forensic clarity.

Treat V12 as simulation, shadow decisions, and tightly bounded experiments. Promote individual capabilities only when their safety controls are proven.

## V13 — Bounded resilient connectivity operating system

### Product definition

V13 keeps V12's useful automation but makes automation itself a controlled subsystem.

The V13 question is:

> How can the system maintain connectivity while ensuring that detection, policy, automation, and recovery cannot become larger failure domains than the endpoints they manage?

### Five planes

```text
Business plane
  identity, money, orders, subscriptions, entitlements

Connectivity-control plane
  profiles, policy, assignment, credential/config lifecycle

Data plane
  Outline, Xray, future transports and endpoints

Observability plane
  metrics, health observations, decision evidence, SLOs

Security/governance plane
  threat model, authorization, audit, privacy, recovery, policy control
```

### Bounded automation controls

- circuit breakers;
- confidence thresholds;
- minimum sample sizes;
- hysteresis and cooldowns;
- destination capacity reservation;
- maximum changes per endpoint/region/time window;
- canary cohorts;
- shadow mode before enforcement;
- human approval for high-blast-radius actions;
- automatic rollback;
- global and regional kill switches;
- immutable decision/audit records;
- policy and configuration versions;
- failure budgets;
- reconciliation against desired and actual state.

### Security model

Assume any data-plane endpoint can eventually be compromised.

Required boundaries:

- endpoint credentials are unique and least-privileged;
- compromise of one endpoint cannot expose other endpoints or the commercial database;
- provider credentials are separated by account/project where practical;
- management access is private/allowlisted and audited;
- customers never receive management credentials;
- secret rotation and emergency revocation are tested;
- sensitive telemetry has minimization, consent, retention, and deletion rules;
- admin roles and high-risk actions use stronger authentication and separation of duties.

### Operational model

V13 must support:

- desired-state versus actual-state reconciliation;
- endpoint lifecycle state machines;
- policy simulation and replay;
- incident timelines;
- disaster recovery with tested restore;
- regional/provider/transport failure exercises;
- cost, performance, reliability, privacy, and security dashboards;
- measurable SLOs and customer-impact accounting.

### V13 completion gate

> The system can lose an endpoint, region, provider, or transport path without losing commercial truth; it finds or assists a verified replacement within policy, contains blast radius, preserves last-known-good connectivity where possible, and can explain and reverse every automated action.

## Recommended codebase evolution

### Stage 1 — preserve behavior while creating modules — completed

```text
app.py / commerce.py       compatibility facades
runtime.py                 composition root
entitlements.py            free/trial policy
free_repository.py         free/trial persistence
commerce_models.py         paid value objects
commerce_repositories.py   SQLite/PostgreSQL adapters
commerce_service.py        paid application workflows
commerce_worker.py         durable external-effect workflows
outline_adapter.py         pinned Outline HTTP adapter
ports.py                   external contracts
telegram_*.py              presentation/admin/maintenance features
persistence.py             shared SQLite lifecycle
migrations.py              migration history contract
observability.py           secret-safe timing seam
```

The physical package layout is flat, but dependency boundaries and compatibility identities are tested. Do not spend the V3 budget moving these files into directories unless that move unlocks a concrete implementation need. Keep one deployable modular monolith.

### Stage 2 — migrate schema without losing current keys — next

1. Add migration version 2 for endpoint/transport/provider/region/profile/assignment/credential tables in both dialects.
2. Insert the current Outline server as endpoint `outline-primary` using a secret reference, never a plaintext management URL copied into ordinary rows.
3. Backfill every active free/paid key with that endpoint assignment.
4. Introduce generic credentials while retaining compatibility reads from `keys` and `paid_vpn_keys`.
5. Reconcile local credential metadata with the current Outline inventory.
6. Dual-read and compare during migration.
7. Cut writes to the generic path.
8. Remove compatibility code only after SQLite, generated PostgreSQL DDL, restored PostgreSQL, and remote inventory evidence agree.

### Stage 3 — move all external effects behind jobs — partially completed

Already durable:

- paid provision and revoke;
- paid expiry and quota termination;
- paid notifications with retry/dead-letter state;
- remote paid-key reconciliation after ambiguous create.

Still to unify:

- free key provision;
- monthly trial provision;
- free/trial remote reconciliation after ambiguous create;
- notification delivery lease/claim;
- replacement/migration;
- endpoint probe;
- endpoint lifecycle operations.

Every workflow uses:

```text
intent
→ durable job
→ external execution
→ observation/reconciliation
→ committed actual state
→ notification/audit
```

### Stage 4 — separate operational deployments only when needed — deferred

Target topology after PostgreSQL/webhook readiness:

```text
Telegram webhook/API process
PostgreSQL
provision/revoke worker
notification worker
health/usage worker
```

Do not split into network microservices merely because the code has modules.

The current PostgreSQL job claim supports `FOR UPDATE SKIP LOCKED`, but that does not make the whole deployment replica-safe. Telegram remains long polling, notification delivery has no lease, free/trial provisioning is synchronous, and maintenance is process-local.

## Cross-version invariants

These rules must survive every version:

1. Payments and subscriptions never depend on current endpoint availability.
2. One external failure never creates duplicate billable credentials.
3. A credential always identifies its endpoint and transport.
4. Endpoint failure changes assignment/credential state, not customer ownership or payment truth.
5. No migration revokes the known-good path before replacement is verified, unless security policy requires immediate revocation.
6. Existing connectivity continues during control-plane outage using last-known-good configuration where technically possible.
7. Health evidence has source, context, freshness, and confidence.
8. Capacity is reserved before migration.
9. Every privileged, financial, credential, policy, and automated action is audited.
10. Automation is idempotent, bounded, observable, reversible, and killable.
11. Transport-specific details remain behind adapters.
12. Customer-facing claims are limited to measured behavior.

## Version promotion checklist

A version is not complete because its schema or classes exist. Promotion requires five kinds of evidence:

| Evidence | Required proof |
|---|---|
| Functional | State transitions and user/admin workflows behave correctly |
| Failure | Timeouts, duplicates, crashes, stale locks, endpoint loss, and rollback converge safely |
| Live integration | Actual Telegram, database, client, VPN server, and network behavior is tested |
| Operational | Metrics, alerts, backup/restore, runbooks, and ownership exist |
| Economic | Cost, transfer, support load, failure rate, and customer value justify the added complexity |

## Metrics that decide when to advance

### V2 → V3

- active customers and keys;
- transfer P50/P90/P95/P99;
- peak Mbps and connection counts;
- one-server capacity headroom;
- endpoint incidents and blocked-IP observations;
- recovery/support time;
- contribution margin.

### V3 → V4

- allocation imbalance;
- endpoint/region-specific success and latency;
- repeated transient versus sustained failures;
- migration frequency and destination headroom;
- operator time spent detecting and draining endpoints.

### V4 → V9

- failures shared across independent Outline endpoints;
- evidence that the failure is transport/network-specific;
- Xray success on matched network/time samples;
- incremental support and operations cost;
- customer segment affected and revenue at risk.

### V9 → V10

- duplicated transport logic leaking into business modules;
- policy complexity requiring one authoritative selector;
- enough endpoint/transport observations to compare candidates;
- demonstrated need for generic config/credential lifecycle.

### V10 → V11

- customer pain from manual profile replacement;
- enough verified network-specific observations;
- measurable value from client-assisted config updates;
- privacy/legal acceptance for client telemetry.

### V11/V12 capabilities → V13

- manual operations dominate incident response;
- automation simulations outperform operators without increasing blast radius;
- rollback and kill switches are proven;
- policy decisions can be replayed and explained;
- team capacity exists for 24/7 operational ownership.

## Final build order

### Build now

```text
V2 Phase-8 remaining gates
→ V3 endpoint registry
→ multiple Outline servers
→ second region
→ deterministic allocation
→ drain/manual migration
```

### Build after V3 evidence

```text
V4 health observations
→ state transitions
→ circuit breaker/hysteresis
→ capacity-aware controlled migration
```

### Research in parallel, but do not operate broadly

```text
V5/V6 Xray experiments
→ V7/V8 operational learning
```

### Build only when Outline diversity is insufficient

```text
V9 Outline + Xray
→ V10 protocol-agnostic fabric
```

### Build only when manual switching is the dominant reliability problem

```text
V11 client-assisted verified adaptation
```

### Build only at fleet scale

```text
selected V12 automation
→ V13 bounded automation and governance
```

## Final decision

For the current repository:

```text
Current network version:  V2 — one Outline endpoint
Current software maturity: V2, refactor Phase 8 modular foundation
Next exact version:       V3
Next transport:           Outline only
Next fleet:               multiple servers, then multiple regions
Next automation:          deterministic allocation and manual drain/migration
Architecture model:       V10/V13-compatible names and boundaries
Deferred operation:       Xray/multi-transport until V4 evidence proves need
Long-term target:         V13
```

The central strategy is:

> Build V3's small, reliable product using abstractions that can grow into V10/V13—without paying V10/V13's engineering and operational cost before real traffic, failures, and customer behavior justify it.
