# AuriX VPN portal and Telegram Mini App

## Purpose

The VPN product has two customer-facing surfaces with one source of truth:

1. `vpn.aurix-mart.tech` / the Telegram Mini App is a mobile-first portal for
   the signed-in customer.
2. Telegram remains the fallback and support path for payment screenshots,
   activation questions, and account recovery.

Both surfaces use the same `ClaimService`, `CommerceService`, endpoint
registry, Outline usage collection, and encrypted access-URL storage. No
Outline management URL is sent to a browser or customer.

The new implementation is:

- `telegram_web_app.py` — server-side Telegram `initData` HMAC verification.
- `vpn_dashboard.py` — shared customer VPN snapshot assembly.
- `vpn_web_api.py` — HTTPS-facing API and static portal server.
- `web/vpn-app/` — AuriX-adapted portal UI and Telegram Mini App shell.
- `deploy/render_vpn_web.py` — Render entrypoint.
- `render-vpn-web.yaml` — separate web-service deployment template.
- `deploy/render_combined.py` and `render-combined.yaml` — recommended MVP
  supervisor/template for one bot+portal service sharing persistent SQLite.

This is an AuriX implementation of the product flow and visual language. It
does not copy third-party branding, source, credentials, or Outline's
management console.

## Future-agent handoff

The implementation is intentionally split at the service boundary:

- `runtime.py` is the single composition root for bot and web services.
- `vpn_dashboard.py` is the shared customer-state read model; do not duplicate
  the old `/myvpn` aggregation in a new transport.
- `vpn_web_api.py` owns authenticated HTTP routes and only calls existing
  commerce/entitlement services.
- `connectivity.py` owns endpoint health, capacity, and preference validation.
- Migration `commerce:3` stores `orders.requested_endpoint_id` and
  `subscriptions.preferred_endpoint_id`.
- `web/vpn-app/` is the Telegram-first Home / Servers / Packages / Settings UI.

Recommended model routing for follow-up work:

| Work | Model / effort | Reason |
| --- | --- | --- |
| Architecture, auth, data migration, final security review | GPT-6 Astra, xhigh | Cross-system decisions and failure analysis |
| Main implementation, tests, deployment debugging | GPT-5.6 Sol, high | Primary engineering path |
| Bounded CSS, copy, fixtures, repetitive checks | GPT-5.6 Luna, medium | Fast, narrow supporting changes |

Do not split one stateful migration across models. Keep one Sol owner for the
implementation and use Astra as a review gate before database or live-routing
changes.

The hostname currently serves the pre-existing static product shell. The API
service is implemented and locally verified, but the hostname must not be
switched until the web process and bot point at the same durable PostgreSQL
state and the same `AURIX_ACCESS_URL_KEY`. Never solve that boundary by
copying a database file or by creating a second Outline key pool.

## Customer flow

1. A customer opens `https://vpn.aurix-mart.tech/` from Telegram.
2. Telegram supplies signed `initData` to the page.
3. Each private request sends that value in `X-Telegram-Init-Data`.
4. The API verifies the HMAC with `TELEGRAM_BOT_TOKEN`, rejects stale sessions,
   and derives the user ID from the signed `user` object.
5. The API reads the customer's own free/paid entitlements and orders.
6. Active keys can be revealed or copied by that verified customer only.
7. The customer may choose a server for the next package. The server choice is
   stored on the order, copied to the pending subscription at approval, and
   rechecked against current endpoint health/capacity by the worker.
8. Plan selection creates a normal AuriX order. Payment references are attached
   to that order and continue through the existing review/provisioning state
   machine.
9. Receipt screenshots remain Telegram-first until a separately reviewed,
   authenticated upload path is implemented.
10. Daily 300 MiB, monthly 3 GiB, and configured promo claims are available
    from Packages. They call the same guarded entitlement service as the bot,
    retain the paid-access/promo guards, and never return a management URL.
11. When an account has multiple active keys, Home exposes an in-memory key
    selector. The selected key is not stored in browser storage and does not
    move or duplicate the underlying Outline key.

The browser never uses `initDataUnsafe` for authorization, never stores keys in
`localStorage`, and never receives `OUTLINE_API_URL`, `OUTLINE_CERT_SHA256`,
`AURIX_ACCESS_URL_KEY`, payment QR values, database URLs, or bot tokens.

## API contract

Public endpoints:

- `GET /api/healthz` — process health only.
- `GET /api/plans` — active plan metadata, official Outline client links, and
  whether a payment provider is configured. It contains no QR values. It also
  states whether legacy text-reference payment is enabled.

Authenticated endpoints require `X-Telegram-Init-Data`:

- `GET /api/dashboard` or `GET /api/me` — signed-in identity, safe key state,
  usage, subscriptions, giveaway state, recent orders, and the customer's
  currently assigned server(s).
- `GET /api/servers?plan_code=<code>` — safe server directory with coarse
  state/eligibility and the latest control-plane check. It never returns an
  Outline management URL, certificate, provider resource ID, or public IP.
- `GET /api/orders` — the signed-in customer's orders.
- `POST /api/orders` with `{"plan_code":"...","endpoint_id":"..."}` —
  creates or reuses the customer's existing open order through
  `CommerceService`; `endpoint_id` is optional and is never trusted without
  server-side endpoint validation.
- `GET /api/orders/<id>` — owner-scoped order detail.
- `POST /api/orders/<id>/payment` with `{"provider":"kpay",
  "reference":"..."}` — only works when the explicitly configured legacy
  text-reference mode is enabled. The production default sends customers to
  Telegram for receipt upload/review.
- `POST /api/claims/daily` — attempts the existing rolling 24-hour 300 MiB
  claim after paid-access and promo-lock checks.
- `POST /api/claims/trial` — attempts the existing rolling 30-day 3 GiB claim,
  including the optional `TRIAL_TELEGRAM_IDS` allow-list.
- `POST /api/claims/promo` with `{"code":"..."}` — redeems an active promo
  through the existing atomic giveaway path.

Authenticated responses use `Cache-Control: no-store`. Request logs omit paths,
query strings, headers, and user IDs.

## Deployment boundary

The portal is separate from the static `aurix-site` container, but it does not
need to be a separate service from the Telegram worker. For the current
single-instance MVP, use `render-combined.yaml`: one Render web service runs the
bot and portal against the same `/var/data/bot.db`. SQLite WAL and the existing
busy timeout support these two local processes. Keep `numInstances: 1`.

Use the split `render.yaml` + `render-vpn-web.yaml` topology only when moving
both processes to PostgreSQL. Render cannot mount one SQLite disk into two
services, so that scale-out shape requires one shared `COMMERCE_DATABASE_URL`.

### Recommended MVP sequence: combined service

1. Back up the existing persistent `/var/data/bot.db` and preserve the current
   service until rollback checks pass.
2. Deploy `render-combined.yaml` using the existing Telegram, Outline, Fernet,
   Supabase, admin, and payment configuration. Do not rotate any 9Router or
   Outline credential.
3. Confirm the Render origin returns 200 for `/api/healthz` and `/api/plans`.
4. Change only the `vpn.aurix-mart.tech` Caddy host from the static shell to the
   verified Render origin. The 9Router sslip.io host remains unchanged.
5. Verify a real Telegram account can open the portal and can see only its own
   keys and orders.
6. Set `AURIX_WEB_APP_URL=https://vpn.aurix-mart.tech/`, redeploy, and confirm
   the bot menu opens the Mini App.
7. Keep the database backup and old Caddy stanza until post-cutover checks pass.

### Later scale-out sequence: PostgreSQL

1. Provision or select one empty PostgreSQL database.
2. Stop the bot so SQLite cannot change during migration. Back up its persistent
   `bot.db`, set `COMMERCE_DATABASE_URL` only in the operator shell, then run:

   ```bash
   python deploy/migrate_sqlite_to_postgres.py --source /var/data/bot.db
   python deploy/migrate_sqlite_to_postgres.py \
     --source /var/data/bot.db \
     --apply --confirm STOP_BOT_AND_MIGRATE \
     --report /var/data/sqlite-postgres-migration-report.json
   ```

   The first command is a read-only integrity/count dry run. The apply command
   refuses a destination containing existing operational state beyond normal
   seeded catalog/endpoint rows, copies in foreign-key order
   inside one PostgreSQL transaction, advances serial sequences, and commits
   only after row-count reconciliation. It never accepts the database URL as a
   command-line argument and never records row values or secrets in its report.
3. Configure the existing bot worker (`render.yaml`) and `aurix-vpn-web`
   (`render-vpn-web.yaml`) with the identical:
   `COMMERCE_DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `OUTLINE_API_URL`,
   `OUTLINE_CERT_SHA256`, and `AURIX_ACCESS_URL_KEY`.
4. Do not regenerate `AURIX_ACCESS_URL_KEY`; it decrypts stored paid keys.
5. Start the bot against PostgreSQL, verify its status and ownership counts,
   then deploy `render-vpn-web.yaml` and verify `/api/healthz` returns HTTP 200.
6. Put the resulting HTTPS service behind `vpn.aurix-mart.tech` using the
   existing Caddy host. Keep the public host as HTTPS only.
7. Set the bot worker's `AURIX_WEB_APP_URL` to the final HTTPS portal URL.
   The bot then adds the Web App to its customer keyboard, `/help`, and the
   Telegram chat menu on its next command-menu synchronization.
8. Set the web service's `AURIX_TELEGRAM_URL` to the bot/support deep link.
9. Run the smoke checks below before opening the product to customers.

Cutover is complete only when all of these are true:

- the bot and portal use the same non-empty `COMMERCE_DATABASE_URL`;
- the existing SQLite state has been migrated and row-count/key ownership
  reconciliation has passed;
- `/api/healthz` and `/api/plans` return 200 at the portal origin;
- a real Telegram launch shows the correct signed-in account and only that
  account's keys;
- Caddy routes the whole VPN hostname to the verified origin;
- `AURIX_WEB_APP_URL=https://vpn.aurix-mart.tech/` is active on the bot; and
- rollback retains the previous bot service and static Caddy route until the
  post-cutover checks pass.

The current repository contains both deployment shapes. As verified
on 2026-09-08, `vpn.aurix-mart.tech/api/healthz` still returns 404, the local
configuration has no `COMMERCE_DATABASE_URL`, the Render CLI is unauthenticated,
and the available SSH key is not accepted by `157.245.63.95`. A live web service
therefore cannot be honestly claimed until an operator supplies Render access
and an accepted host credential for the Caddy cutover. PostgreSQL is no longer
required for the recommended combined MVP. Credentials must not be copied from
9Router or written into the repository.

The configured `SUPABASE_URL` and Storage service-role key are not themselves a
PostgreSQL connection string. Use Supabase's session-pooler PostgreSQL URL (or
another PostgreSQL service) as `COMMERCE_DATABASE_URL`; do not derive or guess a
database password.

### Caddy routing shape

After the web service has a verified origin, route the VPN host as a whole to
that service:

```caddyfile
vpn.aurix-mart.tech {
    import security_headers
    reverse_proxy https://YOUR-AURIX-VPN-WEB.onrender.com {
        header_up Host YOUR-AURIX-VPN-WEB.onrender.com
    }
}
```

Do not route `/api` to 9Router. Do not proxy or publish any Outline
management endpoint from this host.

## Smoke checks

Local code checks:

```bash
python -m py_compile runtime.py telegram_web_app.py vpn_dashboard.py vpn_web_api.py
python -m unittest test_telegram_web_app.py test_vpn_web_api.py
python -m unittest test_runtime.py
python -m unittest test_render_vpn_web.py
python -m unittest test_render_combined.py
node --check web/vpn-app/app.js
git diff --check
```

Deployed checks:

```bash
curl -fsS https://vpn.aurix-mart.tech/api/healthz
curl -fsS https://vpn.aurix-mart.tech/api/plans
```

The private dashboard cannot be tested with a guessed or hand-written
Telegram payload. Test it by opening the Mini App from the configured bot and
confirming that the shown Telegram first name matches the account that opened
it. Then verify an active key appears only for that account, copy works, and a
second Telegram account cannot see it.

## Non-goals and safety boundary

- This portal does not expose Outline server management APIs.
- This portal does not create a second pool of keys; it reads the existing
  durable entitlements.
- Server selection is for a new package. Active keys are not silently copied
  to another endpoint: doing so would reset Outline-side transfer accounting
  or create duplicate billable access. An explicit migration workflow would
  need its own quota-transfer and rollback model before being enabled.
- A user is identified by verified Telegram account, not by an invented device
  fingerprint. Device-count limits must be added deliberately to the commerce
  model before being advertised.
- Receipts are not accepted as arbitrary browser uploads in this version.
- No product is hidden, cloaked, or routed through a misleading public surface.
- The service is for lawful, authorized VPN access and customer support.

## Verified implementation status (2026-09-08)

- Repository suite: 230 tests passing.
- Render entrypoint: imports correctly when invoked from outside the repository
  root; the previous `deploy/`-only import path defect is covered by regression
  test.
- Populated SQLite upgrade: verified against an existing 133-row database copy;
  the endpoint migration uses SQLite-compatible column addition and preserves
  legacy key assignments as `legacy-default`.
- Real runtime smoke: Telegram `getMe` authorized the configured bot and both
  local `/api/healthz` and `/api/plans` returned successfully from the portal
  process using an upgraded temporary database copy.
- Web startup: does not reconfigure or reactivate the bootstrap endpoint.
- Browser preview: Home and Packages inspected at the served static origin;
  unauthenticated claim controls fail closed.
- Live status: static shell only; no `/api/healthz` route yet.
- Payment readiness: the local environment has no `PAYMENT_QR_*` values, so the
  catalog honestly reports every payment provider as unconfigured. Verify the
  production service has the intended QR/recipient values before selling plans.
