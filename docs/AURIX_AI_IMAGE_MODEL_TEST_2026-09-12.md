# AuriX AI image-model live test

Date: 2026-09-12 (Asia/Rangoon)  
Target: `https://ai.aurix-mart.tech/`  
Gateway: deployed AuriX image `sha256:e210dda649a867372d2814d41fd614d0a1bb29cede44035280a06890d5fa474c`

## Method

Each catalog entry was tested through the deployed
`POST /v1/images/generations` route, not by calling a provider directly.
The test used one small prompt and `n=1`. The first pass requested URL output;
failed models were retried without the optional `response_format` field. The
two edit-model entries were also retried with a valid small PNG data URL as
`image`.

The harness recorded only HTTP status, latency, returned image presence, and
sanitized gateway errors. It did not download or retain generated images. A
temporary image-only API account was created for the run; its key, account,
usage rows, audit rows, and database rows were removed after testing.

## Results

### Passed

| Model | Result | Observed latency |
| --- | --- | ---: |
| `cx/gpt-5.5-image` | Passed | 22.3 s |
| `ag/gemini-3.1-flash-image` | Passed | 13.5 s |
| `cf/@cf/black-forest-labs/flux-2-klein-9b` | Passed | 3.0 s |
| `cf/@cf/black-forest-labs/flux-2-klein-4b` | Passed | 8.0 s |
| `cf/@cf/black-forest-labs/flux-2-dev` | Passed | 9.6 s |
| `cf/@cf/leonardo/lucid-origin` | Passed | 4.7 s |
| `cf/@cf/leonardo/phoenix-1.0` | Passed | 3.7 s |
| `cf/@cf/black-forest-labs/flux-1-schnell` | Passed on retry without `response_format` | 2.3 s |
| `cf/@cf/bytedance/stable-diffusion-xl-lightning` | Passed | 3.6 s |
| `cf/@cf/lykon/dreamshaper-8-lcm` | Passed | 2.5 s |
| `cf/@cf/stabilityai/stable-diffusion-xl-base-1.0` | Passed on retry with image input | 11.2 s |

### Failed under the current gateway/provider configuration

| Model | Result | Observed error |
| --- | --- | --- |
| `cx/gpt-5.4-image` | Failed | 9Router upstream HTTP 400 |
| `cx/gpt-5.3-image` | Failed | 9Router upstream HTTP 400 |
| `cf/@cf/runwayml/stable-diffusion-v1-5-img2img` | Failed with image input | 9Router upstream HTTP 403 |
| `cf/@cf/runwayml/stable-diffusion-v1-5-inpainting` | Failed with image input | 9Router upstream HTTP 400 |
| `xai/grok-2-image-1212` | Failed | 9Router upstream HTTP 403 |

The first generic test for `stabilityai/stable-diffusion-xl-base-1.0`
returned a 400, but its retry succeeded. It is therefore classified as
available but intermittently unreliable rather than failed.

## Interpretation

The live catalog contains 16 entries, but 11 were successfully exercised at
least once. Five remain unavailable or incompatible with the current 9Router
configuration, and one model had a transient first-call failure. The catalog
should not be treated as a guarantee of generation success; the UI/API should
keep discovery separate from health and should surface provider failures.

The inpainting model may require a provider-specific mask field. The current
AuriX portable image schema forwards `image`/`images` but does not expose a
dedicated `mask` field, so this test does not claim full inpainting support.

## Visual comparison of the shared prompt

The exact prompt was also regenerated for visual inspection:

```text
A simple blue circle on a white background.
```

Two images were retrieved before 9Router began returning upstream HTTP 429
rate limits for the remaining comparison calls:

- `cx/gpt-5.5-image` produced a 1254×1254 PNG with a clean, flat, saturated
  blue circle and an essentially pure white background. It followed the
  literal prompt most precisely.
- `ag/gemini-3.1-flash-image` produced a 1024×1024 JPEG with a blue/teal
  circle, subtle texture/shadow, and a lightly textured off-white background.
  It looked more illustrative but was less literal than the prompt.

This prompt is too simple for a broad creative-quality ranking. The remaining
models need a later visual pass after the provider rate-limit window resets;
their earlier HTTP-success calls proved endpoint generation but did not retain
images for inspection.

## Recommended next action

Keep the 11 models that passed in the normal selectable list, mark the five
consistently failing routes as unavailable until 9Router/provider access is
fixed, and add a model-health cache with a short TTL so the UI does not show
models that repeatedly fail. Do not silently retry image generation because a
retry can create and charge a second image.
