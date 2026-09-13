# AuriX AI deployment record

Date: 2026-09-10 (Asia/Rangoon)  
Target: `157.245.63.95` (`OutlineServerSingapore`)  
Public origin: `https://ai.aurix-mart.tech/`

## Result

The current AuriX AI UI/backend snapshot was deployed successfully to the
existing `aurix-ai` container. Only the AuriX AI service was restarted. Caddy,
9Router, the storefront, Outline, and the persistent AI data volume were not
changed.

The deployment includes:

- Telegram session recovery, cross-tab session signals, and per-user drafts.
- Assistant starters, New conversation, explicit translation direction, and
  per-turn mode/model ordering.
- Translator result preview with Copy result, Use as source, and per-answer
  Copy actions.
- Admin Recent activity view and safer unauthorized-admin handling.
- Developer-guide navigation/search/link improvements.
- Native translation-direction validation in the AuriX router adapter.

No 9Router source code or 9Router credential was changed.

## Deployment mechanics

- The live image before rollout was preserved as
  `aurix-ai:rollback-20260910-uiux`.
- New image: `aurix-ai:local`
- Final image ID: `sha256:1a94c25fc1ece4ee6817f6797a18040e46edf862fa63c26f76609829e2f28c76`
- Previous image ID: `sha256:76dcbd93b5c7787d4a592010d760aa4f4aadd927fa70e8cea6abeca570b980b1`
- Persistent state remained at `/var/lib/aurix-ai` (156K after deploy).
- The service is active under `aurix-ai.service` on Docker network
  `9router-net`.
- Any authenticated Telegram user can use the browser AI administration
  console. `AURIX_AI_ADMIN_TOKEN` remains available for server automation;
  external site API keys remain independently scoped.

## Live verification

Verified after restart:

| Check | Result |
|---|---|
| `GET /api/healthz` | `200`, service `aurix-ai`, provider `9router` |
| `GET /api/session` without a cookie | `200`, `user: null` (first visit is not mislabeled as expired) |
| `GET /api/modes` | English, translator, and Lisu assistant modes returned |
| `GET /api/models` | Five configured comparison models returned |
| `GET /` | `200` |
| Live homepage assets | New translator workspace and New conversation controls present |
| `GET /admin` | New Recent activity markup present |
| Admin sign-in copy | States that any Telegram account can manage AuriX AI access |
| Unauthenticated `POST /api/chat` | `401` |
| Unauthenticated `GET /api/admin/accounts` | `401` |
| 9Router dashboard | Responded successfully at `https://157-245-63-95.sslip.io/` |
| Container state | `aurix-ai` running; 9Router/Caddy/storefront still running |

No authenticated Telegram chat was executed during this rollout, so live model
quality and Lisu linguistic quality remain separate acceptance checks.

## Rollback

If the new container must be reverted, restore the preserved image tag and
restart only the AI service:

```sh
docker tag aurix-ai:rollback-20260910-uiux aurix-ai:local
systemctl restart aurix-ai
```

Then repeat the health, homepage, unauthenticated-auth, and 9Router checks
above. Do not delete `/var/lib/aurix-ai` during rollback.

## Remaining product gaps

This rollout does not claim completion of durable server-owned conversations,
reconnectable streaming jobs, cancellation, or the full side-by-side translator
workflow. Those remain tracked in
[`AURIX_UI_UX_IMPLEMENTATION_PLAN.md`](AURIX_UI_UX_IMPLEMENTATION_PLAN.md).
