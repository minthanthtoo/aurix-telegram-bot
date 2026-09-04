# AuriX MVP status

Status date: 2026-09-04 (Asia/Rangoon)

The percentages below separate code completion from live readiness. They are
engineering estimates, not production traffic or revenue metrics.

## Progress against the final goal

| Goal area | Status | Evidence / remaining gate |
|---|---|---|
| Telegram customer entry and help | Implemented locally | `/start`, `/help`, button menus, plans, purchase, receipt submission, order tracking, status, key delivery, renewal, wallet, and free entitlements |
| Daily free entitlement | Implemented locally | 300 MB Outline key, renewable once per rolling 24 hours; all private-chat users are tracked |
| Monthly free entitlement | Implemented locally | 3 GB key for 30 days, renewable every rolling 30 days, with expiry/revocation pass |
| Plan catalog and commercial snapshots | Implemented locally | Public catalog exposes 50 GB / 30 days at 3,000 MMK and 100 GB / 30 days at 6,000 MMK; orders snapshot amount, name, quota, and duration |
| Staff-assisted payment review | Implemented locally | Receipt photos/documents are uploaded to a private Supabase Storage bucket, while the database keeps only immutable object metadata/checksum and review state; optional vision LLM extracts candidate fields; `/receipts`, `/receipt`, `/verify`, and `/rejectreceipt` expose a review queue; staff verification remains authoritative and required before screenshot-paid approval |
| Wallet ledger | Implemented locally | Immutable credit/reserve/capture/release ledger and balance projection; external receipts use credit→reserve→capture while wallet purchases use reserve→capture without double deduction |
| Subscription lifecycle | Implemented locally | UTC start/expiry, active/pending/expired states, and independent paid entitlements (multiple simultaneous keys per customer); untouched orders expire after 24 hours |
| Outline provisioning | Implemented locally | TLS pinning, GET/list/POST/optional deterministic PUT, quota set/delete, metrics, and 404-safe key deletion |
| Durable external-effect state | Implemented for one process | SQLite jobs and notifications with retry, stale-running recovery, dedupe; paid and free/trial/promo issuance commits a server-scoped provisioning intent before any Outline call, then reconciles it through the maintenance worker; deterministic IDs and read-after-ambiguous recovery prevent duplicate keys when the adapter supports it; ten-minute conversational prompts survive restarts |
| Expiry and revocation | Implemented locally | Expiry job, 404-safe known-key deletion, expiry notification; expired/pending subscriptions cannot disclose or later provision keys |
| Quota exhaustion enforcement | Implemented locally | Metrics `used >= configured limit` fails closed, records a deduplicated event, and queues hard DELETE; Outline has no documented pause endpoint |
| Usage/capacity operations | Implemented locally | Admin `/capacity`, stable transfer-metrics adapter, mapped active-key totals, and read-only per-node admission/policy posture (freshness, headroom, over-allocation, orphan audit) |
| Endpoint health evidence | Implemented locally | Durable management/inventory observations with latency, state transitions, failure/recovery streaks, and conservative hysteresis; degraded/unreachable nodes are blocked from new admission while other nodes continue |
| Endpoint drain/retirement lifecycle | Implemented locally | Owner-confirmed active/draining/retired state; draining blocks new assignments, and retirement fails closed until local and remotely observed keys, orders, and setup intents are empty |
| Provider/region/transport registry | Implemented foundation | Migration 14 mirrors Outline endpoints into stable provider, region, transport, profile, assignment, and credential identities; legacy paid/free rows backfill idempotently without storing management URLs or plaintext access URLs; non-Outline adapters and live migration remain gated |
| Remote inventory review | Implemented locally | Owner-only in-place classification of untracked present keys as reviewed external or unreviewed; never adopts/deletes credentials and never hides capacity usage |
| Auditability | Implemented locally | Order, payment, approval, rejection, provision, revoke events |
| Order consistency operations | Implemented locally | Derived customer stages, receipt-level rejection/resubmission, untouched-order cancellation/expiry, wallet history, and admin `/reconcile` invariant scan |
| Persistent commercial DB at production scale | Optional backend implemented | `COMMERCE_DATABASE_URL` selects PostgreSQL; hosted DB provisioning and live migration remain gates |
| Independent worker / web control plane | Worker and callback endpoint implemented; live enrollment pending | The guarded DigitalOcean worker/timer and encrypted one-time `/fleet/register` callback (Render or standalone TLS service) are in source; live endpoint/worker canary remains a gate. Render profiles now run read-only Telegram, Supabase, LLM, database, and pinned-Outline startup canaries. |
| Live Telegram and Outline smoke test | Three management/data endpoints verified; customer tranche controlled | Bot, primary, Singapore-B, and BKK/Nube Outline endpoints, firewall, pinned-TLS API canaries, and a reversible BKK data-port/key create-delete canary are verified; a real Telegram-account canary and longer observation remain |
| Automated payment-provider verification | Deliberately deferred | First paid pilot is staff-assisted per final architecture |
| Referrals, affiliates, and resellers | Deliberately deferred | Enable only after paid-pilot retention, abuse, unit-economics, and reliability evidence |
| Multi-node allocation and guarded scale-out | Implemented; BKK admission deliberately closed | Server-scoped allocation, provider inventory, provider-side SSH-key attachment, two-observation provider-orphan audit with separately gated cleanup, non-secret remote-key audit, durable two-observation scale gate, capacity posture, stable provider identity checks, idempotent intents, encrypted single-use enrollment, and worker safety gates are live; free/trial/promo provisioning is restart-safe and counts pending reservations against node admission; untracked remote keys remain a migration blocker and BKK is healthy/canary-verified but remains at zero plan/tier slots until its owner-approved tranche is set |

## Honest aggregate view

- Core paid-concierge code: **100% of the current assisted-scaling MVP scope**;
  automated payment-provider verification, affiliates, resellers, and a second
  control-plane writer remain explicitly outside this MVP gate.
- Local test/evidence coverage: **100%** for the current fake-Outline, TLS,
  SQLite, PostgreSQL-adapter, Supabase Storage client, receipt, trial, quota,
  order, multi-key, quota-warning, Telegram delivery, infrastructure-worker,
  wallet, restart-safe interaction, receipt-fingerprint, Telegram timestamp
  formatting, notification-lease, deterministic-entitlement-recovery, receipt-model-selection, and
  provider-activation-gate, release-unit, preflight-gate, recovery-audit, and
  production-acceptance suite (394 tests passing at the latest verification).
- Live deployment readiness: **staged, not 100%**. The current sanitized
  acceptance run passes source, lint, compilation, tests, required secret names,
  fleet-manifest parsing, backup-key validation, and the recovery entrypoint,
  but fails database/off-site recovery readiness because the local `.env` does
  not declare a hosted database or off-site database/fleet destination. Legacy
  primary allocation is also over-subscribed (70 declared slots against 20
  saleable keys), archive decryptability has not been run in this environment,
  provider mutations remain disabled, stable DNS is not configured, and live
  Render/systemd checks were not requested by the audit. A real Telegram
  account canary, allocation normalization/strict validation, untracked-key
  audit, live enrollment callback/worker canary, and sustained observation
  remain owner-controlled gates.
- End-to-end MVP readiness: approximately **90%** for the controlled paid-
  concierge scope. This is a progress estimate, not a claim that automated
  payment verification, reseller features, or unrestricted scale-out are live.

## Remaining MVP gates

1. Run a real owner-controlled Telegram-account canary: daily 300 MB → monthly
   3 GB → buy both 50 GB and 100 GB plans → receipt photo
   → LLM/manual review → approve → provision → quota-hit DELETE → `/myvpn` smoke
   tests, with before/after key inventories and a receiving-account transaction
   comparison. Confirm whether active sessions stop within the promised window.
2. Keep BKK/Nube at zero issuance slots until the owner sets a conservative
   allocation tranche after the data-plane canary; expand only after measured
   demand, support ownership, and quota/revocation evidence.
3. Complete the 100% acceptance checklist in
   `docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md`, then decide whether the paid
   pilot remains one-process SQLite or enables the PostgreSQL backend plus an
   independent worker before admitting more than a controlled cohort; the
  PostgreSQL schema path is now present but not live-tested.
4. If zero-touch provider expansion is desired, deploy the Render or standalone
   TLS callback, set the matching enrollment gates, provision one canary node,
   verify the callback/reconcile/rollback audit trail, and keep its capacity at
   zero until the owner approves the `AURIX_AUTO_NODE_*` tranche.
