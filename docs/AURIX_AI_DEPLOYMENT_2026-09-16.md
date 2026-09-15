# AuriX AI deployment record

Date: 2026-09-16 (Asia/Rangoon)  
Commit: `6325545` (`feat(ai): deploy OpenAI-compatible gateway controls`)  
Target: `157.245.63.95` (`OutlineServerSingapore`)  
Public origin: `https://ai.aurix-mart.tech/`

## Result

The scoped AuriX AI gateway, usage controls, OpenAI-compatible API changes,
admin UI, capability catalog, and integration documentation were deployed to
the existing `aurix-ai` service. The Outline, 9Router, Caddy, storefront, and
AI persistent state services were not restarted or modified.

## Images and rollback

- Active image: `aurix-ai:local`
- Active image ID: `sha256:1216b0e0731a121a3d211b1fed5dce9b414b4b0f039d47a55e6c5c050272b107`
- Rollback tag: `aurix-ai:rollback-20260916-6325545`
- Rollback image ID: `sha256:e210dda649a867372d2814d41fd614d0a1bb29cede44035280a06890d5fa474c`
- Persistent state: `/var/lib/aurix-ai`

Rollback, if required:

```sh
docker tag aurix-ai:rollback-20260916-6325545 aurix-ai:local
systemctl restart aurix-ai
```

Do not delete `/var/lib/aurix-ai` during rollback.

## Verification

| Check | Result |
|---|---|
| Local regression suite | 74 tests passed |
| Python compilation | Passed |
| JavaScript syntax checks | Passed |
| `aurix-ai.service` | Active/running |
| Container | Running with active image above |
| `GET /api/healthz` | `200`, provider `9router`, external API enabled |
| `GET /api/modes` | `200`, expected mode catalog present |
| `GET /api/models` | `200`, model capability metadata present |
| Browser/static asset parity | Passed for all checked HTML/CSS/JS assets |
| Integration guide parity | Passed for `AURIX_EXTERNAL_API.md` |
| Anonymous conversation route | `401`, Telegram login required |
| Anonymous admin usage route | `401`, admin authentication required |
| Anonymous external key route | `401`, API key required |

The authenticated provider/model call was not run during this rollout, so
provider quota and language-quality behavior remain separate acceptance checks.

Unrelated generated exports, Android files, research artifacts, and social
media assets were intentionally excluded from the release commit.
