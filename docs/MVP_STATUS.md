# AuriX MVP status

Status date: 2026-09-02 (Asia/Rangoon)

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
| Durable external-effect state | Implemented locally | PostgreSQL/SQLite jobs and notifications with retry, stale-running recovery, dedupe, endpoint-pinned assignments, and infrastructure intent records |
| Expiry and revocation | Implemented locally | Expiry job, 404-safe known-key deletion, expiry notification; expired/pending subscriptions cannot disclose or later provision keys |
| Quota exhaustion enforcement | Implemented locally | Metrics `used >= configured limit` fails closed, records a deduplicated event, and queues hard DELETE; Outline has no documented pause endpoint |
| Usage/capacity operations | Implemented locally | Admin `/capacity`, endpoint-scoped transfer/inventory maps, health freshness, global and per-plan slot controls, mapped active-key totals |
| Auditability | Implemented locally | Order, payment, approval, rejection, provision, revoke events |
| Order consistency operations | Implemented locally | Derived customer stages, receipt-level rejection/resubmission, untouched-order cancellation/expiry, wallet history, and admin `/reconcile` invariant scan |
| Persistent commercial DB at production scale | Backend implemented and used by hosted profile | `COMMERCE_DATABASE_URL` selects PostgreSQL; the new endpoint migration still requires backup/restore rehearsal before production rollout |
| Independent worker / web control plane | Partial | One process now keeps Telegram long polling responsive with a dedicated maintenance thread; separate Render worker/web services remain a later scale-out step |
| Live Telegram and Outline smoke test | Existing single-node release previously observed | The working Droplet/Outline installation is external state; this commit's endpoint migration and fleet controls have not yet been deployed or acceptance-tested there |
| Automated payment-provider verification | Deliberately deferred | First paid pilot is staff-assisted per final architecture |
| Multi-node Outline control plane | Foundation implemented; activation gated | Composite endpoint/key identity, allocation, endpoint-scoped enforcement, guarded provider jobs, and admin slot controls exist; real provider mutation and second-node activation default off pending the autoscale runbook gates |
| Referrals, affiliates, resellers | Deliberately deferred | Enable only after paid-pilot retention, abuse, unit-economics, and reliability evidence |

## Honest aggregate view

- Core paid-concierge code: approximately **80%** of the scoped first pilot.
- Local test/evidence coverage: **100%** for the current fake-Outline, fake-provider, TLS,
  SQLite, PostgreSQL-adapter, Supabase Storage client, receipt, trial, quota,
  order, multi-key, endpoint collision, stale-health, partial-metrics, quota-warning,
  Telegram delivery, and wallet suite (178 tests passing).
- Live deployment readiness remains gated; database backup/restore, management-port
  firewalling, second-node acceptance, and live smoke checks are not evidenced by
  the local suite.
- End-to-end MVP readiness: approximately **60%** when code and live gates are
  weighted together. This is the useful progress number for the current task;
  it is not a claim that customers can safely use the service today.

## Remaining MVP gates

1. Back up Supabase/PostgreSQL and rehearse the endpoint migration on a restored copy.
2. Reconcile the current `157.245.63.95` Outline inventory to the bootstrap endpoint
   without committing its management URL or certificate secret.
3. Restrict the Outline management port and separate the provider token from the
   public VPN/9Router host.
4. Deploy this exact commit to one control-plane process with provider mutation and
   new-endpoint activation still disabled.
5. Run daily 300 MiB → monthly 3 GiB → buy both 50 GiB and 100 GiB plans → receipt photo
   → LLM/manual review → approve → provision → quota-hit DELETE → `/myvpn` smoke
   tests, with before/after key inventories and a receiving-account transaction
   comparison. Confirm whether active sessions stop within the promised window.
6. Complete the manual two-node acceptance in
   `AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md` before enabling provider mutation.
