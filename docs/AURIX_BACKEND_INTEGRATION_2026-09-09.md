# AuriX backend integration record — 2026-09-09 / continuation 2026-09-11

Status: local implementation plus a bounded, disposable SG-A Xray canary. No
occupied production service, customer credential, backend deployment, database,
load test, or speed test was changed or performed.

## Source of truth and scope

- Integration target: `/Users/min/projects/tg-AuriX-bot`, branch `codex/aurix-vpn-portal`,
  baseline commit `77c5266`.
- Reference only: `/Users/min/projects/tg-AuriX-bot-production`, branch
  `codex/fleet-cicd`, baseline `f16c9e7` plus user modifications. The reference is a
  pre-package monolith with a large incompatible tree delta; it was not merged blindly.
- Product/fleet evidence: `docs/AURIX_MULTI_PROTOCOL_FLEET_FEASIBILITY_2026-09-09.md`.
- Server validation owner: Codex task
  `codex://threads/01a0786a-3855-75c3-8e85-abb2a17c2c27`. Its server observations are
  treated as evidence, not as authorization to repeat mutations or load tests.
- The server task's earlier handoff was superseded by the coordinator's bounded
  Xray lifecycle run on 2026-09-10. Hysteria2 remains untested, and no SG-B
  mutation or new SG-B access was attempted.

## Integration decisions

1. Existing `subscriptions` and `keys` remain the commercial entitlement sources of
   truth. The new identity layer uses stable `paid:<subscription_id>` and
   `free:<key_id>` keys; it does not create a second entitlement model.
2. `ConnectivityAdapter` and `ConnectivityAdapterRegistry` are the only provider
   boundary. Outline remains the only default registered adapter. Injectable,
   fake-tested `XrayConnectivityAdapter` and `Hysteria2ConnectivityAdapter` now
   exist behind the same contract, but remain opt-in/unregistered until server
   lifecycle and accounting evidence is accepted. WireGuard remains absent.
3. Legacy endpoint IDs remain route-compatible (`outline:<endpoint_id>`), so the
   existing `EndpointRegistry` continues to own multi-server allocation and capacity.
4. The lifecycle is now represented as: entitlement source → endpoint route → durable
   provisioning job → adapter execution → generation/lease projection → encrypted
   delivery → generation-level accounting → verified revoke.
5. `CommerceService` owns the durable `RouteFailoverService` seam. It accepts
   server-reported outcomes/latency and records idempotent failover intent, while
   remote credential execution remains in the worker and server task.

## Defects repaired locally

- Ambiguous deterministic creation: a timeout followed by exact read-back produces
  `ownership=uncertain`; worker failure cleanup never deletes that credential.
- Failover/accounting generations: every generation remains enumerable while remotely
  usable (`pending`, `active`, `retiring`, `unknown`), including generations not present
  in the legacy one-key-per-subscription table.
- Aggregate quota: per-entitlement leases, usage epochs, deduplicated samples, counter
  reset handling, stale-observation rejection, and an immutable quota ledger are now
  persisted. Paid usage is locked at the subscription row before `consumed_bytes` is
  read on PostgreSQL. Generation usage baselines are now explicit: newly owned
  credentials use a trusted zero baseline, explicitly migrated credentials require an
  operator-supplied baseline, and unknown legacy provenance fails closed for
  accounting. Unknown legacy credentials are deferred by the worker; this does not
  itself block remote traffic or revoke the credential.
- Counter semantics are provider-specific. Xray/Hysteria2-style counters retain the
  reset-on-decrease behavior: a newly owned credential credits a
  `400 -> 500 -> 50` sequence as `400 -> 100 -> 50`. Outline's transfer metric is a
  trailing-30-day window, so a decrease is recorded as `rolling_window_decrease`,
  credits zero, advances the observed baseline, and does not re-credit aged-out
  traffic. Explicitly migrated credentials still require their operator-supplied
  baseline before either mode can account usage.
- Provider-reported usage counters are parsed as untrusted input; boolean values are
  rejected rather than being coerced to `0`/`1` bytes.
- Local lease expiry is an operational horizon only. Active reservations remain held
  after `expires_at` until remote revocation/expiry is proven; this prevents a second
  generation from reusing capacity while the first credential may still work.
- The lease usage update uses portable `CASE` arithmetic rather than PostgreSQL-
  incompatible scalar `MIN` syntax.
- Xray/VLESS and Hysteria2 now have contract-only, injectable adapter seams that render
  route-bound customer exports and exercise provision/read-back/rotation/revoke/
  usage/reconcile behavior in fakes. Optional quota/session/data-plane controls are
  surfaced from the injected node-agent contract, but live capability remains
  unverified and both adapters remain unregistered from the production registry.
- `CommerceWorkerMixin.reconcile_managed_route(s)` now provides the durable backend
  recovery boundary: a caller supplies verified route metadata, the worker selects only
  `active`/`retiring` generations with `remote_state='observed'`, rehydrates missing
  provider users through the adapter, verifies read-back, and preserves unknown provider
  users. It additionally requires the commercial entitlement to be active/unexpired and
  the generation to have an active bounded quota lease; otherwise recovery is denied.
  A missing provider credential receives that lease remainder as its provider-side
  quota cap; an already-present credential is not rewritten during reconciliation.
  Unknown or delete-requested generations fail closed and are not recreated.
- Revocation boundary: local UI status can be updated for compatibility, but generation
  lease release and generation status `revoked` require provider-specific remote
  read-back verification. Revoke jobs now construct the adapter from the generation's
  protocol instead of assuming Outline, and fan out across all generation records for
  the entitlement. Authentication revocation and force-disconnect remain separate
  capabilities; deleting a credential is not reported as termination of existing
  sessions. Paid/multi-protocol generations retain their accounting lease until the
  adapter proves session termination; the legacy free-Outline path preserves its
  existing verified-deletion behavior.
- Managed-device control plane: migration `commerce:7` adds opaque accounts,
  Telegram identity bindings, one-time hashed pairing tokens, device public keys,
  revocation epochs, and session records. The signed `/v1/devices/*` API is mounted
  under the VPN portal when a durable Ed25519 manifest seed is configured. It
  delivers only account-owned, protocol-neutral route metadata/configuration and
  feeds signed connection observations into failover state. Telegram `/pair`,
  `/devices`, and `/revoke_device` provide the customer control surface. The
  optional `AURIX_MAX_ACTIVE_DEVICES` setting is enforced inside the pairing
  transaction, with an account row lock on PostgreSQL; it defaults to unlimited
  so no device-count product promise is implied. The read-only Control Center
  summary exposes whether enrollment is bounded, unbounded, or unconfigured.
  The standalone and combined VPN web entrypoints share the same device-service
  builder, so the recommended combined deployment does not silently omit the
  managed-device API.
- Node-agent boundary: `aurix_vpn.node_agent` provides a bounded authenticated
  client contract for Xray/Hysteria2-compatible agents plus an atomic, tagged
  `XrayConfigWriter`. `aurix_vpn.node_agent_app` now supplies the matching
  authenticated WSGI service with strict request bounds and response redaction.
  It preserves unknown provider users/inbounds and never restarts a daemon or
  executes shell commands. The client/server contract is fake-tested but not
  deployed to a live host.
- Concrete provider backends: `aurix_vpn.provider_backends` now supplies a
  locally supervised Xray config-writer backend with injected reload/stats
  hooks, plus a Hysteria2 HTTP-auth user store and bounded Traffic Stats API
  client. The Hysteria2 store encrypts customer secrets at rest and the Xray
  backend preserves unknown config users. These are node-local building blocks,
  not proof of live quota, restart, or client compatibility.
- Explicit node-agent bindings: `aurix_vpn.node_agent_bindings` now validates
  an opt-in `AURIX_MANAGED_NODE_AGENTS_JSON` list, constructs protocol-specific
  authenticated agent clients, and injects route/adapter providers into runtime
  maintenance and failover. The binding layer is bounded and fail-closed;
  configured adapters remain evidence-gated candidates, and an absent variable
  preserves the existing Outline-only runtime.
- Read-only operator visibility: the Control Center now reports protocol
  readiness separately from the allocation registry. Outline is shown as
  enabled, Xray/Hysteria2 as evidence-gated candidates, and WireGuard as not
  implemented; this does not register any new protocol for customer traffic.
- Durable endpoint protocol profiles: the commerce migration 9 and matching
  free-access migration 7 bind each endpoint to explicit protocol/adapter
  metadata, backfill enabled Outline for existing
  endpoints, and keeps candidate profiles non-allocatable until evidence and
  operator promotion exist. The protocol-aware selector now requires an
  explicitly enabled profile when a protocol is requested. The Control Center
  shows profile state and capability counts without route or credential secrets;
  the customer endpoint directory applies the same enabled-profile gate. The
  failover target selector, drain planner, assignment transfer, and verified
  target-generation attach now apply that gate as well, and reject a target
  route whose protocol differs from the source generation.
- Protocol health evidence: commerce migration 10 and free-access migration 8
  add an append-only, protocol-scoped observation ledger. Only bounded scalar
  evidence fields are persisted, and the Control Center can inspect recent
  management/client-path/quota/restart observations without receiving secrets.
  `EndpointRegistry.promote_protocol_profile` is the explicit promotion
  boundary: it requires fresh, non-expired healthy observations for every
  operator-selected signal and matching declared capabilities, then records an
  audit event when the commerce audit schema is available. Observations alone
  do not enable a candidate profile or constitute live compatibility proof;
  direct registration cannot bypass this boundary for non-Outline protocols.
  The non-mutating promotion-readiness check exposes missing fresh signals and
  capabilities before an operator attempts the state-changing decision.
  The commerce worker exposes both readiness and promotion through the existing
  administrator authorization boundary; the web Control Center remains read-only.
  The commerce boundary also requires the corresponding adapter to be registered
  before a profile can become enabled, so healthy evidence cannot activate an
  unimplemented or unavailable protocol. Adapter registration itself remains a
  candidate posture in the Control Center; it is not presented as customer
  traffic readiness.
  Telegram admins can request the same bounded readiness preview with
  `/protocolreadiness` and can invoke `/promoteprotocol` only through a
  state-bound, one-time confirmation that rechecks the evidence before commit.
  They can also use `/disableprotocol` through the same confirmation boundary
  to stop new allocation without revoking existing credentials; the action is
  audited and reversible through the normal profile-promotion workflow.
- Failover executor: `RouteFailoverExecutor` now claims durable decisions,
  provisions a stable target identity, persists it before probing, requires
  management/data-plane evidence when requested, transfers rather than duplicates
  the accounting lease, commits only after verification, and rolls back owned
  credentials on failed validation.

## Protocol and server status

The feasibility artifact still reports Outline as the only commercially usable AuriX
transport today. The bounded Xray canary now proves route connectivity, per-customer
counter observation, new-connection revoke, generation rotation, and controller-driven
revoke. It also proves that existing sessions survive removal and runtime API-added users
are lost on a canary restart. Therefore Xray still lacks the durability and strict-quota
properties required for registration; Hysteria2 remains unverified. The current local
code exposes honest Outline capability flags and keeps both new adapters unregistered.

The reported fleet constraints remain release gates: source-of-truth reconciliation,
capacity isolation from the constrained Singapore node, Myanmar-path measurements,
bounded concurrency/PostgreSQL validation, and explicit Xray/Hysteria2 lifecycle tests.

## Verification recorded before the latest 2026-09-10 continuation

- The focused protocol/accounting/commerce selection passes `78` tests before the
  continuation and the updated identity/adapter focus passes `18` tests.
- The earlier record reported `308` tests with the separate `pay_monitor` suite
  available. That environment result is historical and is superseded by the
  current complete-discovery verification below.
- Ruff passes for all changed backend and regression-test files.
- Python bytecode compilation passes for the changed backend modules.
- Graphify AST graph was refreshed after the changes. It reports `6,129` nodes,
  `9,229` edges, and `599` communities; visualization was skipped because the graph
  exceeds the HTML limit. Graphify also warned that several JSON/manifest source files
  produced zero AST nodes; those files are non-Python artifacts and remain outside the
  code graph.

PostgreSQL behavior is contract-tested through the existing adapter/fake connection,
including the new schema, portable lease arithmetic, and the full `record_usage`
`FOR UPDATE` lock path. A live PostgreSQL concurrency run was not verified in this
workspace.

## Continuation verification — 2026-09-10

The continuation added and committed the authenticated WSGI node-agent boundary,
the reproducible node-agent client/config writer, a real-localhost HTTP transport
round-trip, redacted Control Center fleet-detail/failover/audit visibility, a bounded staged
2/5/16-customer mixed Xray/Hysteria2 contract matrix, and confirmed operator
drain queueing. The local matrix proves shared agent-boundary isolation, quota
assignment, usage/probe/reconcile wiring, latency sampling, and cleanup under
bounded worker counts; it is not evidence of live protocol behavior. Drain
queueing pauses new source assignments and preserves order/payment/entitlement
identity while the verified worker owns target provision, probes, assignment
transfer, lease transfer, and commit.

The current checkout had `341` discovered test entries at this checkpoint, and
complete discovery passed (`Ran 341 tests ... OK`) after the reproducible Python
environment added the previously missing OpenCV dependency. Ruff, Python
compilation, JavaScript syntax validation, and `git diff --check` pass for the
continuation files. Notification
delivery now claims due rows with a bounded lease and token-guarded completion,
so a second worker cannot duplicate a live claim and a stale worker cannot
complete a reclaimed row. Daily free and monthly trial issuance now use the
same durable intent boundary, deterministic provider identity where supported,
and encrypted recovery notifications. Promo/giveaway issuance now has the same
durable reservation and retry boundary, including capacity release on provider
failure. Backup/restore artifact creation, isolated SQLite restore verification,
and receipt-path reconciliation are also covered by tests. The resulting commits are recorded in
Git history after this verification.

## Continuation verification — 2026-09-11

The protocol promotion operator surface is now complete locally. Telegram
admins can inspect a redacted `/protocolreadiness` preview and can request
`/promoteprotocol` only through the existing durable, single-use confirmation
boundary. Confirmation fingerprints bind profile state, required evidence,
capabilities, and evidence timestamps while ignoring only the preview's
volatile `checked_at` timestamp; the promotion method rechecks all evidence
before enabling the profile. Customer identities cannot reach either command.

The complete suite now passes `352` tests (`Ran 352 tests ... OK`) with
`PYTHONWARNINGS=error::ResourceWarning`; focused Telegram authorization and
promotion/disable tests, Python compilation, Ruff, and `git diff --check` also
pass.
This verification is local only: no server, database, credential, customer,
load-test, or speed-test state was changed. The implementation is committed as
`610e3fa` (`Add confirmed protocol promotion workflow`), `ffc2da3` (`Add
confirmed protocol disable workflow`), and `10a0270` (`Serialize protocol
disable state transition`). The disable transition re-reads the profile inside
the write transaction, rejects a concurrently retired profile, blocks new
allocation, preserves existing credentials, and records the actual prior state
in the audit event. The subsequent node-agent quota-boundary hardening is
committed as `5dddbe0` (`Reject boolean node-agent quotas`), `5634df3`
(`Normalize node-agent quota errors`), and `daa4565` (`Reject non-integer
protocol quotas`); malformed boolean, floating-point, empty, and non-numeric
quota values now fail before provider creation.
Adapter operations also reject a route whose declared protocol differs from
the adapter, including provisioning, probing, reconciliation, and recovery.
Concrete Xray and Hysteria2 provider counters also reject fractional values
before usage is credited; this step is committed as `38d558d` (`Reject
fractional provider counters`).
Grant lifecycle operations reject a credential grant whose declared protocol
does not match the adapter; this isolation step is committed as `d16efcd`
(`Enforce protocol-bound credential grants`). Hysteria2 route metadata now
accepts `insecure` only as a real boolean, so a string such as `"false"` cannot
silently enable insecure TLS. Managed adapters also render-validate route fields
before provider-side creation, preventing malformed route metadata from leaving
an orphaned remote user. This step is committed as `f58a27c` (`Reject malformed
Hysteria2 route flags`).

The managed quota worker now reads each remotely observed protocol generation
through its adapter, records the counter through the locked identity ledger,
and queues the existing idempotent subscription revoke job when aggregate quota
is exhausted. Route metadata is supplied explicitly per endpoint/protocol and
must match the durable generation before the adapter is called. Duplicate
observations remain ledger-idempotent and one provider/route failure is isolated
as a partial sweep result. This controller-side path is committed as `d0b685d`
(`Add protocol-neutral managed quota sweep`); it is not evidence of native
provider hard-quota enforcement.
The sweep is wired into scheduled maintenance only when the service is given
an explicit route provider; existing Outline-only deployments therefore retain
their prior behavior. This integration is committed as `f10edb2` (`Schedule
managed quota enforcement`).
The concrete Hysteria2 backend now fails closed before user creation when no
hard-quota capability is available, while its encrypted user store and the
Xray config writer are covered across reinitialization. This guard is committed
as `e3cdcf1` (`Harden Hysteria2 quota boundaries`).

## Continuation verification — 2026-09-12

The VPN fleet-control layer now exposes a read-only
`FleetController.scale_out_recommendation` decision for the Control Center. It
evaluates fresh endpoint health, enabled protocol profiles, plan capacity,
regional/global node caps, daily creation limits, cooldown, active durable
provision intents, and the configured region allowlist without queuing work or
contacting DigitalOcean. Automatic scale remains disabled by default; the
provider worker retains the live billing/budget gate.

Provision admission now serializes PostgreSQL requests with a transaction
advisory lock, counts active intents toward node caps, fails closed when an
active intent lacks a durable region, and returns the existing job for a
repeated hour-scoped request fingerprint. The web Control Center endpoint
detail now also shows redacted capacity-by-plan policy and active assignment
counts, matching the existing Telegram admin capacity view.

Failed infrastructure intents are now included in the redacted admin job
surface and can be requeued through the existing owner confirmation path. A
retry is marked durably and the dedicated worker reads back the stable
provision tag before creating anything, recovering one matching provider
resource and failing closed on multiple matches.
This recovery step is committed as `cd923f7` (`Make VPN infrastructure retries safe`).

The worker now treats the durable provisioning intent as the placement source
of truth, rejects conflicting caller-supplied region/size/image values, and
rechecks the configured allowlists at execution time. The DigitalOcean client
also validates the current region, size, image, availability, and size-region
compatibility catalogs before a create; invalid placement fails closed without
reaching the provider mutation call. This step is committed as `7f9701c`
(`Validate VPN provider placement at execution`).
Deterministic durable-intent validation failures now become terminal failed
jobs with a redacted event, while provider/network errors remain retryable;
this prevents malformed infrastructure work from repeatedly blocking the
worker.

The VPN provisioning worker now has an opt-in Cloud Firewall stage. When
`AURIX_DIGITALOCEAN_FIREWALL_POLICY_JSON` is configured, it validates the
tag-based TCP/UDP/ICMP policy before Droplet creation, persists the normalized
policy with the intent, creates or converges exactly one matching firewall,
reads it back, and only then advances an active Droplet to
`awaiting_verification`. Public SSH is rejected; ambiguous, missing, or
non-matching provider state remains blocked for retry rather than being
silently accepted. The policy is empty by default because actual Outline,
worker-SSH, and management CIDRs must be selected from deployment evidence.
This local implementation and fake-provider coverage are committed as the
VPN-only commit `a6a8683` (`Add guarded VPN cloud firewall stage`).
Managed node-agent bindings now require HTTPS for remote agents, allow plain
HTTP only for loopback-local agents, and reject URL userinfo/query/fragment
decorations that could expose bearer credentials. This boundary hardening is
committed as `bd4c59b` (`Harden VPN node-agent transport bindings`).
Local Xray and Hysteria2 provider stores now serialize concurrent
read-modify-write mutations with mode-0600 sidecar locks while retaining
atomic replacement. The concurrency regression coverage and protocol matrix
remain local-only; this milestone is committed as `31054f7` (`Serialize VPN
provider state mutations`).

The focused scale-control tests, Control Center/API tests, VPN-oriented
regression set (324 tests), Ruff, Python compilation, JavaScript syntax, and
`git diff --check` pass locally. No live server, customer credential,
provider/database deployment, load test, speed test, or automatic scale action
was performed. The remaining action is still the separately authorized
canary/evidence sequence for real Xray/Hysteria2 behavior and second-node
rollout gates.

The latest bounded local mixed-protocol run also passed with 200 Xray and 200
Hysteria2 customers, 64 workers, and no errors across provisioning,
reconciliation, revocation, protocol isolation, quota, usage, and data-plane
contract checks. Provision latency was mean 0.365 ms, p95 0.440 ms, p99 2.762
ms, and max 30.107 ms in the local process. These are disposable in-memory
harness timings only; they are not server capacity, customer speed, or live
protocol-compatibility evidence.

A disposable local PostgreSQL rehearsal also completed: an initialized SQLite
snapshot containing account and protocol-observation timestamps migrated into a
fresh PostgreSQL database with row-count reconciliation, and 16 concurrent
provision requests collapsed to one durable regional intent. This rehearsal
also exercised account creation, one-time device pairing, heartbeat, and
revocation on the PostgreSQL repository. It does not validate the hosted
database, network latency, or production cutover.

Automatic VPN placement is now auditable at the same transaction boundary as
the durable state change. Paid and free endpoint assignments record one
`endpoint_assignment_created` event, while failover/rollback moves record
`endpoint_assignment_transferred`; idempotent allocation and already-on-target
operations do not emit duplicate events. The shared audit writer remains
compatible with the optional SQLite/PostgreSQL commerce audit table and stores
only bounded endpoint, entitlement, protocol, reason, and transition metadata.
This VPN-only hardening is committed as `3a6b3c4` (`Audit VPN endpoint
allocation transitions`).

The same unified audit stream now records owner-approved server-control
decisions: endpoint lifecycle changes, idempotent infrastructure provision
intents, and endpoint verification after the management probe. The records
carry bounded actor, placement, lifecycle, and job metadata without provider
tokens, management URLs, certificates, or credential material. This server
management hardening is committed as `f339884` (`Audit VPN infrastructure
control transitions`).

Endpoint health state changes now use bounded hysteresis: two consecutive
failed capacity observations enter `DEGRADED` and two consecutive healthy
observations recover to `ACTIVE` by default, with validated environment
thresholds for deliberate tuning. Operator `DRAINING` and `RETIRED` states
are preserved, automatic transitions are audited, and normal failover/drain
target selection rejects degraded endpoints. This V4 safety step is committed
as `3094da5` (`Add VPN endpoint health hysteresis`).

## Remaining gates and next action

The local provider backend, node-agent contract, bounded mixed-protocol test
seam, and operator drain control are now implemented and committed in the
current local history; the next action is to bind
the contract to a reviewed canary-only Xray agent and validate restart, quota,
outage, and session behavior. Repeat the evidence gate independently for
Hysteria2. The concrete Hysteria2 auth/stats implementation does not make the
current shared-password service commercially eligible.
Only after those results are accepted should one protocol be registered and integrated
at a time behind the existing registry. The default production registry remains
Outline-only, so unmeasured protocols cannot receive customer traffic. Production
rollout still requires real PostgreSQL concurrency, Myanmar-client checks, bounded
mixed-protocol tests, and soak/cost evidence.

The PostgreSQL rehearsal then exposed and repaired an untyped-NULL selector
compatibility issue in the VPN endpoint, identity, failover, and provisioning
worker queries. The optional integration regression now exercises the complete
disposable PostgreSQL path: evidence-gated Xray profile promotion, endpoint
allocation, failover decision commit, one lease and assignment transfer, and
post-failover usage/recovery authorization. The selector repair is committed
as `9518069` (`Fix PostgreSQL VPN selector compatibility`), and the durable
failover regression as `ac6f655` (`Cover PostgreSQL VPN failover flow`).
The same regression now covers the optional failed-provision retry filter,
committed as `f75c49e` (`Cover PostgreSQL VPN worker retry`).
The disposable PostgreSQL rehearsal now also runs eight concurrent allocation
retries for one subscription and proves one durable assignment plus one audit
event; the parent subscription row is locked before the idempotency check.
This race fix is committed as `dd4673d` (`Serialize PostgreSQL VPN assignment
retries`). The VPN-only regression selection remains green at 328 tests. This
evidence is local and disposable; it does not validate the hosted database,
live provider state, Myanmar client paths, load/speed behavior, or production
cutover.

## Continuation checkpoint — 2026-09-12

Commerce migration 11 adds durable failover safety controls. The seeded global
control allows 100 new migration decisions per 300 seconds; operators can add
global, region, or endpoint pause/budget overrides. Automatic failover and
operator drains reserve every applicable scope in one transaction, and repeated
idempotent drains do not consume the budget twice. A paused or exhausted
automatic path still records the route observation and writes a redacted audit
event. The read-only VPN Operations view exposes each control's current-window
usage and remaining budget; it does not expose a mutation path.

Commerce migration 12 adds a monotonic `policy_version` to failover policies and
copies that version onto each automatic or operator-created decision. The
Operations view shows the captured version, making later policy changes
replayable against the durable decision history without exposing route secrets.

Commerce migration 14 now stores an immutable policy snapshot for every version.
Decision listing joins the captured version to its exact thresholds, cooldown,
standby lease, and retry budget; the failover service also exposes read-only
policy-history and decision-explanation methods. This closes the gap where a
decision had a version number but the old policy values were otherwise lost.

Failover target selection now treats pending/creating/verified decisions as
destination reservations alongside active assignments. This prevents concurrent
failover cohorts from consuming the same endpoint headroom before their
assignments are committed.

Commerce migration 13 adds the durable endpoint health-transition timestamp.
Recovery now requires the configured consecutive healthy sample threshold and a
60-second cooldown by default (`AURIX_ENDPOINT_RECOVERY_COOLDOWN_SECONDS`),
while `DRAINING` and `RETIRED` remain terminal to automatic health transitions.
The cooldown is observable in the capacity result and transition audit metadata.

Failover decision creation, commit, retry/final failure, and rollback now emit
redacted lifecycle events into `audit_events`. The durable decision row remains
the state source of truth; the append-only events provide an operator timeline
without copying access URLs, provider identifiers, or credential material.

The existing Telegram administrator boundary now exposes the failover safety
controls without widening the browser surface. `/failsafety` is read-only and
shows current pause state, window usage, and remaining budget. `/setsafety`
requires explicit global, region, or endpoint scope values, presents the
current state, and applies only after the existing one-time state-bound
confirmation. The commerce wrapper writes the existing audited control change;
the command performs no provider call, credential revocation, or customer
route mutation. Example forms are `/setsafety global pause 5 60` and
`/setsafety region sgp1 resume 20 300`.

Focused failover, migration, Control Center, VPN web API, and render checks pass;
the non-PostgreSQL VPN regression passes `332` tests, and the two non-PostgreSQL
migration-manifest checks also pass. The optional PostgreSQL end-to-end rehearsal
was not re-run at this checkpoint because the host filesystem has only about
`308 MiB` available and the disposable PostgreSQL cluster exhausted that space
before the SQLite source snapshot could be initialized. No live server, provider,
customer credential, database, load test, or speed test was changed or run.
