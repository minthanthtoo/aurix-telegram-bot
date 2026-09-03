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
| Durable external-effect state | Implemented for one process | SQLite jobs and notifications with retry, stale-running recovery, dedupe; ten-minute conversational prompts survive restarts |
| Expiry and revocation | Implemented locally | Expiry job, 404-safe known-key deletion, expiry notification; expired/pending subscriptions cannot disclose or later provision keys |
| Quota exhaustion enforcement | Implemented locally | Metrics `used >= configured limit` fails closed, records a deduplicated event, and queues hard DELETE; Outline has no documented pause endpoint |
| Usage/capacity operations | Implemented locally | Admin `/capacity`, stable transfer-metrics adapter, mapped active-key totals |
| Auditability | Implemented locally | Order, payment, approval, rejection, provision, revoke events |
| Order consistency operations | Implemented locally | Derived customer stages, receipt-level rejection/resubmission, untouched-order cancellation/expiry, wallet history, and admin `/reconcile` invariant scan |
| Persistent commercial DB at production scale | Optional backend implemented | `COMMERCE_DATABASE_URL` selects PostgreSQL; hosted DB provisioning and live migration remain gates |
| Independent worker / web control plane | Worker implemented; web separation pending | The guarded DigitalOcean worker/timer is installed on the primary host; an independently hosted Render web/control service remains a later gate |
| Live Telegram and Outline smoke test | Three management/data endpoints verified; customer tranche controlled | Bot, primary, Singapore-B, and BKK/Nube Outline endpoints, firewall, pinned-TLS API canaries, and a reversible BKK data-port/key create-delete canary are verified; a real Telegram-account canary and longer observation remain |
| Automated payment-provider verification | Deliberately deferred | First paid pilot is staff-assisted per final architecture |
| Referrals, affiliates, and resellers | Deliberately deferred | Enable only after paid-pilot retention, abuse, unit-economics, and reliability evidence |
| Multi-node allocation and guarded scale-out | Implemented; BKK admission deliberately closed | Server-scoped allocation, provider inventory, non-secret remote-key audit, durable two-observation scale gate, capacity posture, stable provider identity checks, idempotent intents, and worker safety gates are live; untracked remote keys remain a migration blocker and BKK is healthy/canary-verified but remains at zero plan/tier slots until its owner-approved tranche is set |

## Honest aggregate view

- Core paid-concierge code: **100% of the current assisted-scaling MVP scope**;
  automated payment-provider verification, affiliates, resellers, and a second
  control-plane writer remain explicitly outside this MVP gate.
- Local test/evidence coverage: **100%** for the current fake-Outline, TLS,
  SQLite, PostgreSQL-adapter, Supabase Storage client, receipt, trial, quota,
  order, multi-key, quota-warning, Telegram delivery, infrastructure-worker,
  wallet, restart-safe interaction, and receipt-fingerprint suite (309 tests
  passing at the latest verification).
- Live deployment readiness: **staged, not 100%**; all three declared nodes,
  the bot, worker/timers, provider inventory, firewall boundary, encrypted local
  and Supabase offsite backups, and pinned-TLS management/data-port canaries are
  verified. A temporary-destination restore drill from the private off-site
  SQLite archive also passed. Stable DNS, a real Telegram-account canary, allocation
  normalization/strict validation, untracked-key audit, and sustained observation remain.
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
