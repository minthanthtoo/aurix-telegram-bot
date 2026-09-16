# AuriX VPN task recovery — 2026-09-16

This is a reconstructed activity record, not a restoration of deleted Codex
messages in their original positions. It combines retrievable task history,
committed repository documentation/history, and the live checks recorded below.
Treat dated server observations as point-in-time evidence. This recovery did
not rerun the complete test suite.

## Scope and task routing

The user supplied a list of related Codex tasks, including a duplicate. The
conversation selected these as the natural owners:

- `01a04f1c-1fd9-7c01-b7c9-c8db3af2fb6c` — original Shadowbox/Xray/Hysteria2
  port and setup evidence;
- `01a0786a-3855-75c3-8e85-abb2a17c2c27` — fleet/server preflight and protocol
  compatibility evidence;
- `01a06acf-8d7d-7cc0-a2bd-13658e57487b` — AuriX backend implementation.

The task then broadened from selecting a continuation to managing the VPN
roadmap. The user explicitly narrowed scope to VPN and said not to change AI
features. That remains the boundary for future implementation.

## Agreed execution order

1. Establish the actual fleet and preserve the existing Outline service.
2. Test existing servers/protocols before binding backend code to them; include
   `sg-a`, standalone Outline `sg-b`, and independent Nube `bkk-a`.
3. For each protocol, establish per-customer identity/isolation, create/delete
   behavior, counters and reset semantics, quota enforcement, new-connection
   rejection versus existing-session termination, rotation, restart persistence,
   and management-outage recovery.
4. Test real client paths on relevant networks, including Myanmar mobile/fixed
   paths. A speed test alone is not a compatibility or lifecycle test.
5. Only integrate and enable a protocol after its evidence is reviewed. Keep
   Xray/Hysteria2 candidate-only while their live gates fail or remain
   unverified. Load/speed tests follow correctness, use bounded disposable
   accounts, monitor shared-node resources, and precede a representative soak
   and cost review.
6. Treat production database restore/concurrency, client acceptance, rollout,
   and rollback as separate release gates; local tests do not clear them.

The initial fleet assessment and the Xray canary handoff are recorded in
`AURIX_MULTI_PROTOCOL_FLEET_FEASIBILITY_2026-09-09.md` and
`AURIX_XRAY_CANARY_SERVER_HANDOFF_2026-09-09.md`. The earlier recovery record
is `AURIX_RECOVERED_TASK_CONTEXT_2026-09-10.md`.

## Recovered implementation milestones

The backend work was incremental and committed in major slices. By the
retrieved 2026-09-11 checkpoint it included:

- corrected usage-baseline provenance, counter-reset accounting, quota leases,
  and revocation/recovery safety;
- durable accounts, identities, devices, endpoint/protocol profiles,
  credential generations, assignments, and accounting leases;
- protocol-neutral provisioning/recovery/revocation boundaries, with Outline
  retained as the only production-registered transport;
- a VLESS-shaped Xray configuration/provider path and a Hysteria2 auth/stats
  implementation seam, both still gated on live lifecycle/accounting evidence;
- a bounded authenticated node-agent interface, local transport tests, and
  redacted operator Control Center views for fleet, accounts, devices, jobs,
  generations, health, and audit data;
- protocol-aware health evidence, allocation/failover/drain guards, durable
  promotion readiness, and confirmation-bound operator promotion/deactivation;
- account suspension/reactivation and access-delivery gates, without claiming
  that unverified remote sessions have terminated;
- local mixed-protocol contract stress, including a reported maximum run of
  200 Xray plus 200 Hysteria2 disposable grants with 64 workers. This is local
  contract evidence, not a server throughput or production-capacity result.

One live Xray canary report found route connectivity and observable per-user
counters, but runtime-added users did not survive a canary restart and existing
sessions survived removal. That evidence is insufficient for Xray commercial
activation. Hysteria2 per-user isolation/accounting also remains unproven.

The last retrieved roadmap report estimated about 97% local implementation,
65% production readiness, and 83% overall. It reported 343 tests passing at
that checkpoint. Later code/documentation changes exist in the repository, so
that count and percentage are historical; they were not revalidated during
this recovery. The source records local acceptance separately from production
readiness. Representative commits include `5e71b62` (bounded protocol stress),
`e4b3edb` (evidence-backed protocol promotion), `90dbf78` (endpoint-scoped
bootstrap), and subsequent account/profile safety work. Do not infer that
protocols were activated from these local changes.

## Fleet and traffic-path facts

| Node | Role | Management path | Customer data path |
|---|---|---|---|
| `sg-a` / primary | DO control plane, Outline, Xray/Hysteria2 services, relays | `61603` | Outline `45524`; existing other listeners are separate |
| `sg-b` | Standalone DO Outline node | `61604`, restricted to sg-a/relay path | Outline TCP/UDP `45525` |
| `bkk-a` | Independent Nube Outline node | direct `61603` and sg-a relay `61605` | Outline TCP/UDP `443` |

The two DO Cloud Firewalls are separate by design and each protects its own
DO droplet; `bkk-a` is not a DO droplet and uses host UFW. Management-relay URLs
must never be placed into customer keys; a relay for management does not
rewrite or proxy the customer data-plane address. The retired `sg-c` is not an
active allocation target. The detailed policy is in
`AURIX_FIREWALL_FLEET_POLICY_2026-09-16.md`.

## Latest sg-b key investigation — 2026-09-16

This is the work performed after the user said “u check and fix.” It corrects
the earlier hypothesis that sg-b host UFW was blocking its Outline port:

1. The existing sg-b Cloud Firewall already allowed public TCP/UDP `45525` and
   kept management TCP `61604` restricted to sg-a.
2. To reach sg-b safely from sg-a, a temporary Cloud Firewall SSH rule was
   added: TCP `22` from `157.245.63.95/32`. Nested SSH from sg-a to sg-b then
   succeeded.
3. On sg-b, UFW already allowed TCP/UDP `45525` from Anywhere. The Outline
   process was listening on wildcard TCP and UDP `45525`, and the management
   listener was on TCP `61604`; no duplicate UFW allow rule was added.
4. TCP probes from sg-a succeeded to both sg-b public `139.59.122.170:45525`
   and private `10.104.0.2:45525`.
5. A bounded `socket.create_connection` from the operator Mac timed out to
   public sg-b TCP `45525`; the Mac could reach sg-a management `61604` and
   could not reach sg-b management `61604`, as intended.

Conclusion: sg-b's Outline listener and its sg-a path are live, but the
affected direct client/Mac path is still unresolved. No actual Outline client
session or key authentication was verified, so **the keys are not declared
fixed**. Next diagnosis must compare the affected client network with a packet
capture at sg-b and a known-good external network; retain the existing
customer-key data endpoint, and do not expose `61604` or replace its address
with the management relay.

The temporary SSH allow rule is still present. During recovery, the available
Chrome profile was signed out of DigitalOcean and no local `doctl` config or
DigitalOcean tool was available, so the rule could not be removed. The login
tab was left available for operator handoff. After the operator signs in,
remove only the sg-b Cloud Firewall rule `SSH / TCP 22 / source
157.245.63.95/32`, and verify that the pre-existing operator-only SSH rule
remains. Do not broaden access. This security cleanup is the immediate pending
action.

The incorrect stale host-UFW statement in the fleet policy was corrected and
recorded in commit `ae0036f`. `git diff --check` passed before that commit.
Unrelated untracked AI, research, Android, and media artifacts were left
untouched. No AI feature files were changed by this recovery.
