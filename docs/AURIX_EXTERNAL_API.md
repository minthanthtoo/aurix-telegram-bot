# AuriX AI API — site-owner guide

## AI-agent integration contract

Use the following rules exactly when implementing the consuming website:

1. Call AuriX only from the website's server/backend.
2. Use the dedicated `ak_live_...` key supplied for that website.
3. For the native AuriX contract, call `POST /v1/chat`; for standard client
   compatibility, call `POST /v1/chat/completions` with `model` and `messages`.
4. Keep the website's canonical conversation history in its own database.
5. Send a bounded `history` window and the same `conversation_id` on every turn.
6. Display the response field `text` to the user.
7. Retry only `429`, `502`, and `503`, using bounded exponential backoff and the
   returned `Retry-After` value when present.
8. Do not retry `400`, `401`, or `403` without changing the request or credential.
9. Save `request_id` and `X-Request-ID` in server logs for support correlation,
   but never log the API key, prompt, response, or Authorization header.

The standard compatibility surface is OpenAI-shaped. It supports non-streaming
and SSE streaming chat, tool/function-call messages, text and image inputs,
embeddings, and audio transcription, translation, and speech endpoints. Actual
model availability is capability-dependent; call `GET /v1/models` with the
AuriX key and choose a model advertising the required capability. AuriX does
not execute tools: the consuming website executes them and sends the resulting
`tool` message back on the next request.

Use this server-to-server endpoint:

```text
Base URL: https://ai.aurix-mart.tech
POST     https://ai.aurix-mart.tech/v1/chat
POST     https://ai.aurix-mart.tech/v1/chat/completions
```

This endpoint is for backend code. Do not call it directly from browser
JavaScript because the API key would be exposed to every visitor.

## Credential

The consuming site must use an AuriX-issued key beginning with:

```text
ak_live_
```

Send it as:

```http
Authorization: Bearer ak_live_your_key_here
```

Do not use or share any of these values with the site owner:

```text
AURIX_AI_ROUTER_API_KEY
AURIX_AI_ACCESS_TOKEN
TELEGRAM_BOT_TOKEN
AURIX_AI_ADMIN_TOKEN
AURIX_AI_DATABASE_URL
```

If the token already shared from `.env` is not an `ak_live_...` key, treat it
as exposed: remove it from the other site, rotate it, and issue a dedicated
AuriX site key instead. Even an `ak_live_...` key belongs in the site's server
secret manager, never in HTML, React/Vue code, or public Git.

## Request

### Classic chat-completions request

Use this route when the consuming site already expects the standard
OpenAI-compatible shape. `conversation_id` is not required, and the site may
omit it entirely. The site still owns the transcript and should send previous
turns in `messages`.

```json
{
  "model": "gemini-3.7-flash-high",
  "messages": [
    {"role": "system", "content": "Be concise."},
    {"role": "user", "content": "Hello"}
  ],
  "user": "site-user-123",
  "stream": false
}
```

The response is standard `chat.completion` JSON:

```json
{
  "id": "chatcmpl_example",
  "object": "chat.completion",
  "created": 1788938051,
  "model": "gemini-3.7-flash-high",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Hello!"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 5,
    "total_tokens": 25
  }
}
```

### Advanced standard features

Discover exposed routes and model capabilities:

```http
GET https://ai.aurix-mart.tech/v1/models
Authorization: Bearer ak_live_...
```

The response marks models with `chat`, `streaming`, `tools`, `embeddings`,
`audio_input`, or `audio_output`. Use the model ID exactly as returned.

For tools, send the normal OpenAI `tools`, `tool_choice`, and `parallel_tool_calls`
fields. AuriX returns `choices[0].message.tool_calls`; the consuming site must
execute the selected function, validate its arguments, and send a follow-up
request containing the assistant tool-call message and a `role: "tool"` result.

For image input, use the standard message-part shape. Only HTTPS image URLs and
bounded base64 PNG/JPEG/WebP/GIF data URLs are accepted:

```json
{
  "model": "vision-model-id-from-v1-models",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "Describe this image."},
    {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
  }]}
```

Embeddings use `POST /v1/embeddings` with `model` and `input` (one string or a
bounded array of strings). Audio uses OpenAI-compatible `multipart/form-data`
for `POST /v1/audio/transcriptions` and `/v1/audio/translations`, and JSON for
`POST /v1/audio/speech`. AuriX forwards audio bytes without storing them.

```js
const embedding = await fetch(`${AURIX_BASE}/v1/embeddings`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
  body: JSON.stringify({ model: "embedding-model-id", input: ["Lisu text"] }),
});

const speech = await fetch(`${AURIX_BASE}/v1/audio/speech`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
  body: JSON.stringify({ model: "tts-model-id", input: "Hello", response_format: "mp3" }),
});
const audioBytes = Buffer.from(await speech.arrayBuffer());
```

The classic route accepts `temperature`, `top_p`, `max_tokens`, and
`max_completion_tokens`. Only `n=1` is supported. Set `stream: true` for SSE;
the final stream chunk includes usage when the upstream supports it. The
optional `aurix_mode` body field can select `english`, `translate`, or
`lisu_assistant`; without it, standard chat messages are passed through as
ordinary OpenAI messages.

Headers:

```http
Content-Type: application/json
Authorization: Bearer ak_live_...
```

JSON body:

```json
{
  "mode": "english",
  "model_id": "gemini-3.7-flash-high",
  "message": "Hello. How are you?",
  "history": [],
  "user_id": "site-user-123",
  "conversation_id": "chat-456"
}
```

Fields:

| Field | Required | Description |
|---|---:|---|
| `mode` | No | `english`, `translate`, or `lisu_assistant`; default `english` |
| `model_id` | No | Approved model ID; omitted uses the server default |
| `message` | Yes | Current user message, 1–12,000 characters |
| `history` | No | Previous `user`/`assistant` messages. Up to 100 items may be submitted within the 128 KiB JSON body limit; AuriX keeps the newest complete turns that fit its bounded context window. |
| `context_summary` | No | Optional site-owned rolling summary, maximum 6,000 characters. It is treated as untrusted context, not as instructions. |
| `user_id` | No | Opaque site-user identifier, maximum 160 characters. Used only for usage attribution; it does not authenticate the request. |
| `conversation_id` | No | Opaque site conversation identifier, maximum 160 characters. Used only for usage attribution and support. |

History items must contain only `role` and `content`; `role` must be `user` or
`assistant`. Do not send a `system` message. AuriX owns the system policy.

## Approved modes

| Mode | Purpose |
|---|---|
| `english` | Natural English assistant conversation |
| `translate` | Bidirectional English ↔ Lisu translation |
| `lisu_assistant` | Experimental Lisu-script assistant |

`lisu_assistant` output is validated for Lisu Unicode. It remains experimental
and should receive native-speaker review for official or sensitive content.

## Approved model IDs

```text
gemini-3.7-flash-high
gemini-pro-agent
claude-sonnet-4-6
gpt-5.6-terra
gemini-3.1-pro-low-legacy
```

The account may be restricted to only some modes or models. A request outside
its account policy returns `403`.

## cURL

Run this from the consuming site's server or deployment terminal:

```sh
export AURIX_API_KEY='ak_live_replace_me'

curl --fail-with-body --silent --show-error \
  -X POST 'https://ai.aurix-mart.tech/v1/chat' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${AURIX_API_KEY}" \
  --data-raw '{
    "mode": "translate",
    "model_id": "gemini-3.7-flash-high",
    "message": "How are you today?",
    "history": [],
    "user_id": "site-user-123",
    "conversation_id": "chat-456"
  }'
```

## Node.js backend

```js
const response = await fetch("https://ai.aurix-mart.tech/v1/chat", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${process.env.AURIX_API_KEY}`,
  },
  body: JSON.stringify({
    mode: "translate",
    model_id: "gemini-3.7-flash-high",
    message: "How are you today?",
    history: [],
    user_id: "site-user-123",
    conversation_id: "chat-456",
  }),
});

const data = await response.json();
if (!response.ok) throw new Error(data.error || "AuriX request failed");
console.log(data.text);
```

For the classic route, a standard client can use this request shape:

```js
const response = await fetch("https://ai.aurix-mart.tech/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${process.env.AURIX_API_KEY}`,
  },
  body: JSON.stringify({
    model: "gemini-3.7-flash-high",
    messages: [{ role: "user", content: "Hello" }],
    user: "site-user-123",
  }),
});
const completion = await response.json();
console.log(completion.choices[0].message.content);
```

## Python backend

```python
import os
import requests

response = requests.post(
    "https://ai.aurix-mart.tech/v1/chat",
    headers={
        "Authorization": f"Bearer {os.environ['AURIX_API_KEY']}",
        "Content-Type": "application/json",
    },
    json={
        "mode": "english",
        "model_id": "gemini-3.7-flash-high",
        "message": "Hello",
        "history": [],
        "user_id": "site-user-123",
        "conversation_id": "chat-456",
    },
    timeout=75,
)
data = response.json()
response.raise_for_status()
print(data["text"])
```

## Response

```json
{
  "request_id": "req_example",
  "text": "Hello! How can I help you today?",
  "mode_result": "complete",
  "model_id": "gemini-3.7-flash-high",
  "model_label": "Gemini 3.7 Flash High",
  "requested_model": "ag/gemini-3.7-flash-high",
  "returned_model": "gemini-3.7-flash-tiered",
  "upstream_request_id": "router-request-id-if-provided",
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 10,
    "total_tokens": 22
  },
  "token_usage": {
    "input_tokens": 12,
    "output_tokens": 10,
    "total_tokens": 22,
    "cached_tokens": null
  },
  "context": {
    "history_received": 8,
    "history_used": 6,
    "history_dropped": 2,
    "context_truncated": true,
    "summary_used": false,
    "input_bytes": 12480,
    "estimated_input_tokens": 3120
  }
}
```

The `usage` object may be `null` when 9Router does not return provider token
data. The request is still counted in AuriX usage records. Save `request_id`
for support; it is also returned in the `X-Request-ID` response header.

AuriX does not persist the consuming site's conversation transcript. The site
should keep the canonical transcript and send a bounded rolling history on each
request. AuriX preserves complete user/assistant turns where possible and
reports any trimming in `context`; it never treats site history or summaries as
system instructions. The usage ledger stores `user_id` and `conversation_id`
when supplied, but never stores prompts or response text.

## Errors

| HTTP | Meaning | Action |
|---:|---|---|
| `400` | Invalid JSON, mode, model, message, or history | Correct the request |
| `401` | Missing, invalid, expired, or revoked AuriX key | Check the server secret |
| `403` | Key is not allowed to use the mode/model | Ask the AuriX operator to update the account |
| `429` | Account request-per-minute limit reached | Respect `Retry-After` and retry later |
| `502` | 9Router/provider failure | Retry with bounded backoff |
| `503` | External API is not configured/available | Contact the AuriX operator |

Error example:

```json
{
  "error": "API request rate limit reached",
  "request_id": "req_example"
}
```

## Operational rules

- Keep the key only on the site's backend.
- Do not expose the key through browser bundles, mobile apps, logs, or URLs.
- Do not send the 9Router URL or 9Router credential to the site owner.
- Use one AuriX key per site environment when possible, such as production and staging.
- Ask AuriX to revoke a key immediately if it is exposed.
- Standard streaming returns `text/event-stream`; consume it incrementally and
  do not buffer an unbounded response. The site owner may inspect public chat
  metadata with `GET /api/models` and authenticated capability metadata with
  `GET /v1/models`.

## Recommended backend wrapper

The consuming site should expose its own application-level function and keep
AuriX details behind it. For example:

```js
export async function askAurix({ mode = "english", model_id, message,
  history = [], context_summary, user_id, conversation_id }) {
  const response = await fetch("https://ai.aurix-mart.tech/v1/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${process.env.AURIX_API_KEY}`,
    },
    body: JSON.stringify({
      mode, model_id, message, history, context_summary,
      user_id, conversation_id,
    }),
    signal: AbortSignal.timeout(75_000),
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "AuriX request failed");
    error.status = response.status;
    error.requestId = payload.request_id || response.headers.get("X-Request-ID");
    throw error;
  }
  return payload;
}
```

After a successful call, append the exact user message and returned `text` to
the site's transcript. On the next call, send the newest complete turns:

```js
const result = await askAurix({
  mode: "translate",
  model_id: "gemini-3.7-flash-high",
  message: userMessage,
  history: transcript.slice(-12),
  user_id: siteUser.id,
  conversation_id: conversation.id,
});
transcript.push(
  { role: "user", content: userMessage },
  { role: "assistant", content: result.text },
);
```

Do not send a `system` message in `history`. AuriX owns the mode policy. If
the website needs to remember older context, it should maintain a concise
`context_summary` and send it as untrusted continuity context.

## Partner-site quota model

When one key is issued to one other website, that key represents the website's
whole backend account. Its quota is shared by all of that website's users and
keys. `user_id` and `conversation_id` provide attribution only; they do not
authenticate users or create separate quotas.

The site must enforce its own end-user authentication before calling AuriX.
AuriX enforces the partner account's server-side request limit and records
requests and provider token usage. A large quota is still subject to upstream
availability and should be agreed as an explicit requests-per-minute and
token/month limit; “unlimited” is not a safe production setting.

## Agent handoff block

The following block can be pasted into another coding agent's task:

```text
Integrate the AuriX AI backend.

Base URL: https://ai.aurix-mart.tech
Preferred standard endpoint: POST /v1/chat/completions
Native AuriX endpoint: POST /v1/chat
Authentication: Authorization: Bearer ${AURIX_API_KEY}
The key is an AuriX ak_live_... partner key and must remain server-side.

Standard request JSON:
{
  "model": "gemini-3.7-flash-high",
  "messages": [{"role":"user","content":"current user text"}],
  "user": "opaque site user ID"
}

Read the assistant answer from response.choices[0].message.content. Persist the
site's transcript and send previous turns in messages on the next turn. A
conversation_id is optional. Retry only 429/502/503 with bounded backoff. Do
not retry 400/401/403 blindly. Preserve the response id and X-Request-ID for
support. Never expose or log the AuriX key, 9Router credential, prompts, or
responses.

The standard route supports streaming, tools, image message parts, embeddings,
and audio routes when `GET /v1/models` advertises the selected model's
capability. AuriX forwards tool calls; the consuming site executes tools and
owns transcript/context management. Do not put the key in browser code.
```
