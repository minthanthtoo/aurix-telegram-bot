# AuriX AI UI/UX review and reference study

**Review date:** 2026-09-09–2026-09-10  
**Scope:** AuriX AI assistant, Telegram sign-in, admin/API-key console, integration guide, and English/Lisu translation.  
**Purpose:** Give product and engineering a defensible design direction for the next release.

Implementation note: this is the pre-implementation evidence study. The first implementation slice is tracked separately in [AURIX_UI_UX_IMPLEMENTATION_PLAN.md](AURIX_UI_UX_IMPLEMENTATION_PLAN.md); observations below describe the reviewed baseline and should not be read as a post-deployment status report.

## Executive decision

AuriX has a coherent visual foundation and a clear product idea, but the three pages currently feel like adjacent pages of a small tool rather than one dependable AI workspace. The highest-value work is interaction architecture, not more gradients, cards, or model labels.

The next release should make one journey reliable:

1. Sign in once and remain signed in.
2. Choose an assistant or translator experience with the right layout.
3. Submit a turn whose mode, model, direction, and status remain attached to that turn.
4. Recover from expiry, slow responses, errors, and refresh without losing the draft.
5. Let operators understand which site/key used the service and what happened.
6. Let a developer reach one verified request without reading the whole guide.

Recommended product shape:

~~~
Public assistant
  ├─ Assistant workspace: history, new chat, model, streaming, retry
  └─ Translator workspace: source/result, direction, copy, review state

Private operator console
  ├─ Overview: accounts, active keys, requests, tokens, failures
  ├─ Accounts: key lifecycle, quota, usage, activity
  └─ Request detail: request ID, model, outcome, latency, usage

Developer portal
  ├─ Quickstart: one verified request
  ├─ API reference: contract and capability matrix
  └─ Product recipes: assistant, translator, streaming, tools, media
~~~

## Bottom-line judgment

### P0 — fix before calling the experience dependable

- **Session recovery is incomplete.** loadAuth() runs once at startup. Successful sign-in removes the Telegram widget. On a later 401, showAuthRequired() changes the copy but does not restore the widget. A user can reach “sign in again” with no sign-in control until a reload. This is a source-backed finding; the expiry flow still needs live reproduction.
- **Assistant and translator are the same transcript surface.** A translator needs visible source, result, direction, and copy/review actions. “English ↔ Lisu translator” is currently a label, not a complete translation interaction.
- **Turn ownership needs to be visual and durable.** A late model response must render inside the user turn that created it. The current client updates a turn object, but it has no durable conversation IDs or reconnectable attempt model.
- **The first viewport is too introduction-heavy.** At 1280×720, the large hero and sign-in card consume the opening area; the conversation controls begin near the bottom edge. Once authenticated, the primary task should move upward.

### P1 — fix in the next product iteration

- Add persistent conversations, New conversation, resume, rename, delete, and a mobile history drawer.
- Add explicit translation direction: English → Lisu, Lisu → English, and optional Auto.
- Add pending, streaming, stop, partial, retry, rate-limit, and expired-session states. Preserve drafts in recoverable failures.
- Replace the two large select controls with a compact mode switch plus a secondary model control. A mode changes the product contract; a model changes inference choice.
- Give the admin page an account detail view and request activity. A card list cannot answer which partner used a key, when, on which model, or why it failed.
- Make the guide task-first: “make your first request” before the long feature inventory, with cURL, JavaScript, and Python variants.

### P2 — improve after the foundation

- Add model comparison as sibling attempts against one frozen prompt/context snapshot.
- Add a verified safe request explorer, API changelog, and capability health checks.
- Add optional light theme and a translation-review workflow only after Lisu quality is evaluated by native speakers.

## Evidence rules

This report separates four kinds of statements:

| Label | Meaning |
|---|---|
| **Observed** | Directly inspected in a live page or local source during this review. |
| **Documented** | Stated by official documentation or an official screenshot. |
| **Judgment** | Product interpretation based on that evidence. |
| **Recommendation** | A proposed AuriX change, not a claim about another product. |

No score is presented as an objective industry measurement. Earlier numeric scores were too precise for the available evidence and are intentionally removed.

## AuriX surfaces inspected

### Assistant home — observed

URL: [ai.aurix-mart.tech](https://ai.aurix-mart.tech/)

At desktop width 1280×720 the page showed:

- AuriX branding and shared navigation with Assistant, Admin console, and API guide.
- A large headline, “Useful answers, in your language.”
- A large Private access / Sign in with Telegram panel.
- A conversation panel containing Mode, AI model, a disabled Message field, and Send.
- A persistent warning that Lisu quality should be reviewed by a Lisu speaker for official or sensitive communication.

The sign-in widget was visible and the unauthenticated state was clear. The cost is vertical: the first useful conversation action is pushed below the opening hero and access card. The panel explains privacy, but not what happens after sign-in, what is retained, or how the three modes differ.

### Admin console — observed

URL: [ai.aurix-mart.tech/admin](https://ai.aurix-mart.tech/admin)

The unauthenticated page showed the same AuriX header and Telegram bot login widget, with the operator-specific message Sign in to manage AI access. This is a good security boundary and a good first-time explanation.

The authenticated operator workflow was not claimed from the live page because it was protected. The local implementation does contain accounts, active/revoked key filtering, quota/request/token summary fields, create-key flow, one-time secret display, and revoke confirmation. It does not yet provide a full account-detail/activity workflow.

### Integration guide — observed

URL: [ai.aurix-mart.tech/docs/ai-api](https://ai.aurix-mart.tech/docs/ai-api)

The guide has a strong starting structure:

- a task-oriented hero: “Build an assistant. Speak your language.”;
- three entry cards for product blueprint, design, and backend connection;
- copy-full-guide and raw-text actions;
- searchable section navigation;
- 25 numbered sections covering standard chat, streaming, tools, images, embeddings, audio, context, Lisu prompts, UI, turn ordering, backend streaming, build order, and acceptance checks.

The guide is unusually explicit about ownership and safety: the external site owns its users and conversations, the partner key stays server-side, tools execute on the external site, and model output is not proof of Lisu accuracy. Its weakness is first-use ergonomics. A new developer must read a large architecture document before finding the smallest verified path.

### Code-backed observations

These are from the local source, not inferred from screenshots:

| Area | Evidence | Product consequence |
|---|---|---|
| Session bootstrap | web/ai-app/app.js: loadAuth() is called once at startup | Refresh/focus recovery is not a designed state. |
| Expired login | Successful sign-in removes the widget; showAuthRequired() does not restore it | A 401 can leave the user without a visible re-login action. |
| Conversation storage | conversation is a browser-memory array | Refresh loses the active transcript; there is no durable resume model. |
| Request lifecycle | activeRequests is a counter and requests use ordinary fetch | No client stop/cancel transport or reconnectable attempt ID. |
| Ordering | setTurnResponse() and addOrderedAssistantEntry() update the turn object | Correct direction, but no persistent turn/attempt identity or comparison layout. |
| Mode switch | Mode change resets working context and retains a short handoff; visible turns remain | Context and visible history diverge; the boundary needs explanation. |
| Guide navigation | syncNav() responds to hashchange, not scroll position | Active section can become stale while reading. |
| Guide search | Search hides/shows whole nav links based on section text | No snippets, highlights, result focus, or document-level search. |
| Guide renderer | inline() supports backtick code and bold only | Links and richer reference content are not rendered as links. |
| Admin filtering | admin.js filters account status and account text | Useful inventory baseline; insufficient for key/model/request investigation. |

## Reference study: assistant products

This is not a ranking. The references cover chat workspace, artifact workflow, and model/tool breadth.

### 1. ChatGPT — live-inspected

Reference: [ChatGPT](https://chatgpt.com/)

The clean browser view showed a dark, quiet canvas with a narrow left rail, a prominent composer near the center, a Chat/Work switcher, temporary chat, and a single Add files and more menu. Opening that menu revealed files, library, image creation, web search, research, and connected tools while the composer remained primary. No prompt was submitted.

What AuriX should learn:

- Make the next action obvious: the composer is the visual center after authentication.
- Put history and New chat in stable navigation.
- Keep advanced capabilities behind one understandable affordance.
- Treat temporary/private conversation as a deliberate state.

Do not copy the entire tool marketplace or add upgrade/promotion surfaces that compete with focused translation.

### 2. Claude — region-blocked, official documentation used

References: [Claude Design](https://support.claude.com/en/articles/14604416-get-started-with-claude-design) and [Artifacts](https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them)

The live app redirected to [App unavailable in region](https://claude.com/app-unavailable-in-region). That is the only direct live observation; no current chat layout or response behavior is claimed. Official documentation is still useful for the pattern that an answer can become a separate, inspectable artifact instead of remaining only in a transcript.

What AuriX should learn:

- Separate durable work products from conversational text.
- Give an answer a clear “use this” boundary. For translation, that means result, copy, review, and use-as-source actions.
- Keep transcript and generated work product visually distinct.

### 3. Gemini — live-inspected

References: [Gemini](https://gemini.google.com/app) and [Canvas overview](https://gemini.google/mp/overview/canvas/?hl=en-GB)

The live desktop view showed a calm dark canvas, compact left icon rail, centered prompt composer, model picker adjacent to the composer, temporary chat, and an Upload & tools menu. The menu disclosed file upload, Drive, image/video creation, and other tools. No prompt was submitted.

What AuriX should learn:

- Keep model selection adjacent to the action it controls, but subordinate to the prompt.
- Use a narrow rail or drawer so history does not consume the central workspace.
- Make tool expansion deliberate.

### Assistant synthesis

The references agree on one structural lesson: composer and current work are primary; navigation and advanced controls are secondary. AuriX currently gives the hero and authentication card more weight than the work area. The right adaptation is a focused private assistant with a durable history drawer, not a copy of a general-purpose AI product.

## Reference study: translation products

Translation is a separate job from chat. The user must see source, direction, and result at the same time.

### 1. Google Translate — live-inspected

Reference: [Google Translate](https://translate.google.com/)

The desktop interface showed tabs for text, images, documents, and websites; a source panel and tinted result panel; language controls and swap; a character count; and result actions including copy, listen, rate, share, and save. A generic “Good morning.” input visibly moved through a translating state before result actions appeared. The linguistic correctness of the observed output was not evaluated.

What AuriX should learn:

- Keep source and result adjacent on desktop and stacked on mobile.
- Show selected direction above the text, not only in a mode name.
- Put copy and other result actions next to the completed result.
- Show an in-progress state without treating incomplete output as final.

The inspected page included source text in the browser URL. AuriX should avoid putting user translation text in URLs unless sharing is explicit.

### 2. DeepL — live-inspected

Reference: [DeepL Translator](https://www.deepl.com/en/translator)

The public page showed a large two-column workspace, source detection, a regional target such as English (American), text/file tabs, speech and API entry points, and a dictionary area. The language pair is a first-class choice rather than an incidental label.

What AuriX should learn:

- Make direction and language variant explicit.
- Keep the work surface larger than surrounding marketing content.
- Make text versus file translation a clear task choice.
- Reserve dictionary/glossary features for later; first make source/result and review reliable.

### 3. Microsoft Translator — live-inspected

Reference: [Bing Translator](https://www.bing.com/translator)

The page showed paired source/output panels, auto-detect, target language, swap, camera input, and a tone selector with Casual, Standard, and Formal options in the inspected state. The surrounding Bing header was visually dominant and is a useful warning about unrelated navigation competing with a focused task.

What AuriX should learn:

- Tone is meaningful only when the language pair and model support it.
- Camera/media input belongs behind an explicit control.
- Keep host-site marketing/navigation out of the translation workspace.

Availability of the exact tone options for every language pair was not tested.

### Translation synthesis

AuriX should use a dedicated translator layout rather than only changing a prompt behind a chat transcript:

~~~
Desktop:  [Source text........................]  ⇄  [Translation.......................]
           English → Lisu                         Lisu output · Copy · Review

Mobile:   Direction + swap
          Source
          Translate
          Translation + Copy + Use as source
~~~

Direction must be recorded on the submitted turn. Editing source after submission must not relabel an old result. Back-translation is diagnostic, not independent proof of correctness.

## Reference study: operator/API-key consoles

The useful pattern is scoped access plus observable usage. AuriX has the beginning of this in account/key cards; it needs the investigation layer.

### 1. OpenAI API Platform — official documentation and screenshot evidence

References: [Managing projects in the API platform](https://help.openai.com/en/articles/9186755) and [Developer quickstart](https://developers.openai.com/api/docs/quickstart)

Official project documentation describes projects as a scope for members, service accounts, API keys, limits, and usage, with organization and project roles. The official API-key screenshot shows a table with key name, masked secret, created date, last used, project access, creator, permissions, and row actions, plus a clear create-key action.

What AuriX should learn:

- Scope a partner integration to an account/site and environment.
- Show last-used and created metadata next to a masked key prefix.
- Put permissions and lifecycle actions in key detail.
- Separate secret reveal from normal inventory.

The live dashboard was not authenticated; the screenshot is official reference evidence, not a claim about the current live console.

### 2. Stripe Workbench — live docs plus official historical illustration

Reference: [Workbench overview](https://docs.stripe.com/workbench/overview)

The live documentation page uses a three-column developer layout: product navigation, focused prose, and on-page navigation. It exposes Ask AI, copy/LLM-friendly actions, and Markdown viewing near page content. The official Workbench illustration is explicitly a historical beta screenshot; it shows test/live scope, request and webhook charts, recent errors, logs, events, webhooks, API keys, and a read-only shell.

What AuriX should learn:

- Treat logs and recent failures as first-class operator work.
- Make environment/test scope visible.
- Place Ask/copy/machine-readable actions next to documentation content.
- Use charts only when they answer an operator question.

### 3. Twilio Console/IAM — official documentation and quickstart

References: [API keys overview](https://www.twilio.com/docs/iam/api-keys), [Create API keys in the Console](https://www.twilio.com/docs/iam/api-keys/keys-in-console), and [SMS developer quickstart](https://www.twilio.com/docs/messaging/quickstart)

Twilio’s official docs distinguish Main, Standard, and Restricted keys and explain individual keys for specific purposes or subsystems. Its console-management guide makes lifecycle concrete: choose region and type, set restricted permissions, copy the secret once, duplicate restricted keys when useful, update metadata/permissions, and delete/revoke unused or compromised keys. Its quickstart uses language tabs and copyable code.

What AuriX should learn:

- A key needs purpose, owner, scope, environment, and lifecycle state.
- Restricted capabilities are more useful than a generic active label.
- Language tabs reduce copy/paste errors.
- Destructive key actions need explicit confirmation and immediate-effect text.

The authenticated Twilio console was not inspected.

### Operator synthesis

~~~
Overview → site/account → key → request/activity
             │          │       ├─ status and failure
             │          │       ├─ model and endpoint
             │          │       ├─ latency and request ID
             │          │       └─ nullable token usage
             │          └─ rotate / overlap / revoke
             └─ quota, rate, owner, environment, last used
~~~

Do not expose raw secrets after creation. Do not treat user or conversation_id from an external site as authentication; they are attribution fields. The consuming site must enforce end-user identity and quotas.

## Reference study: developer portals and API guides

### 1. OpenAI Developers — live-inspected

Reference: [Developer quickstart](https://developers.openai.com/api/docs/quickstart)

The page has global developer navigation, a left API tree, an on-page outline, Copy Page, and a task-first quickstart. It begins with key creation, environment-variable setup, SDK installation, and a first call. Language switches are grouped with the exact code block they change.

Learn: put the smallest successful path first; keep copy actions beside their content; use language tabs to change executable examples.

### 2. Stripe Docs — live-inspected

References: [Stripe Docs](https://docs.stripe.com/) and [Workbench overview](https://docs.stripe.com/workbench/overview)

Stripe’s docs make search, product navigation, page outline, Ask AI, copy-for-LLM, and Markdown access visible. The prose area is restrained, with predictable reading rhythm and clear breadcrumbs.

Learn: make machine-readable content explicit; keep navigation stable while scrolling; make references actionable.

### 3. Twilio Docs — live-inspected

Reference: [SMS developer quickstart](https://www.twilio.com/docs/messaging/quickstart)

Twilio’s quickstart is a complete task: prerequisites, language tabs, signup/setup, first request, copyable code, and a production credential warning. The live page showed Python and Node.js tabs changing the installation block while the task remained stable.

Learn: expose implementation order; make prerequisites, one request, expected response, errors, and production checklist explicit.

### Portal synthesis

Keep the current 25-section document canonical, but add a shorter entry path:

1. Choose a product: standard chat, built-in AuriX mode, embeddings, or audio.
2. Run one request: copyable cURL with placeholder key and known model.
3. Read response: success, error, request ID, nullable usage.
4. Add your users/context: the external site owns login and history.
5. Enable optional features: streaming, tools, images, embeddings, audio.
6. Open the full reference: stable headings, searchable examples, OpenAPI link.

## Cross-page problems

### Identity and navigation do not yet form one workspace

In the reviewed baseline, Assistant and admin used shared navigation while the guide used a different header with only Back to admin. That created a hierarchy problem. The first implementation slice gives the guide direct Assistant, API console, and Developer guide navigation; this should be rechecked after deployment.

Recommendation: one shell everywhere with AuriX home, Assistant, Translator if promoted, Admin only when authorized, Guide, and account/session menu. Back should mean browser history or originating page; the shell should still offer Assistant and Guide directly.

### Authentication messaging is clear but recovery is not

The first visit explains Telegram privacy well. A production experience also needs session expiry detected on focus and before send, a visible re-login action that re-renders the widget, draft preservation across sign-in, and a distinction between ordinary AI access and admin authorization. The frontend must not depend on a full reload to restore the widget.

### Mode, model, and direction are different state

| State | Meaning | UI treatment |
|---|---|---|
| Mode | Changes product behavior/prompt | Primary segmented control or route tab |
| Direction | Changes translator contract | Prominent source/target control with swap |
| Model | Changes inference selection | Secondary menu with capability/experimental labels |
| Turn snapshot | What produced this answer | Immutable metadata on that turn |

Changing a control should not add a transcript event until a message is submitted. On submission, show captured model and mode on that user turn. A late response must update that turn, never append to the bottom because it finished last.

### “Experimental Lisu” is necessary but insufficient

The native-speaker warning is correct. Attach it to the Lisu experience with expandable explanation rather than repeating a large generic warning on every page. Record model ID, prompt version, direction, source, exact output, and human review state. Do not display fabricated confidence or call output verified because it contains Fraser-script Unicode.

## Recommended assistant design

### Desktop

~~~
┌────────────────────────────────────────────────────────────────────────────┐
│ AuriX AI     Assistant   Translator   Guide             account · status    │
├───────────────┬────────────────────────────────────────────────────────────┤
│ New chat      │ English assistant                         Gemini …          │
│ Search chats  │─────────────────────────────────────────────────────────────│
│ Recent        │ conversation transcript                                     │
│  title        │ user turn                                                    │
│  title        │ assistant answer · model · Copy · Translate · Retry          │
│               │                                                             │
│               │ composer                                      Send           │
└───────────────┴────────────────────────────────────────────────────────────┘
~~~

### Mobile

One transcript column; history in a drawer; mode as a labeled compact control; model in a bottom sheet; dynamic viewport and safe-area padding; no page-level horizontal scroll at 320px.

### Required states

| State | Visible behavior |
|---|---|
| Empty assistant | Short explanation plus three relevant starter prompts. |
| Pending | Submitted text remains; response placeholder and Stop appear. |
| Streaming | Text appends to that response; model metadata stays attached. |
| Completed | Copy, Translate, Retry, model, and optional usage appear. |
| Partial/cancelled | Partial output remains with explicit incomplete label. |
| Error | Plain explanation, preserved draft, retry when safe, request ID. |
| 429 | Retry-After/countdown, draft preserved, no duplicate submission. |
| Session expired | Re-login action renders immediately; draft survives. |
| Context trimmed | Small notice that older context was summarized/dropped. |

## Recommended translator design

Translator should be a first-class tab or route sharing the same authenticated session, not merely a different prompt behind a chat transcript.

Required behavior:

- English → Lisu, Lisu → English, and optional Auto.
- Swap changes direction without overwriting unsaved source.
- Source remains editable while output generates.
- Output is associated with the exact submitted source snapshot.
- Copy source and copy translation actions are adjacent.
- Back-translation and explanation are opt-in.
- Translate is explicit; Enter creates a newline in the multiline source field unless a clear shortcut is used.
- Ambiguous mixed-script input asks for direction rather than silently flipping it.
- Uncertain output is a review/clarification state, not a successful translation card.

Recommended consuming-site response shape:

~~~json
{
  "source": "How are you today?",
  "submitted_source": "How are you today?",
  "direction": "en_to_lisu",
  "translation": "...",
  "status": "complete",
  "model_id": "...",
  "prompt_version": "lisu-translation-v1",
  "request_id": "...",
  "review": "unreviewed"
}
~~~

HTTP success is not linguistic certification.

## Recommended operator console

### Overview

Show actionable metrics: active partner accounts, active/revoked keys, requests by date range, input/output/total tokens with unknown distinct from zero, error rate/recent 401/429/5xx, last activity, and refresh timestamp.

### Account list

Each row should show name, environment, owner, status, request rate, period usage, last request, and a detail link. The existing status/search filter is a good baseline; add environment/activity filters when the backend supports them.

### Account detail

- **Overview:** owner, status, quota/rate policy, dates, last used.
- **Keys:** masked prefix, label/purpose, created, last used, scopes, expiry, rotate, revoke.
- **Usage:** date/model/endpoint/user/conversation filters, requests, token totals, unknown usage.
- **Activity:** request ID, status, latency, model, endpoint, error category.

Key creation should support an overlap period for rotation. Secret is displayed once and never retrievable from the list. Revocation should state that authentication stops immediately and name the key.

### Request privacy

Store request ID, endpoint, model, status, timing, and nullable usage. Do not display full prompts, completions, images, audio, or Authorization values by default. Content inspection, if ever added, needs separate permission and retention rules.

## Recommended developer portal

The live guide should add an entry layer above the canonical reference:

~~~
Start here
  1. Base URL + partner key
  2. GET /v1/models
  3. POST /v1/chat/completions
  4. Expected response + request ID
  5. Your site owns users and conversations

Then choose
  Streaming · Tools · Images · Embeddings · Audio · Built-in Lisu modes
~~~

Recommendations:

- Add a quickstart block rather than forcing users to search 25 sections.
- Add JavaScript/Python/cURL tabs for the same request.
- Generate a capability matrix from /v1/models, but label discovery as metadata, not proof a live feature works.
- Add “tested on” dates and a “contract versus recommendation” label.
- Render references as real links.
- Add IntersectionObserver scrollspy while preserving hash navigation.
- Search should show title plus matching snippet and focus the first match.
- Put request-ID/error handling before optional media features.
- Keep real keys out of examples.

## Implementation order

### Release 1 — reliability and information architecture

1. Extract shared shell/session behavior.
2. Re-render Telegram widget after 401/logout/config failures; add focus/visibility refresh.
3. Preserve drafts per authenticated user and page purpose.
4. Add conversation IDs, durable turn IDs, attempt IDs, and ordered rendering.
5. Add pending/error/partial/retry/cancel states.
6. Move authenticated composer into the first viewport.

### Release 2 — assistant and translator jobs

1. Add explicit direction and source/result panels.
2. Add copy, use-as-source, and review status.
3. Keep translator history independent by default.
4. Keep mode/model/direction snapshots attached to each turn/attempt.
5. Test late responses, duplicate submission, cancellation, model switching, and refresh.

### Release 3 — operator visibility

1. Add account detail and key lifecycle metadata.
2. Add request activity and date/model/status filters.
3. Add rotation overlap and scoped permissions where supported.
4. Separate unknown token usage from zero.
5. Add audit events for create, reveal, rotate, and revoke.

### Release 4 — developer portal

1. Add quickstart and language tabs.
2. Improve links, scrollspy, snippets, and copy status placement.
3. Add tested standard-chat and streaming examples.
4. Add optional features only after live acceptance tests.

## Acceptance tests

### Session and navigation

- Assistant, admin, and guide in separate tabs reflect authentication consistently without repeated full reloads.
- On 401, sign-in reappears immediately and the draft remains.
- Logout removes access after the next same-origin session check.
- Guide navigation has Assistant, Admin, and Guide; Back does not strand the user in admin.

### Ordering and model comparison

- Submit A with model 1, change to model 2, submit B, let A finish last: A remains beside A.
- A → B → A without submitting creates no false change event.
- Model comparison uses one frozen input/context snapshot and sibling attempts.
- A cancelled attempt cannot be resurrected by a late completion.

### Translator

- English → Lisu preserves question/command meaning and does not answer the source question.
- Lisu → English returns English or honest clarification.
- Swap does not overwrite unsaved source.
- Editing source after submit does not relabel old output.
- Copy/retry/review do not lose source.
- Missing/uncertain translation is not styled as success.

### Context and transport

- Refresh/reconnect restores durable transcript.
- Context trimming removes complete turn/tool groups and reports the change.
- UTF-8 byte limits are measured on serialized requests; long Lisu input cannot silently hit a short-character cutoff.
- SSE parsing handles split events, split Unicode bytes, keepalives, usage-only events, [DONE], EOF, cancellation, and tool fragments.
- Unknown usage displays as unknown, not fabricated zero.

### Admin and guide

- Operator can find key by account, status, label, and prefix; see last use; rotate; revoke with confirmation.
- Activity filters by request ID, date, model, endpoint, status, account.
- Developer can find base URL, auth header, one request, response, and request-ID/error handling without reading optional sections.
- Copy status appears beside the copied block.

## Risks and deliberate non-goals

- Do not claim Lisu quality from a model name, script detection, back-translation, or successful HTTP.
- Do not make the partner key a browser credential or per-end-user authentication mechanism.
- Do not add full enterprise RBAC, billing, or project hierarchy before account/key/request investigation is useful.
- Do not add model comparison that duplicates inference cost without showing frozen input and model IDs.
- Do not store full prompts/responses in ordinary logs by default.
- Do not use screenshots or static mock responses as proof of streaming, audio, tools, embeddings, or translation quality.

## Source register

### AuriX

- [AuriX AI assistant](https://ai.aurix-mart.tech/) — live UI inspected 2026-09-09.
- [AuriX admin console](https://ai.aurix-mart.tech/admin) — unauthenticated live UI inspected 2026-09-09; authenticated workflow not claimed.
- [AuriX integration guide](https://ai.aurix-mart.tech/docs/ai-api) — live UI and guide inspected 2026-09-09.
- Local implementation: web/ai-app/app.js, web/ai-app/admin.js, web/ai-app/api-guide.js, and web/ai-app/styles.css — read-only source inspection.

### Assistant references

- [ChatGPT](https://chatgpt.com/) — live desktop UI inspected; no prompt submitted.
- [OpenAI Projects in ChatGPT](https://help.openai.com/en/articles/10169521-projects-in-chatgpt) — official documentation.
- [Claude Design](https://support.claude.com/en/articles/14604416-get-started-with-claude-design) — official documentation; live app redirected to regional unavailability.
- [Claude Artifacts](https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them) — official documentation.
- [Gemini](https://gemini.google.com/app) — live desktop UI inspected; no prompt submitted.
- [Gemini Canvas overview](https://gemini.google/mp/overview/canvas/?hl=en-GB) — official product page.

### Translation references

- [Google Translate](https://translate.google.com/) — live desktop UI and generic translation state inspected; linguistic correctness not evaluated.
- [DeepL Translator](https://www.deepl.com/en/translator) — live public UI inspected; no request submitted.
- [Bing Translator](https://www.bing.com/translator) — live public UI inspected; no request submitted.

### Operator/API-console references

- [OpenAI project management](https://help.openai.com/en/articles/9186755) — official documentation and API-key screenshot; live dashboard not authenticated.
- [OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart) — live public documentation inspected.
- [Stripe Workbench overview](https://docs.stripe.com/workbench/overview) — live public documentation inspected.
- [Stripe API keys](https://docs.stripe.com/keys) — official key-management documentation.
- [Twilio API keys overview](https://www.twilio.com/docs/iam/api-keys) — official key types/lifecycle documentation.
- [Twilio create API keys in Console](https://www.twilio.com/docs/iam/api-keys/keys-in-console) — official console workflow documentation.

### Developer-portal references

- [OpenAI Developers](https://developers.openai.com/) — official developer portal.
- [OpenAI quickstart](https://developers.openai.com/api/docs/quickstart) — live task-first quickstart inspected.
- [Stripe Docs](https://docs.stripe.com/) — official developer documentation.
- [Stripe Workbench overview](https://docs.stripe.com/workbench/overview) — live docs layout inspected.
- [Twilio SMS developer quickstart](https://www.twilio.com/docs/messaging/quickstart) — live quickstart inspected, including Python/Node.js tabs and copyable examples.
- [Twilio API overview](https://www.twilio.com/docs/usage/api) — official API documentation.

## Review conclusion

AuriX should keep its dark, restrained, private, language-aware identity but reorganize the product around durable work. The best near-term result is a smaller, more reliable product: a focused assistant workspace, a real source/result translator, an operator console that can answer usage questions, and a guide that gets a developer to one verified request quickly.

That direction is supported by direct inspection of AuriX and the reference products. It does not depend on claiming access to protected consoles, claiming unverified Lisu accuracy, or imitating unrelated enterprise features.
