# AuriX protocol contract matrix

This repository now has a bounded local concurrency test and runner for the
shared node-agent boundary. `test_multi_protocol_contract.py` covers staged
2/5/16-customer cohorts per protocol; `scripts/aurix_protocol_matrix.py` emits
JSON evidence for a selected cohort. Each run checks protocol-specific route
rendering, per-customer usage, quota assignment, data-plane probe results,
inventory reconciliation, one representative rotation/session-termination/
verified-revocation lifecycle per protocol, operation latency, and final
revocation.

Run the local staged matrix with:

```bash
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 2 --workers 2
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 5 --workers 5
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 16 --workers 8
```

These commands exercise only an in-memory provider and the authenticated local
agent contract. They are safe regression checks, not live server load tests.

Latest bounded stress re-run (2026-09-13 04:00 UTC):
`--customers-per-protocol 200 --workers 64` passed with 400
provisioned/revoked grants, two representative rotations, and no errors.
Reconciliation was correctly protocol-scoped at 200 Xray and 200 Hysteria2
users. Provision latency was mean 0.795 ms, p95 0.496 ms, p99 32.330 ms, and
max 33.212 ms in the local process. The staged 2/5/16-customer runs also pass.
These timings describe the test harness only and must not be used as server
capacity or customer speed evidence.

The test is deliberately provider-local and deterministic. It proves that the
controller adapters do not cross customer or protocol state when they share one
authenticated lifecycle contract. It does **not** prove real Xray or Hysteria2
behavior, public-network reachability, Myanmar compatibility, restart persistence,
native quota enforcement, bandwidth, latency, or 24–48-hour soak safety.

When one protocol-neutral node agent serves multiple transports, every
`list_users` record must include its `protocol`. Managed adapters discard users
explicitly tagged for another transport before inventory/reconciliation. A
dedicated single-protocol agent may omit the tag for compatibility, because its
route boundary already scopes the inventory.

The same rule applies to idempotent provision/recovery: if a shared agent
returns an existing external ID tagged for another protocol, the request fails
closed rather than adopting or deleting that credential.

The control plane now applies the same explicit enabled-profile requirement to
allocation, failover target selection, operator drain, assignment transfer, and
target-generation verification. A candidate or disabled profile therefore cannot
be reached by a queued or in-flight move; this is a local invariant, not live
protocol evidence.

Protocol-specific observations are stored separately from endpoint-wide capacity
snapshots, so management, client-path, quota, restart, and session evidence can
be reviewed per transport without promoting a candidate profile. Observation
records are bounded and redacted before persistence. The explicit promotion
boundary requires fresh, non-expired healthy observations for every required
a non-Outline signal's structured proof fields (sample count, quota/restart
assertions, or a data-plane path/status as applicable) and matching declared
capabilities, and records the operator decision in the commerce audit log when
available; observation ingestion never auto-enables
a protocol. Direct profile registration also cannot mark Xray or Hysteria2
enabled; those transports must pass the explicit promotion method.
The registry also exposes a non-mutating readiness preview so operators can
see missing fresh signals and capabilities before attempting promotion.

Those external gates remain ordered: validate one disposable canary per protocol,
then run staged 2/5/expected concurrency and speed/soak measurements on approved
targets before registering either protocol in the production adapter registry.
