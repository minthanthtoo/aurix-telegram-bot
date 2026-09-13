# Multi-protocol coordination gate — 2026-09-09

## Owners and authority

- Backend: `Aurix-vpn`, task `01a06acf-8d7d-7cc0-a2bd-13658e57487b`.
  Focused local accounting corrections and regression tests; no deployment.
- Servers: `Explore server inventory`, task
  `01a0786a-3855-75c3-8e85-abb2a17c2c27`.
  Isolated canary preparation; live execution awaits scoped approval.
- Coordinator: task `01a085a8-b3be-7901-8723-31ec8487e53d`.
  Independent evidence review and sequencing. No spending, resizing, customer
  migration, or production rollout authorized by this record.

## Independently verified local findings

In the portal worktree, 68 focused tests passed across test_identity.py,
test_route_failover.py, test_connectivity_adapters.py, test_commerce.py,
test_migrations.py, and test_mvp.py. Passing tests do not establish end-to-end
or live PostgreSQL correctness.

Two additional reproductions were assigned to the backend owner:

1. New generation readings 400, 500, then 50 after reset yielded total usage
   0, 100, 100. Require explicit provenance distinguishing a fresh credential
   from a trusted migration baseline; do not silently discard initial or
   post-reset usage.
2. A 1,000-byte lease on A with a 30-second local TTL allowed another full
   1,000-byte lease on B after 31 seconds while both generations remained
   remotely usable. Local TTL alone is not proof remote capacity was released.

Also assigned: verify PostgreSQL compatibility of scalar MIN in lease updates.
Existing concurrency coverage checks a lock helper, not real concurrent
PostgreSQL transactions. Worker-level failover accounting, ambiguous-create
cleanup after local persistence failure, and partial fan-out revoke recovery
need explicit coverage before stronger completion claims.

## Ordered gates

1. Fix and independently retest local accounting invariants. Preserve unrelated
   dirty changes and keep production/reference worktrees separate.
2. Obtain approval for the isolated sg-a Xray canary described in
   AURIX_XRAY_CANARY_SERVER_HANDOFF_2026-09-09.md. No occupied-service restart.
3. Verify customer isolation, per-customer counters, quota enforcement delay
   and overshoot, new and existing session revoke, rotation, restart, controller
   outage behavior, and cleanup. Connectivity alone is insufficient.
4. Only then design/validate the Xray adapter against measured capabilities.
   Keep it disabled until backend integration and recovery gates pass.
5. Validate Hysteria2 separately with per-customer auth and accounting; do not
   run it concurrently with the first Xray canary.
6. After correctness, propose bounded per-protocol and mixed-protocol tests,
   comparing existing Outline baselines, latency, throughput, resource use,
   errors, reconnects, quota overshoot and egress cost. Obtain load-test scope
   approval; never infer sustainable capacity from one speed test.
7. Reconcile deployed release/database versus local target, migrations,
   rollback and operational ownership before any rollout authorization.

## Fleet and evidence caveats

- sg-a hosts the control plane and other services; preserve their resource
  headroom and health throughout any approved test.
- bkk-a remains explicitly in compatibility/capacity planning; do not infer
  Xray readiness from its existing Outline service or install there implicitly.
- sg-b's latest handoff says admin access could not be reproduced. This
  supersedes earlier successful-access observations. Direct client data-path
  validation is still open.
- A Myanmar client-network test cannot be replaced by server-to-server traffic.
- Strict quota guarantees cannot be claimed if existing sessions survive
  revocation or a controller outage allows unbounded usage.
- Canary byte ceilings must cover both directions and retries, with independent
  bounded client transfers/timeouts. Do not alter the user's VPN without consent.

## Superseding Xray evidence — 2026-09-10

The coordinator completed the bounded SG-A Xray lifecycle and verified cleanup.
Two disposable VLESS/REALITY users connected through isolated TCP `18443`,
produced distinct per-user counters, and reached the owned test destination.
Removing A blocked new A connections but did not terminate an established A
session. B rotation worked: the new generation connected and the old one failed
new connections after removal. A controller harness revoked B2 after a bounded
10,000-byte threshold; this is controller-enforced blocking, not native Xray
hard-quota enforcement. A canary-only restart also showed that runtime API-added
users are not persistent.

The backend now contains an explicit reconciliation boundary that rehydrates only
durable `active`/`retiring` generations with observed ownership, verifies read-back,
and leaves unknown provider users untouched. It is not yet a deployed node-agent
or config-writer, so it does not change the Xray commercial gate. Settle
existing-session quota/revoke semantics and validate this path against a canary-only
restart before registration. Hysteria2, Myanmar-network behavior, management
outage, and load/soak/speed evidence remain open. Cleanup left occupied Xray,
Hysteria2, Outline, AuriX, Docker, and relay services healthy; no rollout or
customer state changed.

This is a coordination snapshot; the superseding evidence above controls the
Xray canary status, while the ordered gates still control production rollout.
