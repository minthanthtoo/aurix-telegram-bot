# AuriX MVP status

Status date: 2026-09-02 (Asia/Rangoon)

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
| Durable external-effect state | Implemented for one process | SQLite jobs and notifications with retry, stale-running recovery, dedupe |
| Expiry and revocation | Implemented locally | Expiry job, 404-safe known-key deletion, expiry notification; expired/pending subscriptions cannot disclose or later provision keys |
| Quota exhaustion enforcement | Implemented locally | Metrics `used >= configured limit` fails closed, records a deduplicated event, and queues hard DELETE; Outline has no documented pause endpoint |
| Usage/capacity operations | Implemented locally | Admin `/capacity`, stable transfer-metrics adapter, mapped active-key totals |
| Auditability | Implemented locally | Order, payment, approval, rejection, provision, revoke events |
| Order consistency operations | Implemented locally | Derived customer stages, receipt-level rejection/resubmission, untouched-order cancellation/expiry, wallet history, and admin `/reconcile` invariant scan |
| Persistent commercial DB at production scale | Optional backend implemented | `COMMERCE_DATABASE_URL` selects PostgreSQL; hosted DB provisioning and live migration remain gates |
| Independent worker / web control plane | Worker implemented; web separation pending | The guarded DigitalOcean worker/timer is installed on the primary host; an independently hosted Render web/control service remains a later gate |
| Live Telegram and Outline smoke test | Primary verified; node two pending | Bot, primary Outline endpoint, database backup, and worker are live; node-two installation and endpoint registration still require host access |
| Automated payment-provider verification | Deliberately deferred | First paid pilot is staff-assisted per final architecture |
| Referrals, affiliates, and resellers | Deliberately deferred | Enable only after paid-pilot retention, abuse, unit-economics, and reliability evidence |
| Multi-node allocation and guarded scale-out | Implemented; live node-two gate open | Server-scoped allocation, provider inventory, capacity posture, idempotent intents, and worker safety gates are implemented; node two must pass the live canary |

## Honest aggregate view

- Core paid-concierge code: **100% of the current assisted-scaling MVP scope**;
  automated payment-provider verification, affiliates, resellers, and a second
  control-plane writer remain explicitly outside this MVP gate.
- Local test/evidence coverage: **100%** for the current fake-Outline, TLS,
  SQLite, PostgreSQL-adapter, Supabase Storage client, receipt, trial, quota,
  order, multi-key, quota-warning, Telegram delivery, infrastructure-worker,
  and wallet suite (233 tests passing).
- Live deployment readiness: approximately **70%**; the primary node, bot,
  database backup, worker/timer, provider inventory, and guarded release path
  are verified. The second node's Outline installation, endpoint registration,
  capacity declaration, and canary remain open.
- End-to-end MVP readiness: approximately **75%** when code and live gates are
  weighted together. This is a progress estimate, not a claim that the fleet is
  ready for unrestricted public traffic.

## Remaining MVP gates

1. Restore SSH access to `139.59.122.170` and verify the actual host.
2. Install or verify Outline on that host; capture version, management URL, and
   certificate fingerprint without committing them.
3. Create a staging Telegram bot and set `ADMIN_TELEGRAM_IDS`.
4. Apply firewall rules and persistent `/var/lib/aurix-bot` storage.
5. Run daily 300 MB → monthly 3 GB → buy both 50 GB and 100 GB plans → receipt photo
   → LLM/manual review → approve → provision → quota-hit DELETE → `/myvpn` smoke
   tests, with before/after key inventories and a receiving-account transaction
   comparison. Confirm whether active sessions stop within the promised window.
6. Complete the 100% acceptance checklist in
   `docs/AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md`, then decide whether the paid
   pilot remains one-process SQLite or enables the PostgreSQL backend plus an
   independent worker before admitting more than a controlled cohort; the
   PostgreSQL schema path is now present but not live-tested.
