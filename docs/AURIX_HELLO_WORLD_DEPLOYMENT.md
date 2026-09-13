# AuriX storefront and product-shell deployment

Status: **live**  
Deployment date: 2026-09-08 (Asia/Rangoon)

## Public origin

- `https://aurix-mart.tech/`
- `https://www.aurix-mart.tech/` → permanent redirect to the apex
- `https://vpn.aurix-mart.tech/` → VPN product shell
- `https://ai.aurix-mart.tech/` → live AuriX AI gateway backed by 9Router
- `https://accounts.aurix-mart.tech/`, `https://games.aurix-mart.tech/`,
  `https://solutions.aurix-mart.tech/`, and `https://status.aurix-mart.tech/`
  → shared, status-aware product shells
- DNS: root A record `@` → `157.245.63.95`, TTL `Auto`
- TLS: issued and managed automatically by Caddy

## Runtime topology

```text
aurix-mart.tech
  -> Caddy on 157.245.63.95:443
  -> aurix-site container on Docker network 9router-net
  -> /opt/aurix-site/index.html, /opt/aurix-site/product/index.html,
     and the canonical AuriX logo
```

The AuriX container has no host port mapping. Caddy is the only public web
listener. The existing 9Router hostname remains a separate Caddy site block:

```text
157-245-63-95.sslip.io -> 9router:20128
aurix-mart.tech        -> aurix-site:80
vpn.aurix-mart.tech    -> aurix-site:80 (product shell rewrite)
ai.aurix-mart.tech     -> aurix-ai:10000
other public shells     -> aurix-site:80 (product shell rewrite)
```

Outline remains separate from both web routes: its Singapore data and
management ports are `45524` and `61603` respectively.

The checked-in Caddy source for this deployment is
[`deploy/9router-caddyfile`](../deploy/9router-caddyfile). The remote host
keeps a pre-deployment Caddyfile backup beside its active configuration.

The full product, hostname, rebrand, and service-boundary contract is in
[`AURIX_MULTI_PRODUCT_PLATFORM.md`](AURIX_MULTI_PRODUCT_PLATFORM.md).

## Verification evidence

- All four Namify authoritative DNS servers returned `157.245.63.95`.
- HTTPS returned `200` with `Content-Type: text/html`.
- The apex page contained the AuriX storefront title and all five product
  status cards.
- The VPN hostname loaded the dedicated `AuriX VPN` shell in a browser.
- All seven CNAME-backed public aliases returned HTTPS successfully; `www`
  returned a `301` to the apex and the six product/status shells returned `200`.
- The logo asset returned `200 image/svg+xml`.
- The 9Router health endpoint continued returning `{"ok":true}` after the
  Caddy reload.
- `ai.aurix-mart.tech/api/healthz` returned `200` with `service: aurix-ai`.
- `ai.aurix-mart.tech/api/modes` returned English, English ↔ Lisu, and Lisu
  assistant modes.
- Authenticated AI chat returned `PUBLIC_AURIX_AI_OK` from
  `gemini-3.1-pro-low`; unauthenticated chat returned `401`.
- Browser verification loaded the title `AuriX VPN — AuriX` from the live VPN
  subdomain.

## Follow-up

- The VPN portal and Telegram Mini App implementation now live in
  [`AURIX_VPN_PORTAL.md`](AURIX_VPN_PORTAL.md), `vpn_web_api.py`, and
  `web/vpn-app/`. This static storefront deployment has not been switched to
  that API origin yet.
- VPN fulfillment remains Telegram/API-led until `aurix-vpn-web` is deployed
  with the same PostgreSQL state and encrypted access-URL key as the bot. No
  checkout, login, or key display should be considered live on this static
  host until that cutover is verified.
- No public Outline management route was invented by this deployment.
- `api.aurix-mart.tech` and `admin.aurix-mart.tech` remain intentionally
  un-routed. Add them only when the corresponding protected services exist.
- The 9Router credential was intentionally left unchanged in this deployment;
  no credential is stored in this repository.

The repository contains the AI gateway container, policy modes, and
`ai.aurix-mart.tech` Caddy target. The live host runs `aurix-ai` on
`9router-net` with the verified `ag/gemini-3.1-pro-low` route. The gateway
returned `gemini-3.1-pro-low` in an authenticated smoke test; its separate
AuriX access token remains server-side and is not stored in this repository.
