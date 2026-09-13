# Recovered task context — 2026-09-10

This is a reconstructed handoff, not an original transcript or restoration of
the app's message database. Sources: owner completion messages retained in
coordinator task 01a085a8-b3be-7901-8723-31ec8487e53d, independent test results
there, and surviving workspace reports. Task retrieval currently exposes older
interrupted turns instead of these newer exchanges; the cause is unverified.

## Aurix-vpn

Task: 01a06acf-8d7d-7cc0-a2bd-13658e57487b.

Current implementation belongs to /Users/min/projects/tg-AuriX-bot, portal
branch baseline 77c5266. Do not confuse it with the independent dirty production
reference worktree /Users/min/projects/tg-AuriX-bot-production (baseline f16c9e7).
Do not reset, overwrite, blindly merge, or reimplement surviving files.

Surviving report: docs/AURIX_BACKEND_INTEGRATION_2026-09-09.md.
Key files include aurix_vpn/identity.py, connectivity_adapters.py,
route_failover.py, commerce_service.py, commerce_worker.py, entitlements.py,
runtime.py, migrations.py and regression tests.

Latest owner-reported corrections:

- Commerce migration 6 records explicit usage baseline provenance and bytes.
- Newly owned credentials start at zero. Readings 400,500,50 after counter
  reset credit 400,100,50, total 550.
- Migrated credentials with explicitly accounted baseline 400 credit 0,100,50,
  total 150. Unknown provenance stops accounting processing, NOT remote traffic.
- Local lease TTL does not release reservations without remote revocation proof.
- Portable CASE arithmetic replaces scalar MIN in lease usage updates.
- Fake-PG full record_usage coverage verifies source locking before balance read;
  this is not real PostgreSQL concurrency evidence.
- Aggregate quota enforcement returns/counts queued revoke jobs correctly.

Latest owner reports: 77 focused and 252 broader non-pay_monitor tests passed;
Ruff, py_compile and git diff --check passed. Coordinator independently ran 72
tests across identity, route_failover, connectivity_adapters, commerce,
migrations and MVP after the final correction: all passed. These are historical
validation results, not a fresh validation of later workspace edits.

Full test discovery remains blocked by missing cv2 for pay_monitor. Worker-level
fault/recovery coverage and live PostgreSQL concurrency remain acceptance gaps.
No Xray/Hysteria2 production readiness or deployment is established.

## Explore server inventory

Task: 01a0786a-3855-75c3-8e85-abb2a17c2c27.

Surviving reports:
- docs/AURIX_XRAY_CANARY_SERVER_HANDOFF_2026-09-09.md
- docs/AURIX_MULTI_PROTOCOL_FLEET_FEASIBILITY_2026-09-09.md

Prepared, NOT executed: isolated sg-a Xray canary TCP18443, two disposable
customers, at most 30 minutes and 200 MiB combined bidirectional traffic including
retries. No occupied-service restart or modification. Loopback-only management,
independent traffic bounds, stop conditions and verified cleanup required.
Explicit user canary approval is still pending in the coordinator conversation.

sg-a cohosts critical services; protect headroom. bkk-a remains in fleet
compatibility/capacity scope, not implicitly authorized for new installation.
Latest sg-b admin access failure supersedes earlier success; direct client path
is unresolved. Myanmar client-network evidence remains separate and untested.

Xray counters, revoke of existing vs new sessions, quota overshoot, controller
outage, rotation and restart persistence must be demonstrated. Hysteria2 needs
separate per-customer auth/accounting validation. Mixed-protocol stress/speed
tests follow correctness with separate bounded scope approval. No spending,
resizing, migration, live deployment or load testing is authorized here.

## Resume safely

Read this handoff and the owner-specific report, inspect surviving files, and
reconcile newer evidence before doing work. Do not replay historical remote
commands or treat older interrupted turns as current instructions. Recovery
alone authorizes no new implementation or live changes. Return a short
acknowledgment with any discrepancy and await the coordinator's next assignment.
