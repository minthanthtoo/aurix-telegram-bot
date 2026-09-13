# AuriX AI usage analytics deployment record

Date: 2026-09-12 (Asia/Rangoon)  
Target: `157.245.63.95`  
Public origin: `https://ai.aurix-mart.tech/`

## Result

The usage analytics API and admin dashboard were deployed to the existing
`aurix-ai` service. Only `aurix-ai` was restarted. 9Router, Caddy, Outline,
the storefront, and `/var/lib/aurix-ai` were not changed.

The rollout includes:

- aggregate usage at `/api/admin/usage?format=analytics`;
- overall, partner-site, API-key, model, endpoint, and daily statistics;
- request success/failure counts and success rate;
- input/output/total/cached token counts and estimated cost;
- selectable analytics ranges and token/request charts in `/admin`; and
- prompt-free, secret-safe dashboard responses and documentation.

## Image and rollback

- Deployed image: `aurix-ai:local`
- Deployed image ID:
  `sha256:e210dda649a867372d2814d41fd614d0a1bb29cede44035280a06890d5fa474c`
- Rollback tag: `aurix-ai:rollback-20260912-before-analytics`
- Rollback image ID:
  `sha256:93308c3d08789a78726903e2e201e17c34dabce8bacaa2d2f419408103703683`
- Persistent state: `/var/lib/aurix-ai`

Rollback, if required:

```sh
docker tag aurix-ai:rollback-20260912-before-analytics aurix-ai:local
systemctl restart aurix-ai
```

Do not delete `/var/lib/aurix-ai` during rollback.

## Verification

| Check | Result |
|---|---|
| Remote image preflight | Python compilation passed inside the new image |
| `aurix-ai.service` | Active |
| Container | Running with the new image |
| `GET /api/healthz` | `200`, provider `9router`, external API enabled |
| `GET /api/modes` | `200`, expected modes present |
| Browser/static asset parity | Passed for HTML, CSS, JS assets |
| Integration guide parity | Passed |
| Anonymous analytics endpoint | `401` with valid JSON error |
| Anonymous admin usage endpoint | `401` |
| Other services | Not restarted or changed |

An authenticated live analytics query was not run because this deployment has
no operator bearer admin token configured and no Telegram session was exposed
for testing. The authenticated endpoint is covered by the local HTTP
regression test; the browser dashboard uses the existing Telegram session
authentication.

