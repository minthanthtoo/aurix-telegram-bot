# AuriX AI image-generation deployment record

Date: 2026-09-12 (Asia/Rangoon)  
Target: `157.245.63.95`  
Public origin: `https://ai.aurix-mart.tech/`

## Result

The image-generation gateway and browser workspace were deployed to the
existing `aurix-ai` service. Only `aurix-ai` was restarted. Caddy, 9Router,
Outline, the storefront, and `/var/lib/aurix-ai` were not changed.

The rollout includes:

- OpenAI-compatible `POST /v1/images/generations`.
- Optional raw image output through
  `/v1/images/generations?response_format=binary`.
- Image capability discovery through `GET /v1/models`.
- Authenticated first-party `/api/image-models` and
  `/api/images/generations` routes.
- A browser Image generation mode with model discovery, size selection, and
  image previews.
- Image-generation API-key scope enforcement and usage attribution.
- Updated standalone integration documentation.

No 9Router source code or 9Router credential was changed.

## Image and rollback

- Deployed image: `aurix-ai:local`
- Deployed image ID:
  `sha256:93308c3d08789a78726903e2e201e17c34dabce8bacaa2d2f419408103703683`
- Rollback tag: `aurix-ai:rollback-20260912-before-imagegen`
- Rollback image ID:
  `sha256:1a94c25fc1ece4ee6817f6797a18040e46edf862fa63c26f76609829e2f28c76`
- Persistent state: `/var/lib/aurix-ai`

Rollback, if required:

```sh
docker tag aurix-ai:rollback-20260912-before-imagegen aurix-ai:local
systemctl restart aurix-ai
```

Do not delete `/var/lib/aurix-ai` during rollback.

## Verification

| Check | Result |
|---|---|
| Local regression suite | `426 passed, 0 failed, 1 skipped` |
| In-container Python preflight | Passed |
| `GET /api/healthz` | `200`, provider `9router`, external API enabled |
| `GET /` | `200` |
| Live homepage image-generation marker | Present |
| Live integration guide image endpoint | Present |
| Anonymous `GET /api/image-models` | `401` |
| Anonymous `POST /v1/images/generations` | `401` with valid JSON |
| Anonymous `GET /api/admin/accounts` | `401` |
| `aurix-ai.service` | Active |
| 9Router/Caddy/storefront/Outline containers | Still running |

An authenticated live image-generation call was not performed because it
would spend provider image quota and no partner key was exposed for testing.
The authenticated path is covered by the local HTTP tests and in-container
preflight.
