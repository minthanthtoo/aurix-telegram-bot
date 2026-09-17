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

## Read-only BKK-A spot check — 2026-09-11 03:36 UTC

A fresh batch-mode SSH check using the existing BKK-A read-only access confirmed
hostname `VM-BKK1-H3P8W5SFMA`. The host reported approximately 295 MiB
available memory and 850 MiB free swap. `shadowbox` remained up for eight days
and `watchtower` was healthy for two days. Outline continued to own TCP/UDP
443, while management TCP `61603` remained present; no Xray or Hysteria2
listener was observed. This is a point-in-time observation and does not alter
the existing capacity or protocol gate.

## Read-only Singapore access spot check — 2026-09-11 03:37 UTC

Using the documented DigitalOcean key in batch mode, sg-a
`157.245.63.95` still returned `Permission denied (publickey)`. sg-b
`139.59.122.170` still timed out on TCP/22. No remote command was executed on
either Singapore host, and no access key, firewall rule, service, or customer
state was changed.

## Read-only Singapore access recheck — 2026-09-13

A further single, batch-mode SSH attempt to sg-a `157.245.63.95` used the same
documented DigitalOcean key with strict host-key checking and an eight-second
connection bound. Authentication again ended at `Permission denied
(publickey)`, before any remote command ran. No retry was made against sg-b
because this result neither restores Singapore administrative access nor
changes the existing direct-client-path evidence.

This is an access finding, not a server-health result. It blocks only the live
Singapore inspection/canary gates; it does not justify modifying the local
control plane, enabling a protocol profile, or treating historical host
measurements as current capacity evidence.

## Singapore repair and verification — 2026-09-16

The operator workstation's current public address was `45.41.106.60`. The
documented historical allowlist contained `.67`, so the primary management
port was filtered for the current operator path. A narrow sg-a UFW allow was
added for the current address on TCP `61603` (sg-a management) and `61604`
(the existing sg-b management relay). No management port was opened to the
world.

The sg-a relay units had been manually stopped and disabled since 2026-09-09,
while their firewall rules remained present. Both were started and enabled at
boot:

- `aurix-sgb-data-tcp-relay.service` — active, enabled, TCP `45525`;
- `aurix-sgb-data-udp-relay.service` — active, enabled, UDP `45525`.

From sg-a, both relay targets were reachable: sg-b data TCP `45525` and sg-b
management TCP `61604`. The sg-b management relay was already active and
enabled. Through a pinned TLS check over the relay, sg-b returned HTTP 200 for
`/server`, `/access-keys`, and `/metrics/transfer`; the primary sg-a API passed
the same three checks through its management path. No access URLs, certificates,
tokens, or customer identifiers were printed.

The repair also reduced sg-a archived journald data from about 1.1 GiB to a
bounded ~272 MiB, raising free root space from about 502 MiB (98% full) to
about 1.4 GiB (95% full). At verification, AuriX, Xray, Hysteria2, Outline,
the sg-b management relay, and both sg-b data relays were active.

This restores the intended operator/relay path, but it is not an authenticated
customer-session or throughput result. Direct sg-b management remains
intentionally firewalled; Outline Manager must use sg-a's relay endpoint for
sg-b. A full customer-path check still requires a declared client vantage.
