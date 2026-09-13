# Payment-App Transaction Extraction Feasibility

Status: device-verified planning baseline, 2026-09-05  
Target device: Xiaomi 25062RN2DA, Android 15 (SDK 35), HyperOS 2.0  
Scope: KBZPay, WavePay, AYA Pay, uabpay, and CB Pay customer apps installed on the target device

## Conclusion and plan judgement

The earlier two-part architecture—an Android companion collector plus a Mac
controller—is the correct foundation. A Mac-only scrcpy/ADB script cannot
reliably receive future Android notification callbacks, and it cannot legally
or technically read these apps' private databases on this non-rooted retail
device.

The plan is viable after these corrections:

1. Treat notification *delivery* as available, but notification transaction
   fields as app/version-specific until real samples are captured.
2. Read shared receipt media through `MediaStore`; do not assume access to
   another app's private or `Android/data` directories.
3. Treat transaction-history extraction as a foreground, unlocked probe. A
   version-calibrated login PIN may be supplied at runtime for login only, but
   OTP, forced biometric, lockout, and recovery challenges require attention.
4. Prefer SMS notification capture through the notification listener. Direct
   SMS inbox access is restricted and is not part of the baseline.
5. Keep `detected`, `corroborated`, and `verified` as distinct states. No single
   OCR result or amount-only notification may approve an AuriX payment.
6. Make an official merchant feed the primary source wherever the provider
   offers one. Consumer-app observation is a resilient secondary signal, not
   the accounting system of record.

## Full-automation boundary

The normal monitoring loop can be fully unattended after one-time installation,
Android permission grants, and provider merchant onboarding. "No intervention
ever" is not technically honest: a bank may expire a session, require an OTP or
biometric, revoke credentials, force an app upgrade, or place the account under
review. Those states must raise `needs_attention`; the automation must never
attempt to bypass them.

| Tier | Sources | Normal operation | Authentication dependency | Maximum trust |
| --- | --- | --- | --- | --- |
| **A — passive device collector** | App/SMS notifications, `MediaStore`, receipt OCR | Fully unattended | One-time notification/media permission; no pay-app login needed | `detected` or `corroborated` |
| **B — session-bound reconciliation** | Read-only visible app history | Scheduled while phone is unlocked; the controller may enter a runtime login-only PIN on a recognized keypad | OTP, forced biometric, lockout, recovery, and UI-version drift require attention | `corroborated` |
| **C — provider merchant integration** | Official API, callback/webhook, merchant portal/app export | Fully unattended after business onboarding and credential setup | Provider-issued merchant credentials with rotation/recovery | `verified` when reference, recipient, amount, currency, status, and replay checks pass |

Tier C is the target for order approval. Tier A should run for every provider
because it provides fast alerts and a second signal. Tier B is optional and
must not be on the critical path: consumer UI and sessions change too often to
guarantee continuous operation.

## Evidence legend

| Mark | Meaning |
| --- | --- |
| **Yes** | Verified on this device or guaranteed by the cited Android API |
| **Conditional** | Technically possible, but content/security/app-version behavior requires a real sample |
| **No** | Unavailable on this device without bypassing the Android/app security boundary |

## Installed app baseline

| Provider | Installed package | Version | Target SDK | Notification permission |
| --- | --- | ---: | ---: | --- |
| KBZPay | `com.kbzbank.kpaycustomer` | 5.8.5 (242) | 35 | Granted |
| WavePay | `mm.com.wavemoney.wavepay` | 2.6.1 (1470) | 35 | Granted |
| AYA Pay | `com.ayaplus.subscriber` | 3.3.17 (153) | 36 | Granted |
| uabpay | `com.uab.uabbankpay` | 3.2.5 (145) | 36 | Granted |
| CB Pay | `com.cbbank.cbmbanking` | 1.34.1 (76) | 35 | Granted |

All five packages reject Android `run-as` because their production builds are
not debuggable. Their internal databases, preferences, tokens, WebView storage,
and private receipt files are therefore out of scope.

## Pay-app × extraction-source matrix

| Provider | Notification event | Amount/reference from notification | Visible history extraction | Dedicated shared receipt path | Receipt OCR | Direct app database | Unattended authentication |
| --- | --- | --- | --- | --- | --- | --- | --- |
| KBZPay | **Yes** | **Conditional**—capture English/Burmese samples | **Yes on 5.8.5**—deterministic structured list/detail replay | **Yes:** `Pictures/KBZ_Customer/` | **Conditional** | **No** | **Conditional login PIN only** |
| WavePay | **Yes** | **Conditional**—capture English/Burmese samples | **Yes on 2.6.1**—opaque WebView, versioned coordinate route, six-second load wait, list and incoming detail captured | **Yes:** `Pictures/WavePay/` | **Conditional** | **No** | **Conditional login only** |
| AYA Pay | **Yes** | **Conditional**—capture English/Burmese samples | **Conditional**—official app advertises transaction tracking | **Yes:** `DCIM/AYA Pay/` | **Conditional** | **No** | **No** |
| uabpay | **Yes** | **Conditional**—capture English/Burmese samples | **Yes on 3.2.5**—two wallets and one populated incoming detail captured; virtual-card route also captured | **Conditional:** no dedicated folder currently indexed; watch screenshots and user-selected exports | **Conditional** | **No** | **Conditional login PIN only** |
| CB Pay | **Yes** | **Conditional**—capture English/Burmese samples | **Yes on 1.34.1**—popup-safe login, two account cards, two history lists, and positive details captured | **Yes:** `Pictures/CBPay Transaction/` | **Conditional** | **No** | **Conditional guarded coordinate PIN** |

Here **Yes** means the collector can receive a notification *if that app posts
one*. It does not claim that every incoming transfer causes a notification;
that behavior remains subject to the provider, account role, channel settings,
network delivery, and installed app version.

The four dedicated paths above were obtained from `MediaStore` path metadata on
the target phone. They are observations of the installed versions, not
permanent provider contracts, so the collector must discover and configure
paths rather than hard-code them as its only source.

## On-device login and history smoke test

Tested 2026-09-05 through the bundled ADB/scrcpy connection. Navigation was
read-only. The user-authorized PIN was entered only into recognized login flows
for uabpay and CB Pay; it was never stored in artifacts or used on a transaction-capable
screen. No OTP, transfer, payment, refund, or confirmation action was entered or
triggered. The biometric prompt was canceled rather than satisfied or bypassed.
Personal names, balances, phone numbers, amounts, and transaction references
observed during the test are intentionally not recorded here.

| Provider | Session state | History result | Extraction result |
| --- | --- | --- | --- |
| KBZPay 5.8.5 | Authenticated | History list and transaction detail opened successfully | **Structured extraction verified.** The accessibility hierarchy exposes stable IDs for transaction type, time, signed amount, list/filter controls, and incoming/outgoing tabs. The detail screen visibly supplies success state, timestamp, provider reference, transaction type, counterparty, signed amount, remark, and receipt QR. |
| WavePay 2.6.1 | Authenticated | Home → `စာရင်း` opened after a six-second load; an incoming row and its detail opened | **Versioned coordinate/OCR extraction verified.** Accessibility exposes one opaque `WebView`, so reproducibility depends on the tested screen geometry and app version rather than semantic nodes. |
| AYA Pay 3.3.17 | Authenticated | Home-page History section reached; this account displayed `No Transaction History` | **Semantic route verified; row schema unverified.** Android exposes `History` and the empty-state text as accessibility nodes. A real incoming/outgoing row is needed to identify its fields and detail route. |
| uabpay 3.2.5 | Login automated with runtime PIN | Home, two wallets, one populated incoming detail, and one Virtual Card opened | **Structured Flutter extraction verified for the observed routes.** The flow cancels biometric, enters the PIN only on the complete semantic login keypad, enumerates expandable wallet rows, and visits the Cards tab. |
| CB Pay 1.34.1 | Login automated with version/geometry-guarded PIN grid | Launch popup dismissed before interaction; authenticated popup dismissed; two horizontally paged accounts, both histories, and positive details opened | **Structured extraction verified.** Generic pre-login swipes are disabled so promotional content cannot be opened accidentally. |

### Revised history feasibility

| Provider | Semantic Android UI | Screenshot/OCR | Current readiness |
| --- | --- | --- | --- |
| KBZPay | **Yes** | **Yes** | Deterministic list/detail replay and sanitized fixtures captured |
| WavePay | **No on tested screens**—opaque WebView | **Yes for visible capture** | Version-calibrated list/detail route works; recalibrate after geometry or app-version drift |
| AYA Pay | **Yes for section/empty state** | **Yes** | Needs a populated transaction fixture |
| uabpay | **Yes on tested login/home/wallet/card screens** | **Yes** | Two wallets and Virtual Card enumerated; only one wallet currently supplies a populated incoming sample |
| CB Pay | **Yes on login/dashboard/history/detail** | **Yes** | Two accounts enumerated; popup dismissal and custom-drawn login keypad are version/geometry guarded |

## Data available by source

### 1. Notification listener

Guaranteed when notification-listener access is enabled:

- source package;
- notification key, channel, posted time, update/removal event, and visibility;
- title, text, big text, subtext, and structured extras when the payment app
  places those values in the notification.

Not guaranteed:

- amount, sender, direction, balance, or provider reference;
- historical notifications that were already dismissed before the collector
  was enabled;
- text hidden by the provider, lock-screen privacy, or a custom notification UI.

Use notification content to create a transaction candidate. Mark it
`corroborated` only when it includes a stable provider reference or matches a
second independent source.

### 2. Shared gallery and exported receipts

Available through `MediaStore` after the user grants the required media access:

- media URI and relative path;
- MIME type, dimensions, byte size, creation/addition time, and file hash;
- the image bytes needed for local OCR or explicitly configured remote vision;
- owner-package metadata when Android exposes it.

OCR may propose:

- provider;
- incoming/outgoing direction;
- amount and currency;
- transaction/reference ID;
- sender/recipient;
- transaction timestamp and status.

OCR is evidence extraction, not proof. Preserve the existing AuriX checks for
duplicate image hash, duplicate provider reference, exact amount/currency,
recipient, timestamp freshness, status, and extraction confidence.

### 3. In-app transaction history

Possible only while the app is foregrounded and authenticated, either by an
existing session or a runtime login-only PIN on an explicitly recognized flow.
The probe may read the visible accessibility hierarchy and, where screen
capture is allowed, OCR a screenshot. It must stop when:

- the app asks for an OTP, password, pattern, forced biometric, recovery step,
  or a PIN outside the calibrated login-only context;
- Android/app secure-window policy blocks capture;
- no stable semantic selector is available;
- navigation would enter transfer, payment, refund, or account-setting flows.

The probe must use package-versioned semantic anchors, never fixed screen
coordinates as the primary method. Each app update invalidates the profile
until the smoke test passes again.

### 4. SMS

Baseline support is notification capture from the user's SMS application when
the SMS preview is visible. Direct `READ_SMS` access is excluded from the MVP:
it is highly sensitive, restricted for normal Play-distributed applications,
and unnecessary if payment-app or SMS notifications contain sufficient data.

### 5. Provider APIs

No merchant credentials or provider contracts are present in this repository,
so provider extraction is **not connected yet**. Current official evidence
supports the following integration plan:

| Provider | Strongest official route found | Automation judgement |
| --- | --- | --- |
| KBZPay | KBZPay Partner App/Merchant Portal and business integration/Mini App onboarding | Apply for merchant integration and request server-side payment status/callback documentation. Until granted, consume Partner App/Portal reports only as an authorized export; do not automate the customer wallet login. |
| Wave Money | Pay with Wave API integration, Merchant Portal, transaction history, and real-time payment notifications | **Best documented API candidate.** Onboard through the Partner/Developer Portal and use its signed server-to-server result as the primary source. |
| AYA Pay | Public API developer portal, Merchant Portal, Merchant App, and merchant business application | Apply as an AYA Pay merchant, register an API application, and request the production payment-status/callback contract. |
| uabpay | uabpay+ merchant/agent app with transaction details and settlement views; a merchant portal is publicly reachable | Onboard uabpay+ and request an official report/API channel. No public callback contract was located, so portal/app data is not assumed to be an API. |
| CB Pay | CB Merchant Portal/CB Merchant+ for real-time MMQR transaction viewing; CB eCommerce Gateway accepts CB Pay | Apply for CB MMQR or eCommerce acquiring and request an official machine-readable transaction feed. No public callback contract was located. |

Use only provider-issued documentation and credentials. Never discover private
endpoints by intercepting app traffic, extracting tokens, reading private app
storage, defeating TLS pinning, or automating a PIN/OTP/biometric challenge.

## Normalized event contract

Every collector emits the same immutable observation shape:

```text
event_id
device_id
provider
package_name
app_version
source                 notification | media | history_ui | sms_notification | manual
source_event_id
direction              incoming | outgoing | unknown
status                 successful | pending | failed | reversed | unknown
amount_minor
currency
provider_reference
sender
recipient
transaction_time
observed_at
evidence_uri
evidence_sha256
raw_payload_sha256
parser_version
confidence
flags[]
correlation_id
review_state           detected | corroborated | verified | rejected
```

Raw notification text and receipt images should be encrypted locally, retained
for a configured short period, and excluded from logs. Logs use hashes and
redacted field summaries only.

## Correlation and trust rules

Deduplicate in this order:

1. exact `(provider, normalized_provider_reference)`;
2. exact receipt/evidence SHA-256;
3. fallback `(provider, direction, amount, currency, time-window, account)`.

Fallback correlation must never create a verified payment because two customers
can transfer the same amount near the same time.

| Evidence | Maximum automatic state |
| --- | --- |
| Package/time notification with no transaction fields | `detected` |
| Notification with amount but no stable reference | `detected` |
| Notification with stable reference and successful/incoming semantics | `corroborated` |
| Receipt OCR alone | `detected` |
| Matching notification + receipt/reference | `corroborated` |
| Matching visible history row + stable reference | `corroborated` |
| Existing AuriX admin verification or future official provider API | `verified` |

## Implemented production baseline and remaining order

1. Start merchant onboarding/API-document requests for all five providers;
   prioritize Wave, then AYA, because public developer channels are visible.
2. **Implemented:** Android notification and `MediaStore` collectors for all
   five packages, with no app-control or SMS permission.
3. Record redacted fixtures from owned test accounts: incoming success,
   outgoing success, pending/failed/reversed where possible, duplicate
   notification update, and saved receipt in English and Burmese.
4. Implement package/version-specific parsers against those fixtures.
5. **Implemented:** read-only foreground history profiles with authentication,
   version, geometry, and selector circuit breakers.
6. Add provider adapters behind one idempotent interface: signed callback or
   status poll, cursor/checkpoint, retry with backoff, replay protection, and a
   reconciliation job against merchant reports.
7. **Implemented:** encrypted observation ingestion into a separate local
   SQLite database, idempotent source-event deduplication, source correlation,
   and optional unique open-order suggestions for a supplied local SQLite
   commerce database. The installed runtime has no commerce database attached,
   so it currently records observations only. A review UI remains optional work.
8. Integrate with AuriX as a read-only admin evidence source. Permit automatic
   order approval only for Tier-C events that pass exact order reference,
   recipient, amount, currency, successful/final status, freshness, signature,
   and replay checks. Everything else remains an alert or review candidate.

## Deterministic capture harness

The repository now contains a no-LLM ADB capture harness in `pay_monitor/`.
It does not store the login PIN in repository files, raw screenshots, notification text, names,
phone numbers, balances, transaction amounts, or references. Runtime secrets
are read from an environment variable for an interactive run or macOS Keychain
for the scheduled run, and journaled only as `<secret>` plus the digit count.

The harness produces this provider-local layout under `var/pay-monitor/`:

```text
<provider>/history_list_sample.json
<provider>/transaction_sample.json
<provider>/wallet_00.json ...
<provider>/screen_flow.jsonl
noti.json
receipt_dir.json
```

Each touch journal entry contains the semantic selector, absolute device
coordinate, normalized coordinate, purpose, app version, and time. CB Pay
dismisses `btn_close` promotions before interaction, then uses bounded account
card swipes and stops when the account fingerprint repeats. uabpay uses its
Wallets/Cards tabs and expandable wallet rows. Semantic
bounds are preferred; WavePay's opaque WebView uses explicit normalized
coordinates calibrated to version 2.6.1 and the tested 1080×2340 screen.

Before accessibility traversal, KBZPay and CB Pay also use an in-memory visual
fast path for popup close controls. A tap is permitted only when the tested app
version and 1080×2340 geometry match and image analysis finds both dark diagonals
of an X inside a white circle in the top-right popup zone. A plain white circle
is rejected. The screenshot is discarded without being written to disk. A
process lock prevents scheduled and interactive foreground probes from touching
the phone concurrently.

CB Pay calibration rewinds the carousel to its first card, maps every SHA-256
account fingerprint to a zero-based horizontal index, and stores the map in
`cbpay/account_index.json` without the raw account IDs. A selective check reads
the currently visible fingerprint, computes the signed index delta, performs
only that many swipes, and verifies the destination fingerprint before opening
history. Select by index or an unambiguous hash prefix:

```bash
python -m pay_monitor.cli probe --provider cbpay --account-index 1
python -m pay_monitor.cli probe --provider cbpay --account-hash 17450315085c
```

On the tested two-account device, index 1 to index 0 required one swipe and the
complete selective history/detail check finished in 27.9 seconds. An unknown,
duplicated, reordered, added, or removed fingerprint blocks selective access and
requires a full recalibration instead of guessing the card.

Run a read-only capture after exactly one authorized phone reconnects:

```bash
PAY_MONITOR_PIN='<from-secure-secret-store>' \
python -m pay_monitor.cli probe --provider all
python -m pay_monitor.cli passive
```

The probe only enters a PIN when the current hierarchy contains a recognized
login/unlock context and exposes a semantic numeric keypad. It blocks controls
named send, transfer, pay, confirm, refund, cash out, withdraw, request payment,
or scan. A provider with an unknown keypad, missing history selector,
unrecognized authentication challenge, or changed version reports `blocked`.
The only opaque WebView exception is a package/version/geometry-specific
coordinate profile recorded during an observed calibration run.

This ADB harness is the reproducibility/calibration layer, not the final
sub-minute monitor. The production path remains an Android notification listener
and `MediaStore` observer, which commit events locally at arrival; periodic app
history is reconciliation only. The target service-level objective is event
capture within 10 seconds and correlated AuriX evidence within 60 seconds.

Launch the bundled scrcpy directly, without the GUI terminal pause:

```bash
python scripts/scrcpy_phone.py
python scripts/scrcpy_phone.py --wifi
```

The launcher prefers a connected USB device, can discover its current `wlan0`
address and enable ADB TCP port 5555, and always passes an explicit serial to
scrcpy so duplicate USB/TCP entries for the same phone are not ambiguous.

### Live deterministic-capture result

Retested on 2026-09-05 using USB serial `1376bcb7`; the same physical phone was
also visible at `192.168.1.2:5555`. The direct launcher was verified on both
transports and did not use the terminal wrapper that prints `Press Enter`.

| Provider | `history_list_sample` | `transaction_sample` | Notification presence | Receipt-path result | Current deterministic blocker |
| --- | --- | --- | --- | --- | --- |
| KBZPay | **Captured** through deterministic semantic route | **Captured** with structured status/amount/detail fields | Present | `Pictures/KBZ_Customer/` populated | Needs real incoming notification fixture for parser validation |
| WavePay | **Captured** after Home → `စာရင်း` and six-second wait | **Captured** from a visible incoming row | Present | `Pictures/WavePay/` populated | Opaque WebView requires OCR and recalibration after version/geometry drift |
| AYA Pay | **Captured**, including launch-overlay dismissal and vertical-search swipes | Not present because the account showed no rows | Present | `DCIM/AYA Pay/` populated | A populated owned-account fixture is required |
| uabpay | **Captured** for two wallet rows | **Captured** for the one wallet with a visible incoming row; Virtual Card detail also captured | Present | No dedicated directory; generic screenshot/download paths are noisy | Second wallet has no populated incoming fixture; forced OTP/biometric or version drift remains a stop condition |
| CB Pay | **Captured for two horizontal account cards** | **Captured for one positive row on each account** | Present | `Pictures/CBPay Transaction/` populated | Custom PIN grid and popup handling must be recalibrated after version/geometry drift |

The redacted calibration samples store only package/field or path/metadata
presence. The installed collector may send raw notification text or receipt
bytes only inside authenticated encrypted envelopes; plaintext is not written
to the Mac database or logs. These results are calibration evidence, not claims
of complete provider coverage.

## Installed monitoring runtime

`com.aurix.paymonitor` is installed on the target phone with notification-listener
and image-media permissions. It observes only the five package names and four
dedicated receipt paths listed above. Events are encrypted on the phone using a
fresh AES-256-GCM key per event; that key is wrapped by the Mac host's RSA-OAEP
public key. Failed deliveries remain encrypted in the app-private outbox.

The Mac receiver binds to loopback only and is reached through ADB reverse. It
decrypts in memory, validates and normalizes fields, stores the original encrypted
envelope plus normalized facts, deduplicates source IDs, and correlates references,
image hashes, or conservative amount/time buckets. It cannot update `payments`,
verify receipt evidence, or approve an AuriX order. Unique amount/provider matches
can be stored only as suggestions when a separate local commerce SQLite database
is explicitly configured. The installed LaunchAgent does not configure one and
cannot access the hosted PostgreSQL commerce database.

The sub-minute daily-workflow objective applies to passive notification and
receipt events: they are collected at arrival and queued immediately even while
the phone UI is elsewhere. A complete five-app foreground history sweep is a
slower reconciliation pass and is intentionally not placed on that latency path.
When an app forces OTP/biometric or its UI drifts, the passive monitor continues
and the foreground probe reports `needs_attention` instead of delaying alerts.

## History memory and server cursors

The monitor database keeps a privacy-preserving ledger for every history row it
has seen: provider, hashed account ID, hashed row fingerprint, optional hashed
provider reference, first/last-seen times, lookup count, and whether its detail
was already inspected. Raw row text is not part of the ledger. A repeated row is
updated as seen but its detail is not reopened; new rows are inspected and then
marked checked. This makes repeated daily checks incremental.

Semantic apps fingerprint the fields aligned with each list row. WavePay's
opaque WebView instead fingerprints a quantized crop around the first visible
transaction row, excluding its changing status bar and promotional areas.

Every new notification or receipt observation also creates one idempotent
reconciliation request. A server-side caller can request a bounded lookup with
the fields `provider`, optional `account_hash`, optional `since_time`, optional
`after_reference_hash` or `target_reference_hash`, and `max_items` (1–100).
Requests support `pending`, `running`, `completed`, and `blocked` states; a
running request abandoned for 15 minutes is safely returned to the queue.

The current receiver exposes this contract on loopback only:

```text
POST /v1/reconciliation-requests
GET  /v1/reconciliation-requests?status=pending&limit=20
GET  /v1/history-checkpoint?provider=cbpay&account_hash=<sha256>
```

The scheduled reconciler claims the oldest request first, targets only its
provider and CB account when supplied, and records the result. With no queued
request it checks one healthy provider per interval in round-robin order; CB
accounts also rotate by mapped index. This avoids repeatedly opening every app
and gives evidence-driven work priority. Provider-specific circuit breakers remain isolated. Exposing
this API beyond the Mac requires HTTPS plus authenticated, replay-protected
server credentials; the loopback service must not simply be opened to the LAN.

Two macOS LaunchAgents are installed:

- `com.aurix.pay-monitor-host` supervises Wi-Fi ADB reverse every ten seconds and
  drains the encrypted phone outbox;
- `com.aurix.pay-monitor-reconcile` performs a read-only all-provider history run
  every five minutes. Its login PIN comes from macOS Keychain, and three consecutive
  blocked runs for any provider activate a persistent `needs-attention.json`
  circuit breaker rather than retrying indefinitely into an account lockout.

## Acceptance gates

For every provider before enabling alerts:

- installed package/version is identified;
- notification permission and listener health are visible;
- one incoming transaction produces exactly one correlated candidate;
- outgoing, pending, failed, and reversed events are not labeled as received;
- repeated notification updates and duplicate receipt files do not duplicate a
  transaction;
- hidden notification content degrades to `detected`, not a guessed amount;
- locked or secure screens stop the history probe safely;
- no PIN, OTP, biometric, token, account database, or intercepted network data
  is collected;
- loss/reconnect of ADB does not lose already committed Android observations.

For a provider feed before enabling automatic approval:

- merchant identity and receiving account are bound in configuration;
- webhook signatures or equivalent authenticity checks are verified before
  parsing, with secrets excluded from logs;
- provider reference and merchant order reference are unique and replay-safe;
- only final successful incoming states approve, while pending, failed,
  reversed, refunded, and chargeback states never do;
- callback retries are idempotent and a scheduled report/status reconciliation
  repairs missed delivery;
- credential expiry, provider downtime, schema drift, and reconciliation gaps
  produce `needs_attention`, never a guessed success.

## Primary platform references

- Android `NotificationListenerService`:
  <https://developer.android.com/reference/android/service/notification/NotificationListenerService>
- Android `MediaStore`:
  <https://developer.android.com/reference/android/provider/MediaStore>
- Shared storage and Storage Access Framework restrictions:
  <https://developer.android.com/training/data-storage/shared/documents-files>
- SMS/default-handler restrictions:
  <https://developer.android.com/guide/topics/permissions/default-handlers>
- Android 15 background activity restrictions:
  <https://developer.android.com/about/versions/15/behavior-changes-15>
- KBZPay listing:
  <https://play.google.com/store/apps/details?id=com.kbzbank.kpaycustomer>
- Wave Money History guidance:
  <https://www.wavemoney.com.mm/wave-care-center>
- AYA Pay Wallet listing:
  <https://play.google.com/store/apps/details?id=com.ayaplus.subscriber>
- uabpay listing:
  <https://play.google.com/store/apps/details?id=com.uab.uabbankpay>
- CB Pay listing:
  <https://play.google.com/store/apps/details?id=com.cbbank.cbmbanking>
- KBZPay MMQR/Merchant Portal and Partner App:
  <https://www.kbzpay.com/en/products/mmqr>
- KBZPay business integration/Mini App:
  <https://www.kbzpay.com/en/products/mini-app>
- Wave Pay with Wave merchant/API integration:
  <https://www.wavemoney.com.mm/partner/pay-with-wave/>
- Wave Partner/Developer Portal:
  <https://partners.wavemoney.com.mm/>
- AYA Pay API developer portal:
  <https://developer.ayainnovation.com/devportal/apis/21437e8e-7f0e-4b44-b807-a8b7374337bd>
- AYA Pay Merchant Portal:
  <https://merchant.ayapay.com/>
- AYA Pay merchant business application:
  <https://applications.ayapay.com/ayapay-business-application>
- uabpay+ merchant product:
  <https://uab.com.mm/mm/digital/uabpayplus/>
- CB MMQR merchant service:
  <https://www.cbbank.com.mm/mm/consumer-banking/cards-payment-services/mmqr>
- CB Merchant Portal:
  <https://www.cbbank.com.mm/mm/login-form/merchant-portal>
