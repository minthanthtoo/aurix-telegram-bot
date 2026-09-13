# AuriX AI UI/UX implementation plan

Planning baseline: 2026-09-10. Status: first implementation slice applied locally; deployment has not been performed.

## Product direction

Build one recognizable AuriX AI workspace with three destinations: Assistant, API console, and Guide. Within Assistant, offer English assistant, Lisu assistant, and Translator as distinct experiences. Keep Telegram identity consistent across destinations. Make everyday conversation inviting through useful starter prompts, saved work, clear responses, and easy follow-up actions.

Preserve the current dark navy/mint identity. Reduce oversized introductions after sign-in, use restrained surfaces and clear typography, and put the current task within the first viewport. Optimize for mobile reading, slow connections, and repeated use. Success means users complete their task with fewer interruptions and can recover their work.

This plan builds on [the reference study](AURIX_UI_UX_BENCHMARK_2026-09-09.md), with the source corrections below. Reference patterns guide design choices; they are not proof that AuriX behavior has been tested.

## Current implementation status

Applied in this slice:

- Shared browser session utilities with focus/visibility checks, cross-tab session signals, Telegram widget restoration after expiry, and per-user draft storage.
- Assistant starters, mode descriptions, explicit English → Lisu/Lisu → English direction, New conversation, context-preserving turn rendering, and submitted-model/mode logging.
- Translator result workspace with Copy result, Use as source, per-answer Copy, and a clear source/result relationship.
- Shared Developer guide navigation, safe Markdown links, scroll-aware active section, and improved search status.
- Admin Recent activity view backed by the existing prompt-free usage endpoint.
- Server-side validation and prompt instruction for the new native translator direction field; the existing external `/v1` contract remains unchanged.

Still required for the complete plan:

- Server-owned conversation/turn/attempt storage and reconnectable streaming jobs.
- A full side-by-side source/result translator panel with frozen source snapshots, review state, and updated-text handling.
- Request cancellation, streaming UI, account detail drill-down, scoped activity filters, and audited key rotation.

The remaining items are intentionally separate work because they require additive storage and job lifecycle design; the current slice does not describe them as complete.

## Verified starting point and corrections

| Component | Existing code | Implementation implication |
|---|---|---|
| UI | Plain HTML/CSS/JavaScript under web/ai-app; no frontend package.json found | Keep native modules and the existing serving/build arrangement initially. |
| HTTP server | aurix_ai/web_api.py; ThreadingHTTPServer and explicit route dispatch | Add narrowly scoped browser routes; account for bounded workers and active streams. |
| Sessions | AISessionStore supports SQLite persistence; default configured lifetime is 30 days | Fix client renewal/recovery, and align cookie and server expiry before changing lifetime. |
| Login recovery | Successful setAuthenticated(user) removes the widget; showAuthRequired() does not restore it | Recreate the widget on expiry. setAuthenticated(null) itself does not clear the widget. |
| Turn rendering | createTurn() nests response beneath its user message | Preserve this invariant and add durable IDs; do not rewrite into completion-order appends. |
| Mode selection | Clears the working conversation array and keeps four messages for handoff | Visible transcript remains. Make context boundaries explicit and preserve separate mode histories. |
| Model selection | Change events compare with the preceding selection | Compare with the last submitted selection to fix A → B → A without submission. |
| Browser inference | /api/chat returns JSON; client has no streaming/cancellation | Streaming requires an authenticated browser transport and job lifecycle, not only UI controls. |
| External inference | /v1/chat/completions and openai_chat_stream already exist | Reuse transport/normalization internally without putting a partner key in the browser. |
| Usage | APIKeyStore has usage_summary, usage_events, token fields, account/user/conversation attribution, and 9router export | Surface existing reporting first; add only missing filtering, pagination, timing, and audit fields. |
| Guide | Canonical Markdown rendered by api-guide.js; copy actions and section filter exist | Improve navigation/rendering without duplicating the documentation source. |
| Deployment | Dockerfile copies aurix_ai and web/ai-app and runs python -m aurix_ai | Keep module startup; verify new assets and persistent storage are included. |
| Tests | test_ai_router.py and test_ai_api_keys.py exist | Extend these for router/account compatibility and add focused session/storage/browser coverage. |

The reference report's statements that mode changes clear the visible transcript and that setAuthenticated(null) removes the widget are inaccurate. This table supersedes them. Its claim that operator usage needs a new backend is also too broad: the existing report returns account summaries and request events.

## Navigation and visual specification

Use the same brand position, navigation height, spacing, active state, and account menu everywhere. Label the ordinary destination “API console”; show operator actions only to authorized administrators. Do not imply that ordinary Telegram users have self-service API access unless the backend authorizes it. Unauthorized users receive a clear access explanation while Assistant and Guide remain reachable.

At desktop widths, Assistant has a collapsible history sidebar and a readable central column, approximately 720–800px wide. The composer sits at the bottom of the workspace. At mobile widths, history moves into a drawer, model selection into a labeled sheet, and the composer stays above the software keyboard. The guide uses a wider reading layout; console tables use the available width. Shared shell does not require identical content widths.

Start with 16px body text and generous line spacing tested with Fraser glyphs. Use a small spacing scale (4, 8, 12, 16, 24, 32px), one accent for primary actions, muted metadata, and separate error/success treatments. Use text labels with icons, visible keyboard focus, adequate contrast, and approximately 44px touch targets. Verify at 320, 390, 768, and 1280px plus 200% zoom. These are proposed acceptance targets, not measurements already passed.

Engagement comes from three contextual starters per empty mode, quick continuation of recent work, Copy/Translate/Explain actions, and clear progress. Avoid rotating promotions or repeated warnings. Respect reduced motion. Preserve the existing experimental Lisu notice with details on demand.

## Delivery sequence

### 1. Shared shell and reliable session recovery

Files: index.html, admin.html, api-guide.html, styles.css, api-guide.css; new shared/session.js and shared/shell.js under web/ai-app. Server changes remain in the existing session boundary in aurix_ai/web_api.py.

Create an explicit client session state: checking, authenticated, signed_out, unavailable. A network failure must preserve known identity while explaining that connectivity is unavailable; only a verified invalid session signs the client out. Deduplicate checks and ignore stale responses after logout or a newer sign-in. Revalidate on focus/visibility return with a short throttle, on a same-origin login/logout signal, and after a 401. Broadcast only an event, never credentials. Other tabs verify with the server.

Restore the Telegram widget after session expiry and keep a Retry sign-in action for widget/network failure. Collapse the access panel into the account menu after authentication. Keep the draft during same-user reauthentication, but never show another user's draft after account switching. Prefer session-scoped draft storage initially; durable transcripts belong in ownership-scoped server storage.

Audit cookie renewal through /api/session against server renewal in AISessionStore.get(). Ensure revoked sessions cannot be revived by delayed client checks. Keep the current lifetime initially, document its idle/absolute behavior, and verify service-restart persistence. Changing duration alone would not solve the reported refresh problem.

Acceptance: login in one tab is reflected in another on focus; a chat 401 restores sign-in without reload; offline does not appear as logout; drafts survive same-user recovery; account switching does not reveal another user's content.

### 2. Assistant layout and turn-state correctness

Split app.js into focused modules for workspace state, turn rendering, composer, model/mode controls, and transport. Extract along the existing features, keeping a small entry file. No framework migration is needed for this phase.

Capture mode/model when submitting. Compare changes with the last submitted turn. Render a compact mode badge, then a muted model-change line immediately above the governed user message. Every result retains its own model label even after controls change. Selecting A → B → A without submission produces no event.

Keep one active generation per conversation initially; users may edit the next draft and choose future settings. Display why Send is unavailable. Do not offer queueing until queue semantics are implemented. Add Copy and Translate to completed answers and retain submitted text when a request fails. Retry creates a new attempt under the same user turn. Autoscroll only near the bottom; otherwise show “New response.”

Acceptance: no false selection logs; old responses retain old models; failed turns keep their input; keyboard/IME submission works; scrolling upward is not interrupted. Existing nested response placement must remain intact.

### 3. Dedicated translator

Add a translator module using the shared shell, session, and model catalog. Show Source and Translation panels side by side on desktop and stacked on mobile. Direction is explicit: English → Lisu or Lisu → English; Auto is a later convenience. Remember the user's explicit choice. Swap changes the direction without overwriting source; “Use translation as source” performs replacement separately.

Submit a frozen source snapshot. If the user edits while waiting, mark the existing result as belonging to the previous submission and offer Translate updated text. Translate opens with an exact selected assistant answer, a visible preview, and requires submission. Explain opens an assistant branch with source/result attached; it must not change future translation semantics.

Add optional direction to the authenticated native browser request contract and select a versioned server-owned prompt in aurix_ai/router.py. Preserve old automatic behavior when the field is omitted; do not invent a direction field for the existing standard external endpoint. Keep translator context independent by default. Buffer translation until completion, then show completed, needs clarification, failed, or incomplete. A script check only detects formatting concerns.

Acceptance: questions translate as questions; Lisu input requests English output; source edits do not relabel old results; names/numbers survive deterministic request construction; mixed input follows explicit direction. Native-speaker evaluation remains a separate language-quality gate.

### 4. Durable conversations and streaming

Add AI-owned storage and application modules under aurix_ai, for example conversations.py and jobs.py. Use additive migrations following the repository's existing migration patterns. Reuse the configured database backend and connection strategy; verify both supported storage modes. Keep AI conversation data separate from VPN entitlement/commerce records.

Minimum records: conversation (owner, title, timestamps); turn (conversation, sequence, mode, direction, submitted source, context revision); attempt (turn, model, status, output, request ID, nullable usage, timestamps); optional summary (conversation, mode, through-sequence, version). Derive owner from the Telegram session on every operation.

Proposed browser routes are /api/conversations, conversation detail, turn submission, attempt events, cancellation, and conversation deletion. They are new internal application routes, not changes to the stateless /v1 contract. Add the required HTTP verbs deliberately; the handler currently exposes GET, POST, and HEAD.

Use a bounded job executor with durable state. Return turn/attempt IDs before generation. Reconnecting reads existing state and never starts another inference call. Periodically persist output; on restart, mark abandoned running attempts interrupted. Enforce one active job per conversation plus user/global concurrency limits. Deduplicate submissions by a client UUID scoped to owner and conversation.

Reuse router streaming through a cookie-authenticated browser route with existing server authorization/rate policy. Never route browser traffic using an embedded partner key. Persist final usage once; preserve upstream request IDs. Cancellation changes local state conditionally and closes the upstream stream when possible; it cannot promise to reverse already consumed provider usage. Late completion cannot overwrite cancelled or deleted work.

Keep full transcript separate from bounded model context. Preserve complete user/assistant and tool-call groups. Add summarization only after rolling context and restoration work; summaries are versioned, lossy context. Model comparison comes after this foundation: sibling attempts share a frozen context snapshot, and the user selects which answer continues the conversation.

Acceptance: refresh restores history; two users cannot access each other's records; duplicate submit creates one job; retry creates a new attempt; cancellation survives late completion; restart never silently resends; split Unicode/SSE events produce correct text; partial output remains visibly incomplete.

### 5. API console from existing usage records

Build account list/detail and Usage/Activity views using admin_usage_report and APIKeyStore. Preserve existing account IDs, token hashes, scope checks, request attribution, and usage_9router_events. Keep 9Router source unchanged.

Show input tokens, output tokens, requests, failures, and usage coverage for a selected date range. Missing usage is unknown; aggregate known tokens with a coverage label. Distinguish requests/minute limits from cumulative consumption; do not label recorded token totals as a remaining balance or enforced monthly quota.

Expose key prefix, label, status, expiry, created and last-used dates already available. Default to active inventory with a discoverable revoked view. Extend server filtering/pagination for model, endpoint, status, key, and account where absent. Do not calculate account totals from a limited page of events. Add latency fields only after recording them; historical rows show unavailable.

Rotation first uses the existing issue-new-key and revoke-old-key operations: show that both remain active until revocation. Scheduled overlap needs additional backend scheduling and is deferred. Add audit records for administrator mutations with actor, target, timestamp, outcome, and request correlation. Secret values never enter audit records.

Acceptance: displayed totals match existing summaries; paginated requests do not change totals; export shape remains compatible; revocation works as before; unknown usage remains distinguishable from zero; an ordinary user cannot invoke operator routes.

### 6. Developer guide and final product pass

Keep docs/AURIX_EXTERNAL_API.md as the canonical integration text. Add a compact first-request entry, human-readable task navigation, stable semantic anchors with aliases for existing section-N links, and scoped search with snippets. Render safe Markdown links and tables without enabling raw HTML. Put copy confirmation beside each control and preserve raw-text/copy-full-guide actions.

Offer cURL, JavaScript, and Python examples for equivalent requests, label placeholders, and validate examples against the current contract. Keep standard stateless API instructions separate from optional consuming-site conversation storage. Link to existing OpenAPI. A first successful request and a full assistant implementation are separate guide paths.

Acceptance: existing bookmarks resolve; browser Back works; search leads to a match; links render safely; copy failure offers usable fallback; a developer can discover key setup, models, request, response, and errors in one short path.

## Verification and release

For each slice, run focused Python tests for affected router/session/account/storage behavior. Add browser coverage for the state transitions above using deterministic fake responses in a local test harness, then a staging smoke with real Telegram login and controlled model calls. Do not infer live language quality from mocked tests.

Before publishing, inspect final diffs against the dirty worktree, verify Docker asset inclusion and persistent volume paths, test the existing external chat/streaming contract and account export, and capture desktop/mobile screenshots for the same scenarios. Use a documented previous-image/static-asset rollback and additive database changes compatible with rollback. Deploy slices independently where practical; keep the existing chat path available until the new transport passes acceptance.

Measure task completion with content-free events: login recovery success, first submission, time to first visible response, failed/retried attempts, conversation restoration, translation copy, and quickstart completion. Establish a baseline before promising improvements. No prompts, translations, secrets, or Telegram names are needed in these events.

Completion means usable flows demonstrated in staging, regression checks passed, and limitations documented. A visual mockup alone does not complete sessions, streaming, persistence, quotas, or Lisu validation.
