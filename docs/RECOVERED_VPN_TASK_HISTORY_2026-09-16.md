# Recovered VPN task history

Date: 2026-09-16 (Asia/Rangoon)

This document reconstructs the VPN-relevant decisions and evidence from the
Codex tasks supplied in the conversation. Secret URLs, fingerprints, tokens,
passwords, keys, and other credential material are intentionally omitted.

## Source index

The supplied list contains 19 unique task IDs; one ID was repeated:
`01a0483d-0d1a-7e60-adbe-9949223d8d45`.

Relevant VPN/control-plane sources:

- `01a04b66-91be-7982-86ba-876269424e18` — V1–V13 roadmap, Outline API fact-check, V2/V3/V4/V9/V10/V13 classification.
- `01a06acf-8d7d-7cc0-a2bd-13658e57487b` — final-plan comparison, multi-server orchestration, device/client testing, implementation judgment.
- `01a0786a-3855-75c3-8e85-abb2a17c2c27` — server inventory, sg-b management/data-path history, relay/key-mapping decisions.
- `01a04f1c-1fd9-7c01-b7c9-c8db3af2fb6c` — Shadowbox bindings and Xray/REALITY canary work.
- `01a05b6b-23c4-7612-9d92-0fa15e5d7a36` — customer reset, quota alerts, receipt/admin separation.
- `01a04201-5b2c-73e0-a131-c4aea933739e` and `01a041f5-044e-73c2-8299-ab3e876ae02e` — initial Telegram/Outline MVP and claim-bot safety.
- `01a041d3-9de4-7f30-88b4-15f003e11648` — interrupted initial task; no reliable implementation evidence.

Out-of-scope sources were also readable but are not used as VPN roadmap
evidence: 9Router/AI tasks, payment-app UI automation, social-media review,
domain launch, and monolith-reduction work.

## Recovered product progression

1. The initial system was a Telegram-first paid Outline bot. The early MVP
   supported claims, expiry/revocation, SQLite persistence, certificate
   pinning, and basic tests, but lacked durable recovery around remote key
   creation and Telegram delivery.
2. Commerce, receipt storage, admin review, quota warnings, PostgreSQL support,
   auditability, and worker/retry boundaries were added over subsequent tasks.
3. The architecture was classified as **V2** while it remained single-node and
   single-transport. The canonical progression became:

   ```text
   V2 → V3 multi-server/multi-region Outline
      → V4 health-aware Outline
      → V9 Outline + Xray
      → V10 protocol-agnostic fabric
      → V11 verified adaptation
      → V13 bounded resilient platform
   ```

4. V3 is the correct next transport milestone: multiple Outline servers and
   regions, one active endpoint assignment per entitlement, explicit endpoint
   identity, migration/reconciliation, and server-local usage accounting.
5. A stock Outline access key cannot roam automatically between servers. A
   Telegram bot can select or migrate an endpoint, but seamless failover needs
   a client-refreshable profile or a future AuriX client. Quota transfer is not
   provided by Outline and must be ledgered by AuriX.
6. Xray/VLESS REALITY and Hysteria2 were approved only as isolated, bounded
   canaries. They are not production-ready multi-customer adapters merely
   because their binaries are installed.

## Fleet identity recovered

| Node | Provider/region | Address | Recovered role |
|---|---|---|---|
| sg-a / primary | DigitalOcean SGP1 | `157.245.63.95` | Control plane, Outline, Xray, Hysteria2, web services, sg-b relay |
| sg-b | DigitalOcean SGP1 | `139.59.122.170` | Standalone Outline data node |
| sg-c | DigitalOcean SGP1 | historical `139.59.123.125` | Destroyed/retired; do not treat as active |
| bkk-a | Nube Bangkok | `191.40.15.51` | Independent Outline regional edge |

The current two-server scope is sg-a and sg-b. bkk-a remains a separate
regional option and sg-c is not an allocation candidate.

## Important historical contradiction

An earlier sg-b repair task proposed rotating old keys to an sg-a relay
endpoint. Later, the owner explicitly required:

```text
sg-a keys → sg-a address
sg-b keys → sg-b address
```

The later requirement supersedes the earlier relay-key experiment. The latest
live observation confirms sg-b's Outline API advertises its own address and
data port `45525`; 13 keys were present and the management API and metrics
endpoint returned successfully. The sg-a relay remains a management/data
fallback, not the canonical identity of sg-b keys.

Therefore:

- direct sg-b keys must be reachable on `139.59.122.170:45525` over TCP and
  UDP;
- sg-b management `61604/tcp` must remain restricted to the sg-a management
  path/admin access;
- do not rotate or recreate keys merely to compensate for a firewall problem;
- if direct customer reachability cannot be made reliable, document a separate
  relay profile rather than silently rewriting the canonical sg-b identity.

## Latest implementation snapshot recovered

The current checkout is `/Users/min/projects/tg-AuriX-bot`, branch
`codex/aurix-vpn-portal`, at commit `cdf1516`.

Recent VPN implementation steps:

- `9bb4cb6` — dynamic multi-server SSConf issuance, migration 20, health-aware
  route selection, encrypted token storage, fail-closed behavior.
- `cdf1516` — customer portal Settings UI for refreshable Outline profiles,
  with route-count and stock-client limitations shown.

Latest local verification recorded:

- 220 VPN-focused tests passed.
- Ruff, JavaScript syntax, Python compilation, and whitespace checks passed.
- Graphify was refreshed.
- No unrelated AI, Android, social, research, or conversation-export files
  were staged.

## Current operational facts recovered in this task

- sg-a is reachable with the local SSH identity and has active UFW rules,
  Outline, Xray, Hysteria2, and sg-b management/data relay services.
- sg-b has a healthy standalone Outline management API, Outline 1.12.3, 13
  access keys, data port `45525`, and working transfer metrics.
- Direct TCP access to sg-b data port from the operator path timed out, while
  sg-a could reach sg-b and sg-a's relay port was reachable.
- DigitalOcean currently showed no Cloud Firewall assigned to the sg-b
  Droplet. The two existing objects (`hysteria2` and
  `aurix-hysteria2-sg-c`) each showed zero Droplets and were therefore inert.
- No server, key, or firewall resource was changed during the interrupted
  firewall-configuration attempt; only read-only inspection and unsaved UI
  drafts occurred.

## Recovered continuation order

1. Reconcile Cloud Firewall and host-UFW state for sg-a and sg-b.
2. Apply dedicated per-node firewall policies without deleting stale objects.
3. Verify direct sg-b TCP/UDP `45525` from a declared non-tunneled client
   vantage; verify sg-b management only through the restricted path.
4. Test existing keys before recreating anything.
5. Record health, latency, loss, throughput, and relay/direct results separately.
6. Only after Outline direct-path acceptance, proceed to isolated Xray and
   Hysteria2 canaries and later adapter/accounting work.

