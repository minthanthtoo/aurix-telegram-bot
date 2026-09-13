# Google Flow HAR Analysis

## Scope and handling

Source inspected: `/Users/min/Downloads/flow.google.com.har` (HAR 1.2,
Chrome DevTools export, 532 entries). The raw HAR was read only and remains
unchanged. This report deliberately omits all cookie values, account data,
prompts, media identifiers, query values, request payload values, and response
payload values. Those values can be sensitive and are not a supported API
contract.

## Frontend-to-backend map

| Host | Method and path pattern | Observed role | Captured result |
| --- | --- | --- | --- |
| `flow.google.com` | `GET /` | App entry point; one unauthenticated redirect followed by a successful document load | `302`, `200` |
| `flow.google.com` | `GET /accounts/SetOSID` | Google account/session bootstrap redirect | `302` |
| `flow.google.com` | `POST /_/AiSandboxAngularFrontend/data/batchexecute` | Primary internal RPC multiplexer | 157 calls: 151 `200`, 6 `401` |
| `flow.google.com` | `POST /_/AiSandboxAngularFrontend/data/google.internal.labs.aisandbox.proto.flow.agent.v1.FlowCreationAgentService/StreamChat` | Internal creation-agent streaming transport | 9 `200` |
| `flow.google.com` | `GET /asb/<opaque-resource>` | App-hosted image retrieval | 12 `200`, `image/webp` |
| `flow-content.google` | `GET /image/<opaque-resource>` | Image retrieval | 50 `200`, `image/jpeg` |
| `flow-content.google` | `GET /video/<opaque-resource>` | Video retrieval, including byte-range reads | 14 successful reads: `200` or `206`, `video/mp4` |
| `flow-content.google` | `GET /audio/<opaque-resource>` | Audio retrieval, including byte-range reads | 5 successful reads: `200` or `206`, `audio/wav` |

Other observed hosts are supporting infrastructure rather than Flow application
backends: `accounts.google.com` (sign-in), `www.gstatic.com`/font hosts
(static assets), and Google Analytics/Play hosts (telemetry or product
integration).

## Request and response shape

Both Flow POST transports use `application/x-www-form-urlencoded;charset=UTF-8`.
The primary form field is `f.req`, an encoded structured request envelope. Some
batched calls and every captured `StreamChat` call also include `at`, consistent
with an anti-forgery/request-context value. The HAR shows no stable JSON object
schema that can be treated as a public client API.

The batched RPC URL carries these *names* (values omitted): `rpcids`,
`source-path`, `bl`, `f.sid`, `hl`, `_reqid`, and `rt`. `StreamChat` carries
`bl`, `f.sid`, `hl`, `_reqid`, and `rt`. Across the capture there are 34
distinct `rpcids` values, so `batchexecute` is a generic dispatch channel, not
a single endpoint per product operation.

Successful POST responses are labeled `application/json`, but their bodies are
XSSI-guarded and frame/length-prefixed before the payload. They are therefore
not ordinary single JSON response documents. Captured request sizes ranged from
74 to 27,669 bytes for the multiplexer and 2,734 to 2,889 bytes for
`StreamChat`; batched response sizes reached about 5.6 MB. This supports a
mixture of startup/state, content, and polling payloads, but does not by itself
assign semantic meaning to any RPC identifier.

## Authentication and session evidence

No `Authorization: Bearer ...` API-key scheme appears on the Flow requests.
The first six multiplexer calls returned `401`; the trace then includes Google
Accounts navigation/sign-in traffic and Flow calls begin succeeding. This is
strong evidence that Flow relies on browser-account session state rather than a
developer key passed directly to the application endpoint.

The HAR's structured request-cookie arrays are empty for `flow.google.com`, and
there is no captured `Cookie` header there, so this file cannot prove the exact
cookie set or whether the exporter omitted it. Relevant request-context
controls observed by name include `f.sid`, `at`, `Origin`, `Referer`,
`X-Same-Domain`, `X-Client-Data`, and several `X-Browser-*` headers. Treat their
values, opaque media locators, and any account cookies in the original HAR as
potentially sensitive. Their exact lifetime, credential role, binding, and
reusability were not established by this analysis.

## Observed sequence and dependencies

1. Flow's initial background RPC attempts fail with `401`.
2. The browser follows Flow-to-Google-Accounts redirects, then loads Flow
   successfully.
3. A burst of successful `batchexecute` calls starts immediately after the app
   load, consistent with bootstrap and UI/state hydration.
4. The session continues with multiplexed state/content operations. The trace
   contains repeated roughly five-second batched calls in two intervals, which
   is consistent with polling or progress refresh.
5. Nine `StreamChat` calls are interleaved with the batched calls. Subsequent
   batch calls and content-host media reads depend on prior application state;
   the HAR does not establish a durable, standalone job API.
6. Browser playback/retrieval uses `flow-content.google`; `206` responses show
   range-based video/audio delivery.

## Opaque or high-risk traffic

The following is opaque by design or should be treated as sensitive:

- `batchexecute` RPC IDs plus the `f.req` envelope: undocumented internal
  protocol, coupled to the current frontend build (`bl`) and session context.
- `google.internal...FlowCreationAgentService/StreamChat`: internal service
  namespace exposed through the web product, not evidence of a supported public
  API.
- `f.sid`, `at`, opaque `/asb/` paths, and `flow-content.google` resource
  paths: correlation/session/resource identifiers. Do not publish, replay, or
  commit them.
- XSSI-prefixed, framed responses and opaque media URLs: expected Google web
  application transport patterns, not proof of malicious or independently
  usable traffic.

## HAR Activity Map

This table is the closest task-oriented map justified by this capture. It is an
observed dependency map, not a request recipe: the capture contains no stable,
separate Flow endpoint labeled `image.generate`, `video.generate`, or
`download`.

| Activity class | Frontend transport observed | What follows in the trace | Confidence |
| --- | --- | --- | --- |
| Account bootstrap | Initial `batchexecute` calls return `401`, followed by Google Accounts redirects and a successful Flow page load | Successful Flow application calls begin | High |
| Application bootstrap | Burst of small successful `batchexecute` form posts | Additional state/content RPCs, then previews and media | High |
| Creation-agent interaction | Nine calls to the named internal `FlowCreationAgentService/StreamChat` transport | Follow-up batch calls, including poll-like traffic; in some cases later media retrieval | High for the calls; medium for their exact product meaning |
| Generation/progress tracking | Repeated batched requests at roughly five-second intervals during two periods | Batch responses change size; later media retrieval occurs | Medium: cadence supports polling, but job state fields are opaque |
| Image preview/download | `GET /asb/<opaque-resource>` and `GET /image/<opaque-resource>` | `image/webp` or `image/jpeg` response | High |
| Video playback/download | `GET /video/<opaque-resource>` on `flow-content.google` | `video/mp4`, including `206 Partial Content` reads | High |
| Audio playback/download | `GET /audio/<opaque-resource>` on `flow-content.google` | `audio/wav`, including `206 Partial Content` reads | High |

The plausible lifecycle is therefore:

```text
browser account state
  -> Flow bootstrap batches
  -> internal creation/state batches
  -> poll-like state refreshes
  -> opaque image/video/audio resource retrieval
```

This analysis identifies output delivery but has not decoded enough application
payloads to attribute individual batch RPCs to generation modes. The same
multiplexer carries 34 distinct RPC-selector values. Undecoded payloads may
contain project, conversation, asset, and operation references; their absence
has not been established. Likewise, this analysis has not identified an upload
operation within the multiplexer.

See [Flow session security audit](FLOW_SESSION_SECURITY_AUDIT.md) for the
session, token, context, concurrency, and recovery evidence gaps.

## Conclusion

### Follow-up structural inspection

An offline parser examined all 166 Flow application POST request envelopes and
response bodies. It printed only aggregate types/counts, never payload values,
RPC selectors, tokens, or resource identifiers.

| Observation | Batch transport | Creation stream |
| --- | --- | --- |
| Requests examined | 157 | 9 |
| HTTP outcomes | 151 x 200; 6 x 401 | 9 x 200 |
| Parsed outer request type | Nested arrays | Array containing null/string values |
| Response bodies stored as base64 in HAR | 2 | 1 |
| XSSI prefix after HAR decoding | 157 | 9 |
| Bodies fully consumed by JSON-frame parser | 157 | 9 |
| JSON frames per response | 2-3 | 7-43 |

Response frames contain positional arrays and, in many cases, strings that
themselves parse as JSON arrays. This is layered serialization, not evidence of
encryption. The parser skipped decimal framing lines; it did not independently
validate their declared byte lengths. Frame counts include transport records
and must not be interpreted as counts of generated assets or UI messages.

The larger number of frames in the creation stream supports a multi-record
response contract. A HAR body alone does not reveal when individual frames
arrived on the wire. HTTP 200 likewise does not establish application success.

This corrects the earlier implication that opaque RPC selectors prevent
analysis: the envelopes are parseable. Parameter meaning, mandatory versus
optional fields, and operation semantics remain unassigned by this structural
audit. No attempt was made to reconstruct authentication or session replay.

SHA-256 before and after this inspection matched:
`484f86ab49220dbba14eb67151143811d0f5cf0b1774b2e2544edb5c7839228a`.
The raw HAR was not modified.

This capture documents a browser-bound Flow web application using internal,
build-coupled RPC transports and authenticated content delivery. It is useful
for understanding the product's architecture and diagnosing a user's own
browser session, but it is not a stable or authorized basis for a CLI client or
an OpenAI-compatible provider adapter. A production CLI should use Google's
documented Gemini/Veo or Vertex AI APIs and their supported authentication,
operation polling, and media-download contracts instead.
