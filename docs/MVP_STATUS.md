# AuriX MVP status

Status date: 2026-08-28 (Asia/Rangoon)

The percentages below separate code completion from live readiness. They are
engineering estimates, not production traffic or revenue metrics.

## Progress against the final goal

| Goal area | Status | Evidence / remaining gate |
|---|---|---|
| Telegram customer entry and help | Implemented locally | `/start`, `/help`, button menus, plans, purchase, receipt submission, order tracking, status, key delivery, renewal, wallet, and free entitlements |
| Daily free entitlement | Implemented locally | 300 MiB Outline key, renewable once per rolling 24 hours; all private-chat users are tracked |
| Monthly free entitlement | Implemented locally | 3 GiB key for 30 days, renewable every rolling 30 days, with expiry/revocation pass |
| Plan catalog and commercial snapshots | Implemented locally | Public catalog exposes 50 GiB / 30 days at 3,000 MMK and 100 GiB / 30 days at 6,000 MMK; orders snapshot amount, name, quota, and duration |
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
| Independent worker / web control plane | Partial | One process now keeps Telegram long polling responsive with a dedicated maintenance thread; separate Render worker/web services remain a later scale-out step |
| Live Telegram and Outline smoke test | Blocked externally | Credentials and installed Outline output are not available; SSH to the supplied IP timed out |
| Automated payment-provider verification | Deliberately deferred | First paid pilot is staff-assisted per final architecture |
| Referrals, affiliates, resellers, multi-node scale-out | Deliberately deferred | Enable only after paid-pilot retention, abuse, unit-economics, and reliability evidence |

## Honest aggregate view

- Core paid-concierge code: approximately **80%** of the scoped first pilot.
- Local test/evidence coverage: **100%** for the current fake-Outline, TLS,
  SQLite, PostgreSQL-adapter, Supabase Storage client, receipt, trial, quota,
  order, multi-key, quota-warning, Telegram delivery, and wallet suite (89 tests passing).
- Live deployment readiness: approximately **35%**; the unit and runbook exist,
  but SSH, Outline installation/version, secrets, firewall, and live smoke checks
  are unverified.
- End-to-end MVP readiness: approximately **60%** when code and live gates are
  weighted together. This is the useful progress number for the current task;
  it is not a claim that customers can safely use the service today.

## Remaining MVP gates

1. Restore SSH access to `139.59.122.170` and verify the actual host.
2. Install or verify Outline on that host; capture version, management URL, and
   certificate fingerprint without committing them.
3. Create a staging Telegram bot and set `ADMIN_TELEGRAM_IDS`.
4. Apply firewall rules and persistent `/var/lib/aurix-bot` storage.
5. Run daily 300 MiB → monthly 3 GiB → buy both 50 GiB and 100 GiB plans → receipt photo
   → LLM/manual review → approve → provision → quota-hit DELETE → `/myvpn` smoke
   tests, with before/after key inventories and a receiving-account transaction
   comparison. Confirm whether active sessions stop within the promised window.
6. Decide whether the paid pilot remains one-process SQLite or enables the
   PostgreSQL backend plus an independent worker before admitting more than a
   controlled cohort; the PostgreSQL schema path is now present but not live-tested.
