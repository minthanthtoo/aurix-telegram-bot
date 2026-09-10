# AuriX protocol contract matrix

This repository now has a bounded local concurrency test for the shared node-agent
boundary: `test_multi_protocol_contract.py` provisions eight Xray and eight
Hysteria2 customer records concurrently, checks protocol-specific route rendering,
per-customer usage, quota assignment, data-plane probe results, inventory
reconciliation, and revocation.

The test is deliberately provider-local and deterministic. It proves that the
controller adapters do not cross customer or protocol state when they share one
authenticated lifecycle contract. It does **not** prove real Xray or Hysteria2
behavior, public-network reachability, Myanmar compatibility, restart persistence,
native quota enforcement, bandwidth, latency, or 24–48-hour soak safety.

Those external gates remain ordered: validate one disposable canary per protocol,
then run staged 2/5/expected concurrency and speed/soak measurements on approved
targets before registering either protocol in the production adapter registry.
