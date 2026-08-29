# Refactor Phase 0 — Safety Baseline

Status: repository safeguards implemented; live staging evidence and a recoverable
source-control tag remain operator gates before Phase 1.

Phase 0 does not move production code or change application behavior. It makes the
current behavior measurable so later module extraction can be reviewed and rolled
back safely.

## 1. Frozen baseline

| Item | Baseline |
| --- | --- |
| Deployment Python | 3.13.4 (`render.yaml` and `render-free.yaml`) |
| Pre-Phase-0 local suite | 103 passing tests |
| Pre-Phase-0 branch coverage | 62% across application and deploy modules |
| Phase-0 local suite | 106 passing tests |
| Phase-0 branch coverage | 63% across application and deploy modules |
| Database implementations | merged SQLite schema and hosted PostgreSQL schema |
| Cross-backend structural fingerprint | `af107dfe16a19c4d6749ae5dd973f5a61885a28117368b1eb606823610c7cbf0` |
| SQLite metadata fingerprint | `cef5886281cd0d5a1e6561bf114527b18bcdbf94312c4551d9f96ede39728999` |
| PostgreSQL DDL fingerprint | `5d6fca1a4c9815aa9356e6f7022671d93061d9194b62681b038434964b180b92` |
| Verified remote `main` SHA | `f9b8eaba247b447db60e640fc08c5946db851cb8` |

The workspace Git metadata has no usable `origin`. A clean temporary application clone
reverified the remote SHA during Phase 0. Before Phase 1, use clean application Git
metadata, commit this Phase 0 snapshot, verify `origin/main`, and create an annotated
baseline tag. Do not tag the unrelated workspace Git history.

## 2. Automated gates

GitHub Actions now uses the same Python version as Render and runs:

```text
python -m py_compile app.py commerce.py receipt_llm.py supabase_storage.py deploy/render_preflight.py deploy/render_web.py
ruff check app.py commerce.py receipt_llm.py supabase_storage.py deploy test_app.py test_commerce.py test_free_profile.py test_mvp.py test_storage.py
coverage run -m unittest discover
coverage report
```

The initial lint policy catches syntax errors, invalid control flow, undefined names,
and invalid exports without introducing a formatting-only diff. Branch coverage may
not fall below 60%. Raise the threshold as seams are extracted and tested; never lower
it merely to make a refactor pass.

The schema contract:

- initializes the actual merged SQLite schema;
- captures all PostgreSQL `CREATE TABLE`, `ALTER TABLE ... ADD COLUMN`, and named
  index statements without contacting a server;
- requires identical table, column, and named-index sets; and
- pins the cross-backend structure, SQLite column/default/key/index metadata, and
  normalized PostgreSQL DDL to separate Phase 0 SHA-256 fingerprints.

An intentional schema change must update both database implementations, add migration
coverage, explain the compatibility impact, and then update the fingerprint.

## 3. Behavior invariants

All later phases must preserve these invariants unless a separately reviewed product
change says otherwise.

### Identity and authorization

1. Only private chats where `chat.id == from.id` are handled.
2. Customer commands and buttons cannot call administrator operations.
3. Admin-only commands remain invisible in the default Telegram command scope.
4. Every privileged mutation is re-authorized at execution time and requires a
   single-use, expiring confirmation tied to the admin, chat, command, arguments, and
   current state fingerprint.
5. Stale, malformed, cross-user, and expired callbacks fail closed.

### Free and trial access

1. A failed Outline create does not consume an entitlement.
2. Daily access is one 300 MiB key per 24-hour boundary; monthly trial access is one
   3 GiB key per 30-day boundary under the configured allow-list policy.
3. Expiry and quota enforcement are independent, idempotent maintenance stages.
4. Local revocation blocks disclosure/reactivation while remote deletion is retried.
5. Quota warnings are deduplicated at 25%, 10%, and 5% remaining and delivery failure
   never blocks enforcement.

### Orders, evidence, wallets, and paid access

1. Repeated purchase intent reuses the same open order; changing plan requires an
   explicit replacement and is rejected after payment activity.
2. New receipt images are uploaded to private storage before the order enters review;
   raw image bytes and access URLs are not stored in plaintext database text fields.
3. Extraction output is untrusted. Approval requires verified evidence or an already
   reserved wallet payment in public configuration.
4. Transaction references are normalized and unique per provider.
5. Approval, provisioning, notification enqueue, retry, refund, wallet ledger entries,
   expiry, quota termination, and revoke processing remain idempotent.
6. A user may own multiple paid keys. Customer status, usage, and VPN views must show
   only keys owned by that user.
7. Paid duration begins at successful activation, not at order creation or approval.
8. Refund is an admin-only operation; it reverses wallet value exactly once, records
   audit state, and revokes associated access.
9. Worker failures stay distinguishable from key `revoke_failed` state and remain
   actionable in administrator views.

### Telegram UX and operations

1. `/help` is side-effect free. `/start` currently may attempt the welcome daily claim;
   tests and live checks must not treat the two commands as equivalent.
2. Customer and administrator home messages/keyboards retain their Phase 0 golden
   contracts unless an intentional UX change updates the test.
3. Admin list panels paginate at five items, keep callback data within Telegram's
   64-byte limit, and refresh by editing the existing message when possible.
4. Polling stays responsive while maintenance runs in its own bounded path.
5. A single maintenance pass reuses one Outline metrics snapshot and continues expiry
   processing when a quota stage fails.
6. Notifications retry durably and become visible dead letters after the terminal
   retry threshold.

## 4. Characterization map

| Risk boundary | Existing proof |
| --- | --- |
| Claim timing, concurrency, create rollback, expiry, quota | `test_app.py` claim/database tests |
| TLS certificate pin and known-key delete convergence | `test_app.py` Outline transport tests |
| Customer/admin separation and confirmation replay protection | `test_app.py` Telegram authorization tests |
| Golden customer/admin message and keyboard contracts | Phase 0 tests in `test_app.py` |
| Editable paginated panels | `test_admin_panel_refresh_reuses_same_message` and panel-navigation tests |
| Receipt media/storage review path | receipt tests in `test_app.py` and `test_storage.py` |
| Order replacement and payment-reference uniqueness | `test_commerce.py` order/payment tests |
| Approval/provision idempotency and ambiguous remote recovery | worker tests in `test_commerce.py` |
| Multiple paid keys and ownership | paid-key tests in `test_mvp.py` and `test_commerce.py` |
| Refund/revoke/wallet ledger | `test_refund_is_idempotent_wallet_reversal_and_access_revoke` |
| Expiry/quota notification dedupe and dead letters | maintenance tests in `test_app.py` and `test_commerce.py` |
| Render worker health/process behavior | `test_free_profile.py` and `test_mvp.py` |
| SQLite/PostgreSQL schema parity | Phase 0 schema contract in `test_commerce.py` |

## 5. Live staging evidence gate

Run this only with a staging bot, disposable users/orders, a staging receipt bucket,
and disposable Outline keys. Capture timestamps and non-secret identifiers. Never
record bot tokens, service-role keys, database URLs, receipt signed URLs, or `ss://`
access URLs.

Before the run:

- record Render deploy SHA, region, service type, Python version, and worker instance
  count;
- record Supabase project region and database connection mode;
- record Outline `/server` version and a sanitized access-key inventory containing
  only key ID, name, and limit;
- confirm `ALLOW_TEXT_PAYMENT_REFERENCES=0`, receipt storage is required, and the
  bucket is private; and
- confirm a maintenance heartbeat completed recently.

Exercise and verify:

1. `/help` renders the customer menu without creating a key.
2. A daily claim creates one bounded key; a duplicate claim is denied; maintenance
   removes it at expiry and sends one termination notice.
3. A monthly trial follows its independent 30-day entitlement and expiry path.
4. An upgrade order accepts a screenshot, stores it privately, requires administrator
   verification, provisions once after approval, and appears immediately in customer
   order/VPN/usage views.
5. A second paid purchase creates a second owned key without hiding the first.
6. Receipt rejection requests replacement evidence without closing a valid open order.
7. Refund is absent from customer controls and, from the admin order view, reverses
   value once and removes access.
8. A disposable small-quota key generates the warning sequence and cannot transfer
   beyond the Outline-enforced limit. Record separately whether an already connected
   client stops immediately; do not infer this from API deletion alone.
9. A forced retryable notification failure appears in operational state, recovers on
   retry, and does not duplicate the user message.
10. More than five pending items paginate and refresh the same Telegram message.
11. A non-admin account cannot see or execute any admin command, typed command,
    callback, or stale copied button.
12. The final sanitized Outline inventory differs only by intentionally retained test
    keys; remove the rest and verify deletion.

## 6. Backup, restore, and rollback gate

Before Phase 1:

- SQLite deployment: stop the single writer and take a recoverable copy or platform
  snapshot of `/var/data/bot.db`; retain the matching environment-variable inventory
  without secret values.
- PostgreSQL deployment: create a custom-format logical backup with `pg_dump` using a
  read-only secret source. Restore it into an isolated non-production database and run
  the schema contract plus order/wallet/subscription count checks.
- Supabase Storage: inventory the private receipt bucket and verify that a restored
  database's evidence paths resolve in a non-production restore exercise.
- Outline: take a sanitized before/after key inventory. Database restore cannot recreate
  externally deleted credentials, so remote-key reconciliation is a separate rollback
  step.

Rollback is allowed only to the verified Phase 0 commit/tag. Restore data only when the
rollback's schema is compatible. After rollback, run the health endpoint, maintenance
heartbeat check, `/help`, one disposable claim/revoke, one disposable paid workflow,
and the final Outline inventory comparison.

## 7. Phase 1 entry criteria

Known baseline debt must remain visible during later phases:

- `deploy/render_preflight.py` has no direct coverage and `app.py` branch coverage is
  57%. Extracted seams should raise these numbers before the global threshold moves.
- The PostgreSQL contract currently validates generated DDL and cross-backend
  structure without applying it to a live PostgreSQL catalog. The staging restore test
  remains the proof for engine-specific execution and constraints.
- Local clean-environment validation used Python 3.13.5. The workflow deliberately
  selects the exact Render version, 3.13.4, and must pass remotely before Phase 1.

Entry criteria:

- CI passes from a clean clone on Python 3.13.4.
- The Phase 0 commit is pushed and its remote SHA is verified.
- A recoverable Phase 0 tag exists in the application repository.
- Database backup and isolated restore evidence is recorded.
- The live staging checklist passes or each external limitation is explicitly accepted.
- The final diff contains safety-net/configuration/test/documentation changes only; no
  production behavior has changed.
