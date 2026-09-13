# Outline Fleet Capacity and Connectivity Report Template

Copy this file for each measurement cycle. Replace every `TBD` with an
observed value or explicitly write `not measured`. Do not paste management
secret paths, certificate bundles, access URLs, QR codes, bot tokens, or
provider credentials into the report.

## Report metadata

- Report date and timezone: `TBD`
- Measurement window: `TBD`
- Operator / agent: `TBD`
- Control-plane vantage point: `TBD`
- Client vantage point(s), including Myanmar ISP if available: `TBD`
- Deployment revision / image versions: `TBD`
- Compared with previous report: `TBD`

## Decision summary

- Servers approved for allocation: `TBD`
- Servers blocked and why: `TBD`
- Recommended active users per server: `TBD`
- Recommended devices per user: `TBD`
- Registered-key ceiling: `TBD`
- Upgrade trigger: `TBD`
- Confidence: `low | medium | high` — explain: `TBD`

## Fleet inventory

Record only non-secret host, provider, zone, and port metadata.

| Endpoint | Provider / zone | Management host:port | Data port(s) | API status | Data status | Allocation decision |
| --- | --- | --- | --- | --- | --- | --- |
| `primary` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-b` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-c` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `new-endpoint` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Control-plane checks

| Endpoint | `/server` status / p50 | `/access-keys` status / p50 | `/metrics/transfer` status / p50 | Key count | Pin verified |
| --- | --- | --- | --- | ---: | --- |
| `primary` | `TBD` | `TBD` | `TBD` | `TBD` | `yes/no` |
| `sg-b` | `TBD` | `TBD` | `TBD` | `TBD` | `yes/no` |
| `bkk-a` | `TBD` | `TBD` | `TBD` | `TBD` | `yes/no` |
| `sg-c` | `TBD` | `TBD` | `TBD` | `TBD` | `yes/no` |
| `new-endpoint` | `TBD` | `TBD` | `TBD` | `TBD` | `yes/no` |

## Network probes

### From the control plane

| Endpoint | Ping loss / avg | Management TCP | Data TCP/TCP+UDP | Notes |
| --- | --- | --- | --- | --- |
| `primary` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-b` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-c` | `TBD` | `TBD` | `TBD` | `TBD` |
| `new-endpoint` | `TBD` | `TBD` | `TBD` | `TBD` |

### From the client path

- Client country / ISP / access type: `TBD`
- VPN client and protocol: `TBD`
- Ping targets and results: `TBD`
- DNS resolution behavior: `TBD`
- MTU / fragmentation observations: `TBD`
- Failure or retry observations: `TBD`

## Throughput probes

State the endpoint, URL/provider, stream count, byte bound, and exact start/end
times. Single-stream results must not be presented as multi-user capacity. A
speed result is incomplete unless ping was sampled during the active transfer.
Prefer the repository probe
[`scripts/outline_loaded_latency_probe.py`](../scripts/outline_loaded_latency_probe.py),
which uses one bounded neutral download per run, bounded upload bytes, and a
cooldown between repetitions. Do not use a burst of large Cloudflare requests.

| Location | Download bytes / Mbps | Upload bytes / Mbps | Streams | Endpoint / provider | Caveats |
| --- | ---: | ---: | ---: | --- | --- |
| `primary` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `Myanmar client` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

### Loaded-latency evidence

Start continuous ping immediately before each transfer and stop it immediately
after the transfer ends. Report statistics only from that overlap window. Do
not let the ping process continue into an idle period and then call the result
“ping under load.” For repeatable evidence, retain the raw reply samples in a
private operator artifact while keeping this report free of secrets.

| Location | Direction | Target | Payload / streams | Transfer success | Loss | Avg | p95 | Max | Start/end UTC |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `primary` | download | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `primary` | upload | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-b` | download | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-b` | upload | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | download | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | upload | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-c` | download | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-c` | upload | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

Loaded-latency method checklist:

- [ ] Ping starts before the transfer and stops after it, with no idle tail.
- [ ] Download payload is bounded; if the endpoint rejects a large request,
      use repeated smaller chunks and record the chunk count.
- [ ] Upload payload is bounded; never pipe an unbounded `/dev/zero` stream.
- [ ] Failed, partial, or rate-limited transfers are marked invalid rather
      than converted into a speed number.
- [ ] At least three repetitions are planned before changing a capacity cap.
- [ ] Results include loss, average, p95, max, and the target's anycast/route
      caveat.
- [ ] The repository probe or an equivalent method records exact payload size,
      HTTP status, transfer duration, and whether the result is valid.
- [ ] The neutral endpoint was checked from the actual test host before the
      measurement window; endpoint rate limits are recorded as invalid results.

## Resource and traffic evidence

| Endpoint | vCPU / RAM | CPU p95/max | Memory p95/max | Swap | Load | NIC in/out | Packet rate | OOM / errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `primary` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-b` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `bkk-a` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |
| `sg-c` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

Capture both provider counters and host counters. Record the counter start
time; a cumulative interface counter is not a per-key or per-user total.

## Per-key and concurrency evidence

- Snapshot timestamp: `TBD`
- Active key count: `TBD`
- Active user count: `TBD`
- Active device count: `TBD`
- Top key transfer values, by key ID only: `TBD`
- Test duration: `TBD`
- Concurrent synthetic clients: `TBD`
- p50 / p95 / p99 latency: `TBD`
- Aggregate throughput: `TBD`
- Per-key throughput distribution: `TBD`
- Reconciliation result and test-key cleanup: `TBD`

## Capacity decision

Explain the decision from measured constraints, not key count alone.

- Current bottleneck: `CPU | memory | swap | bandwidth | packet rate | upstream route | unknown`
- Recommended active users: `TBD`
- Recommended devices per user: `TBD`
- Recommended registered-key ceiling: `TBD`
- Hard stop / upgrade trigger: `TBD`
- Evidence supporting the number: `TBD`
- What was not measured: `TBD`
- Confidence and reason: `TBD`

Default operational guard for an unproven 1-vCPU/1-GB-class Outline node:

- start at 10 active users / 20 devices;
- review at 24–48 hours;
- upgrade before 15 active users / 30 devices if memory or swap is stressed;
- do not describe this guard as an Outline-imposed device limit.

## Historical traffic and anomaly review

- Server boot time: `TBD`
- Existing key creation times available: `yes/no`
- Existing key usage history available: `yes/no`
- Management polling observed: `TBD`
- Client VPN/on-demand state observed: `TBD`
- Firewall blocks versus accepted data traffic: `TBD`
- Traffic that remains unattributed: `TBD`
- Follow-up instrumentation required: `TBD`

## Evidence and safety checklist

- [ ] Secrets removed from report and command output.
- [ ] Management TLS certificate pin verified.
- [ ] API and data-plane tests came from a named vantage point.
- [ ] No destructive or load-generating test ran without a bounded scope.
- [ ] Synthetic keys, if any, were removed and reconciled.
- [ ] Provider and host counter windows are timestamped.
- [ ] Myanmar client-path evidence is separated from server-to-internet speed tests.
- [ ] Unreachable or partially healthy servers are excluded from allocation.
- [ ] Report linked from the repository README or runbook.
