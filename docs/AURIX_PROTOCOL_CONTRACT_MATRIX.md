# AuriX protocol contract matrix

This repository now has a bounded local concurrency test and runner for the
shared node-agent boundary. `test_multi_protocol_contract.py` covers staged
2/5/16-customer cohorts per protocol; `scripts/aurix_protocol_matrix.py` emits
JSON evidence for a selected cohort. Each run checks protocol-specific route
rendering, per-customer usage, quota assignment, data-plane probe results,
inventory reconciliation, operation latency, and revocation.

Run the local staged matrix with:

```bash
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 2 --workers 2
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 5 --workers 5
uv run python scripts/aurix_protocol_matrix.py --customers-per-protocol 16 --workers 8
```

These commands exercise only an in-memory provider and the authenticated local
agent contract. They are safe regression checks, not live server load tests.

Latest bounded stress run (2026-09-11 03:56 UTC): `--customers-per-protocol
200 --workers 64` passed with 400 provisioned/reconciled/revoked grants and no
errors. Provision latency was mean 0.150 ms, p95 0.203 ms, and max 6.301 ms in
the local process. These timings describe the test harness only and must not be
used as server capacity or customer speed evidence.

The test is deliberately provider-local and deterministic. It proves that the
controller adapters do not cross customer or protocol state when they share one
authenticated lifecycle contract. It does **not** prove real Xray or Hysteria2
behavior, public-network reachability, Myanmar compatibility, restart persistence,
native quota enforcement, bandwidth, latency, or 24–48-hour soak safety.

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
signal and matching declared capabilities, and records the operator decision in
the commerce audit log when available; observation ingestion never auto-enables
a protocol. Direct profile registration also cannot mark Xray or Hysteria2
enabled; those transports must pass the explicit promotion method.
The registry also exposes a non-mutating readiness preview so operators can
see missing fresh signals and capabilities before attempting promotion.

Those external gates remain ordered: validate one disposable canary per protocol,
then run staged 2/5/expected concurrency and speed/soak measurements on approved
targets before registering either protocol in the production adapter registry.
