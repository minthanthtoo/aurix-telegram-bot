# Outline Fleet Capacity and Connectivity Baseline

- Measurement date: 2026-09-07 (Asia/Rangoon)
- Evidence status: point-in-time operational baseline, not a capacity guarantee
- Scope: `primary`, `sg-b`, `bkk-a`, and `sg-c`
- Secret handling: management URLs, secret paths, certificate fingerprints, access URLs, and credentials are intentionally omitted

Post-baseline update (2026-09-09): `sg-c` was confirmed destroyed and is no
longer an active fleet candidate. The historical measurements below retain the
original `sg-c` row for auditability; allocation and health code must treat it
as retired/absent. The sg-b TCP/UDP relay on sg-a was subsequently enabled on
port `45525`, but still requires an authenticated client-session check.

This is the durable handoff for future AI agents. Read it together with
[`docs/outline-infrastructure-and-device-limits.md`](outline-infrastructure-and-device-limits.md)
and the autoscale runbook before changing allocation or server limits.

## Executive decision

`bkk-a` is reachable and serving Outline API traffic, but it is a 1-vCPU,
1-GB-class VM with memory pressure. CPU and measured network throughput are
not the current constraints; memory headroom is.

Recommended starting policy for Myanmar users on `bkk-a`:

- cap at **10 active users / 20 active devices** initially;
- use **one Outline key per human user**, with an operational limit of two devices per user;
- treat **15 active users / 30 active devices** as a soft ceiling until a 24–48 hour observation confirms stable memory and swap behavior;
- upgrade BKK to at least a 2-GB VM before intentionally exceeding that soft ceiling;
- keep registered-but-idle keys below roughly 20–30 until reconciliation and quota monitoring have been observed under load. This is an operational guard, not an Outline product limit.

The current evidence does not justify approving `sg-c`: it was unreachable from
the control-plane host during all bounded checks. `sg-b` answered its management
API, but its configured data port refused a TCP connection, so it is not
currently a valid client-traffic target without further repair.

These user/device numbers are conservative operating estimates, not measured
Myanmar subscriber capacity. They must be revisited using real per-key usage,
concurrent sessions, 95th-percentile throughput, and a test from a Myanmar ISP.

## Fleet inventory and health

The following host and port values are non-secret operational metadata. API
health checks were made from the control-plane host `157.245.63.95`.

| Endpoint | Provider / zone | Management | Data | Ping from control plane | API | Data TCP | Decision |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `primary` | DigitalOcean / SGP1 | `157.245.63.95:61603` | `45524` | 0% loss, 0.057 ms avg | `/server`, `/access-keys`, `/metrics/transfer` 200 | open | healthy |
| `sg-b` | DigitalOcean / SGP1 | `139.59.122.170:61604` | `443` | 0% loss, 1.228 ms avg | all three checks 200 | refused | management-only until repaired |
| `bkk-a` | Nube / BKK1 | `191.40.15.51:61603` | `443` | 0% loss, 25.571 ms avg | all three checks 200 | open | usable, memory-constrained |
| `sg-c` | DigitalOcean / SGP1 | `139.59.123.125:42628` | `34275` | 100% loss | timeout | timeout | do not allocate |

API response-time samples from the control-plane host were:

| Endpoint | `/server` | `/access-keys` | `/metrics/transfer` | Key count observed |
| --- | ---: | ---: | ---: | ---: |
| `primary` | 12.6 ms | 10.0 ms | 37.7 ms | 28 |
| `sg-b` | 11.0 ms | 10.5 ms | 20.3 ms | 6 |
| `bkk-a` | 85.3 ms | 81.5 ms | 94.0 ms | 2 |
| `sg-c` | timeout | timeout | timeout | unknown |

The BKK management API is therefore working. A slow, isolated TCP connect
sample of about 1.0 s was observed once, while the subsequent HTTP API samples
were about 80–95 ms; do not treat that isolated connect sample as the normal
latency.

### BKK management URL topology

The two management addresses shown for BKK are two ingress paths to the same
Outline Management API, not two Outline servers:

- direct: `191.40.15.51:61603`;
- relay: `157.245.63.95:61605` forwarding to `191.40.15.51:61603`.

The relay service was live during the recheck. Keep only one of these paths as
the canonical `bkk-a` endpoint identity in AuriX. Having both URLs does not
double proxy traffic, transfer accounting, or key usage. Registering them as
two independent fleet endpoints would still be unsafe because the control
plane could poll twice or create/reconcile against the same key store twice.

The relay's purpose is management-plane reachability and firewall separation:
the control plane can reach BKK through the primary host while BKK's direct
management port remains restricted. A private network, VPN, or bastion relay
is the preferred pattern for management access; the access-key data port is a
separate client-traffic path.

## BKK live resource snapshot

### Provider dashboard, supplied by the operator

Last-seven-day dashboard values for `bkk-a`:

| Signal | Now | Average | Maximum |
| --- | ---: | ---: | ---: |
| CPU utilization | 2.46% | 1.53% | 11.62% |
| Memory utilization | 91.06% | 60.79% | 93.27% |
| NIC out | 27.82 KB/s | 0.68 KB/s | 27.82 KB/s |
| NIC in | 34.22 KB/s | 6.12 KB/s | 34.22 KB/s |
| NIC packets out | 67.7 pps | 1.97 pps | 67.7 pps |
| NIC packets in | 171.9 pps | 96.86 pps | 171.9 pps |

The dashboard traffic row supplied with the snapshot showed `44.71 MB` out
and `491.81 MB` in at the current point, with displayed maxima of `51.26 MB`
out and `800.62 MB` in. Its aggregation/window labels are not available here,
so those values are retained as evidence but not converted into a monthly
forecast.

### Host and container checks

- VM: 1 vCPU, approximately 848 MiB visible to the guest, 1 GiB swap.
- Linux `available` memory: approximately 318 MiB.
- Swap in use: approximately 159 MiB.
- Load average: `0.71 0.19 0.05` at the sample.
- Docker `shadowbox`: approximately 100.7 MiB, 0.10% CPU.
- No OOM event was found in the checked kernel logs.
- Shadowbox was running the official `quay.io/outline/shadowbox:stable` image.
- Live Outline state contained two keys on port 443. Key ID 11 had
  `311,948,942` bytes of reported transfer (about 297.3 MiB); key ID 12 had
  no reported transfer in the response.

That earlier two-key snapshot is now superseded by a live BKK inventory
recheck: six keys were present, IDs 11–16, all on port 443, and none had a
per-key data-limit field. The names were `MTH` and five
`AURIX_FREE_UNLIMITED_7DAYS_20260907` keys. This confirms that the current
“unlimited-6” set means six keys without native per-key byte limits; it does
not mean the server has unlimited aggregate capacity.

The combination of low CPU, low measured bandwidth, and high memory use means
adding users is presently a memory-risk decision even though the network is
quiet. A key count alone is not a useful capacity metric.

## Current unlimited-key decision

The six existing no-limit keys are technically usable for a short, controlled
pilot. They are **not approved for unrestricted public use** yet. The limiting
risk is not the duplicate management URL; it is that no per-key byte limit
protects a 1-vCPU/1-GB-class BKK node whose dashboard memory reached 93.27%
and whose host swap already had about 159 MiB in use.

Use the six keys only with trusted testers, a defined seven-day expiry, and
active monitoring until a multi-client concurrency test and Myanmar client
path test pass. For public distribution, use explicit per-key limits first or
upgrade BKK and establish a fair-use/aggregate-transfer control. “Unlimited”
should remain an internal pilot label, not a promise of unlimited server
capacity.

## Latency and bounded internet throughput

These tests were deliberately small and non-disruptive. They are comparative
samples, not an SLA and not a substitute for a concurrent VPN load test.

### Internet ping from BKK

| Target | Packet loss | Average |
| --- | ---: | ---: |
| `1.1.1.1` | 0% | 10.557 ms |
| `8.8.8.8` | 0% | 18.334 ms |
| `157.245.63.95` | 0% | 24.326 ms |
| `139.59.122.170` | 0% | 19.946 ms |

### Earlier unloaded Cloudflare stream samples

| Test location | Download sample | Upload sample |
| --- | ---: | ---: |
| `primary` | 49,236,586 B/s, about 393.9 Mbps | 8,796,143 B/s, about 70.4 Mbps |
| `bkk-a` | 13,759,265 B/s, about 110.1 Mbps | 9,374,589 B/s, about 75.0 Mbps |

These are single-stream baseline samples without synchronized ping capture.

### Loaded-latency test, synchronized with each transfer

New test window: `2026-09-07T13:15:45Z` UTC. On each run, a continuous
`ping -i 0.2 1.1.1.1` started immediately before the transfer and was stopped
immediately after it. The reported ping statistics therefore cover the active
transfer window, rather than a mostly-idle ping series after the transfer
finished.

The download used ten successful 10,000,000-byte Cloudflare chunks (100,000,000
bytes total). The upload used five bounded 10 MiB streams (52,428,800 bytes
total) piped from `/dev/zero`; it was never an unbounded upload. Results:

| Location | Direction | Transfer result | Ping loss | Ping average | Ping maximum |
| --- | --- | ---: | ---: | ---: | ---: |
| `primary` | upload | 52,428,800 bytes, about 227.43 Mbps | 0% | 1.450 ms | 2.593 ms |
| `primary` | download | invalid: 2 chunks were rate-limited | not used | not used | not used |
| `bkk-a` | download | 100,000,000 bytes, about 121.02 Mbps | 0% | 0.464 ms | 0.998 ms |
| `bkk-a` | upload | 52,428,800 bytes, about 94.00 Mbps | 0% | 0.466 ms | 0.656 ms |

The successful BKK run did not show packet loss or material loaded-ping
inflation in this sample. That does not prove a concurrency ceiling: the test
did not exercise many Outline clients, did not represent a Myanmar ISP path,
and used one anycast ICMP target. The result supports the conclusion that BKK
is not currently bandwidth-bound; memory pressure remains the reason for the
conservative user/device cap.

For that initial burst method, the `primary` download was deliberately
excluded after Cloudflare returned rate-limit responses. A failed or
rate-limited speed sample is not converted into a throughput estimate. No
loaded speed sample was approved for `sg-b` or `sg-c` because their
data-plane states were not both healthy.

### Adapted probe verification

The reusable probe at
[`scripts/outline_loaded_latency_probe.py`](../scripts/outline_loaded_latency_probe.py)
now avoids the failed burst pattern:

- downloads one bounded 10 MiB file from `proof.ovh.net` per run;
- uploads exactly 50 MiB to the upload endpoint;
- starts ping before the transfer and stops it immediately afterward;
- calculates average, p95, and maximum from the ping replies inside that
  transfer window;
- emits `valid: false` for non-2xx, partial, or rate-limited transfers;
- supports cooldown and repetitions instead of firing a burst of ten large
  requests.

Verification runs on 2026-09-07 around 13:29 UTC completed successfully on
both hosts. One run per direction produced the following descriptive sample;
it is not a new capacity ceiling because the sample count is small and the
neutral endpoint's route varies:

| Location | Direction | Speed | Loss | Ping average | Ping p95 | Ping maximum | Replies |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `primary` | download | 30.73 Mbps | 0% | 1.246 ms | 1.732 ms | 2.610 ms | 14 |
| `primary` | upload | 438.45 Mbps | 0% | 1.342 ms | 1.915 ms | 2.040 ms | 6 |
| `bkk-a` | download | 33.36 Mbps | 0% | 0.442 ms | 0.544 ms | 0.572 ms | 13 |
| `bkk-a` | upload | 129.45 Mbps | 0% | 1.567 ms | 4.382 ms | 19.700 ms | 17 |

The large variation between this sample and the earlier single-stream
Cloudflare sample is why speed is recorded as evidence, not as a customer
promise. For a capacity decision, run at least three repetitions with a
cooldown and use the median throughput plus p95/p99 loaded latency.

## Historical BKK traffic interpretation

The available evidence proves that BKK has carried real proxy traffic, but it
does not reconstruct every historical byte:

- the VM has been up since approximately 2026-09-02 17:59 UTC, so interface
  counters are cumulative from boot rather than per-key history;
- current persisted Shadowbox state does not contain key-creation timestamps;
- Shadowbox logs retained almost no per-flow history;
- accepted TCP/UDP traffic on the public data port is not fully represented by
  UFW block logs;
- the local machine had an Outline BKK profile connected with on-demand
  behavior, and packet capture showed actual proxy traffic from the operator's
  public IP. This explains current traffic but cannot prove the origin of all
  bytes before the key was created;
- the AuriX control plane polls `/server`, `/access-keys`, and metrics. Those
  management requests explain small port-61603 traffic, not hundreds of MiB on
  the data port.

Therefore, the honest conclusion is: **historical traffic before the presently
observed key state remains unattributed**. Do not claim that all earlier BKK
bytes came from the current key. Preserve future evidence with periodic
per-key metrics snapshots and provider counter samples.

## Capacity policy and next gate

Use these signals for the next decision:

1. Keep BKK at 10 active users / 20 devices until at least 24–48 hours of
   normal operation are recorded.
2. Alert before memory stays above 85%, swap rises continuously, or available
   memory falls below 200 MiB.
3. Upgrade before exceeding 15 active users / 30 devices, or sooner if the
   memory signals trigger.
4. After the upgrade, run a bounded concurrency test with synthetic keys and
   record per-key transfer, CPU, memory, swap, packets per second, and p95
   latency. Remove the synthetic keys after reconciliation.
5. Do not approve `sg-b` for clients until its data port is verified from an
   actual client path. Do not approve `sg-c` until management and data checks
   pass from the control plane.
6. Add a Myanmar-ISP sample before converting this estimate into a customer
   promise. A Singapore/Bangkok server-to-internet speed test cannot represent
   every Myanmar mobile or residential route.

The operational rule “one key per human, up to two devices” is consistent with
the project decision that Outline pools a key's quota across devices; it is a
product policy, not a physical-device enforcement mechanism. Outline's
Management API exposes key inventory, limits, and transfer metrics, while the
key limit is a trailing 30-day byte window. See the
[official Outline API overview](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/README.md)
and [key-limit implementation](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/server/server_access_key.ts).

## Reproduction notes for future agents

Run all measurements from a declared vantage point and keep the output free of
secret paths and access URLs. At minimum collect:

- ICMP ping and TCP connect to each management and data endpoint;
- pinned `GET /server`, `GET /access-keys`, and `GET /metrics/transfer`;
- host `free`, load, swap, Docker resource, OOM, and interface counters;
- one bounded download and upload sample from a neutral endpoint;
- for every accepted speed sample, a continuous ping that begins immediately
  before the transfer and ends immediately after it, with loss, average, max,
  and preferably p95/p99 calculated only over that overlap window;
- a Myanmar-ISP client sample when user capacity is being changed;
- a timestamped per-key metrics snapshot before and after any load test.

The standard host-side probe is:

```sh
python3 scripts/outline_loaded_latency_probe.py \
  --direction both \
  --repetitions 3 \
  --cooldown-seconds 10
```

Run it from a Linux test host with `ping` and `curl`. The neutral download
endpoint is replaceable with `--download-url`; if an endpoint returns 403,
429, a partial body, or another non-success result, preserve the invalid
status and do not calculate a speed from it.

Use [`docs/OUTLINE_CAPACITY_REPORT_TEMPLATE.md`](OUTLINE_CAPACITY_REPORT_TEMPLATE.md)
for the next measurement cycle. Never put a full `apiUrl`, certificate secret
bundle, `accessUrl`, QR code, or token in this report.

## `sg-a` socket recheck from operator Mac (2026-09-08)

The profile named `sg-a-new1` in the Outline client points to the primary
Singapore data endpoint `157.245.63.95:45524`. From the operator Mac, while the
BKK tunnel remained active, the following checks passed:

- ICMP: 4/4 replies, 0% loss, 0.604 ms average;
- TCP data port `45524`: connection succeeded;
- TCP management port `61603`: connection succeeded.

This confirms that the IP and both ports accepted connections at the check
time. It is stronger than a DNS-only check but is not a full Outline client
session or throughput test; the route was not switched to `sg-a` during this
recheck.

## Operator client route confirmation (2026-09-08)

The macOS Outline client was observed connected to the existing `BKK-a-k1`
profile, whose displayed client endpoint was `191.40.15.51:443`. Independent
IPv4 egress checks through `api.ipify.org` and `ifconfig.me` both returned
`191.40.15.51`. This confirms the current operator session exited through
`bkk-a` at the check time. It is not a Myanmar-ISP test, an uptime guarantee,
or evidence of capacity for additional users.
