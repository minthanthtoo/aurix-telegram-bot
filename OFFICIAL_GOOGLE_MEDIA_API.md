# Official Google Media API Map

This is a supported Google API integration map. It is intentionally separate from the Flow web HAR: it does not use Flow cookies, internal RPC IDs, or browser-session parameters.

## Backend Choice

| Need | Gemini API | Vertex AI |
| --- | --- | --- |
| Fast application integration | generativelanguage.googleapis.com and a restricted Gemini API key | Usually unnecessary |
| Cloud IAM, service accounts, GCS output, enterprise controls | Available, but API-key oriented | aiplatform.googleapis.com and OAuth |
| Image create/edit | Native Gemini image models | Use the separately documented Vertex offering |
| Veo asynchronous video | predictLongRunning plus operation polling | predictLongRunning plus GCS output |

For Gemini API, use a restricted GEMINI_API_KEY or GOOGLE_API_KEY held only on the server. Google documents that newer Gemini keys are authorization keys bound to a service account. For Vertex AI, use a short-lived OAuth access token issued to a user or service account with the minimum necessary IAM permissions.

## Current Model Families

Model availability and preview/stable status change; fetch the catalog at deployment time and make the chosen model explicit.

| Capability | Current documented choices | Use case |
| --- | --- | --- |
| Image | gemini-3.1-flash-lite-image, gemini-3.1-flash-image, gemini-3-pro-image, legacy gemini-2.5-flash-image | Create, edit, compose, and multi-turn image work |
| Video, conversation-oriented | Gemini Omni Flash | Short video, multimodal input, conversational editing |
| Video, shot controls | veo-3.1-generate-preview and documented Veo variants | Text/image video, references, start/end frames, extension, native audio |

All generated images and Veo videos include SynthID watermarking. Veo creates one video per request; it must be treated as asynchronous.

## Authentication

### Gemini API

~~~http
x-goog-api-key: <server-held Gemini API key>
Content-Type: application/json
~~~

Base URL: https://generativelanguage.googleapis.com/v1beta

### Vertex AI

~~~http
Authorization: Bearer <short-lived Google OAuth access token>
Content-Type: application/json; charset=utf-8
~~~

Base URL shape: https://us-central1-aiplatform.googleapis.com/v1

Never accept a Flow browser cookie, f.sid, HAR export, or arbitrary client-supplied Google token as a substitute for either credential path.

## Image Generation

### Text to image

POST /models/{IMAGE_MODEL}:generateContent

~~~json
{
  "contents": [
    {"role": "user", "parts": [{"text": "Create a product still life"}]}
  ],
  "generationConfig": {
    "responseModalities": ["IMAGE"],
    "responseFormat": {"image": {"aspectRatio": "1:1", "imageSize": "2K"}}
  }
}
~~~

Use only configuration fields accepted by the chosen model. Image size and aspect-ratio support differ by model. The response is synchronous. Inspect candidates[].content.parts[]; image parts carry inlineData or inline_data with a MIME type and base64 bytes. Persist those bytes to application-controlled object storage and return an application asset URL.

### Image editing and composition

Use the same endpoint. Add one or more source images to parts beside the instruction:

~~~json
{
  "contents": [{
    "role": "user",
    "parts": [
      {"text": "Replace only the background with a daylight studio"},
      {"inline_data": {"mime_type": "image/png", "data": "<BASE64>"}}
    ]
  }],
  "generationConfig": {"responseModalities": ["IMAGE"]}
}
~~~

For iterative editing, retain prior user and model turns in contents. For larger media or repeated use, upload the source through the Files API and reference its returned URI where the selected model supports it.

## Input File Lifecycle

Use inline base64 for small inputs. The Files guide recommends resumable upload when the total request, including files and prompt, exceeds 100 MB.

1. POST https://generativelanguage.googleapis.com/upload/v1beta/files with X-Goog-Upload-Protocol: resumable, X-Goog-Upload-Command: start, file metadata, byte length, and MIME type.
2. Read the server-provided resumable upload URL from the response header.
3. POST the bytes to that URL with X-Goog-Upload-Offset: 0 and X-Goog-Upload-Command: upload, finalize.
4. Store the returned file.name and file.uri; use GET /v1beta/{file.name} for metadata when needed.
5. Files are temporary. The current guide states 48 hours. Generated files can be downloaded; uploaded source files cannot be downloaded through this API.

## Veo Video: Gemini API

### Start a job

POST /models/{VEO_MODEL}:predictLongRunning

~~~json
{
  "instances": [{
    "prompt": "A 6-second tracking shot of a ceramic cup rotating on a table",
    "image": {"inlineData": {"mimeType": "image/png", "data": "<BASE64_OPTIONAL>"}}
  }],
  "parameters": {
    "aspectRatio": "16:9",
    "durationSeconds": "6",
    "resolution": "720p",
    "personGeneration": "allow_all"
  }
}
~~~

The response is an operation, not video bytes:

~~~json
{"name": "operations/<opaque-operation-name>", "done": false}
~~~

Only include image, lastFrame, referenceImages, or video when the selected Veo model supports the control. lastFrame requires image; Veo 3.1 accepts up to three reference images; video extension requires a recent Veo-generated source video. For Veo 3.1, aspect ratio is 16:9 or 9:16; durations are 4, 6, or 8 seconds; 1080p/4K and reference/extension cases have additional eight-second constraints.

### Poll and finish

GET /{operation.name}, using the same Gemini API authentication. Poll with capped exponential backoff; the official examples use roughly ten seconds. Stop on done: true and honor an operation error rather than blindly retrying it.

Representative terminal result:

~~~json
{
  "name": "operations/<opaque-operation-name>",
  "done": true,
  "response": {
    "generateVideoResponse": {
      "generatedSamples": [
        {"video": {"uri": "<authorized-download-uri>", "mimeType": "video/mp4"}}
      ]
    }
  }
}
~~~

Download the returned URI with the same server-held authentication, following redirects where required. Verify MIME type, checksum the bytes, and copy them to application-controlled storage. Treat the provider URI as temporary and sensitive; current Veo guidance states generated videos are retained for two days.

## Veo Video: Vertex AI

### Start a job

POST /projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/{MODEL_ID}:predictLongRunning

~~~json
{
  "instances": [{"prompt": "A product shot with a slow dolly move"}],
  "parameters": {"storageUri": "gs://<controlled-bucket>/veo/", "sampleCount": 1}
}
~~~

The response returns a full operation name. Poll with:

POST /projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/{MODEL_ID}:fetchPredictOperation

~~~json
{"operationName": "projects/.../operations/<id>"}
~~~

A successful response contains done: true and response.videos[], each with gcsUri and mimeType. The router service account needs least-privilege access to the output bucket. Copy or serve from controlled storage; never expose the OAuth token to callers.

## Router Contract

| Public route | Provider work | Return |
| --- | --- | --- |
| POST /v1/images/generations | generateContent | Synchronous image object with application asset URL or base64 |
| POST /v1/images/edits | generateContent with text plus source image parts | Same image result shape |
| POST /v1/videos/generations | Veo predictLongRunning | 202 job object: application job ID, status queued |
| GET /v1/videos/generations/{job_id} | Poll provider operation when due | queued, running, succeeded, or failed; asset URLs only when succeeded |
| POST /v1/videos/generations/{job_id}/cancel | Mark local cancellation; call provider cancellation only if the selected official operation supports it | Terminal local state |

Persist an idempotency key, normalized request hash, model, provider operation name, next-poll time, final asset metadata, failure classification, and audit timestamps. Do not persist raw API keys, tokens, prompts beyond the user retention policy, or provider download URIs after copying the output.

~~~text
accepted -> queued -> submitted -> running -> succeeded
                                      \-> failed | cancelled | expired
~~~

Use a worker to submit and poll, capped exponential backoff, and a recovery sweep for jobs that remain non-terminal. The caller should never need to hold a provider session while a render runs.

## Sources

- [Gemini API key guidance](https://ai.google.dev/gemini-api/docs/api-key)
- [Gemini native image generation and editing](https://ai.google.dev/gemini-api/docs/generate-content/image-generation)
- [Gemini Files API](https://ai.google.dev/gemini-api/docs/files)
- [Gemini Veo 3.1 guide](https://ai.google.dev/gemini-api/docs/veo)
- [Vertex AI Veo long-running video operations](https://cloud.google.com/vertex-ai/generative-ai/docs/video/generate-videos-from-text)

