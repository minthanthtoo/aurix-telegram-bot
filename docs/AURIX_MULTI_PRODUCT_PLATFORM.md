# AuriX multi-product platform

Status: canonical storefront and platform plan  
Last verified: 2026-09-08 (Asia/Rangoon)

This is the handoff for future AI agents working on the AuriX web storefront
and product platform. It records what is live, what is reserved, and what must
remain separate. Do not infer that a planned product is implemented merely
because its card or hostname exists.

## 1. Current live state

- Storefront: [`https://aurix-mart.tech/`](https://aurix-mart.tech/)
- Root DNS: `aurix-mart.tech` A record → `157.245.63.95`
- Web front door: Dockerized Caddy on the Singapore host
- Static storefront upstream: `aurix-site` on Docker network `9router-net`
- 9Router remains on its own hostname and upstream
- Outline remains a separate VPN data plane; customer VPN traffic does not pass
  through the AuriX web application
- The storefront is deployed, but it is not yet a full customer portal. The
  VPN customer flow remains Telegram/API-led.

The current page is a product-ready shell. Its cards expose status and
direction without inventing checkout links, API credentials, or unsupported
supplier relationships.

## 2. Public hostname contract

Use explicit hostnames so each product can be deployed or retired independently.
Reserve names now; add DNS and Caddy routes only when the corresponding service
exists and has passed its product gate.

| Hostname | Purpose | Current state |
| --- | --- | --- |
| `aurix-mart.tech` | Main storefront | Live |
| `www.aurix-mart.tech` | Storefront alias | Live; permanent redirect to apex |
| `vpn.aurix-mart.tech` | VPN customer portal and Telegram Mini App | Portal implemented; live hostname remains the static shell until shared-state deployment |
| `ai.aurix-mart.tech` | Customer-facing AI web interface and API | Live; 9Router-backed Gemini Pro gateway |
| `accounts.aurix-mart.tech` | Authorized account/subscription services | Live shell; planned and supplier-gated |
| `games.aurix-mart.tech` | Official game-credit fulfillment | Live shell; planned and supplier-gated |
| `solutions.aurix-mart.tech` | Custom solutions and quote workflow | Live shell; planned |
| `api.aurix-mart.tech` | Stable versioned API origin | Reserved |
| `status.aurix-mart.tech` | Public service status shell | Live shell; monitoring integration pending |
| `admin.aurix-mart.tech` | Operator administration | Private only; no public customer route |

Do not use the `sslip.io` hostname as the AuriX brand. It is an IP-derived
infrastructure alias, not a domain owned by AuriX.

## 3. Product identity and rebranding

Product branding must be separate from implementation names:

```text
stable service_code: ai-router
current implementation: 9router container
current infrastructure alias: 157-245-63-95.sslip.io
customer display name: AuriX AI
future display name: changeable without changing the account or API contract
```

Persist stable identifiers in data and logs. Keep `display_name`, `public_slug`,
and `service_code` separate. A future rebrand can change the UI, marketing
name, and canonical hostname while retaining a temporary compatibility alias
and the stable API version.

The current 9Router deployment is reachable at
`https://157-245-63-95.sslip.io/`. Its dashboard and health endpoint must remain
a separate Caddy site from the AuriX storefront. The 9Router service may become
an AuriX-branded product later, but its provider credentials, OAuth state,
database, and security policy must not be merged into the VPN product.

The AI gateway implementation is documented in [`AURIX_AI.md`](AURIX_AI.md).
It runs as `aurix-ai` on `9router-net`, calls the existing 9Router privately,
and serves the AuriX-branded AI UI at `ai.aurix-mart.tech`. The verified route
is `ag/gemini-3.1-pro-low`, which returned `gemini-3.1-pro-low` in a live
authenticated smoke test.

The product-level source boundaries are explicit: `aurix_ai/` owns the AI
gateway and `aurix_vpn/` owns VPN commerce, Outline provisioning, Telegram bot
handlers, and the VPN portal. Shared infrastructure remains at repository level
and is not allowed to import either product package.

## 4. Product catalog model

The catalog should be policy-driven, not hard-coded in HTML or Telegram
handlers. The conceptual records are:

```text
products
  code, display_name, public_slug, category, status
  terms_url, fulfillment_adapter, provider_ref, risk_class

offers
  product_code, plan_code, price, currency, duration, quota, availability

entitlements
  customer_id, product_code, offer_id, starts_at, expires_at, status

fulfillment_jobs
  product_code, entitlement_id, adapter, state, attempts, last_error

audit_events
  actor, product_code, action, target, reason, created_at
```

Use explicit product states:

```text
DRAFT → PENDING_REVIEW → ACTIVE → PAUSED → RETIRED
                         └──────→ BLOCKED
```

`ACTIVE` means the product has passed its supplier, legal, security, and
operational gates. `PAUSED` removes checkout or shows a clear unavailable
state according to policy. `BLOCKED` must not be fulfilled. Keep state changes
auditable and recoverable.

## 5. Initial product map

### AuriX VPN — `vpn`

- Storefront status: live pilot card; authenticated portal implementation ready
- Product hostname: `https://vpn.aurix-mart.tech/` (static shell until the shared-state web service is deployed)
- Fulfillment: AuriX entitlement → endpoint assignment → Outline key
- Primary customer interface: Telegram bot plus the authenticated VPN portal
  (`vpn` web surface; deployment requires the shared-database web service)
- Current operating guard: BKK is memory-constrained; do not turn the pilot
  card into an unrestricted capacity promise
- Management URLs, certificate fingerprints, and access URLs remain secret

### AuriX AI — `ai`

- Storefront status: live pilot; package and deployment are independent from VPN
- Implementation: [`aurix_ai/`](../aurix_ai/) with a separate `aurix-ai` container
- Underlying routing implementation can change; customer identity and order
  history must not depend on the 9Router product name
- OAuth/provider credentials require explicit consent, secure persistence,
  provider-policy review, and authenticated API access
- Never expose provider OAuth tokens, local callback secrets, or the internal
  9Router database through the storefront

### Authorized account services — `accounts`

- Storefront status: planned
- Only offer accounts, subscriptions, or delegated access when the supplier
  explicitly permits resale or sharing
- Do not store or distribute compromised credentials, stolen sessions, or
  provider tokens without authorization

### Game credits — `games`

- Storefront status: planned
- Use official or authorized distribution channels
- Fulfillment must validate product, region, recipient, amount, and duplicate
  redemption state

### Custom solutions — `solutions`

- Storefront status: planned
- Use a quote/contract workflow with an identified customer, scope, owner,
  acceptance criteria, and support boundary

## 6. Runtime topology

```text
Browser
  -> Caddy :80/:443
       -> aurix-site          storefront
       -> future product apps by hostname
       -> 9router:20128       9Router hostname only
       -> aurix-ai:10000      ai.aurix-mart.tech

Customer VPN device
  -> assigned Outline node directly
  -> Internet

Telegram / product APIs
  -> AuriX control plane
       -> PostgreSQL or persistent SQLite
       -> durable jobs and audit
       -> endpoint-scoped Outline adapter
```

Current Singapore host listeners:

```text
:80/:443    9router-caddy container / Caddy front door
:45524      Outline client/data port
:61603      Outline Management API port
```

Only Caddy binds public web ports. Product containers must join the private
Docker network and must not claim host ports 80 or 443. The checked-in Caddy
source is [`deploy/9router-caddyfile`](../deploy/9router-caddyfile).

The target architecture keeps the AuriX control plane and customer VPN data
plane independent. Prefer hosting the API/control plane on a separate Render
service or protected application host. Do not place customer VPN traffic,
payment state, or provider tokens behind the 9Router web route.

## 7. Security and compliance boundaries

- No cloaked, misleading, or hidden product catalogs.
- No alternate payment paths designed to avoid audit or review.
- Unapproved or prohibited offers remain disabled and auditable; they are not
  disguised as another product.
- Product-specific terms and supplier authorization are required before
  `ACTIVE`.
- Use least-privilege service credentials and separate secrets per product.
- Use audience-scoped tokens; do not share a broad cookie across all subdomains.
- Keep the admin surface private through an allowlisted/VPN path.
- Keep 9Router OAuth data isolated from AuriX commerce and VPN credentials.
- Restrict Outline Management API access; never publish its secret management
  URL or generated access URLs.
- Treat model output, receipt parsing, supplier responses, and browser content
  as untrusted input. Human or policy checks remain authoritative for risky
  actions.

## 8. Deployment procedure

For a static storefront change:

1. Update `web/index.html` and `web/styles.css` locally.
2. Run `git diff --check` and inspect the final diff.
3. Copy only intended public assets to `/opt/aurix-site`; never copy `.env`
   files, databases, keys, or OAuth state.
4. Validate the mounted Caddy configuration before reload.
5. Back up the remote Caddyfile, reload Caddy, and confirm both the AuriX page
   and the 9Router health endpoint.
6. Verify DNS, TLS, page markers, static assets, and an independent browser
   load.

For the AI product, follow [`AURIX_AI.md`](AURIX_AI.md): build and start the
`aurix-ai` container on `9router-net`, verify the upstream model, then reload
Caddy so `ai.aurix-mart.tech` points to the healthy container.

For another new product subdomain or product shell:

1. Create and test the product upstream on the private Docker network or its
   separate Render service.
2. Add one explicit DNS record for the chosen host.
3. Add one Caddy hostname block or Render custom-domain mapping.
4. Validate and reload the front door.
5. Run product-specific authentication, terms, fulfillment, audit, and rollback
   tests before changing the catalog state to `ACTIVE`. A hostname or shell is
   not evidence that fulfillment is active.

## 9. Live shell deployment record

Verified 2026-09-08 (Asia/Rangoon):

| Route | DNS | Front-door behavior | Verified result |
| --- | --- | --- | --- |
| `www.aurix-mart.tech` | CNAME → `aurix-mart.tech` | Redirects to apex | HTTPS `301` |
| `vpn.aurix-mart.tech` | CNAME → `aurix-mart.tech` | Rewrites `/` to the shared product shell; page selects product from hostname | HTTPS `200`; VPN shell loaded in browser |
| `ai` | CNAME → `aurix-mart.tech` | Proxied to `aurix-ai:10000`; authenticated gateway backed by 9Router | HTTPS 200; chat verified |
| `accounts`, `games`, `solutions`, `status` | CNAME → `aurix-mart.tech` | Rewrites `/` to the shared product shell; page selects product from hostname | HTTPS `200` each |

The `api` and `admin` hostnames intentionally have no public DNS route yet.
They must not be pointed at the storefront just to make them appear complete.
The shell deployment changes presentation and routing only; it does not create
checkout, customer authentication, supplier fulfillment, or public management
API behavior.

Do not add a wildcard DNS record or wildcard Caddy route without a documented
need and an explicit security review.

## 9. Future-agent checklist

Before changing this platform, future agents should:

- read this document, `docs/AURIX_HELLO_WORLD_DEPLOYMENT.md`, and the canonical
  Outline runbook;
- confirm the current Caddy site blocks before editing;
- preserve the `157-245-63-95.sslip.io` → `9router:20128` route;
- preserve Outline data and management port separation;
- keep credentials, access URLs, and OAuth state out of Git and reports;
- verify the intended hostname after every Caddy or DNS change;
- distinguish `LIVE`, `PLANNED`, `PAUSED`, and `BLOCKED` rather than inferring
  readiness from a UI card;
- record material deployment changes and test evidence in a dated document.
