# AuriX Xray Canary — Server Handoff

Date: 2026-09-10 06:00 UTC (superseding the earlier 2026-09-09 snapshot)

Scope: server-side validation and a bounded Xray/VLESS REALITY correctness
canary only. No occupied production service, customer credential, backend
deployment, or database was changed. The earlier limited preflight was
followed by a completed disposable lifecycle sequence on sg-a; Hysteria2,
Myanmar-network, load, and speed tests remain separate open gates.

## Latest bounded Xray result — 2026-09-10

The isolated canary completed its lifecycle checks and was then cleaned up.
Xray `26.3.27` ran only as `xray@aurix-canary.service` on TCP `18443`, with
loopback API/stats on `127.0.0.1:10085`; the occupied `xray.service` on `8443`
was not restarted or reconfigured. Two disposable customer labels were added
and read back through the API, and two local Mac SOCKS clients reached the
owned `aurix-mart.tech` destination with HTTP 200 and 8,520-byte responses.
The two users produced distinct non-zero per-user counters (coordinator
sample: A `592/12,774` and B `592/12,752` uplink/downlink bytes), proving
customer-scoped observation for this bounded path. This was not a load or
speed test and did not change the existing Mac VPN route.

The lifecycle findings are:

1. Removing A blocked new A connections while an already-established A
   transfer completed; B remained usable. Revoke therefore does not terminate
   existing sessions on this control path.
2. Rotating B to a new disposable generation worked. The new generation
   connected, the old generation failed new connections after removal, and the
   new generation remained usable.
3. A controller harness observed B2 above a 10,000-byte threshold, revoked it,
   and verified that a subsequent new connection failed. This demonstrates a
   controller-enforced revoke action, not native Xray hard-quota enforcement;
   overshoot and existing-session behavior still require product policy.
4. A canary-only Xray restart discarded a runtime API-added disposable user
   (read-back count `1` before restart and `0` after). Runtime API state is not
   durable in this deployment, so production use requires a durable config or
   node-agent reconciliation step before registration.

Hysteria2, management-outage behavior, expiry, Myanmar client networks, and
bounded concurrency/throughput/soak tests were not run. The canary is therefore
useful correctness evidence but not a production-readiness pass.

Cleanup was verified: the canary unit is inactive/disabled, its config and
runtime state are absent, the temporary firewall rule is absent, ports `18443`
and `10085` are free, occupied Xray `8443` remains bound and config-valid, and
Hysteria2, Outline, AuriX, Docker, and the management relays remained active.

## Current evidence

### Hosts in scope

| Host | Provider / region | Current server role | Read-only result |
|---|---|---|---|
| sg-a `157.245.63.95` | DigitalOcean / SGP1 | AuriX control plane, Outline, Xray, Hysteria2, management relays, co-hosted services | Root access works; Xray and Hysteria2 are active; this is the only isolated canary target currently prepared |
| sg-b `139.59.122.170` | DigitalOcean / SGP1 | Separate Outline data node | Direct Mac SSH timed out and available jump/key attempts were rejected in the latest check; earlier fleet-key access is historical and not currently reproducible |
| bkk-a `191.40.15.51` | Nube Cloud / BKK1 | Separate Outline data node | Approved root access works; Outline TCP/UDP 443 and management 61603 are reachable; no Xray installation found |

Fresh resource observations are timestamped evidence, not capacity claims:

- sg-a: 1 vCPU, about 957 MiB RAM, 308 MiB available, 238 MiB swap used,
  4.1 GiB root free, 84% used, load `0.30/0.22/0.17`.
- sg-b: latest documented observation remains 1 vCPU, about 961 MiB RAM,
  505 MiB available, 65 MiB swap used, 19 GiB root free, load
  `0.08/0.05/0.01`.
- bkk-a: 1 vCPU, about 848 MiB RAM, 294 MiB available, 168 MiB swap used,
  3.8 GiB root free, 60% used, load `0.03/0.04/0.01`.

No VM size, user ceiling, purchase, or resize is inferred from these samples.

### Deployed AuriX release

On sg-a, `aurix-bot.service` runs:

```text
/opt/aurix-current/.venv/bin/python -u /opt/aurix-current/app.py
```

`/opt/aurix-current` resolves to release
`/opt/aurix-releases/5789128fcf83ea83113a126bd67a2298164141ed`.
The live state remains disk/SQLite-oriented at
`/var/lib/aurix-bot/bot.db`; this does not match the PostgreSQL target
architecture. The handoff does not modify that state.

## Installed Xray capability check

The occupied service is:

```text
Binary:       /usr/local/bin/xray
Version:      26.3.27
Commit:       d2758a0
Config:       /usr/local/etc/xray/config.json
Unit:         xray.service
Listener:     VLESS/TCP/REALITY on 8443
```

Read-only config inspection found:

```text
api:          absent
stats:        absent
policy:       absent
VLESS clients with email identity: 1
Config test:  Configuration OK
```

The installed binary exposes runtime API commands including add/remove users,
statistics queries, and online-user queries. The live service cannot use these
controls because its API and routing service are not configured. The official
Xray documentation describes [HandlerService](https://xtls.github.io/en/config/api.html)
and [per-user statistics](https://xtls.github.io/en/config/stats.html).

Consequences:

1. Per-customer counters are not currently enabled on the occupied service.
2. The current service has no demonstrated per-customer revoke or rotation
   path.
3. Xray statistics are counters; no native customer byte-quota enforcement was
   found in the deployed configuration. A controller/harness must measure a
   quota and explicitly block or revoke when the threshold is reached. Any
   overshoot and counter-reset behavior must be measured.
4. Runtime API changes must not be assumed to survive a process restart. The
   isolated test must read back users after a canary-only restart. If runtime
   users disappear, persistence requires a durable config/agent reconciliation
   step.
5. Removing a user and terminating an existing session are separate acceptance
   questions. The canary must test both instead of assuming that revoke kills an
   established flow.

## Limited authorized execution — 2026-09-10 05:12–05:15 UTC

The approved canary was started only after a clean preflight of the canary path,
ports, unit mapping, and firewall state. The following limited checks completed
before execution was paused for the sg-b reconciliation gate:

- The temporary config passed the remote Xray `run -test` check. The first
  start attempt failed because the template runs as `nobody` while the file was
  `root:root 600`; this was corrected to `root:nogroup 640`. No occupied
  service was changed.
- The canary unit ran as `xray@aurix-canary.service` with TCP `18443` and
  loopback API `127.0.0.1:10085`. API read-back showed both labels
  `xray-canary-customer-a` and `xray-canary-customer-b`.
- The only temporary public rule allowed TCP `18443` from the operator Mac's
  observed address `45.41.106.152`; it was removed during cleanup.
- Two local Xray client instances used SOCKS listeners `127.0.0.1:10990` and
  `127.0.0.1:10991`. This did not disable or reroute the user's existing Mac
  VPN.
- Both clients received HTTP 200 from the owned `aurix-mart.tech` site with
  an 8,520-byte response. A separate low-volume egress diagnostic to
  `api.ipify.org` reported `157.245.63.95` for both clients; this was not a
  load test.
- The first per-user statistics snapshot after these probes showed:

  ```text
  customer-a: uplink 1,797 bytes; downlink 28,593 bytes; total 30,390 bytes
  customer-b: uplink 1,773 bytes; downlink 28,591 bytes; total 30,364 bytes
  ```

  These are Xray counter readings for the observed sample, not quota results.

This task's limited preflight did not proceed to lifecycle checks. A subsequent
coordinator-owned continuation completed the approved lifecycle sequence without
touching SG-B or occupied services. Its results were:

- Both disposable users connected and produced distinct per-user counters.
- Removing customer A blocked new A connections but did not terminate an
  established A session.
- B rotation worked: the new generation connected while the old generation was
  rejected for new connections.
- A small controller threshold revoked B2 sufficiently to block new B2
  connections. This is controller-enforced behavior, not native Xray quota
  enforcement.
- A canary-only restart showed that users added only through the runtime API do
  not persist across restart. Durable config/agent reconciliation is required.

Controller-outage behavior, exact quota overshoot, expiry semantics, and
Myanmar client-network compatibility were not tested. No Myanmar client
network was available, so that case remains untested.

### Cleanup evidence

All temporary state was removed after the limited preflight:

1. `xray@aurix-canary.service` is inactive and disabled.
2. `/usr/local/etc/xray/aurix-canary.json` is absent; local client configs and
   test helper files were removed.
3. TCP `18443` firewall rule is absent and ports `18443`/`10085` are unbound.
4. Occupied `xray.service` remains active on `8443`; its original config passes
   `run -test`.
5. Hysteria2 UDP `443`, Outline TCP/UDP `45524`, AuriX, Docker, both management
   relays, and the checked co-hosted services remain active; checked restart
   counts stayed at zero.
6. No canary credential or client export was persisted in the repository or
   report.

The canary is therefore a **partial lifecycle result**: connectivity, distinct
counters, new-session revoke, rotation, controller-threshold blocking, and
non-persistence across restart were observed. Strict quota enforcement,
existing-session termination, controller-outage behavior, expiry, and Myanmar
compatibility remain open gates. No SG-B mutation was initiated or left pending
by this task; the latest Mac checks with both available DigitalOcean keys timed
out on TCP 22. The separate coordinator continuation also left SG-B untouched.

## Exact isolated Xray canary

### Target and management

- Host: sg-a `157.245.63.95`.
- Binary: Xray `26.3.27`.
- Service: `xray@aurix-canary.service` only.
- Separate config: `/usr/local/etc/xray/aurix-canary.json`, a temporary
  root-owned file that must not overwrite an existing file. Before mutation,
  `systemctl cat xray@.service` and the instance's resolved `ExecStart` must be
  read back to prove that `xray@aurix-canary.service` uses this path. The
  template unit itself must not be edited.
- Data listener: TCP `18443` with VLESS/REALITY.
- Local management: loopback-only Xray API at `127.0.0.1:10085` with
  HandlerService and StatsService, driven by a temporary root-owned test
  harness. Port `10085` must be verified unbound immediately before setup; the
  API must not be exposed publicly.
- Current occupied services remain unchanged: Xray `8443`, Hysteria2 UDP
  `443`, Outline, AuriX, Docker, and management relays.

Before writing the config or adding a firewall rule, snapshot the existence,
metadata, and hashes of the proposed config path and the relevant firewall
state. If the config path, unit mapping, API port, data port, or rule state is
already occupied or differs from the expected pre-state, abort rather than
overwrite or repair it.

### Disposable customers

Use labels only in logs and this handoff:

- `xray-canary-customer-a`
- `xray-canary-customer-b`

Each receives a newly generated UUID, a unique Xray `email` identity, and a
route-bound client export containing only sg-a's canary endpoint. UUIDs and
client exports must not be printed, committed, or retained after cleanup.

### Client vantage and limits

1. An explicitly authorized client vantage. Do not disable or re-route the
   user's existing Mac VPN without separate consent. If a clean Mac path is not
   available, use a separate authorized device/network or mark the Mac case
   untested.
2. One available Myanmar mobile or fixed network, if available. If unavailable,
   mark the Myanmar case untested; do not substitute a server-to-server path.
3. Maximum duration: 30 minutes.
4. Maximum concurrency: one connection per customer, two total.
5. Maximum transfer: 100 MiB per customer and 200 MiB total, where each limit
   counts customer uplink plus downlink, all reconnects, retries, probes, and
   watchdog traffic. An independent external watchdog must enforce the byte
   and wall-clock ceilings; the quota controller is not trusted as the safety
   boundary. Stop at 90 MiB/customer and 190 MiB total to leave accounting
   margin, with a hard process-level stop at 100/200 MiB.
6. Each individual probe or transfer has a 30-second timeout and a 5 MiB
   per-operation cap. No sustained load, public speed-test traffic, or
   concurrent Hysteria2 test.

### Correctness sequence

The test harness should record an initial stats snapshot, then:

1. Provision A and B through the local Xray API and verify both users by
   read-back; assert idempotent repeat-provision does not duplicate users.
2. Connect both clients. Verify handshake, DNS resolution, expected exit IP,
   and a controlled authorized destination.
3. Record per-user uplink/downlink deltas and connection-establishment times.
4. Revoke A through the API. Test A's new connection separately from A's
   existing connection. Confirm B remains connected and can reconnect.
5. If an existing A session survives removal, record that revoke blocks new
   sessions but does not terminate existing sessions. Test explicit session
   termination only if the installed control path supports it; otherwise mark
   the capability absent.
6. Rotate B by adding a new generation, verifying the new export, then removing
   the old generation. Confirm old B fails to establish a new connection and
   the new B remains isolated.
7. Apply a small harness-enforced quota below the 100 MiB ceiling. Record the
   observed counter delta, enforcement delay, overshoot, and whether existing
   sessions continue. Do not claim native Xray quota enforcement. If an
   established session survives controller/API loss, classify the quota as
   best-effort for new connections and explicitly not strict fail-closed for
   existing sessions.
8. Test expiry and recovery using disposable state. Record whether expiry is
   enforced by Xray, the harness, or both.
9. Make the local management harness/API unavailable without changing the
   occupied service. Record whether existing sessions continue and whether new
   provisioning/revoke operations fail closed.
10. Restart only `xray@aurix-canary.service` if all earlier checks are clean.
    Verify service health, user read-back, reconnect behavior, stats continuity,
    and occupied `xray.service` health. This is the only restart in scope.

The management-outage and quota checks must not rely on the quota controller to
stop traffic safely. The independent watchdog remains authoritative for the
30-minute and 200 MiB ceilings.

### Monitoring and stop conditions

Record before, during, and after the canary:

- canary and occupied service state, restart counters, and logs;
- CPU, RSS, available memory, swap, disk, and listener state;
- per-customer uplink/downlink counters and counter deltas;
- handshake time, DNS/exit result, reconnects, errors, and session state;
- AuriX, AI, web, Outline, Hysteria2, relay, and Docker health.

Stop immediately if any of these occurs:

- available memory below 200 MiB or continuously increasing swap;
- OOM, unexpected restart, listener collision, or config-test failure;
- any degradation or unexpected traffic on occupied services;
- co-hosted service failure;
- credential isolation or accounting result cannot be unambiguously observed;
- the traffic ceiling, duration, or connection limit would be exceeded.

### Rollback and cleanup proof

After either completion or a stop condition:

1. Stop and disable only `xray@aurix-canary.service`.
2. Remove the temporary config, harness state, firewall rule, UUIDs, and client
   exports.
3. Verify TCP `18443` is unbound and the canary unit is inactive/disabled.
4. Re-run the occupied Xray config test and verify `xray.service` remains active
   on `8443` with its pre-test config identity.
5. Verify Hysteria2 UDP `443`, Outline, AuriX, Docker, and relay services remain
   healthy.
6. Reconcile that no canary customer or credential remains in test state.

Cleanup is not complete until all six checks are recorded.

## Hysteria2 follow-up boundary

Hysteria2 `2.12.2` is active on sg-a UDP `443` with password authentication
only. A follow-up isolated target could use
`hysteria-server@aurix-canary.service` on UDP `8444`, but it must use separate
per-customer authentication and a verified accounting mechanism. A shared
password cannot prove customer isolation. Do not run Hysteria2 concurrently
with the Xray canary.

The official Hysteria2 documentation exposes password/userpass/HTTP/command
authentication and a Traffic Stats API, but the live deployment has not enabled
per-customer control. Hysteria2 therefore remains unready for integration and
has no approved execution step in this handoff.

## Approval / continuation boundary

Approve exactly one production mutation:

> On sg-a `157.245.63.95`, create a separate Xray canary config with local
> API/stats controls at `127.0.0.1:10085` and two disposable customers,
> temporarily allow TCP `18443`, start only `xray@aurix-canary.service`, run
> the two-customer correctness sequence for at most 30 minutes, one connection
> and 100 MiB aggregate uplink+downlink per customer including retries, then
> stop, delete, close, and verify cleanup. Preserve pre-existing files and
> firewall rules; abort on any path/port/rule conflict. Do not modify or
> restart occupied services, disable the user's VPN, or run Hysteria2/load
> tests concurrently.

The exact canary scope was authorized and its limited preflight was executed,
but continuation is blocked by the sg-b reconciliation gate above. Once that
read-only state is cleanly resolved, continuation may use the same approved
scope; do not expand it to Hysteria2, load, speed, spending, resizing, backend
change, customer migration, or a public capacity claim.

## Server-side readiness

- Outline: usable with documented sg-b direct-path and reconciliation limits.
- Xray/VLESS REALITY: not ready for commercial integration. Runtime API
  lifecycle works for new sessions, but existing sessions survive removal and
  runtime-added users are not restart-persistent; strict quota and outage
  behavior remain unproven.
- Hysteria2: blocked pending per-customer auth/accounting design.
- sg-b: direct client data path and reproducible admin access remain open gates.
- Myanmar networks/platforms: untested in this server-only handoff.
