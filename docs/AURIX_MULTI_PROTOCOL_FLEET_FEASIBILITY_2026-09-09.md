# AuriX multi-protocol fleet feasibility assessment

Date: 2026-09-09 (Asia/Rangoon)

Status: preparation plus bounded remediation. The original measurements below
remain read-only; the remediation note records the limited SG relay/firewall
changes made after the operator confirmed that sg-c was destroyed. A later
bounded SG-A Xray lifecycle canary is recorded in section 17; it does not
change the production-readiness gates.

Secrets intentionally omitted: management secret paths, certificate fingerprints,
access URLs, tokens, private keys, customer identifiers, and payment credentials.

### 2026-09-09 SG remediation

- Removed the obsolete sg-c relay firewall rules from sg-a. sg-c is destroyed and
  must not be represented as an allocation candidate.
- Enabled and started `aurix-sgb-data-tcp-relay.service` and
  `aurix-sgb-data-udp-relay.service` on sg-a. Both relay `45525` to the verified
  sg-b Outline listener on `139.59.122.170:45525`.
- Verified that the sg-b management relay is active on `sg-a:61604`, and that
  sg-a accepts TCP connections on both `61604` and `45525`.
- Outline Manager should use the sg-a relay endpoint (`157.245.63.95:61604`)
  with sg-b's original secret path and certificate fingerprint; direct
  `139.59.122.170:61604` remains intentionally restricted.
- A real Outline client session has not been run, so customer-path acceptance
  remains pending even though the relay listeners are now live.

## Executive decision

AuriX can support a controlled multi-protocol pilot, but the current fleet is not
sustainable for commercial multi-protocol integration without four gates:

1. Complete the source-of-truth reconciliation between the deployed release and
   this checkout.
2. Keep customer traffic off the overloaded control-plane host and provision a
   dedicated, larger protocol node.
3. Implement real per-customer Xray and Hysteria2 lifecycle/accounting adapters
   behind the existing `ConnectivityAdapter` registry.
4. Establish Myanmar-client measurements, concurrency evidence, and a durable
   PostgreSQL production profile before making capacity or reliability promises.

The recommended order is:

```text
Outline remains the default
    -> Xray/VLESS REALITY as the first managed alternative
    -> Hysteria2 as a UDP canary
    -> WireGuard only after protocol-neutral usage accounting is proven
```

## 12. Validation continuation recheck — 2026-09-09

This section records the next read-only validation pass after the original
assessment.

### Rechecked state

- sg-a remains active: AuriX bot, Docker/co-hosted services, Outline, Xray,
  Hysteria2, monitoring, and the sg-b/BKK management relays are running.
- sg-a currently has about 381 MiB available memory, about 269 MiB swap in use,
  and 4.1 GiB free on an 83%-full 25-GiB root filesystem. This is a current
  observation, not a sizing requirement.
- bkk-a remains Outline-only with Docker, about 284 MiB available memory, about
  169 MiB swap in use, and 3.8 GiB free on a 60%-full root filesystem. These
  values are current observations, not an upgrade mandate.
- All rechecked services reported active and zero systemd restart counts at the
  observation point. This does not prove long-term stability.

### Exact deployed backend release

The live bot is executed from:

```text
/opt/aurix-current/.venv/bin/python -u /opt/aurix-current/app.py
```

`/opt/aurix-current` resolves to release directory:

```text
/opt/aurix-releases/5789128fcf83ea83113a126bd67a2298164141ed
```

The service uses the `aurix-bot.service` unit and its working directory is the
release symlink. The release directory is deployment-artifact based rather than
a checked-out Git worktree, so the release hash is the exact deployment
identity available from the host; a source commit could not be established from
the release directory alone.

The deployed release contains the existing `ConnectivityAdapter` contract,
`ConnectivityAdapterRegistry`, `OutlineConnectivityAdapter`, fleet routing,
usage, health, and failover modules. It still registers only Outline as a
concrete adapter.

### Protocol testability without restarting occupied services

#### Xray

- Installed version: Xray `26.3.27`.
- Current public inbound: VLESS over TCP with REALITY on `8443`.
- Current configuration has one client, no Xray API, and no Xray stats service.
- `xray run -test` returns `Configuration OK`, with the existing warning that
  REALITY on a non-443 port may be blocked by some networks.
- The existing `xray.service` must not be reloaded for a canary because that
  would alter the occupied manual inbound. An `xray@.service` template exists
  and reads a separate `/usr/local/etc/xray/<name>.json`, so an isolated second
  instance is technically possible without restarting `xray.service`.
- Starting that instance, opening its firewall port, and creating its client
  identities are mutations. They were not performed.

#### Hysteria2

- Installed version: Hysteria2 `2.12.2`.
- Current listener: UDP `443`.
- Current configuration uses one password-style authentication block; the
  password and TLS material remain redacted.
- No per-customer management or usage interface is configured.
- An `hysteria-server@.service` template exists and reads a separate
  `/etc/hysteria/<name>.yaml`, so an isolated second instance is technically
  possible without restarting `hysteria-server.service`.
- Starting it, opening an alternate UDP port, and creating per-customer auth
  are mutations. They were not performed.

### sg-b administrative and direct-path resolution

The configured fleet SSH key, used only from sg-a, successfully authenticated to
sg-b. The read-only host check verified:

- Ubuntu 24.04, 1 vCPU/1-GB-class DigitalOcean VM;
- Outline `shadowbox` and `watchtower` only;
- Outline TCP/UDP listener on `45525`;
- no listener on `443`;
- UFW permits `45525/tcp` and `45525/udp` from sg-a only;
- the sg-b management listener is on `61604` and is separately restricted.

This explains the earlier confusion: sg-b keys correctly target
`139.59.122.170:45525`, but direct customer access is firewall-dependent and
the normal sg-a data-relay units are disabled. A raw socket check from the Mac
returned TCP success while multiple `utun` interfaces were active; because that
does not prove an authenticated Outline session or an unambiguous client route,
it is recorded as inconclusive rather than as client success.

The next sg-b test must be an actual Outline client session from a declared
non-tunneled operator or Myanmar network, with egress verified as
`139.59.122.170`. No key was changed to perform this check.

### Isolated test location decision

No unused, already-authorized protocol node was found. bkk-a and sg-b are
Outline-only. sg-a is the only host with the installed Xray and Hysteria2
binaries, but it is also the control plane and has limited memory/disk headroom.

Therefore the least disruptive available experiment is a pair of isolated
systemd instances on sg-a using alternate ports, not a change to the occupied
services:

```text
xray@aurix-canary      -> TCP 18443 (proposed, not yet opened)
hysteria-server@aurix-canary -> UDP 8444 (proposed, not yet opened)
```

This is suitable for bounded correctness only. It is not a canonical REALITY
443 or Hysteria2 443 performance test, and it still competes for sg-a memory.
A dedicated larger node remains the preferred production experiment, but no
purchase or resize is justified before the correctness requirements are
approved and the target is selected.

## 13. Exact canary proposal and approval boundary

The full correctness sequence has not passed. A limited approved connectivity
preflight was later executed and cleaned up; revoke, quota, outage, and restart
cases remain untested. The following is the bounded mutation definition used by
that preflight and any authorized continuation.

### Xray/VLESS REALITY canary

- Host: sg-a, `157.245.63.95`.
- Binary/version: Xray `26.3.27`.
- Isolated service: `xray@aurix-canary.service` using a separate config file.
- Proposed listener: TCP `18443`; current occupied `xray.service` on `8443` is
  untouched.
- Management mechanism: root-owned atomic config generation followed by
  `xray run -test`, then start/restart of only the template instance. No Xray API
  or stats API is currently available, so customer lifecycle and counters would
  be controlled by the temporary test harness, not AuriX production.
- Disposable credentials: `xray-canary-customer-a` and
  `xray-canary-customer-b`, each with a fresh UUID and separate route-bound
  client export. UUIDs will not be printed or persisted in this report.
- Vantage: an explicitly authorized client vantage; the operator Mac used local
  SOCKS proxies without disabling or rerouting its existing VPN. One available
  Myanmar mobile or fixed network remains a separate untested case.
- Limits: 30 minutes, one connection per credential, two credentials maximum,
  100 MiB aggregate uplink+downlink per credential including retries and
  reconnects, 200 MiB total, no sustained load.
- Monitoring: service state/restarts, CPU, RSS, available memory, swap, disk,
  connection logs, client handshake time, DNS result, exit IP, reconnect result,
  and co-hosted AuriX/AI/web service health.
- Stop conditions: available memory below 200 MiB, continuously growing swap,
  any OOM/restart, co-hosted-service failure, p95 latency 50% above the matched
  baseline, or unexpected traffic on the occupied Xray service.
- Rollback: stop and disable only `xray@aurix-canary`, remove its temporary
  config and client exports, remove the temporary firewall rule, run a config
  and listener check, and verify the original `xray.service` is unchanged.

### Hysteria2 canary

- Host: sg-a, `157.245.63.95`.
- Binary/version: Hysteria2 `2.12.2`.
- Isolated service: `hysteria-server@aurix-canary.service` using a separate
  config file.
- Proposed listener: UDP `8444`; current occupied Hysteria2 UDP `443` is
  untouched.
- Management mechanism: separate auth/config file and template service. The
  current Hysteria2 deployment exposes only password authentication, so the
  exact per-customer auth and usage mechanism must be validated before this
  canary is considered meaningful. A single shared password is not acceptable.
- Disposable credentials: `h2-canary-customer-a` and
  `h2-canary-customer-b`, separate only if the installed version's supported
  auth mechanism proves that separation; otherwise the canary is blocked.
- Vantage and limits: same as Xray, with one connection per credential and 100
  MiB maximum per credential.
- Monitoring: UDP handshake/reconnect, DNS/exit behavior, packet loss/jitter,
  MTU symptoms, service state/restarts, CPU/RSS/swap, and co-hosted health.
- Stop/rollback: same resource and service stop gates; stop only the template
  instance, remove temporary auth/config/firewall state, and verify UDP 443 and
  the original Hysteria2 service are unchanged.

### Exact approval requested

The above canaries require production mutations even though they do not restart
the occupied services: create temporary configs/auth, open TCP 18443 and UDP
8444 on sg-a, start two template instances, and transfer at most 200 MiB per
protocol in total. They may consume additional memory on the control-plane host.

The exact Xray scope was subsequently authorized and a limited connectivity
preflight was executed. The full correctness canary remains incomplete; no
Hysteria2 or performance experiment is authorized by this section.

## 14. Backend handoff after validation recheck

### Ready protocol

**Outline is the only protocol currently ready for integration**, with the
documented limitations around sg-b direct data acceptance, BKK memory, and
remote-key reconciliation.

Xray and Hysteria2 are **not ready** for AuriX integration. Their binaries and
listeners are present, but per-customer provisioning, accounting, quota,
revocation, rotation, and persistence have not passed a canary.

### Required adapter semantics

Use the existing `ConnectivityAdapter` contract and registry. Each adapter must
implement, with durable idempotent jobs and read-back verification:

```text
provision
render_managed_config
render_manual_export
apply_quota_cap
read_usage
rotate
revoke_auth
terminate_sessions
probe_management
probe_data_plane
reconcile
```

For Xray, provisioning means one UUID per grant, route-bound client export,
atomic validated config/API update, per-user counters, and explicit behavior for
existing sessions after revoke. For Hysteria2, provisioning is blocked until
the deployed version's supported per-user auth and accounting model is proven;
shared-password access cannot satisfy customer isolation.

Usage must be keyed by server, protocol, credential, generation, and usage
epoch. A server restart, counter reset, management outage, or ambiguous create
must not silently reset quota or create a duplicate grant.

### Acceptance tests required before adapter activation

1. Two isolated grants connect and exit through the assigned server.
2. Revoke one grant while the other remains usable.
3. Record new-connection rejection separately from existing-session termination.
4. Verify upload/download counters for each grant and document reset semantics.
5. Verify small quota exhaustion, expiry, rotation, restart persistence, and
   recovery after management outage.
6. Confirm that all generated client exports contain only the assigned data-plane
   address and never a management relay URL.
7. Reconcile remote state after every test and prove cleanup.
8. Pass a matched Outline comparison, staged concurrency test, and bounded soak
   before changing a route from canary to active.

### Remaining live gates

- Myanmar mobile and fixed-network sessions for sg-a and bkk-a;
- authenticated direct sg-b Outline session from a declared client path;
- approval and execution of the two-customer isolated canaries;
- per-user accounting and quota proof for Xray and Hysteria2;
- resource-safe mixed-protocol and staged-concurrency tests;
- 24–48 hour representative soak without accounting drift or co-hosted-service
  degradation;
- provider invoice, transfer allowance, and overage evidence;
- PostgreSQL cutover/restore and concurrency validation for production control
  state.

Server validation informs the parallel backend task; it does not resolve the
known backend lifecycle, ambiguous-create ownership, accounting, or PostgreSQL
concurrency defects.

## 15. Validation continuation recheck — 2026-09-09 11:52 UTC

This is a fresh recheck. It supersedes earlier point-in-time socket results
where they differ. A later bounded Xray connectivity preflight temporarily
created disposable state and was fully cleaned up; no persistent customer
credential, database, or route was changed.

### Current resource/service observations

| Node | Current observation | Interpretation |
|---|---|---|
| sg-a | 1 vCPU, about 957 MiB RAM; 302 MiB available; 242 MiB swap used; 4.1 GiB free/84% root disk; load 0.41/0.14/0.12 | Existing services are stable at the sample, but the control-plane host remains a poor place for extra protocol load |
| sg-b | 1 vCPU, about 961 MiB RAM; 505 MiB available; 65 MiB swap used; 19 GiB free/19% root disk; load 0.08/0.05/0.01 | More spare headroom in this sample, but not a measured multi-user capacity result |
| bkk-a | 1 vCPU, about 848 MiB RAM; 304 MiB available; 168 MiB swap used; 3.8 GiB free/60% root disk; load 0.17/0.10/0.04 | Outline-only node with memory pressure; no second protocol should be added without a separately approved experiment |

On sg-a, AuriX, AI, Outline, Xray, Hysteria2, both management relays, Docker,
and monitoring were active. On bkk-a, Docker, Shadowbox, and Watchtower were
active. On sg-b, Docker/Shadowbox/Watchtower and Outline were active. The
rechecked original protocol services had zero systemd restart counts at the
observation point. This is not a long-term stability guarantee.

### Administrative access

- sg-a: root access with the operator SSH key works.
- sg-b: the fleet SSH key works when invoked from sg-a; root read-only inspection
  succeeded.
- bkk-a: root access with the approved BKK SSH key works.
- Operator Mac: the Outline desktop client is installed and contains profiles
  for sg-b, sg-a, and BKK, but its visible state currently includes a failed
  connection dialog and no confirmed active session. Clicking `CONNECT` would
  change the Mac VPN route and was not performed.

### sg-b direct-client resolution

The sg-b host-side check now confirms:

- Outline listens on TCP/UDP `45525`;
- sg-b has no listener on public `443`;
- UFW allows `45525/tcp` and `45525/udp` from sg-a only;
- the sg-a sg-b data-relay units are active on TCP/UDP `45525`.

From the operator Mac, the latest raw socket results were:

```text
139.59.122.170:443    timeout
139.59.122.170:45525  timeout
191.40.15.51:443       connect succeeded
```

From sg-a, the corresponding results were:

```text
139.59.122.170:443    refused
139.59.122.170:45525  connect succeeded
191.40.15.51:443       connect succeeded
```

These are TCP socket checks, not authenticated VPN sessions. The current
evidence explains why sg-b customer keys fail from the Mac: the server listener
is healthy, but its firewall and disabled relay prevent the public client path.
The relay path is now enabled as the bounded network-path decision. It remains
an emergency/shared bottleneck until an actual Outline client session confirms
that the generated key path works end to end.

### Exact current protocol testability

#### Xray/VLESS REALITY

- Xray `26.3.27` is running on TCP `8443`.
- The live VLESS inbound has one configured client.
- The current config has no Xray API and no StatsService/statistics section.
- The config passes `xray run -test`; the live binary warns that REALITY on a
  non-443 port may be blocked by some networks.
- An inactive `xray@.service` template exists for a separate config and instance.

The official Xray API supports adding/removing VLESS users through
`HandlerService`, and the official statistics system exposes per-user uplink
and downlink when users have an `email` identity and stats/policy are enabled:
[Xray API](https://xtls.github.io/en/config/api.html) and
[Xray Statistics](https://xtls.github.io/en/config/stats.html). The current
config does not enable those control surfaces. Therefore the occupied Xray
service is testable only for passive listener/config evidence; a two-customer
lifecycle test requires an isolated instance or an approved config mutation.

#### Hysteria2

- Hysteria2 `2.12.2` is running on UDP `443`.
- The live config uses password authentication only.
- An inactive `hysteria-server@.service` template exists for a separate config
  and instance.

The official Hysteria2 configuration supports password, user/password, HTTP,
and command authentication. Its Traffic Stats API can return per-client TX/RX,
online client counts, and kick clients; the `clear` query can reset counters:
[Hysteria2 server authentication and Traffic Stats API](https://hysteria.network/docs/advanced/Full-Server-Config/)
and [Traffic Stats API](https://hysteria.network/docs/advanced/Traffic-Stats-API/).
The current deployment has none of this per-customer control configured. The
occupied UDP/443 service must not be changed for a canary.

### Isolated canary status

The proposed unused ports remain unbound and both template units remain
inactive/disabled:

```text
Xray canary:       sg-a TCP 18443, xray@aurix-canary.service
Hysteria2 canary:  sg-a UDP 8444, hysteria-server@aurix-canary.service
```

This is the least disruptive available topology, but it still consumes sg-a
resources. The Xray canary can be approved as a bounded two-customer test. The
Hysteria2 canary is additionally blocked until its per-customer authentication
and statistics design is selected; a shared password does not meet the
isolation requirement.

### Current capability classification after recheck

| Combination | Classification | Reason |
|---|---|---|
| sg-a / Outline | Usable with documented limitations | API/socket evidence; no current Myanmar-client test |
| sg-b / Outline | Failed for direct Mac client path; management usable | Listener works locally and from sg-a, but public client path is blocked |
| bkk-a / Outline | Usable with documented limitations | Direct data/socket and management evidence; memory constrained |
| sg-a / Xray | Untested for customer use | One manual client, no API/stats, no two-customer isolation evidence |
| sg-a / Hysteria2 | Untested for customer use | Shared password only, no per-customer accounting/lifecycle evidence |
| bkk-a or sg-b / Xray/Hysteria2 | Untested | Not installed and no approved mutation target |

### Exact remaining approval

The next production mutation is limited to the Xray canary described in
Section 13: create two disposable UUID credentials in a separate config, open
TCP `18443` temporarily, start only `xray@aurix-canary.service`, run two
single-connection correctness checks for at most 30 minutes and 100 MiB per
credential, then stop, delete, close, and verify cleanup. It does not restart
or edit the occupied Xray `8443`, Hysteria2 `443`, Outline, Docker, or AuriX
services.

The limited Xray preflight below demonstrated connectivity and observed
per-user counters only. No Xray or Hysteria2 correctness result can be claimed
until lifecycle, quota, outage, restart, and cleanup gates pass. No
performance/load experiment is approved by this recheck.

Do not add more protocol load to sg-a. It is both the control-plane host and a
shared infrastructure bottleneck.

## 16. Follow-up read-only recheck — 2026-09-09 14:19–14:20 UTC

This follow-up used read-only SSH, service, listener, resource, and TCP socket
checks after the bounded Xray preflight was cleaned up. No persistent customer
credential, database, or customer route was changed.

### Current host and deployment state

- **sg-a `157.245.63.95`**: root access still works. Host load was
  `0.30/0.22/0.17`; available memory was about `308 MiB`; swap used was about
  `238 MiB`; root disk remained `84%` used with about `4.1 GiB` free. AuriX,
  Xray, Hysteria2, both management relays, and the Outline listener were
  active; the checked services reported zero restarts. Xray remained on TCP
  `8443`, Hysteria2 on UDP `443`, and Outline on TCP/UDP `45524`.
- **bkk-a `191.40.15.51`**: approved BKK root access still works. Host load was
  `0.03/0.04/0.01`; available memory was about `294 MiB`; swap used was about
  `168 MiB`; root disk remained `60%` used with about `3.8 GiB` free. The
  Outline process was listening on TCP/UDP `443` and management on TCP
  `61603`; the expected systemd unit names were not active because the service
  is running through the deployed container/process path. This is a service-
  naming observation, not evidence of a data-plane outage.
- **sg-b `139.59.122.170`**: direct SSH from the Mac timed out; the available
  jump/key attempts were rejected. The earlier report recorded a successful
  fleet-key inspection from sg-a, but that access is not reproducible in this
  recheck and must remain an unresolved administrative gate until verified
  again. No key replacement or firewall change was attempted.

The live AuriX deployment identity remains `aurix-bot.service` with working
directory `/opt/aurix-current`, resolving to
`/opt/aurix-releases/5789128fcf83ea83113a126bd67a2298164141ed`, launched by
`/opt/aurix-current/.venv/bin/python -u /opt/aurix-current/app.py`. The SQLite
database `/var/lib/aurix-bot/bot.db` was present and had a fresh metadata time
at `14:20 UTC`; this continues to differ from the PostgreSQL-oriented target
architecture.

### Current operator-Mac path checks

These are unauthenticated TCP socket checks, not VPN sessions:

```text
157.245.63.95:45524    connected
157.245.63.95:8443     timeout
139.59.122.170:443     timeout
139.59.122.170:45525   timeout
191.40.15.51:443       connected
```

The result confirms the previously documented distinction: sg-b’s customer
path is not proven from the intended client vantage point, while BKK and sg-a
Outline listeners accept a TCP connection. No authenticated Myanmar client
session was substituted for these checks.

### Canary safety recheck

On sg-a, TCP `18443` and UDP `8444` remained unbound; both
`xray@aurix-canary.service` and `hysteria-server@aurix-canary.service` remained
inactive and disabled. The isolated Xray canary therefore remains the only
smallest proposed mutation. Hysteria2 remains blocked until separate
per-customer authentication and accounting are selected. No performance or
concurrency experiment was run.

## Evidence boundary and consulted sources

Inspected:

- the local AuriX checkout at `/Users/min/projects/tg-AuriX-bot`;
- the deployed AuriX release selected by `aurix-bot.service` on sg-a;
- live read-only host/service/listener/resource checks on sg-a and bkk-a;
- read-only AuriX SQLite state on sg-a;
- the existing Outline fleet baseline dated 2026-09-07;
- the historical Xray/VLESS REALITY and Hysteria2 thread;
- the AuriX architecture/future-backend thread;
- the server-only Xray canary handoff at
  `docs/AURIX_XRAY_CANARY_SERVER_HANDOFF_2026-09-09.md`.

The historical material was treated as context, not current state. In particular,
the earlier assumption that TCP and UDP 443 were free on sg-a is obsolete: the
live host now has Docker/Caddy on TCP 443, Hysteria2 on UDP 443, and Xray on TCP
8443.

## 1. Active ecosystem inventory

The current AuriX database has four endpoint records, three providers, three
regions, and only one registered transport (`outline`). Three nodes are active
operational candidates. The fourth record is the old sg-c node and is explicitly
excluded from capacity.

### Active and excluded nodes

| Node | Verified address | Provider / region | Role | Current protocol state | Decision |
|---|---|---|---|---|---|
| `sg-a` / `primary` | `157.245.63.95` | DigitalOcean / SGP1 | AuriX control plane, Outline, relays, web/AI services, monitoring | Outline 1.12.3 on TCP/UDP 45524; Xray 26.3.27 on TCP 8443; Hysteria2 2.12.2 on UDP 443 | Keep, but do not increase protocol load |
| `sg-b` | `139.59.122.170` | DigitalOcean / SGP1 | Separate Outline data node | Outline 1.12.3, advertised data port 45525; management reached through sg-a relay | Keep Outline-only until its client data path and SSH/admin path are verified |
| `bkk-a` | `191.40.15.51` | Nube Cloud / BKK1 | Separate regional Outline data node | Outline 1.12.3 on TCP/UDP 443 | Keep as the current independent data-plane region; upgrade before adding protocols |
| `sg-c` | historical `139.59.123.125` | DigitalOcean / SGP1 | Retired/destroyed historical node | No usable management or data service observed; registry health `unreachable` | Exclude from active capacity and allocation |

The sg-c row remains stale in the live registry with an enabled-looking record,
but the current health and prior bounded checks show 100% loss/timeouts. It must
be treated as retired, not as an active node. Provider-side deletion was not
repeated in this preparation because that would require a provider mutation.

No additional active VPN node was discovered in the current fleet registry or
deployment records beyond sg-a, sg-b, and bkk-a.

### Host resources and headroom

| Node | Observed resources | Current pressure | Transfer/billing evidence |
|---|---|---|---|
| sg-a | 1 vCPU, about 957 MiB RAM, 25 GiB disk, 3 GiB swap | About 308 MiB available at the latest check, swap in use, root filesystem 83% full | Local configuration estimates a DigitalOcean 1-GB node at `$6/month`; actual transfer allowance and invoice were not available |
| sg-b | Deployment record: 1 vCPU, 1 GiB RAM, 25 GiB disk, Ubuntu 24.04 | Current host resource snapshot unavailable because the approved SSH key is not accepted; management API is reachable through relay | Same DigitalOcean estimate may apply to this size, but exact billing/transfer must be verified in the provider account |
| bkk-a | 1 vCPU, about 848 MiB RAM, 9.8 GiB disk, 1 GiB swap | About 269 MiB available, about 170 MiB swap used, disk 60%; provider dashboard previously peaked above 90% memory | Nube price, included transfer, and overage terms are not recorded in the repository |

The measured resource constraint is memory, not CPU. A 1-GB node is not a safe
place to combine Outline, Xray, Hysteria2, AuriX control services, AI routing,
web hosting, and relays under customer load.

### Customer-facing listeners and persistence

#### sg-a

- Outline data: TCP/UDP `45524`, owned by `outline-ss-serv`.
- Outline management: TCP `61603`, owned by the node management process.
- Hysteria2: UDP `443`, `hysteria` service.
- Xray: TCP `8443`, `xray` service; the Xray configuration uses VLESS/REALITY.
- TCP `443` and `80`: Docker proxy for the website/9Router/Caddy stack, not Xray.
- Local monitoring: Prometheus `127.0.0.1:9090`, Outline metrics
  `127.0.0.1:9092`.
- Management relays: TCP `61604` for sg-b and TCP `61605` for bkk-a.
- `hysteria-server.service`, `xray.service`, the two management relays, AuriX
  bot, AI, Docker, and related containers were active and enabled at inspection.
- UFW was active. Outline data was public; management ports were restricted to
  selected operator/control-plane addresses. The obsolete historical sg-c relay
  firewall rules were removed during the 2026-09-09 remediation.

#### sg-b

- Outline management is reachable through the sg-a TCP relay on `61604`.
- Customer keys observed in AuriX point to `139.59.122.170:45525`, not to the
  sg-a relay address.
- The sg-b data relay units on sg-a are active. The relayed data-plane path has
  not passed a current client-path acceptance test, so sg-b remains
  management-healthy but not approved as a customer data target.
- Direct SSH administration is currently unavailable with the known operator
  key. This prevents safe installation or inspection of additional protocols.

#### bkk-a

- Outline data: TCP/UDP `443` on `191.40.15.51`.
- Outline management: TCP `61603`.
- `shadowbox` and `watchtower` Docker containers were present; Docker was active.
- UFW allowed public data TCP/UDP 443 and restricted management primarily to sg-a
  and selected operator addresses.
- BKK has a direct data path and a direct management path, plus an sg-a
  management relay on `61605`. Those are ingress paths to one Outline server, not
  two separate fleet nodes.

### Provider/region registry

The live AuriX SQLite state contains:

- providers: manual/existing host, DigitalOcean, Nube Cloud;
- regions: unknown/manual, DigitalOcean SGP1, Nube BKK1;
- transports: Outline only;
- endpoints/routes: primary, sg-b, bkk-a, and stale sg-c;
- sg-c server state: `health_status=unreachable`, zero observed remote keys.

The endpoint registry is therefore ready to describe provider and region
diversity, but it cannot yet describe Xray or Hysteria2 as first-class routes.

## 2. Control-plane and data-plane dependency map

```text
Telegram / web portal
          |
          v
      AuriX on sg-a ---- local SQLite state and backups
          |
          +---- Outline management relay :61604 ---- sg-b management API
          |
          +---- Outline management relay :61605 ---- bkk-a management API
          |
          +---- direct/pinned Outline management ---- sg-a Outline API
          |
          +---- co-hosted AI / 9Router / Caddy / website / Prometheus

Customer data plane:
  client ---> sg-a Outline / Xray / Hysteria2
  client ---> sg-b Outline directly (not currently accepted)
  client ---> bkk-a Outline directly
```

### Shared bottlenecks and single points of failure

1. **sg-a is the common control-plane host.** It runs AuriX, management relays,
   web/AI services, Docker, and monitoring. Losing it blocks provisioning,
   revocation, usage polling, route decisions, and the BKK/sg-b management relays.
2. **Management relay dependence is not data-plane independence.** BKK has an
   independent customer data path, but AuriX administration currently depends on
   sg-a for the relay. sg-b has the same management dependence and additionally
   lacks current data-plane acceptance evidence.
3. **Live storage is SQLite.** The running process reports disk storage mode and
   `DATABASE_PATH=bot.db`; no PostgreSQL URL is present in its process
   environment. This is a single-host control-plane and concurrency risk even
   though the repository documents a PostgreSQL production profile.
4. **The control plane does not carry normal VPN traffic.** Existing direct
   Outline sessions should not require Telegram, the bot, or the management API
   after establishment. Provisioning and accounting do require control-plane
   availability.
5. **No independent protocol control agent is enrolled on BKK or sg-b.** Xray
   and Hysteria2 on sg-a are manually configured services, not AuriX-managed
   per-customer adapters.
6. **The historical sg-c rule/record can cause false placement.** The stale
   enabled-looking registry row must be blocked from allocation until retired
   state is persisted and reconciliation proves no live service.

## 3. Existing plans and actual customer activity

### Product records

The live database contains these plan records:

| Plan | Price | Quota | Duration | State |
|---|---:|---:|---:|---|
| Basic | 3,000 MMK | 50 GB | 30 days | Active |
| Standard | 6,000 MMK | 100 GB | 30 days | Active |
| Wallet top-up | 0 MMK | No VPN quota | 1 day | Inactive |

The repository also documents public free entitlements of 300 MiB daily and 3
GiB monthly. These are product quotas, not capacity measurements.

### Live AuriX state, read-only count

- Registered users: **2**.
- Accounts: **2**.
- AuriX key rows: **6**.
- Active entitlements: **2** monthly-trial entitlements.
- Expired daily entitlements: **4**.
- Paid subscriptions: **0**.
- Active endpoint assignments: **2**: one primary and one sg-b.
- Device rows and device sessions: **0**.
- Orders: 6 total: 1 approved, 1 payment-submitted, 3 cancelled, 1 rejected.
- AuriX usage samples: 2,410, all associated with sg-b in the observed window
  from 2026-09-07 through 2026-09-09.

The remote Outline inventory is larger than the AuriX active-customer state:

- primary: 27 remote keys and about 52.4 billion observed transfer bytes;
- sg-b: 8 remote keys and about 373 million observed transfer bytes;
- bkk-a: 7 remote keys and about 29.3 billion observed transfer bytes;
- sg-c: 0 remote keys and unreachable.

These are server-local inventory/counter observations, not customer counts. The
difference from AuriX's six key rows indicates historical, test, orphaned, or
otherwise unreconciled remote credentials. It must not be converted into a
simultaneous-user estimate.

### What cannot currently be claimed

The fleet has no reliable measurement for:

- simultaneously connected customers;
- simultaneously transferring customers;
- sustained aggregate throughput under customer concurrency;
- customer-network handshake success across Myanmar mobile and fixed networks;
- per-user usage for the manual Xray or shared-password Hysteria2 services.

The absence of device/session telemetry is a measurement gap, not evidence of
zero concurrent use.

## 4. Existing performance and connectivity evidence

The durable baseline dated 2026-09-07 provides small, bounded measurements.
They are useful as comparative evidence, not an SLA or multi-user capacity test.

### Control-plane reachability from the declared control-plane vantage point

| Node | Management result | Data result | Interpretation |
|---|---|---|---|
| sg-a | `/server`, `/access-keys`, `/metrics/transfer` returned 200 | TCP 45524 open | Healthy management and socket evidence |
| sg-b | Management API through relay returned 200 | Previous configured data path refused TCP | Management-only until actual client data path passes |
| bkk-a | Direct and relay management paths returned 200 | TCP/UDP 443 open | Usable Outline data path, subject to memory guard |
| sg-c | Timeout | Timeout | Excluded |

The BKK direct and relay management addresses were correctly recognized as two
paths to one server, not two nodes. The operator Mac also connected to BKK and
verified external egress as `191.40.15.51`; this was not a Myanmar-ISP test.

### Bounded host-side throughput evidence

Existing single-stream samples varied materially by neutral test endpoint:

- sg-a earlier download: about 394 Mbps; earlier upload: about 70 Mbps;
- bkk-a earlier download: about 110 Mbps; earlier upload: about 75 Mbps;
- adapted one-run sample: sg-a about 30.7 Mbps download and 438 Mbps upload;
- adapted one-run sample: bkk-a about 33.4 Mbps download and 129 Mbps upload.

The variation is why no peak value is suitable for admission control. A loaded
BKK sample of 100 MB download and 50 MiB upload showed 0% loss and low loaded
ping in that window, but it did not exercise concurrent VPN clients or Myanmar
last-mile networks. sg-b and sg-c were deliberately excluded from loaded tests
because their data-plane states were not healthy.

## 5. Capability classification

| Node/protocol | Connectivity | Customer lifecycle | Accounting/enforcement | Performance suitability | Sustainable capacity | Overall |
|---|---|---|---|---|---|---|
| sg-a / Outline | Verified socket and API; existing operator evidence | AuriX-managed Outline path exists | Native per-key limits/metrics, with AuriX reconciliation | Bounded host evidence only | Control-plane co-hosting and 83% disk make expansion unsafe | Usable with documented limitations |
| sg-b / Outline | Management verified through relay; current data acceptance incomplete | AuriX records exist | Server counters exist; route/data mismatch risk remains | No approved loaded sample | Host resources and direct admin path not verified | Usable with documented limitations; not approved for new customers |
| bkk-a / Outline | Direct data and management evidence verified | Existing Outline lifecycle | Native counters exist; active AuriX assignment coverage is incomplete | Bounded host evidence; not Myanmar-client evidence | Memory-constrained; 10-user/20-device starting guard from baseline | Usable with documented limitations |
| sg-a / Xray VLESS/REALITY | Service/listener and config syntax verified | No AuriX per-customer lifecycle | No proven per-customer quota/revoke/accounting path | No real customer tunnel test in this preparation | Unsafe to add load to sg-a | Untested for commercial use |
| sg-a / Hysteria2 | Service/listener verified on UDP 443 | Shared-password style config, not customer lifecycle | No per-customer accounting/quota proof | No real customer tunnel test in this preparation | Unsafe to add load to sg-a | Untested for commercial use |
| bkk-a / Xray or Hysteria2 | Not installed/verified | None | None | None | Not appropriate before memory upgrade | Untested |
| sg-b / Xray or Hysteria2 | Cannot safely inspect/install until SSH/admin access is fixed | None | None | None | Unknown | Untested |

The Xray and Hysteria2 entries are not failures of the protocols themselves.
They are not commercially ready because connectivity, customer isolation,
revocation, accounting, and quota behavior have not been demonstrated through
AuriX.

## 6. Test matrix and bounded experiments

The existing evidence covers infrastructure paths, not all customer paths. The
following matrix is the minimum representative continuation set.

| Case | Target | Path | Client/network | Purpose | Current status |
|---|---|---|---|---|---|
| O-SGA | sg-a Outline | Direct | Operator Mac plus one approved Myanmar mobile/fixed sample | Baseline handshake, reconnect, DNS/exit, usage | Partial: host/socket evidence only |
| O-SGB | sg-b Outline | Direct client data; relay management only | Operator Mac and Myanmar samples | Prove IP/port isolation and no sg-a bandwidth mixing | Blocked: data path not accepted |
| O-BKK | bkk-a Outline | Direct client data; direct/relay management comparison | Operator Mac plus Myanmar samples | Regional comparison and memory impact | Partial: operator egress and host tests; no Myanmar sample |
| X-SGA | sg-a Xray | Direct TCP 8443 | Representative Xray-compatible clients | Real tunnel, DNS/exit, reconnect, config persistence | Untested |
| H-SGA | sg-a Hysteria2 | Direct UDP 443 | Representative Hysteria2-compatible clients | Real tunnel, UDP behavior, reconnect, MTU/loss | Untested |
| X/H-new | Dedicated larger node | Direct | Same clients/networks as above | Compare protocol value without sg-a contention | Not provisioned; requires approval |

For each candidate protocol, correctness must precede speed:

1. Use disposable test credentials, created only after approval.
2. Create two isolated test customers.
3. Prove both can connect and that each sees the correct target address.
4. Revoke one and verify the other remains usable.
5. Record whether revocation rejects new connections, terminates existing
   sessions, or does both.
6. Test expiry, quota exhaustion, counter reset, rotation, restart persistence,
   and management/control-plane outage behavior.
7. Record whether a reload is disruptive and whether provisioning is atomic.
8. Remove test credentials only after the server inventory and AuriX records both
   confirm cleanup.

No disposable credentials or production load were used in this preparation, so
there is no cleanup action outstanding.

### Proposed experiment limits

Before any mutation, each experiment must name the exact host, protocol, port,
credential IDs, operator/network, and rollback owner. Initial limits should be:

- 30 minutes maximum per correctness test;
- two test credentials maximum;
- one active connection per test credential initially;
- 100 MiB maximum transferred per credential for correctness;
- no public speed-test service and no unbounded upload/download;
- no test during a customer-impacting maintenance window unless explicitly
  approved;
- stop if available memory falls below 200 MiB, swap grows continuously, OOM or
  restart appears, p95 latency increases by 50% over baseline, packet loss
  exceeds 2%, or a co-hosted service becomes unhealthy.

After correctness passes, stage concurrency at 2, then 5, then the expected
operating load, with a final bounded headroom step. A bounded soak should run
24–48 hours at representative load, not as an exhaustion test.

## 7. Sustainable-capacity interpretation

There is not enough evidence for a single supported-user number. The correct
capacity unit is a workload envelope:

```text
active transferring users
× sustained Mbps per user
× protocol CPU/memory profile
× region/client-network success target
× quota/accounting overhead
```

The existing baseline supports only these conservative operating guards:

- bkk-a: start at 10 active users / 20 devices; treat 15 / 30 as a soft ceiling
  until memory and swap remain stable for 24–48 hours; upgrade to at least 2 GB
  before intentionally exceeding that ceiling;
- sg-a: do not add customer protocol load while it remains the control plane with
  about 308 MiB available and an 83% full root filesystem;
- sg-b: no admission decision until data-plane and host-admin evidence is fixed;
- sg-c: zero capacity.

These are operating guards, not Outline product limits and not customer
promises. Registered keys, active subscriptions, connected users, and actively
transferring users must remain separate metrics.

## 8. Economics and relay implications

Known commercial records are 3,000 MMK for 50 GB and 6,000 MMK for 100 GB over
30 days. The local infrastructure guard estimates a 1-GB DigitalOcean node at
`$6/month`; the hard monthly budget variable is blank. The Nube invoice,
included transfer, overage rates, payment fees, exchange rate, and control-plane
costs were not available in the repository or live host state.

Therefore the honest scenarios are formulas, not invented totals:

| Scenario | Known fixed component | Unknown component | Operational meaning |
|---|---:|---|---|
| Low / hold | 2 DigitalOcean 1-GB estimates = `$12/month` | Nube fixed price, transfer overage, control-plane costs | Keep current three-node inventory; no new managed protocol |
| Base / measured pilot | `$12/month` plus existing Nube cost | One dedicated 2-GB protocol node and egress | Add one protocol only after a canary proves customer value |
| High / resilient | Base plus another provider/region and/or BKK upgrade | New-node price, Nube upgrade, all transfer overage | Only justified if Myanmar-client evidence shows a real route/protocol benefit |

Management relays are low-volume control traffic. Relaying customer data through
sg-a is different: it adds a shared bottleneck and may create provider egress on
the relay host. sg-b customer keys must remain direct to sg-b once that data path
is proven. A data relay should be an explicit emergency mode with measured
bandwidth and billing impact, not the default architecture.

The cost-effective strategy is not “more protocols on every server.” It is one
stable Outline route per failure domain, one dedicated protocol canary, and
admission based on measured transfer and margin.

## 9. Recommended topology

```text
                     control plane / management only
                               sg-a
              AuriX + DB + relays + monitoring + legacy canaries
                 /             |                  \
                /              |                   \
      Outline direct     Outline direct       dedicated protocol node
          sg-b              bkk-a              Xray first, H2 second
       DO / SGP1         Nube / BKK1          larger VM / separate FD
```

Recommended policy:

1. Keep sg-a Outline, Xray, and Hysteria2 available only as observed/canary
   services until AuriX lifecycle and resource gates exist.
2. Keep bkk-a as the first non-Singapore customer region, but upgrade memory
   before adding a second protocol there.
3. Fix and accept sg-b as a direct Outline node; do not use sg-a data relays as
   the normal client path.
4. Put the first AuriX-managed Xray adapter on a dedicated larger node, ideally
   in a region/provider whose client path is measured to differ from Singapore.
5. Add Hysteria2 only after per-customer credentials and accounting are proven;
   do not sell the current shared-password configuration.
6. Keep at least 25–30% RAM/CPU headroom and disk reserve on every customer node.

## 10. Backend continuation plan

The deployed AuriX release already has the intended seam:

- `ConnectivityAdapter` in the deployed `ports.py` contract;
- `ConnectivityAdapterRegistry` and `OutlineConnectivityAdapter` in the deployed
  `connectivity_adapters.py`;
- route/endpoint/credential/generation/usage/migration tables;
- fleet health, failover, probe, and allocation modules.

Do not create a second protocol architecture. The local checkout is behind this
deployed release: its local `ports.py` is still primarily an `OutlineGateway`
boundary and its local runtime is still centered on one `OUTLINE_API_URL`. Source
of truth must be resolved before implementation.

### Phase A — reconciliation and production safety

- Identify the exact deployed source commit and merge/recover it into a clean
  implementation branch without overwriting the user's unrelated work.
- Back up the live SQLite state and verify restore; migrate/rehearse PostgreSQL
  on a restored copy before enabling independent workers.
- Reconcile each remote Outline key to an AuriX credential or an explicitly
  reviewed orphan; never auto-delete unknown remote keys.
- Persist sg-c as retired/blocked and remove it from allocation candidates.
- Restore safe sg-b administrative access and verify direct data-plane client
  traffic.

### Phase B — protocol-neutral correctness

Make every adapter satisfy the existing contract for:

```text
provision
render_managed_config
render_manual_export
apply_quota_cap
read_usage
rotate
revoke_auth
terminate_sessions
probe_management
probe_data_plane
reconcile
```

Each operation needs an idempotency key, durable job state, timeout/ambiguous
result handling, read-back verification, and audit evidence. Usage must be
identified by `server_id + protocol + credential_id + generation + usage_epoch`.

### Phase C — Xray adapter

- Use one UUID per customer credential, not the current shared/manual inbound.
- Generate client URI/JSON from the assigned route; assert the public address is
  the assigned server address.
- Manage users through a validated Xray API or a controlled node agent, with
  atomic config writes and validated reloads where an API is insufficient.
- Add user-level statistics and an AuriX quota worker; document whether quota is
  native or agent-enforced.
- Test revoke, rotation, restart persistence, ambiguous-create recovery, and
  management-outage behavior.

### Phase D — Hysteria2 adapter

- Replace the current shared-password assumption with customer-scoped auth.
- Verify the deployed Hysteria2 version's supported authentication and traffic
  accounting mechanism before choosing an agent or separate-instance design.
- Add client YAML/URI rendering, UDP data-plane probes, usage sampling, quota
  enforcement, rotation, and revoke.
- Treat UDP success as network-specific; do not infer it from TCP success.

### Phase E — rollout and failover

- Start with three to five invited testers per selected protocol/region and at
  least two Myanmar network types.
- Publish only routes that pass management, data-plane, isolation, usage, revoke,
  and resource gates.
- Keep failover assisted initially. A third-party client normally needs a new
  config or reconnect; do not promise silent migration.
- Preserve remaining quota by observing and committing source usage before
  destination provisioning. Never clone a full quota onto two nodes.

## 11. Unverified gates and exact next actions

1. Obtain provider console evidence for actual DigitalOcean and Nube fixed price,
   transfer allowance, and overage rates.
2. Verify whether the live AuriX production database is intentionally SQLite or
   was deployed with the wrong storage profile.
3. Keep sg-b SSH/admin access available through the fleet key from sg-a; the
   remaining gate is an authenticated direct-client data-path test and the
   decision between direct client firewall access and a shared relay.
4. Reconcile remote Outline inventories against AuriX credentials, preserving
   unknown keys for review.
5. Run two real Myanmar client/network samples for Outline on sg-a and bkk-a.
6. Review and accept the bounded two-credential Xray experiment already run on
   the isolated sg-a template instance; design durable restart reconciliation
   and existing-session quota semantics before registration. Select and
   validate a per-customer Hysteria2 auth and accounting design before its
   separate canary.
7. Upgrade or replace the 1-GB protocol target before staged concurrency.
8. Run 2/5/expected-load concurrency and 24–48 hour soak tests with memory,
   swap, CPU, p95/p99 latency, packet loss, restarts, usage counters, and
   co-hosted-service health.
9. Only after evidence passes, implement and enable one adapter at a time.

## Paste-ready implementation instruction for “Aurix-vpn”

```text
Continue AuriX multi-protocol preparation from
docs/AURIX_MULTI_PROTOCOL_FLEET_FEASIBILITY_2026-09-09.md.

Do not make backend or server mutations until the read-only gates are reviewed.
Do not expose or print management secret paths, certificate fingerprints,
customer access URLs, tokens, private keys, or customer identifiers.

First reconcile the deployed AuriX release on sg-a with the local checkout. Treat
the deployed ConnectivityAdapter contract, ConnectivityAdapterRegistry,
OutlineConnectivityAdapter, fleet routing, health, failover, usage, and migration
modules as the existing architecture. Do not create a duplicate protocol layer.

Treat these as the only active candidate nodes: sg-a 157.245.63.95 (DigitalOcean
SGP1), sg-b 139.59.122.170 (DigitalOcean SGP1), and bkk-a 191.40.15.51 (Nube
BKK1). Treat historical sg-c 139.59.123.125 as retired/unreachable and block it
from allocation. Keep sg-a as the control plane and do not add customer protocol
load there. Keep BKK's memory guard and do not add a second protocol before an
upgrade. Do not use sg-a data relays as the normal sg-b client path.

Before implementation, verify storage mode, backup/restore, remote Outline key
reconciliation, sg-b SSH/admin access, direct sg-b data connectivity, and actual
provider billing/transfer terms. Separate registered users, active credentials,
connected users, transferring users, and sustained throughput; never infer one
from key count.

Implement only the next smallest slice after approval: a fake-tested
XrayConnectivityAdapter behind the existing registry, with per-customer UUIDs,
route-bound client exports, idempotent provision/rotate/revoke, validated atomic
reload or node-agent control, per-customer usage, quota enforcement, management
and data-plane probes, and reconciliation. Preserve server_id, protocol,
credential_id, generation, and usage_epoch in every usage/migration record.

Do not use the current manual Xray or shared-password Hysteria2 service as a
commercial customer backend. After Xray passes correctness, repeat the same
design review for Hysteria2's supported per-customer authentication and usage
mechanism, then run controlled canaries. No automatic failover or public capacity
promise is allowed until real Myanmar-client, concurrency, soak, accounting, and
 cost evidence passes the documented gates.
```

## 17. Earlier limited Xray preflight result — 2026-09-10

Under the exact canary authorization, a limited SG-A Xray/VLESS REALITY
preflight was executed from `05:12` to `05:15 UTC`, then rolled back. It did
not include revoke, existing-session termination, rotation, quota/expiry,
controller-outage, canary-only restart, or Myanmar-network testing.

Observed results:

- Xray `26.3.27` started as `xray@aurix-canary.service` with the separate
  TCP `18443` listener and loopback-only API/stats endpoint.
- The API read back both disposable customer labels without exposing UUIDs.
- Two local Mac Xray client instances used SOCKS listeners only; the existing
  Mac VPN route was not disabled or changed.
- Both clients reached the owned `aurix-mart.tech` site with HTTP 200 and
  8,520-byte responses. A separate low-volume egress diagnostic reported
  `157.245.63.95` for both. This was not a speed or load test.
- Xray counters after the probes were approximately:

  ```text
  customer-a: uplink 1,797; downlink 28,593; total 30,390 bytes
  customer-b: uplink 1,773; downlink 28,591; total 30,364 bytes
  ```

  These counters demonstrate observation, not quota enforcement.

Cleanup evidence passed: the canary unit is inactive/disabled, its temporary
config is absent, its temporary firewall rule is absent, ports `18443` and
`10085` are unbound, occupied Xray `8443` config validation passes, and Xray,
Hysteria2, Outline, AuriX, Docker, and both management relays remained active.
Temporary local client configs, helper files, UUIDs, and exports were removed.

The earlier preflight was held for reconciliation of the stale SG-B
hostname-edit operation. That hold did not authorize any SG-B mutation. A fresh
read-only attempt with both available DigitalOcean keys timed out on TCP 22, so
the current SG-B hostname/config and whether the older edit committed remain
unverified. The later Xray lifecycle run did not touch SG-B.

## 18. Completed Xray lifecycle result — 2026-09-10

The coordinator subsequently completed the disposable lifecycle sequence on
sg-a and verified cleanup. Xray `26.3.27` ran only as the canary unit on TCP
`18443`, with a loopback-only API/stats endpoint; the occupied Xray service on
`8443` was not restarted or reconfigured. Two disposable VLESS/REALITY users
were read back through the API, connected from local SOCKS clients to the
owned `aurix-mart.tech` destination, and produced distinct non-zero per-user
counters. No existing Mac VPN route was changed.

The sequence proved that removing A blocks new A connections but does not
terminate an established A session; B rotation works for new connections; and
a controller harness can revoke a credential after an observed byte threshold.
It did not prove native hard-quota enforcement. A canary-only restart removed a
runtime API-added user, proving that runtime user state is not persistent in the
current deployment. The backend now has a fail-closed reconciliation boundary
for observed durable generations, but the node-agent/config-writer that must
execute it on a real server is not deployed. Therefore Xray remains
opt-in/unregistered until server-side reconciliation and an explicit
existing-session quota/revoke policy exist.

Hysteria2, Myanmar client networks, management outage, expiry, and
concurrency/throughput/soak tests remain open. Cleanup left the occupied Xray,
Hysteria2, Outline, AuriX, Docker, and relay services healthy, with no customer
state or production deployment changed.

## 19. Fresh read-only fleet snapshot — 2026-09-10

A fresh read-only SSH snapshot was taken after the local node-agent and mixed
protocol contract work. No service, configuration, firewall, credential, or
customer state was changed.

### SG-A

- Root inspection is currently reachable with the existing local operator SSH
  identity.
- `aurix-bot.service`, `aurix-ai.service`, Docker, both management relays,
  Xray, and Hysteria2 are active.
- Available memory was approximately 369 MiB on a 957 MiB host; swap usage was
  approximately 138 MiB. The root filesystem was 87% full with about 3.4 GiB
  available.
- The occupied Xray listener remains TCP `8443`; Hysteria2 remains on UDP
  `443`. No disposable Xray `18443` or Hysteria2 `8444` listener was present.

### BKK-A

- Root inspection is reachable with the existing BKK operator SSH identity.
- The customer service is owned by the running Docker Shadowbox container;
  guessed systemd unit names are inactive and must not be interpreted as an
  Outline outage.
- Available memory was approximately 304 MiB on an 848 MiB host; swap usage
  was approximately 166 MiB. The root filesystem was 61% full with about 3.7
  GiB available.
- The observed public listeners remain Outline TCP/UDP `443` and management
  TCP `61603`; no Xray or Hysteria2 listener was present.

This snapshot reinforces the release decision: BKK-A remains an Outline-only,
memory-constrained region and must not receive a second protocol before a
separately approved capacity experiment or host upgrade. SG-A remains a
control-plane/canary host with no permission to absorb customer protocol load.

## 20. Local provider implementation update — 2026-09-10

The local checkout now contains concrete, offline-tested provider backends
behind the existing node-agent seam. This is an implementation milestone, not
a production activation decision.

### Xray

- `XrayConfigProvider` manages only the explicitly tagged `aurix-managed`
  inbound through atomic config writes and an injected supervised reload.
- `XrayStatsParser` normalizes the documented per-user uplink/downlink counter
  names for the accounting layer.
- Unknown inbounds/users are preserved; reload and malformed-stat failures are
  fail-closed.
- Hard byte quotas and existing-session termination are deliberately not
  advertised. The provider must remain unregistered until the canary's
  controller-enforced quota policy, restart reconciliation, and session policy
  are accepted.

### Hysteria2

- `Hysteria2UserStore` uses keyed digests for authentication and Fernet
  encryption for the customer secret needed for delivery.
- `Hysteria2TrafficStatsClient` is bounded and supports `/traffic`, `/online`,
  and `/kick` with an explicit API authorization header.
- `/kick` is recorded as a request only because a Hysteria2 client can
  reconnect; authentication blocking is the revocation boundary.
- No hard quota method is exposed. The live shared-password UDP/443 service
  remains commercially ineligible until an isolated per-customer auth and
  accounting canary passes.

### Operator visibility and verification

The read-only Control Center now shows Outline as enabled, Xray/Hysteria2 as
evidence-gated candidates, and WireGuard as unimplemented. It does not register
candidate protocols or expose secrets. The latest local verification passes all
331 discovered tests after the reproducible Python environment installed OpenCV;
the earlier 302/303 count is historical.

The implementation commits are `151186f` (provider backends), `46b9b99`
(readiness record), `60a9009` (operator readiness view), `890c165`
(verification refresh), and `e9b978d` (authenticated node-agent integration
coverage). The reproducible Python 3.13 environment now installs the previously
missing OpenCV dependency, and complete local discovery passes with 331 tests.
Remaining release gates are unchanged: approved
canary-only node-agent binding, Hysteria2 isolation, Myanmar client paths,
PostgreSQL concurrency/restore, staged 2/5/expected-load tests, 24–48 hour
soak, cost evidence, and explicit approval before any server mutation.
