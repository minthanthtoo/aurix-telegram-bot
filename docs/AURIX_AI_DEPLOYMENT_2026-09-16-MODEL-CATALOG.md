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

## Follow-up source and latency audit (2026-09-16)

This section records a local source/test audit after the deployment above; it is
not a new deployment or a fresh production probe.

- The deployed-release record above identifies `2628af8` and image
  `sha256:812ba0b38aeb09a2e9e21fba3ba96fcfbc54232f0afe19da0e45de836865abfb`
  as the rollout. Hualogu's execution log later recorded its live
  `GET /api/ai/models` returning HTTP 200 in about two seconds with exactly
  three server-allowlisted chat models. That is historical end-to-end evidence,
  not a fresh measurement of AuriX's public endpoint or its response bytes.
- In local source at commit `8d14051`, an authenticated loopback request to the
  default `/v1/models` path returned 2,652 bytes in 4.25 ms with the three
  Hualogu model IDs allowed and a fake router. A one-model account returned
  1,011 bytes in 4.80 ms. These are local fixture measurements, not production
  latency guarantees or upstream-provider measurements.
- The HTTP regression
  `ExternalAPIHTTPTest.test_http_model_discovery_uses_fast_curated_chat_catalog_by_default`
  verifies that the default path does not call `router.list_models`, returns
  only the account-authorized model, preserves its route ID and capability
  metadata, and stays below the test's 20 KB response bound. The `/v1/models`
  handler defaults to `view=curated`; model-policy filtering is applied to the
  curated list.
- Hualogu's current `AurixClient::models()` requests `/v1/models` without a
  `view` or `category` query and configures model discovery for one attempt
  with an eight-second default timeout. This exercises the fast default path.
  Explicit `/v1/models?view=live` still discovers all configured categories;
  category-scoped paths remain the intended way to query individual media
  catalogs. The old large/slow behavior is therefore addressed for the normal
  partner model-picker request, not promised away for explicit full discovery.
- Since `2628af8`, the only AuriX-owned change through local HEAD `8d14051` is
  a test-only commit for downstream SSE disconnect cleanup. There is no
  `aurix_ai/` runtime diff after the recorded deployment commit, so that test
  commit does not change the deployed image or API behavior. It is local-only
  and has not been deployed.
- Retry contract: AuriX does not automatically replay partner inference
  requests. It preserves upstream `429`, `502`, `503`, or `504` statuses and a
  valid `Retry-After` header for the caller (covered by
  `test_openai_route_preserves_upstream_rate_limit_for_partner_retry`).
  Hualogu's client independently retries those transient statuses and
  transport failures up to three times before it starts consuming an SSE body;
  model discovery is explicitly single-attempt. Once stream consumption begins,
  Hualogu does not resubmit the inference. These are client-source semantics,
  not new server behavior in this deployment.

### Staging status and proposed acceptance gate (not provisioned)

No dedicated AuriX AI staging URL, service, or staging partner key is defined
in the inspected AI deployment configuration. `deploy/README.md` describes an
Outline/bot staging setup on `139.59.122.170`; fleet documentation identifies
that host as the standalone `sg-b` Outline data node, not an AI staging host.
Do not use it for this acceptance run. The existing AuriX AI deployment is on
the Singapore `sg-a` control-plane host; the recorded public origin is the
production `ai.aurix-mart.tech` endpoint.

Proposed setup for owner approval only:

1. AuriX provisions an isolated staging gateway at a new hostname such as
   `ai-staging.aurix-mart.tech`, with its own service/state directory and
   partner-key database. It must not mount production AuriX state. DNS, TLS,
   host placement, and any 9Router connectivity/account cost require approval
   before provisioning. If no separate 9Router staging account is available,
   any later live-model call through production 9Router must be separately
   approved as billable use; local fake-router checks remain free and isolated.
2. AuriX issues one expiring staging key, restricted to the three Hualogu
   allowlisted routes (`ag/gemini-3.1-pro-low`, `ag/gemini-pro-agent`, and
   `ag/gemini-3.7-flash-high`) and only the English/translator/Lisu-assistant
   chat modes needed by the test. Hualogu's existing environment contract is
   `AURIX_BASE_URL` plus `AURIX_API_KEY` in the Laravel `api` component's
   server-side DigitalOcean App Platform environment settings; mark the key
   secret and do not put it in source, the frontend/static component, or task
   messages. Use a separate Hualogu staging app/environment; none is identified
   in the inspected `deploy/app.yaml`.
3. For image coverage, allow one small, non-sensitive image-input canary in a
   normal chat request. Do not grant standalone image-generation, audio,
   embeddings, or video modes for this test.
4. Proposed live-test budget: at most eight provider inference requests total,
   including retries, across three one-request model smoke checks, two
   translation directions, one short conversation follow-up, one SSE response,
   and one image-input request; limit traffic to two requests per minute and
   revoke the key immediately after the run. AuriX currently enforces a
   per-minute account limit but no hard cumulative request/token budget, so the
   eight-call ceiling would be an operator-supervised budget, not a technical
   quota guarantee. Confirm the model plan's actual cost before approving any
   live calls.
5. The AuriX platform owner must approve staging infrastructure, upstream
   account use, model scope, and the request budget. AuriX's operator provisions
   and later revokes the key; the Hualogu application owner installs it into
   the staging API component's secret store and approves that environment's
   test deployment. No access/key provisioning is authorized by this note.

Acceptance order: local fake-provider tests first; then authenticated staging
`GET /v1/models`; then the explicitly budgeted text/translation/SSE/image
canaries; finally verify key revocation and prompt-free usage attribution. Do
not substitute the production endpoint for missing staging.
