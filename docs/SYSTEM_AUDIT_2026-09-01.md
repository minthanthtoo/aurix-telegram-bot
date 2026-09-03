# AuriX production-flow audit — 2026-09-01

## Executive verdict

AuriX is a functional, single-process MVP with durable order, wallet, receipt,
provisioning, notification, audit, expiry, and quota state. It is suitable for a
controlled launch on one DigitalOcean host after the release checks pass. It is
not yet a horizontally scalable or screenshot-automated payment system.

Capacity management is **partly real**: Outline inventory and telemetry are
observed live, and paid-order admission enforces configured key, plan-slot, and
committed-quota limits. The configured limits are operator declarations, not
physical capacity discovered from the VPS/ISP, and free/trial/promo traffic is
not included in committed paid traffic.

Receipt automation is deliberately **triage, not approval**. A screenshot is
forgeable. Final approval requires a staff member to confirm the transaction in
the receiving wallet/account.

## Flow audit

| Flow | Durable state / controls | Verdict |
|---|---|---|
| Onboarding | Tracks the Telegram user; `/start` does not create a key | Good |
| Daily free | One 300 MB key per rolling 24 hours; DB lock; Outline limit; expiry/quota deletion | Good for one process |
| Monthly free | One 3 GB key per rolling 30 days; same lifecycle controls | Good for one process |
| Promo | Campaign window/count/quota/duration; first-N allocation; blocks paid/free while gift is active; restores normal plans afterward | Good; campaign copy should be reviewed per launch |
| Paid order | Catalog snapshot, one open order per user, 24-hour capacity reservation, cancellation/expiry | Good; multiple keys are sequential, not simultaneous open checkouts |
| Payment method | Five method picker, private QR assets, selected provider persisted | Good |
| Receipt upload | Image only, size/type checks, SHA-256 and Telegram identity dedupe, private Supabase object before review state | Good; altered/cropped duplicates still need perceptual detection |
| AI receipt triage | Provider-aware extraction, deterministic amount/provider/time/reference-label/recipient checks, three-way candidate verdict | Safe because it cannot approve |
| Admin verification | Admin must enter/confirm transaction ID and amount from receiving account; normalized reference uniqueness | Strong trust boundary; still operationally manual |
| Approval | Verified receipt or wallet reservation required; transactional wallet/provisioning state | Good |
| Wallet top-up | Preset/manual amount, five methods, exact receipt amount, idempotent credit | Good; depends on manual receiving-account verification |
| Wallet purchase | Reserve then capture; rejection/expiry releases exactly once | Good |
| Provisioning | Durable jobs, retries, idempotent local state, encrypted access URL, server assignment | Good; remote create uncertainty still needs reconciliation |
| My VPN | Active/history filters, pagination, focused key copy, in-message edit navigation | Good |
| My Orders | Status filters, pagination, message edits, focused order actions | Good |
| Usage | Outline rolling transfer metrics, remaining quota, configurable customer warnings | Accurate to Outline's metric semantics; not live speed/billing-month usage |
| Expiry/quota | Bot-side enforcement, remote delete, verification, durable event/audit, retry/escalation | Strong for Outline API availability |
| Staff access | Persistent owner/admin roles, owner-only management, optional Telegram control-group bootstrap | Good; Telegram cannot enumerate ordinary group members |
| Staff alerts | Durable, deduplicated event notifications with per-staff preferences | Good |
| Diagnostics | Isolated real-image test, masked host/request IDs, no financial side effects | Good after provider selection was added |
| Deployment | GitHub CI gate, atomic release, systemd restart/rollback, persistent SQLite | Good for one node; no horizontal replicas |

## Capacity: real versus declared

Real inputs:

- Outline `/server`, `/access-keys`, transfer metrics, and experimental bandwidth
  telemetry are queried.
- Remote key count, health, observed transfer, and bandwidth are persisted.
- Paid admission counts active/pending subscriptions and open paid reservations.
- Plan slot limits, usable key count (`max_keys - reserved_keys`), and committed
  paid quota are enforced before order creation.

Declared/modelled inputs:

- `max_keys`, reserved headroom, monthly traffic budget, and plan slots are set
  by an owner/admin. Outline does not report the VPS's safe user capacity.
- “Monthly traffic budget” is quota commitment, not measured remaining ISP
  allowance and not a throughput/SLA calculation.
- Free, monthly-free, and promo keys affect observed remote key count but not
  committed traffic allocation.
- Inventory can be stale between refreshes. A remote key can also exist outside
  AuriX, which is why remote count is used as a safety input.

Therefore the feature is not a mock, but it is a conservative admission model,
not autoscaling or physical-capacity discovery.

## Provider receipt rules

| Method | Unique reference labels accepted | Labels never treated as a payment ID | Required shared checks |
|---|---|---|---|
| KBZPay | Transaction No/Number/ID | account, phone, recipient | completed, exact amount/MMK, <=1 hour, AuriX recipient |
| WavePay | Transaction ID/No/Number | phone, mobile, recipient | same |
| AYA Pay | Transaction ID, Reference No/Number | **Transaction Code**, recipient, To, account | same |
| UABPay | Transaction ID/No, Reference No | account, phone, recipient | same |
| CB Pay | Payment Reference Number, Transaction ID/No, Reference No | E-filing Reference Number, user name, account | same |

The expected amount and merchant identity are not included in the vision prompt,
which avoids leading the model toward a desired answer. They are compared after
extraction by deterministic code. Missing merchant configuration fails closed to
manual review. Negative, mismatch, duplicate, stale, and future cases become
reject candidates; missing/unreadable/ambiguous fields remain manual review.

## Receipt benchmark and accuracy limits

The configured primary and fallbacks were tested against official success-screen
examples and the five deployed QR cards. The stricter prompt fixed the observed
AYA defect: Gemini, Luna, and Sol all stopped treating recipient alias `YAMIN`
under `Transaction Code` as a transaction ID.

Primary-route measured decision accuracy on this tiny fixture set:

| Method | Correct / tested | Measured sample score | What was covered |
|---|---:|---:|---|
| KBZPay | 2 / 2 | 100% | QR negative; promotional/composite negative |
| WavePay | 1 / 1 | 100% | QR negative |
| AYA Pay | 2 / 2 | 100% | QR negative; completed screen with no unique ID correctly held for review |
| UABPay | 1 / 1 | 100% | QR negative |
| CB Pay | 2 / 2 | 100% | QR negative; official completed receipt/reference/amount |

These are fixture scores, **not production accuracy estimates**. With only one
or two examples per method, a 95% Wilson lower bound is roughly 21% (1/1) or 34%
(2/2). Honest production accuracy requires at least 30–100 labelled, de-identified
receipts per provider spanning app versions, languages, crops, dark mode, blur,
fees, delayed timestamps, duplicates, edits, failure, pending, and QR screens.

Model observations on the shared benchmark:

- Gemini was the best primary: safe AYA handling, all QR negatives, and correct
  CB reference/amount; roughly 7–9 seconds per image.
- Luna was sometimes faster but missed the CB amount and previously produced the
  unsafe AYA alias result under the generic prompt.
- Sol was safe on AYA/QR but rejected the valid CB example as a guide/other page.

## Remaining production risks

1. No bank/wallet API, webhook, or statement feed exists; screenshots cannot be
   auto-approved safely.
2. Re-encoded and lightly altered duplicate images now produce a bounded
   perceptual-hash signal and remain in manual review; heavily cropped or edited
   images can still evade it. Keep transaction-reference uniqueness primary and
   never treat the image fingerprint as payment proof.
3. Build a labelled evaluation corpus and show per-provider confusion matrices;
   do not display an “accuracy” percentage before minimum sample size.
4. Capacity should include free/trial/promo quota commitments and freshness/SLA
   gates. Throughput and CPU/RAM need separate load tests.
5. SQLite plus long polling is one-process architecture. Webhooks, PostgreSQL
   job claiming, and independent workers are required before replicas/autoscale.
6. Telegram-facing timestamps are now rendered in Myanmar-local, human-readable
   form (`Asia/Yangon` / MMT by default) while persistence remains UTC; operators
   can set `AURIX_DISPLAY_TIMEZONE` for another IANA zone.
7. Short-lived conversational input state is now persisted with a ten-minute
   expiry, so top-up/admin replies survive a restart; larger multi-step flows
   still require explicit durable order state.
8. Provider UI/version drift is inevitable; rules and fixtures need versioned,
   periodic review.
