# AuriX backend integration record — 2026-09-09 / continuation 2026-09-10

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
  `/devices`, and `/revoke_device` provide the customer control surface.
- Node-agent boundary: `aurix_vpn.node_agent` provides a bounded authenticated
  client contract for Xray/Hysteria2-compatible agents plus an atomic, tagged
  `XrayConfigWriter`. `aurix_vpn.node_agent_app` now supplies the matching
  authenticated WSGI service with strict request bounds and response redaction.
  It preserves unknown provider users/inbounds and never restarts a daemon or
  executes shell commands. The client/server contract is fake-tested but not
  deployed to a live host.
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

## Verification performed

- The focused protocol/accounting/commerce selection passes `78` tests before the
  continuation and the updated identity/adapter focus passes `18` tests.
- Full discovery passes: `308` tests in the current checkout, including the separate
  `pay_monitor` suite. The earlier `cv2` import failure is no longer present in this
  environment.
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

## Remaining gates and next action

The local backend and node-agent contract are now implemented; the next action is to
bind the contract to a reviewed canary-only Xray agent and validate restart, quota,
outage, and session behavior. Repeat the evidence gate independently for Hysteria2.
Only after those results are accepted should one protocol be registered and integrated
at a time behind the existing registry. The default production registry remains
Outline-only, so unmeasured protocols cannot receive customer traffic. Production
rollout still requires real PostgreSQL concurrency, Myanmar-client checks, bounded
mixed-protocol tests, and soak/cost evidence.
