# Maintenance completion plan

Status: active implementation candidate; final gates remain open.
Reviewed baseline: `d3dff04` on `codex/monolith-reduction-prep`, 2026-09-06.
Working checkout: `/Users/min/projects/tg-AuriX-bot-monolith-prep`.

This plan is the current completion proposal following the architecture review.
It supersedes the percentage estimates in `MONOLITH_REDUCTION_PLAN.md`, while
retaining that document's behavior invariants and historical extraction record.
The original checkout and deployed checkout are separate worktrees; a committed
change here is not evidence that either has been updated or deployed.

## 0. Current evidence checkpoint

Measured on the current candidate after the latest extractions:

The measured candidate is committed as the current HEAD (`Reduce monolithic
workflows and harden maintenance gates`).

- 223 recursively inventoried production sources; architecture guard passes with
  zero violations and zero application/worker SQL calls.
- 537 tests pass with the disposable PostgreSQL contract environment enabled;
  the dedicated SQLite/PostgreSQL contract suite passes all 14 tests.
- Coverage runs over every owned production source and reports 69% overall,
  above the configured 60% floor; the coverage-scope guard passes.
- All 223 production sources compile; Ruff's configured safety rules and the
  strict Pyright scope pass.
- The code graph was refreshed after the extractions.

This is evidence for the completed portions of G01, G02, the endpoint SQL
extraction in G04, explicit worker/service/entitlement contracts, and several
G06/G07 workflow slices. It is not a 100% completion claim: package migration,
full versioned ownership of compatibility/backfill schema changes, removal of
all compatibility forwarders, and final failure/recovery evidence remain open.

## 1. What 100% means

Completion means that all twelve gates in section 4 pass on the same candidate
revision, with reproducible evidence and no unresolved mandatory acceptance item.
The result is a maintainable modular monolith: explicit dependencies, predictable
transactions, recoverable external effects, bounded workflows, and automated
prevention of architectural regression.

Future maintenance remains ongoing. Completion of this project does not guarantee
that future changes introduce no debt, that every possible bug is tested, or that
live production has passed its separate acceptance process.

Keep the existing product contracts: Telegram behavior, commercial rules, paid and
free entitlements, security boundaries, SQLite and PostgreSQL support, and Outline
as the independent data plane. Preserve the current deployment topology. Additional
VPN protocols, payment automation, native clients, microservices, unrestricted
provider scaling, and enabling multiple control-plane writers are outside this plan.

The central architectural acceptance test is concrete: a workflow such as payment
approval, provisioning, usage recording, or receipt submission can be constructed
and tested through its declared dependencies without constructing `CommerceService`,
`TelegramBot`, or a general-purpose service container.

## 2. Evidence and corrections

The following observations were checked against the current source. Earlier
percentages and zero-SQL claims remain historical; the current evidence above is
the candidate-level measurement after implementation work.

| Observation | Current evidence | Consequence |
|---|---|---|
| Service facade is smaller | `commerce_service.py` has 264 lines | Useful navigation improvement; facade size does not measure total coupling |
| Service dependencies remain implicit | `CommerceService.__getattr__` binds implementation functions to the service | Replace active-path dependency lookup with explicit collaborators |
| Worker still sees the whole service | `CommerceWorker.__getattr__` falls back to `self.service` | Remove worker-to-service back references |
| Telegram components share host state | `TelegramComponent.__getattr__` and `__setattr__` forward to the host | Assign state ownership and inject transport/application interfaces |
| Other shared-host structures remain | `ClaimService` uses dynamic binding; identity and infrastructure use feature mixins | Include these families in the dependency audit |
| Earlier SQL audit had a classification bug | It excluded names containing `migration` | Classify by actual layer and explicit path |
| Endpoint-migration application SQL remains | 5 calls in `commerce_service_inventory_migration.py`; 14 in `commerce_worker_migrations.py` | The earlier zero-application-SQL claim is withdrawn; extract these 19 calls |
| Schema initialization is still substantial | `commerce_schema_bootstrap.py`: 670 lines, PostgreSQL initializer 376 lines | Move base definitions and compatibility work under versioned migration ownership |
| Architecture guard is incomplete | Root-only Python discovery, selected file ceilings, limited import resolution | Guard packages recursively and include newly added modules automatically |
| Some SQL ceilings remain permissive | `commerce_service_orders.py` allows 29 calls although actual count is zero | Ratchet actual application SQL to zero after correcting classification |
| Coverage gate is not exercised in current CI | CI uses plain unittest; coverage source is an explicit module list | Include all production modules and execute coverage reporting in CI |
| Coverage source omissions exist | Examples: usage recording, receipt submission, provisioning worker, worker coordinator, VPN dashboard | Correct the measurement denominator before claiming coverage improvement |
| Lint policy is deliberately narrow | Ruff selects `E9`, `F63`, `F7`, `F82` | Passing Ruff is limited evidence; expand checks incrementally |
| PostgreSQL checks include recording fakes | `FakeRawPostgresConnection` records statements in `test_commerce.py` | Add execution and concurrency contracts against a real disposable PostgreSQL instance |
| Package migration remains open | 178 non-test root Python modules; no `aurix/` directory | Move stable components incrementally after their boundaries are explicit |
| Some tests pin transitional structure | `test_refactor_boundaries.py` asserts mixin MRO and dispatch-map identity | Preserve public behavior contracts while retiring assertions for superseded internals |

Direct SQL counts above are AST counts of `execute`, `executemany`, and
`executescript`. The two endpoint-migration files were also inspected manually:
they contain query text and worker orchestration, not schema migrations. An AST
name count alone is not a complete information-flow or database-access proof.

### Priority workflow inventory

These are measured source spans, including their bodies and docstrings. They are
review triggers; the implementation must also reduce hidden dependencies and
decision density.

| Existing owner | Function | Lines | Proposed responsibility split |
|---|---|---:|---|
| `identity_usage_recording.py` | `record_remote_usage` | 351 | resolve binding, calculate epoch delta, allocate leases, persist ledger and enforcement intent |
| `commerce_service_receipt_submission.py` | `submit_receipt` | 298 | authorize and validate, detect duplicate evidence, record upload intent, upload, finalize review state |
| `telegram_transport_customer_vpn_dashboard.py` | `_send_my_vpn` | 274 | application query, immutable dashboard model, formatting, sending |
| `telegram_customer_commerce_commands.py` | `dispatch_customer_commerce_command` | 270 | command parsing, route dispatch, one handler per command |
| `commerce_service_inventory_reconciliation.py` | `refresh_server_inventory` | 263 | remote observation, reconciliation plan, persistence, repair and health decisions |
| `commerce_worker_capacity_snapshot.py` | `capacity_snapshot` | 222 | metrics collection, committed reservations, admission policy, snapshot projection |
| `telegram_admin_state.py` | `_admin_state_snapshot` | 221 | authorized query, immutable state model, presentation |
| `commerce_service_wallet_approval_flow.py` | `approve_order` | 217 | evidence validation, wallet/subscription decision, atomic writes and outbox |
| `commerce_worker_provisioning.py` | `_provision` | 197 | preflight, create/recover, atomic finalization, deferred follow-up |
| `runtime_bootstrap.py` | `compose_application` | 193 | settings validation, adapter factories, explicit wiring, startup and shutdown |
| `commerce_worker_repairs.py` | `_process_managed_key_repair` | 188 | repair eligibility, conservative quota calculation, remote repair, committed completion |

## 3. Target architecture and rules

### Dependency ownership

- Domain policy owns decisions and immutable values. It does not read environment
  variables, query databases, call provider clients, or depend on Telegram.
- Application use cases own transaction boundaries. They depend on small contracts
  for transactions, repositories, time, identifiers, encryption, and external effects
  only when those dependencies are actually needed.
- Repositories own query text, locks, dialect handling, and row mapping. Split them
  by cohesive aggregate or read model; avoid one repository per table or SQL call.
- Workers own bounded execution, leasing, retries, and recovery. Each handler
  receives explicit dependencies. The coordinator owns dispatch and scheduling.
- Telegram owns parsing, authorization entry checks, interaction state, formatting,
  and delivery. Application use cases enforce ownership and privilege requirements
  where bypassing the transport would otherwise allow unauthorized mutations.
- Composition roots construct concrete dependencies. A compatibility constructor
  may delegate to a composition factory while existing consumers migrate.

Keep transaction ownership visible. An application transaction interface should
expose only repositories required by that workflow, commit/rollback behavior, and
typed results. Its SQL connection stays inside persistence. An initial bridge may
pass the caller's existing connection to repositories internally; do not replace
one large service object with a unit of work that exposes every service or gateway.

External operations follow the existing durable intent and recovery contracts:
commit intent, execute remotely outside the database write transaction, observe the
outcome, then commit final state. Receipt upload remains a staged operation before
review admission. Do not promise exactly-once network delivery where the provider
cannot guarantee it; record ambiguous outcomes and reconcile them.

### Layout after boundary extraction

Retain the established layer-based target and group modules by feature within each
layer as needed. This is the proposed final layout; directories do not yet exist.

```text
aurix/
  domain/          # policy, states, immutable values
  application/     # commands, queries, transaction/port contracts
  persistence/     # repositories, dialect adapters, schema history
  workers/         # coordinator and explicit handlers
  integrations/    # Outline, Telegram HTTP, storage, provider adapters
  telegram/        # routers, interaction state, presenters
  bootstrap/       # settings, factories, lifecycle
tests/
  unit/            # pure policy and isolated use cases
  contracts/       # both database backends and adapter contracts
  integration/     # assembled workflows and failure/recovery scenarios
deploy/            # intentional operational entry points
```

Initially preserve existing root imports as explicit exports/delegation. Root
entry points such as `app.py` may remain permanently. Any other retained facade
needs a named compatibility consumer, a bounded API, and a documented review/removal
condition. Internal package code must not import root compatibility facades.

### Proposed size and complexity policy

- Aim for 30-80 lines in ordinary decision functions and below 300 lines in a
  focused executable module. These are design preferences, not completion metrics.
- Preserve the existing plan's review ceilings: application/worker orchestration
  at most 150 lines; Telegram dispatch/presentation at most 120 lines.
- Audit every executable module over 500 lines. Keep cohesive SQL, immutable
  migrations, declarative tables, and fixtures intact when splitting adds no clarity.
- Every remaining ceiling exception identifies the exact symbol, invariant,
  reason, owner, validating tests, and review trigger. A hard architectural violation
  cannot be waived through a line-count exception.
- No new dynamic binding that exposes an entire service/host, worker-to-service
  fallback, SQL in application layers, or imports from lower layers into composition.
- Track branch count/complexity and dependency fan-out alongside line counts.
  Freeze method and thresholds after the first complete measurement, then ratchet
  them without substituting empty wrappers for responsibility extraction.

## 4. Ordered implementation gates

Each gate can span several cohesive commits or pull requests. All are open unless
supported by final-candidate evidence. The ordering below gives the default sequence;
independent work can overlap as described in section 7.

### G01 — Correct the baseline and inventory

Work:

1. Record the candidate SHA, worktree, supported runtimes, dependency versions,
   source inventory, architecture measurements, test results, and coverage scope.
2. Inventory every production module recursively, including deployment scripts,
   with a layer, owner role, relevant contracts, and external consumers.
3. Replace substring exclusions with explicit classification. Treat endpoint
   migration as application/worker work and schema migration as persistence work.
4. Identify dynamic dispatch, shared host mutation, raw rows/connections crossing
   boundaries, concrete adapter construction, and compatibility consumers.
5. Mark previous percentages and zero-SQL claims as superseded. Retain historical
   measurements with their revision and denominator.

Exit: every owned module is classified, the 19 missed SQL calls are visible, and
the completion ledger records facts independently of editorial status prose.

### G02 — Make architecture and coverage checks complete

Primary files: `tools/check_architecture.py`, `architecture_baseline.json`,
`test_architecture_guard.py`, `pyproject.toml`, `.github/workflows/ci.yml`.

Work:

1. Discover root modules and packages recursively; exclude third-party environments,
   generated graph artifacts, and test fixtures explicitly.
2. Resolve qualified and relative imports, package initializers, aliases, and
   first-party cycles. Reject an unclassified new production module.
3. Apply layer rules to every module in a layer. Cover direct driver imports,
   concrete persistence imports, SQL-shaped calls, and known host-forwarding patterns.
4. Add negative fixtures: a nested violation, a relative-import cycle, a renamed
   application file containing `migration`, a new unlisted module, and a positive
   fixture for legitimate schema migrations. Document static analysis limitations.
5. Ratchet zero SQL ceilings where extraction is already complete. Keep the known
   19 calls as explicit temporary debt until G04; do not hide them through exclusions.
6. Include all production code in coverage, including never-imported modules and
   deployment code. Run coverage in CI. Measure the corrected denominator first;
   retain the existing 60% floor and restore coverage if it is below that floor.
7. Record evidence artifacts with SHA, tool versions, scope, failures and skips.

Exit: fixtures prove that the guard rejects each supported regression pattern;
the main guard reports known debt accurately; coverage reporting actually runs in CI.

### G03 — Establish narrow contracts and real database verification

Primary owners: `repositories.py`, `ports.py`, both commerce database adapters,
`persistence.py`, `test_persistence.py`, and repository contract tests.

Work:

1. Define the first transaction boundary around one cohesive use case. Specify
   commit, rollback, connection lifetime, repository participation, and concurrency.
2. Return immutable records or precise typed mappings. Remove `Any` and raw driver
   row objects from the migrated public contracts; keep compatibility conversion at
   the edge until consumers have moved.
3. Keep database locking and dialect choices inside persistence. Applications must
   not use `isinstance(connection, _PostgresConnection)` to select SQL syntax.
4. Run the same aggregate contracts against SQLite and disposable PostgreSQL in
   CI. Keep recording fakes for fast translation checks, with their limitations clear.
5. Test commit/rollback, uniqueness races, lease claiming/expiry, row-count conflict
   detection, UTC/time mapping, and resources returned after an exception.
6. Compose a pilot use case with a minimal fake transaction interface and explicit
   gateways. Verify that unrelated service dependencies are unnecessary.

Exit: both backends execute the shared contracts, and the pilot works without a
service host or an exposed raw connection. Missing PostgreSQL execution is an open
gate, not a skipped pass.

### G04 — Finish endpoint-migration persistence extraction

Primary files: `commerce_service_inventory_migration.py`,
`commerce_worker_migrations.py`, `connectivity_registry.py`.

Work:

1. Add an aggregate repository for migration eligibility/read context, durable
   request deduplication, claiming, retry state, and cutover writes.
2. Move the 5 service and 14 worker SQL calls, including lock-clause selection,
   behind that repository. Keep policy and external calls in the use case/handler.
3. Preserve quota/expiry checks, source/target identity, deterministic target keys,
   conditional cutover, notification/audit atomicity, and source-deletion retries.
4. Cover paid and free credentials; stale source usage; duplicate requests;
   ambiguous target creation; changed entitlement at cutover; source deletion
   failure after committed cutover; and a crash during each durable phase.
5. Prove stale lease ownership cannot overwrite a newer attempt. If a pre-existing
   defect is reproduced, isolate its regression fix from mechanical extraction.
6. Rerun the corrected whole-source audit and enforce zero application/worker SQL
   without filename-based exceptions.

Exit: both files have zero direct SQL, use no concrete database-type branches,
and preserve tested recovery behavior on both supported database backends.

### G05 — Make worker handlers independent

Primary owners: `commerce_worker_coordinator.py`, worker dispatch/provisioning,
repairs, lifecycle, capacity, endpoint-migration, and notification modules.

Work:

1. Introduce explicit handlers, beginning with notifications as a small pilot,
   then provisioning/revocation, repairs, endpoint migration, quota, and capacity.
2. Give each handler its required repositories/transaction factory and gateways;
   pass a clock or identifier source only when needed for behavior or reproducibility.
3. Construct the coordinator and handlers in bootstrap. A typed handler table is
   acceptable for dispatch; a registry that binds arbitrary methods to a host is not.
4. Preserve public `CommerceService.process_jobs()` and similar methods as explicit
   delegation during caller migration. Remove `CommerceWorker.service` and its
   attribute fallback from the active implementation.
5. Prove independent construction, durable claiming, bounded batches, retry policy,
   stale-owner rejection, shutdown, and recovery after a remote success/local failure.

Exit: workers neither access private service methods nor receive the full service.
Every handler can be tested through its own declared contract.

### G06 — Extract application decisions and long workflows

Work in small vertical slices: wallet/payment approval; receipt submission;
inventory/capacity; usage/quota; entitlement claims; identity/generations; fleet
administration, route failover, and infrastructure orchestration.

For each slice:

1. Inventory its decisions, transaction scopes, external effects, callers, and
   compatibility requirements before changing the implementation.
2. Extract named, testable decisions and read models using the priority table.
   Keep atomic writers in a visible transaction and avoid nested connections.
3. Inject protocols through constructors or explicit function parameters. Move
   concrete repository/client construction to bootstrap or compatibility factories.
4. Replace free-form `self` access and dynamic implementation binding with explicit
   delegation. Replace cross-feature mixin state with collaborators in identity,
   infrastructure, and entitlement paths.
5. Consolidate shared allocation, credential lifecycle, and usage policy only where
   behavior is actually common. Preserve legitimate paid/free/trial differences.
6. Test counter resets, duplicate and out-of-order samples, stale observations,
   quota boundaries, concurrent reservations, entitlement expiry, receipt duplicates,
   and the evidence/wallet prerequisites for approval.

Exit: the inventory has no unresolved hidden host dependencies; critical workflows
are independently constructible; transaction invariants pass; size exceptions are
documented per the policy. A cohesive repository is not split solely for SQL density.

### G07 — Bound Telegram state, routing, and presentation

Primary owners: `telegram_components.py`, `telegram_transport.py`, callback and
command modules, customer VPN dashboard, admin state, and capacity panels.

Work:

1. Separate update parsing, authorized command dispatch, application queries,
   immutable view models, rendering, and delivery.
2. Replace `TelegramComponent` host get/set forwarding with explicit presenters,
   routers, a conversation-state interface, and a delivery interface.
3. Build customer VPN, capacity, and admin-state views from application query
   results. Rendering must perform no database, service, or network reads.
4. Preserve persisted interaction expiry, callback payload contracts, private-chat
   boundaries, ownership checks, single-use confirmations, and stale-state rejection.
5. Use exhaustive routes with tests for unknown input and collisions. Keep routing
   functions within the agreed budget through cohesive command handlers.
6. Verify that a blocked remote probe/maintenance operation does not stall ordinary
   update handling; retain bounded timeouts, synchronization, and orderly shutdown.

Exit: presenters run with only view data; application operations enforce privilege
requirements; interaction-state ownership is explicit; callbacks and latency/failure
contracts pass without shared-host mutation forwarding.

### G08 — Make versioned migrations the schema authority

Primary owners: `commerce_schema_bootstrap.py`, `free_repository.py`,
`schema_migrations.py`, migration definitions and hooks, migration tests.

Work:

1. Inventory fresh-create and supported historical database states, including the
   free-only initializer. Capture schema, constraints, data, and ledger fingerprints.
2. Design adoption of unversioned base tables before moving DDL. Existing version
   numbers, names, and applied history must remain valid; do not rewrite migration
   1 or renumber existing commerce/free-access migrations to make room for base DDL.
3. Introduce a separately versioned base/adoption component where appropriate.
   Fresh databases create the canonical base; supported existing snapshots are
   validated and adopted without recreating or discarding business data.
4. Move compatibility alterations and backfills into ordered migration ownership.
   Keep table-specific DDL and data upgrades out of adapters/bootstrap. Keep seeds
   deterministic with a stated rule for preserving operator-edited configuration.
5. Preserve immutable history. Add registry integrity checking without inventing
   historical checksums that were never recorded; document any adoption policy.
6. Test both backends for fresh creation, every supported snapshot upgrade, repeat
   startup, unknown/renamed versions, constraint parity, interrupted application,
   transaction behavior, and retry after a failed migration.
7. Document expand/contract compatibility and recovery. A code revert alone may be
   invalid after a schema change; validate the exact fallback revision or restore.

Exit: business schema and upgrades have one versioned authority per dialect;
bootstrap only orchestrates migration execution, connection setup, and seeds;
fresh/upgrade/retry paths preserve schema and business-state contracts.

### G09 — Finish composition, packages, and compatibility retirement

Work:

1. Reduce runtime to validation, visible construction order, startup reconciliation,
   run loop, and shutdown. Separate factories from startup effects.
2. Move stable modules to the target package one feature boundary at a time. Update
   imports, tests, coverage discovery, deployment entry points, and guard ownership
   together. Do not mix package moves with behavior or schema changes.
3. Preserve tested public imports and constructor behavior while callers migrate.
   Replace tests that pin obsolete mixin/dispatch internals with API and behavioral
   contracts before deleting those internals; retain genuine compatibility tests.
4. Inventory first-party callers, deploy units/scripts, patch targets, and known
   external consumers. Remove unused registries and facades only after this inventory
   proves them unnecessary. Retained external facades use explicit delegation.
5. Verify startup and clean shutdown from the documented working directories and
   supported deployment commands. Internal imports must never depend on root facades.

Exit: packages own implementation; root modules are intentional entry points or
documented compatibility APIs; no production cross-feature host forwarding remains;
all discovered first-party imports satisfy the recursive dependency guard.

### G10 — Make development checks reproducible

Work:

1. Align the supported Python policy across deployment, CI, lint, packaging, and
   documentation. Current CI tests 3.12/3.13, Ruff targets 3.10, and the mounted
   project environment contains Python 3.14 packages. Verify the deployed contract
   before changing support; do not infer support from one local interpreter.
2. Record reproducible development/runtime dependency resolution, version updates,
   and interpreter/tool commands. Keep runtime dependency changes in separate commits.
3. Introduce a pinned type checker with strict checking for extracted domain,
   application, and worker contracts; make legacy adoption explicit and finite.
4. Expand Ruff correctness checks, including the rest of relevant `F` checks, then
   add import/style rules in isolated cleanup changes. Avoid blanket ignores.
5. Run recursive architecture, branch coverage, types, lint, compilation, both-backend
   contracts, shell validation, and offline acceptance in CI using documented commands.
6. Establish a full-source coverage baseline. Retain at least the existing 60% floor
   and prevent regression; propose at least 90% branch coverage for extracted pure
   policy and at least 80% for new/changed application-worker orchestration. These
   targets supplement named invariant tests and require real measured reports.
7. Verify the installed/built application and required entry points from a clean
   checkout. Compile owned source only, excluding environments and generated output.

Exit: a clean supported environment reproduces the candidate checks; no production
module disappears from coverage; critical contracts type-check; no mandatory backend
job can silently skip or be reported successful when its dependency job fails.

### G11 — Complete reliability and change-maintenance acceptance

Use section 5 as the minimum risk-driven failure matrix. Map existing tests first;
add tests only for missing behavior, regressions, and newly independent boundaries.

Work:

1. Verify transaction/effect sequencing with independent connections and fault
   injection at each boundary, including actual concurrent database clients.
2. Exercise ambiguous provider outcomes, duplicate delivery, stale leases, exhausted
   quota, partial migrations, and restart recovery without production credentials.
3. Record diagnostic fields needed to troubleshoot failures: correlation/job ID,
   phase, attempts, lease ownership, and sanitized error class. Verify secrets and
   receipt/credential payloads stay out of logs and snapshots.
4. Compare representative database query counts, open connections, transaction
   duration, memory, and update/maintenance responsiveness against the G01 baseline
   using fixed fixtures. Treat unexplained regressions as open work; avoid brittle
   wall-clock assertions where event/barrier tests establish the behavior directly.
5. Run three maintenance exercises: adjust an admission/quota policy, add a harmless
   dashboard field, and add a tested migration on a disposable database. Demonstrate
   that each touches its expected owners and tests without modifying unrelated flows.
   Use temporary tests/diffs; do not introduce unrequested product changes.

Exit: every in-scope external effect has named outcome/recovery evidence; critical
invariants hold on both backends; the maintenance exercises validate the boundaries.

### G12 — Produce the final evidence and maintenance handoff

Work:

1. Update README, architecture documentation, deployment/runbooks, package map, and
   this gate ledger from measured final state. Preserve historical reports as history.
2. Document module ownership by responsibility, how to add a workflow/query/migration,
   how to run tests, supported runtime policy, and how to diagnose and recover failures.
3. Run the complete required checks on one candidate SHA. Attach command, environment,
   outcome, failures/skips, coverage denominator, and artifacts for each gate.
4. Refresh Graphify after code changes and verification. Check high-risk call paths
   against source and disclose missing/extractor-limited content. Graph completeness
   or node count is not a completion criterion.
5. Review the final diff for behavior drift, schema compatibility, secrets, obsolete
   code, ignored files, and unrelated changes. Record the exact rollback procedure.
6. Publish the final completion report only when G01-G12 pass. Report source status,
   remote CI status, merge status, and deployment status independently. Publishing,
   merging, deploying, and provider actions follow the user's applicable authority;
   a plan or local test pass does not constitute those actions.

Exit: every gate links to evidence for the same revision, the remaining-exception
list contains only justified policy exceptions, and no mandatory task is labeled
complete using a percentage estimate or an unverified claim.

## 5. Required behavior and recovery evidence

| Boundary | Cases to verify | Invariant |
|---|---|---|
| Payment/wallet approval | duplicate requests, concurrent approvals, failure before commit, invalid evidence, reservation conflict | One financial outcome and subscription; state and durable intent commit together |
| Receipt pipeline | invalid/cross-user input, duplicate image/reference, upload failure, upload success followed by local failure, extraction retry | Review admission follows persisted valid evidence; retry preserves evidence identity |
| Provisioning | create failure, timeout after remote success, crash before finalization, replay, expiry during attempt | Reconciliation recovers the intended key; no duplicate entitlement activation |
| Quota/usage | duplicate/out-of-order sample, counter reset, missing endpoint, concurrent lease grants, exhaustion | Usage remains conservative and monotonic; missing evidence never becomes zero usage |
| Revocation/repair | deletion failure, stale usage, repair replay, expiry during repair, local commit failure | Access policy remains conservative; retry cannot reset quota or resurrect invalid access |
| Endpoint migration | stale source usage, target ambiguity, changed entitlement, crash at cutover, source delete retry | Conditional cutover and remaining quota remain consistent; source cleanup is recoverable |
| Jobs/outbox | simultaneous claim, lease expiry, stale completion, crash after send, restart | One current claim owner; durable retry; duplicate network delivery limits are documented |
| Telegram/admin | expired/replayed/cross-user callbacks, role change, state fingerprint change, blocked maintenance | Privilege and ownership checks hold at execution; ordinary updates remain responsive |
| Fleet/provider workflow | retry after uncertain creation, enrollment replay, observation freshness, changed provider identity | Intent, observed state, and committed allocation remain distinct; existing mutation gates remain enforced |
| Schema/runtime | fresh create, supported upgrade, interrupted migration, unknown history, startup failure, shutdown | Data/history preserved; migration ledger is truthful; connections and workers close predictably |

## 6. Evidence ledger and reporting

For each gate maintain these fields in the implementation completion report:

```text
gate_id | owner_role | status | candidate_sha | required_tests
command_and_environment | result | artifact | exceptions | remaining_work
```

Statuses: `pending`, `in_progress`, `verified`, or `blocked_by_external_requirement`.
A blocked or skipped mandatory check remains incomplete. An early gate's evidence
must be rerun or revalidated against the final candidate before final sign-off.

Report completed gate IDs, remaining work, and next gate. Do not equate the count
of gates or merged PRs with percentage of effort. Split a gate into checklist items
before execution so its scope cannot be silently narrowed to claim completion.

## 7. Execution and review discipline

Default order: G01 -> G02 -> G03 -> G04 -> G05 -> G06 -> G07 -> G08 -> G09 ->
G10 -> G11 -> G12. Enabling G02 coverage and G03 PostgreSQL checks early makes
later extraction failures visible immediately.

After G03, schema work can proceed independently of worker/application extractions
provided schema behavior changes are reviewed separately. Telegram read-model work
can proceed once the relevant application query contracts stabilize. G09 moves each
component only after its dependency interface stabilizes. G10 checks should be
enabled progressively on completed components, not postponed to the end.

Use one ownership change per reviewable commit/PR. The repeated unit of work is:
characterize the risky boundary, extract one component, run focused checks, review
the diff, tighten its guard, then run the broader required gate. Avoid simultaneous
edits to shared dispatch maps, bootstrap, migration registries, or baselines. Parallel
contributors should own disjoint files and integrate through an explicit coordinator.

Every change records public behavior preserved, dependency changes, transaction
scope, failure semantics, evidence, and rollback. Application refactors preserve
schema/data; schema changes carry their own upgrade and fallback proof. Keep commits
separable. Revert only the relevant committed change when appropriate; never reset
unrelated user work or rewrite applied database history.

Planning does not establish a reliable calendar duration. After the first pilot,
estimate the remaining slices using observed review/test cost. The long usage,
receipt, migration, and Telegram workflows carry the greatest uncertainty.

## 8. Storage and local validation

The existing mounted workspace is available at `/Volumes/MacHomeVenvs/aurix-mr`,
with `project-venv`, `tmp`, and `pycache` paths. The read-only planning check observed
about 7.5 GiB available on that mounted volume and 2.6 GiB on the internal data
filesystem. These values are time-specific; the mount name alone does not establish
its physical backing or available expansion capacity.

Reuse the existing environment only after checking its interpreter against the
supported runtime matrix. Place temporary files, bytecode, coverage artifacts, and
dependency caches on the designated mounted storage. Prefer disposable PostgreSQL
in CI if local database/container images would pressure internal storage. Do not
install or migrate environments as part of this documentation-only step.

Before validation, verify mount availability and writable paths. Never silently
fall back to internal storage when the intended volume is missing. Record the
interpreter and commands with the evidence, keep artifacts separate from source,
and remove only identified task-owned temporary artifacts when cleanup is needed.

## 9. Maintenance after completion

- Every change: run the relevant contract tests, types, architecture and coverage
  gates; update documentation when an interface or operational behavior changes.
- Every dependency/runtime update: use an isolated reviewable change, supported
  matrix checks, adapter contracts, and reproducible resolution evidence.
- Every release: produce the candidate evidence bundle and validate upgrade and
  rollback compatibility; run live acceptance only through its deployment workflow.
- Monthly review: inspect new dependency crossings, expired exceptions, skipped or
  flaky tests, dependency/security updates, and growth in high-change workflows.
- Quarterly review: rehearse documented database recovery and assess whether module
  ownership and troubleshooting guidance still match actual maintenance work.

These are proposed team practices, not scheduled automations. Source architecture
acceptance can close when its gates pass; live customer canaries, hosted database
cutover, provider enrollment, offsite restore/fencing drills, and sustained production
observation remain separately recorded deployment gates. No production-readiness
percentage is inferred from local code or documentation.
