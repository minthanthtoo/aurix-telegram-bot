# Monolith Reduction Plan

Status: MR extraction pass complete; final completion roadmap prepared
Baseline branch: `codex/fleet-cicd`
Baseline commit: `f16c9e7`
Architecture style: one deployable modular monolith with an external Outline data plane

## Objective

Reduce change coupling and make ownership explicit without changing deployment topology,
customer behavior, commercial rules, or the durable-job safety model. The desired result is
still one process and one PostgreSQL database. Reduction means smaller cohesive components,
typed boundaries, and independently testable policy—not microservices.

## Historical pre-MR snapshot

The limits below are enforced by `tools/check_architecture.py`. They are ceilings, not targets.
An extraction should lower the relevant values in `architecture_baseline.json`; ordinary feature
work must not raise them.

| Hotspot | Lines | Longest function | Execute-family calls | Primary responsibilities currently mixed |
|---|---:|---:|---:|---|
| `commerce_service.py` | 6,022 | 363 | 311 | catalog, orders, receipts, wallet, server inventory, route migration, repair |
| `commerce_worker.py` | 2,341 | 294 | 100 | job leasing, provisioning, revocation, repair, migration, quota, notification |
| `entitlements.py` | 2,250 | 226 | 112 | free/trial/giveaway policy, allocation, provisioning, usage, enforcement |
| `telegram_transport.py` | 2,447 | 274 | 1 | Telegram gateway, process state, receipt flow, customer views, polling |
| `telegram_commands.py` | 1,487 | 1,353 | 0 | all message routing and most conversation-state transitions |
| `telegram_callbacks.py` | 942 | 927 | 0 | all callback routing and navigation |
| `telegram_admin_panels.py` | 1,934 | 221 | 1 | admin queries, view models, rendering, confirmations |
| `commerce_repositories.py` | 1,135 | 376 | 80 | connection adapters, initialization DDL, seeds, compatibility alterations |
| `migrations.py` | 2,484 | 80 | 52 | free and commerce migrations for both database dialects |
| `runtime.py` | 466 | 440 | 0 | environment parsing, validation, construction, reconciliation, process loop |

`Execute-family calls` is a coarse AST ratchet over `.execute()`, `.executemany()`, and
`.executescript()`. It intentionally measures persistence-shaped coupling, even where a call may
be an adapter operation rather than SQL.

The extracted collaborators introduced by MR-02–MR-07 are also listed in
`architecture_baseline.json`, so future maintenance cannot silently re-grow the new seams into
replacement monoliths.

## Measured post-MR state

The following are the measured compatibility facades and their focused owners after this pass.
The detailed ceilings for every extracted module are enforced in `architecture_baseline.json`.

| Boundary | Before | After | Focused owners |
|---|---:|---:|---|
| Commerce service | 5,969 lines | 252 | core, fleet, inventory, orders, receipts, wallet, repairs, allocation |
| Commerce worker | 2,255 lines | 126 | lifecycle, capacity, migrations, repairs, notifications, job operations |
| Entitlements | 2,250 lines | 82 | provisioning, giveaways, quota, support |
| Telegram transport | 2,447 lines | 394 | customer, customer VPN, receipts, admin, runtime |
| Telegram commands | 1,487 lines | 142 | typed context plus customer/admin/operations routers |
| Telegram callbacks | 942 lines | 14 facade + 162 router | customer and admin action handlers |
| Admin panels | 1,934 lines | 20 facade | navigation, capacity, state, confirmations, orders |
| Commerce repositories | 1,135 lines | 9 facade | SQLite database, PostgreSQL database, connection adapter |
| Commerce migrations | 1,672 lines | 21 registry facade | receipts, capacity, identity, quota, probes, endpoints, routing |
| Runtime composition | 563 lines | 13 facade | settings, models, Outline helpers, bootstrap |

Verification evidence for this state: 509 repository tests pass, the architecture guard passes,
the production modules compile, and the Graphify refresh rebuilt 3,622 nodes, 8,796 edges, and
163 communities. Graphify reports four non-Python configuration/manifest files as zero-node
inputs; this is an extractor limitation, not an application dependency-cycle finding.

## Non-negotiable invariants

Every reduction change must preserve these properties:

1. A database transaction commits business state and its durable job/outbox intent together.
2. Outline, Telegram, object storage, DNS, and provider mutations remain retryable external
   effects; they must not become prerequisites for committing business state.
3. Job claiming remains atomic, leased, idempotent, and recoverable after process death.
4. Payment approval cannot create more than one subscription or active credential generation.
5. A remote key is not treated as present or absent solely because a network request returned.
   Reconciliation remains authoritative for remote observations.
6. Account-wide quota enforcement remains conservative when an endpoint counter is stale or
   unavailable. Missing evidence must never be interpreted as zero usage.
7. Access URLs, signing material, pairing secrets, provider credentials, and receipt contents do
   not enter logs, metrics labels, audit payloads, or test snapshots.
8. Telegram customer/admin authorization is checked before command execution, not only while
   rendering menus.
9. Existing `app.py` and `commerce.py` imports remain compatibility facades until all deployed
   scripts and tests have migrated.
10. The Outline data plane remains independent of control-plane availability.

## Target dependency direction

```text
bootstrap/runtime
       |
       v
telegram routers -----> application use cases <----- worker coordinator
                              |
                              v
                    domain policy and models
                         |           |
                         v           v
                 repository ports  external-system ports
                         ^           ^
                         |           |
                 SQL repositories  Outline/Telegram/storage adapters
```

Rules:

- Domain modules do not import Telegram, runtime, SQL implementations, or deployment scripts.
- Application use cases own transaction boundaries and depend on protocols.
- Repository implementations own query text and row mapping.
- Adapters translate provider-specific payloads and errors; they do not decide commercial policy.
- Telegram routers parse updates and invoke use cases; they do not query SQL directly.
- Runtime/bootstrap modules are the only composition roots and may depend on every layer.
- First-party imports remain acyclic.

## Incremental target layout

New extractions should enter an `aurix` package while root modules remain thin compatibility
facades during migration:

```text
aurix/
  domain/          # pure models, state transitions, allocation and quota policy
  application/     # commands/use cases and explicit transaction orchestration
  persistence/     # unit of work, SQL repositories, migrations and row mapping
  workers/         # job handlers plus the worker coordinator
  integrations/    # Outline, storage, probes, provider and DNS adapters
  telegram/        # gateway, typed updates, routers, conversation state and views
  bootstrap/       # settings and object construction
```

Do not move files only to improve appearance. A move is complete when the extracted component has
an explicit API, owns its tests, and the old module delegates without reaching into private state.

## Execution sequence

### MR-00 — Guardrails and baseline

Delivered by the branch:

- machine-readable hotspot budgets;
- forbidden dependency checks;
- first-party import-cycle detection;
- a unit-test entry point and an explicit CI step;
- coverage-source registration for newer production modules.

Exit criterion: the guard passes on the refreshed baseline and fails when a hotspot grows, a
forbidden import is introduced, or a cycle appears.

### MR-01 — Characterize transaction and side-effect boundaries

This MR adds focused contracts in `test_reduction_contracts.py` for durable
provisioning, retry behavior, receipt storage, and remote revocation failure.

- Add focused tests around order creation, receipt submission, approval, provisioning, revocation,
  repair, endpoint migration, and account quota termination.
- For each workflow, assert committed rows, queued jobs/outbox events, idempotency keys, retries,
  and remote-call timing.
- Record the current public methods used by Telegram, deploy scripts, maintenance, and tests.

Exit criterion: each high-risk workflow can be refactored behind its current facade with a test
that detects duplicated effects and transaction-order regressions.

### MR-02 — Extract persistence seams

Introduce transaction-aware protocols and implementations for these aggregates, one pull request
at a time:

1. server inventory, health, and route decisions;
2. jobs, leases, outbox events, and notifications;
3. orders, payments, receipts, and subscriptions;
4. wallets and immutable ledger entries;
5. entitlements, credential generations, devices, and quota leases.

Application services should receive repositories or a unit of work. SQL, row-shape compatibility,
and dialect differences move into persistence implementations. Do not create one repository per
table; use one per transactional aggregate or cohesive read model.

Exit criterion: the corresponding execute-call budgets in services decrease and repository
contract tests run against SQLite plus the existing PostgreSQL adapter test harness.

### MR-03 — Replace the worker mixin with collaborators

Create explicit handlers for provisioning/revocation, managed-key repair, endpoint migration,
usage/quota, infrastructure, and notification delivery. A `WorkerCoordinator` performs claiming,
dispatch, retry classification, and lease completion. Handlers receive only the repositories and
ports they require.

Keep `CommerceService.process_jobs()` as a temporary delegating facade. Remove it only after
runtime, maintenance, deploy scripts, and compatibility tests use the coordinator.

Exit criterion: `CommerceService` no longer inherits `CommerceWorkerMixin`; no worker calls a
private service method; crash/retry tests still demonstrate idempotency.

### MR-04 — Unify allocation and credential lifecycle policy

Extract protocol-neutral `CapacityPolicy`, `EndpointSelector`, `CredentialLifecycle`, and
`UsageLedger` domain services. Paid, trial, giveaway, repair, and failover workflows must invoke
the same policy rather than maintaining parallel server-selection and quota rules.

Exit criterion: free and paid paths share policy tests; provider adapters contain no price,
eligibility, campaign, or account-status decisions.

### MR-05 — Split Telegram routing and presentation

- Normalize Telegram payloads into typed message/callback commands.
- Separate customer, payment/receipt, support, and admin routers.
- Move mutable conversation state behind a `ConversationStore` interface.
- Move admin data gathering into application read models and keep renderers pure.
- Keep `TelegramBot` as the polling/webhook gateway and compatibility facade.

Exit criterion: no dispatcher exceeds 200 lines, route tables are exhaustive and collision-tested,
and Telegram modules do not depend on persistence implementations.

### MR-06 — Consolidate schema ownership

Split immutable migration registries by component and make migrations the canonical schema source.
Database initialization should create the migration ledger and apply migrations; it should not
maintain a second full copy of table DDL. Keep dialect-specific statements adjacent under the same
migration version and test fresh creation plus upgrade from supported historical snapshots.

Exit criterion: one canonical definition exists for each table/index/constraint, and both fresh
and upgrade paths pass for SQLite and PostgreSQL-compatible execution.

### MR-07 — Reduce the composition root

Introduce validated settings objects and factories for database, fleet/connectivity, managed
devices/probes, commerce/entitlements, and Telegram. Keep signal handling and lifecycle ownership
in runtime.

Exit criterion: runtime visibly describes construction order and shutdown, while environment
parsing and validation are independently tested.

Branch result: `runtime_composition.py` now exposes only the stable composition API while
settings parsing, factories/models, Outline setup, and bootstrap reconciliation are separate
modules. `runtime.py` retains Telegram authorization, polling lifecycle, signal handling, and
shutdown. The worker, Telegram transport, callback router, admin panels, identity service,
database implementations, migration registry, wallet workflows, and infrastructure controller
likewise delegate through explicit focused owners. SQLite and PostgreSQL initialization now share
one schema-bootstrap owner, leaving the database adapters responsible for connectivity and
interaction-state persistence rather than embedding their own schema lifecycle.

Remaining follow-up is intentionally method-level rather than another broad move: application
use cases still contain some legacy SQL for high-risk aggregate operations, and several focused
handlers remain longer than the preferred maintenance target. Those are the next safe reductions
because their public facades, transaction ownership, retry semantics, and compatibility tests are
now stable.

## Final completion roadmap — 85% to 100%

The extraction pass completed the structural split. The remaining percentage is gated by
ownership and testable boundaries, not by moving code into more files. Execute the remaining MRs
in this dependency order:

```text
persistence + schema
        -> domain policy
        -> application + worker orchestration
        -> Telegram + runtime coordinators
        -> compatibility-facade retirement
        -> release proof
```

### MR-08 — Complete transaction-aware persistence boundaries (85% → 90%)

Add aggregate repositories that accept the caller's active connection or unit of work. Prioritize
the current orchestration hotspots by execute-family call count:

| Workflow owner | Current calls | Repository boundary to introduce |
|---|---:|---|
| `commerce_service_wallet_approval_flow.py` | 24 | wallet/order approval aggregate |
| `identity_usage_recording.py` | 16 | entitlement usage epoch and quota ledger |
| `commerce_service_inventory_reconciliation.py` | 13 | endpoint inventory and repair read/write model |
| `commerce_worker_capacity_snapshot.py` | 13 | capacity and admission snapshot repository |
| `commerce_worker_provisioning.py` | 11 | subscription provisioning aggregate |
| `commerce_service_fleet_health.py` | 11 | endpoint lifecycle and health repository |
| `entitlement_server_allocation.py` | 10 | allocation candidate read model |
| `commerce_service_receipt_submission.py` | 7 | receipt evidence aggregate |

Implementation rules:

1. Add protocols to `repositories.py`; do not expose raw connection implementations through the
   protocol surface.
2. Keep transaction ownership in the use case while passing the active unit of work to every
   repository involved in that transaction.
3. Move SQL, row-shape compatibility, locking syntax, and dialect differences into persistence
   implementations.
4. Preserve idempotency keys, unique-conflict behavior, lease ownership, and durable outbox/job
   writes in the same transaction as business state.
5. Run repository contract tests against SQLite and the PostgreSQL-compatible adapter harness.

Exit gate: Telegram, domain policy, application use cases, and worker handlers have zero direct
execute-family calls. The architecture baseline enforces that zero ceiling.

### MR-09 — Decompose the remaining long workflows (90% → 94%)

Reduce decision density without splitting transaction ownership. The first targets are:

| Workflow | Current longest function | Intended collaborators |
|---|---:|---|
| inventory reconciliation | 363 lines | remote observation, ledger reconciliation, repair decision, health result |
| identity usage recording | 351 lines | binding resolver, epoch calculator, lease allocator, ledger writer |
| wallet approval | 332 lines | approval validator, top-up handler, subscription activator, ledger capture |
| receipt submission | 317 lines | evidence validator, duplicate detector, storage finalizer, notification intent |
| capacity snapshot | 294 lines | metric collector, reservation reader, admission evaluator, snapshot presenter |
| customer VPN dashboard | 274 lines | dashboard query model, entitlement presenter, action builder |
| managed-key repair worker | 250 lines | preflight, remote repair, identity convergence, completion recorder |
| paid provisioning worker | 230 lines | preflight, idempotent create/recovery, local commit, notification intent |

Pure calculators and validators should be independently tested. Transactional writers remain
small orchestration functions and must not open nested connections. Do not split a workflow merely
to satisfy a line count when doing so would hide ordering or retry semantics.

Exit gate: application and worker orchestration functions are at most 150 lines; Telegram
dispatch/presentation functions are at most 120 lines. Any exception requires a written invariant
and a dedicated architecture-baseline entry.

### MR-10 — Make migrations the only schema authority (94% → 96%)

`commerce_schema_bootstrap.py` now owns both dialects, but its 670 lines still combine base schema,
legacy compatibility alterations, seeding, and migration execution.

1. Encode the supported base schema as immutable component version-1 migrations.
2. Move every compatibility alteration into an immutable numbered migration.
3. Reduce bootstrap to migration-ledger creation, ordered registry application, and deterministic
   seed invocation.
4. Keep SQLite hooks only for rebuild operations that SQLite cannot express with ordinary `ALTER`.
5. Add fresh-create, supported-snapshot upgrade, repeated-startup idempotency, renamed-version,
   unknown-version, and partial-failure tests for both dialects.

Exit gate: one canonical definition exists for every table, index, and constraint; database
adapters contain no DDL; bootstrap contains no table-specific DDL or compatibility alterations.

### MR-11 — Finish bounded Telegram and runtime coordination (96% → 98%)

1. Replace the remaining large customer-commerce and admin callback branches with exhaustive route
   tables and typed command payloads.
2. Build the VPN dashboard from an application read model so transport code only formats and sends.
3. Split admin capacity/state gathering from rendering; renderers receive immutable view models.
4. Keep authorization at router entry and preserve callback collision tests.
5. Reduce runtime bootstrap to visible construction order, startup reconciliation, and shutdown;
   factories retain provider-specific setup.

Exit gate: dispatchers contain routing only, renderers perform no service/database reads, route
tables are exhaustive, and polling remains responsive while maintenance work is blocked.

### MR-12 — Migrate packages and retire compatibility facades (98% → 99%)

Move the stable modules into `aurix/domain`, `aurix/application`, `aurix/persistence`,
`aurix/workers`, `aurix/integrations`, `aurix/telegram`, and `aurix/bootstrap` in dependency order.
Keep root facades during caller migration, then remove each facade only after `rg`, deployment
entry points, tests, and operational scripts prove that it has no remaining consumer.

Exit gate: first-party imports follow the target direction, no import cycle exists, deploy scripts
use package APIs, and every remaining root module is an intentional executable entry point.

### MR-13 — Release proof and final cleanup (99% → 100%)

1. Delete superseded mixins, registries, aliases, and dead compatibility exports only after their
   consumers reach zero.
2. Tighten architecture ceilings to the final measurements and enforce zero SQL in non-persistence
   layers.
3. Run focused failure/retry/concurrency tests, the complete unit suite, Ruff, compilation,
   whitespace checks, migration tests, and first-party cycle checks.
4. Refresh Graphify and inspect affected paths for the public service, worker, Telegram, schema,
   and runtime boundaries.
5. Run offline production-acceptance checks in CI. Keep credentialed Outline, Telegram, Supabase,
   DNS, and provider smoke tests as an explicit operator-gated release step.
6. Update `README.md`, deployment runbooks, and the architecture report from measured final state.

Exit gate: every item in the definition of done below is evidenced by code, tests, architecture
guard output, and release documentation. Only then report overall MR progress as 100%.

## Pull-request discipline

Each reduction pull request should:

1. change one ownership boundary;
2. add or strengthen characterization/contract coverage before deleting the old path;
3. preserve public facades unless the pull request explicitly migrates every caller;
4. avoid schema changes and behavior changes in the same patch as a code move;
5. lower at least one architecture budget and never raise another without a written rationale;
6. run architecture guard, compile, Ruff, unit tests, shell validation, and whitespace checks;
7. include rollback notes for schema, worker, or external-effect changes;
8. update this plan when evidence changes the extraction order.

## Definition of done

Monolith reduction is complete when:

- application workflows depend on repository and external-system protocols;
- SQL is absent from Telegram, domain policy, and application orchestration;
- worker handlers are explicit collaborators, not inherited mixins;
- free and paid allocation/quota behavior use shared domain policy;
- Telegram dispatchers and runtime are bounded coordinators;
- migrations are the single schema authority;
- first-party imports are acyclic and CI enforces the intended direction;
- compatibility facades contain exports/delegation only;
- failure, retry, concurrency, idempotency, and recovery tests cover every external effect.
