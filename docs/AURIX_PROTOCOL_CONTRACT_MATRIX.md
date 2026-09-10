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

The test is deliberately provider-local and deterministic. It proves that the
controller adapters do not cross customer or protocol state when they share one
authenticated lifecycle contract. It does **not** prove real Xray or Hysteria2
behavior, public-network reachability, Myanmar compatibility, restart persistence,
native quota enforcement, bandwidth, latency, or 24–48-hour soak safety.

Those external gates remain ordered: validate one disposable canary per protocol,
then run staged 2/5/expected concurrency and speed/soak measurements on approved
targets before registering either protocol in the production adapter registry.
