# AuriX read-only fleet check — 2026-09-11

Status: diagnostic snapshot only; no server or customer state was modified.

## Scope

The check used the documented candidate nodes and short, batch-mode SSH
connections. Remote output was restricted to hostname, coarse resource state,
container/service state, and selected listener presence. No management URLs,
certificates, access URLs, tokens, or private material were read or printed.

## Results

| Node | SSH result | Current observation |
|---|---|---|
| sg-a `157.245.63.95` | Rejected with `Permission denied (publickey)` using the documented DigitalOcean key | No host state asserted |
| sg-b `139.59.122.170` | Rejected with `Permission denied (publickey)` using the documented DigitalOcean key | No host state asserted |
| bkk-a `191.40.15.51` | Accepted with `/Users/min/.ssh/aurix_bkk3` | Host `VM-BKK1-H3P8W5SFMA`; Docker active; `shadowbox` and `watchtower` containers healthy; TCP and UDP `443` listeners present |

BKK reported approximately 290 MiB available memory, 850 MiB free swap, and
62% root-filesystem use at the observation point. These are snapshots, not
sustainable capacity measurements.

## Gate interpretation

- `bkk-a` remains Outline-only in the evidence set. Its reachability does not
  authorize adding Xray, Hysteria2, or any other service.
- Singapore administrative access must be reconciled before any live
  inspection, migration, or canary can proceed there.
- Listener presence is not proof of authenticated client connectivity, quota
  enforcement, per-user accounting, restart persistence, or safe capacity.
- The next live action still requires explicit target/credential review and
  approval for any production mutation or load generation.

## Read-only recheck — 2026-09-11

A second batch-mode check was performed without reading secrets or changing
remote state. The documented DigitalOcean key still received `Permission
denied (publickey)` from sg-a; sg-b did not complete SSH within the bounded
timeout. BKK-A remained reachable and reported approximately 309 MiB available
memory, 859 MiB free swap, and 62% root-filesystem use. `shadowbox` remained up
for eight days and `watchtower` was healthy; TCP/UDP `443` and management TCP
`61603` were present. No Xray or Hysteria2 listener was observed on BKK-A.

This recheck does not establish customer data-path compatibility, quota,
accounting, restart persistence, or sustainable capacity. It leaves BKK-A
Outline-only and keeps Singapore inspection and all protocol experiments behind
the existing access and approval gates.
