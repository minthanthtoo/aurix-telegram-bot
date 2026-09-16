# AuriX AI model-catalog deployment record

Date: 2026-09-16 (Asia/Rangoon)  
Target: `157.245.63.95` (`OutlineServerSingapore`)  
Public origin: `https://ai.aurix-mart.tech/`

## Change

The authenticated HTTP `GET /v1/models` path now returns a small,
policy-filtered curated chat catalog immediately. It no longer waits for all
sequential live 9Router category calls on a partner's first model-picker
request. Live discovery remains available explicitly:

- `GET /v1/models?category=image` (also `embedding`, `stt`, `tts`, or `video`)
- `GET /v1/models?view=live` for the full live catalog

Provider route IDs, display names, capabilities, and AuriX metadata remain in
the response. The key's allowed modes and models are enforced in both paths.

## Release and rollback

- Base live image before rollout:
  `sha256:c8e5d72c5336507f38e0e1a6b76b2dd9f90e5b63d009e5a0dfa2df0ce22c33ee`
- Active image after rollout:
  `sha256:812ba0b38aeb09a2e9e21fba3ba96fcfbc54232f0afe19da0e45de836865abfb`
- Rollback tag: `aurix-ai:rollback-20260916-model-catalog`
- Rollback image:
  `sha256:c8e5d72c5336507f38e0e1a6b76b2dd9f90e5b63d009e5a0dfa2df0ce22c33ee`
- Source archive SHA-256:
  `f896e74fd4f814ab9d1b98e8f7d74a0f01013faed3372d129f91ec4a933272d1`
- Persistent state: `/var/lib/aurix-ai`

Rollback, if required:

```sh
docker tag aurix-ai:rollback-20260916-model-catalog aurix-ai:local
systemctl restart aurix-ai
```

Do not delete `/var/lib/aurix-ai` during rollback.

## Verification

| Check | Result |
|---|---|
| Targeted AI regression suite | 90 tests passed |
| Python compilation | Passed locally and inside the image |
| Diff/whitespace validation | Passed |
| `aurix-ai.service` | Active/running |
| `GET /api/healthz` | `200` |
| `GET /api/modes` | `200` |
| Public API guide parity | Byte-for-byte match, 58,716 bytes |
| Anonymous `/v1/models` | `401` |
| Anonymous `/api/admin/usage` | `401` |
| 9Router/VPN/Caddy/persistent state | Not modified |

Authenticated partner-catalog verification requires a real API key and was
not performed with a credential in this task. The default-vs-live policy
behavior is covered by HTTP regression tests with a fake router, including a
guard that fails if the default path calls live discovery.
