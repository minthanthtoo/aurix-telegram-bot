# Flow session, token, and context security audit

## Scope and evidence

This document assesses the observations recorded in HAR_ANALYSIS.md. It is a
defensive review, not a complete reconstruction of Google's implementation.
No live requests, interception, credential extraction, or session replay were
performed for this audit. The original HAR is unchanged.

A single capture cannot establish behavior under all conditions. In particular,
the earlier analysis examined transport metadata and envelope structure, not a
complete decode of application payloads. Undecoded fields are unknown; they are
not evidence that operation IDs or useful schemas do not exist.

## State ownership

| Layer | Evidence | What remains unknown |
| --- | --- | --- |
| Account authentication | Six initial application HTTP 401 responses, account navigation, then successful application requests | Exact credential, authentication assurance, expiry, revocation |
| Browser session | No Cookie header or populated request-cookie arrays observed on Flow requests in the export | Export omission versus actual absence; cookie scope and attributes |
| Request context | Query/form field names and origin-related headers are present | Their security semantics, validation, binding, and lifetimes |
| Application context | Multiple RPC selectors and a separately named creation stream | Project, conversation, asset, and operation schemas and ownership checks |
| Asynchronous state | Repeated requests with roughly five-second cadence | Whether they poll jobs, refresh UI state, or perform another action |
| Output delivery | Image, video, and audio responses, including HTTP 206 | Whether access uses cookies, signed locators, or another authorization mechanism |

These layers must be assessed separately. An account identity, an HTTP session,
a conversation identifier, a job identifier, and a media URL are not
interchangeable. Knowing an identifier does not establish permission to use it.

## Session and token management

No access-token or refresh-token lifecycle has been demonstrated for Flow by
this analysis. Neither OAuth refresh nor cookie renewal can be inferred from
successful HTTP responses. Likewise, field names alone do not prove that a
value is an authentication token, a CSRF token, or an expiring nonce.

For a defensive assessment using provider-owned test infrastructure, establish:

| Property | Required evidence | Expected security behavior |
| --- | --- | --- |
| Session issuance | Authentication-service logs and session policy | Session belongs to the authenticated principal |
| Session fixation protection | Before/after authentication lifecycle records | Authentication changes invalidate inappropriate prior session state |
| Expiry | Server policy and controlled expiry tests | Idle and absolute expiry enforced server-side |
| Logout and revocation | Server invalidation records plus subsequent protected-action checks | Revoked sessions cannot create work or access protected resources |
| Credential renewal | Issuance/rotation audit events | Renewal observes policy and revoked credentials remain invalid |
| CSRF protection | Server validation logic and tests | Cross-site state changes require valid request authorization |
| Cookie restrictions | Redacted cookie attributes from a complete authorized capture | Appropriate Secure, HttpOnly, SameSite, Domain, and Path settings |
| Sensitive logging | Log configuration and redaction tests | Credentials and access-bearing URLs are excluded |

Cookie attributes alone do not establish session security. HttpOnly restricts
script access to a cookie; it does not prevent an application from sending it
over an authorized connection. Server-side invalidation remains necessary.

## Context management and consistency

The HAR does not establish where conversation history lives, whether requests
carry full history or references, or whether browser storage is involved.
Answering those questions requires frontend implementation evidence and
backend contracts, with synthetic test content.

| Context | Contract to verify | Failure prevented |
| --- | --- | --- |
| Account and workspace | Every operation is scoped to the authenticated principal and permitted workspace | Cross-account access |
| Project and asset | Authorization is checked whenever an asset is referenced or retrieved | Object-level authorization failure |
| Conversation | History and referenced objects remain scoped to their owner | Context leakage |
| Generation job | Submit, status, result, and cancellation use consistent ownership rules | Unauthorized job control |
| Concurrent editing | Explicit version or conflict policy | Lost updates and stale UI state |
| Repeated submission | Defined deduplication and billing semantics | Duplicate jobs or charges |
| Stream | Completion and failure have explicit application semantics | HTTP 200 mistaken for successful generation |
| Media retrieval | Access policy covers full objects and range reads | Partial-content authorization gaps |

Temporal adjacency in a HAR is not proof of causality. Strong dependency
evidence needs request initiators, redacted correlation matches, or backend
trace spans. Retrieving a video also does not prove it was generated during
the capture; it may already have existed.

## Lifecycle and recovery coverage

These are test requirements, not verified Flow behavior.

| Condition | Required behavior to assess |
| --- | --- |
| Reload or tab closure | Recover persisted job state without submitting duplicate work |
| Multiple tabs | Avoid account/context mix-ups and duplicate mutations |
| Account switch | Clear or rescope cached state and in-flight UI results |
| Logout during generation | Define whether server work continues; protect subsequent status and result access |
| Session expiry during polling | Require appropriate reauthentication while retaining the job reference securely |
| Submit times out | Distinguish unknown submission outcome from confirmed rejection before retrying |
| Stream disconnects | Reconcile terminal state; do not infer cancellation or success from disconnect alone |
| Out-of-order responses | Prevent stale responses from overwriting newer state |
| Rate limit or service outage | Bounded retries with backoff; preserve error categories |
| Cancellation race | Define completion-versus-cancellation outcome and any charge implications |
| Media URL expires | Reauthorize retrieval through supported application behavior |
| Permission removed | Apply the new permission state to subsequent operations and downloads |
| Frontend deployment changes | Fail incompatibilities explicitly; do not silently reinterpret payloads |

## MITM exposure assessment

The HAR does not establish an exploitable MITM vulnerability or a numerical
probability of session compromise. Seeing decrypted traffic in DevTools is
normal: the browser is a TLS endpoint. It does not show that an on-path attacker
could decrypt the connection.

Assessment must distinguish a network attacker without a trusted certificate
from a locally trusted inspection proxy or a compromised endpoint. Review the
client trust configuration, upstream certificate validation, credential
exposure in logs, session revocation, and any sender-binding controls using
isolated infrastructure and synthetic accounts. A trusted inspection root
changes the threat model; that is different from defeating TLS cryptography.

Do not infer either replay resistance or replay feasibility from the absence
of captured cookies. The credential's lifetime, binding, and backend checks
remain unverified.

## Conclusions and remaining work

Verified from the previous analysis: two application transport families,
initial authentication failures followed by success, poll-like timing, and
separate media delivery. Unverified: token lifetime/rotation, cookie properties,
revocation, context ownership, exact job dependencies, and replay resistance.

A complete internal review needs server-side authorization and lifecycle
contracts, redacted traces with synthetic data, and tests for the conditions
above. This document makes no claim that any listed missing evidence is itself
a Google vulnerability.
