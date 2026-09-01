# AuriX Content Direction Backlog — September 2026

Status: strategy exploration complete; the selected receipt-confidence and quota-control ads are implemented at R2

Reference standard: `AURIX-SOCIAL-S1`

Selected next-two brief: `docs/AURIX_NEXT_TWO_CONTENT_SELECTION.md`

## Audit conclusion

The repository already contains substantial work for public launch, pricing, 100 GB promotions, quota visibility, payment marks, retention, and Outline setup. The remaining need is not another broad launch graphic. It is a disciplined lifecycle campaign that reduces payment hesitation, setup abandonment, quota surprise, and avoidable support load.

Older assets remain evidence and may be reused, but `publish_ready` in an old manifest does not override current product, language, logo, or live-operation gates.

## Visual audit findings

- `bot-launch-v3` has appropriate launch scale, but its dark monumental visual should not become the default look for everyday lifecycle content.
- `plan-selector-v1` communicates the offer clearly, but the equal three-column structure remains closer to pricing UI than S1's situation-led storytelling.
- `quota-control-payments-v1` has the strongest older feature hierarchy and should be upgraded rather than discarded.
- `giveaway-100gb-v2` has clear first-frame prize memory, but it is campaign-specific and should not define the paid brand.
- The V10 retention and guide images preserve useful content strategy, but several rely on generic neon vessels, abstract technology sculpture, small supporting lines, and an older bot/logo system. They are source material for redesign, not current visual standards.
- S1 adds a lighter audience-relevant lane to the brand without abandoning AuriX navy, cyan, violet, and amber. Future campaigns should rotate light documentary/editorial scenes with darker precision or launch assets according to the content job.

## Current coverage

| Direction | Existing evidence | Current judgment |
|---|---|---|
| Situation-led seller conversion | `online-seller-access-v2` R4 | Canonical S1 standard |
| Public Bot launch | `bot-launch-v3` R8 | Implemented; re-audit receipt-speed claim before reuse |
| Plan and payment comparison | `plan-selector-v1`, `pricing-v2` | Implemented; migrate Telegram modifier and current typography before a new campaign |
| Quota lookup and warnings | `tg-bot-quota-control-v2` R2 | Implemented in the current identity and typography system |
| 100 GB giveaway/promo | `giveaway-100gb-v2`, `bot-launch-100gbfree-v1` | Implemented but campaign-specific and gated |
| Setup and retention series | `campaign` V8–V10 | Implemented in an older system; needs S1-era language/brand QA |
| Platform guides | V8 Android/iOS/Windows/macOS/Linux/ChromeOS | Implemented editorially; verify current steps and official links before reuse |
| Dedicated receipt-confidence post | `receipt-confidence-v1` R3 | Implemented; publish after automated receipt-review flow is live-verified |
| Dedicated free-entry ladder | only embedded in pricing/launch work | Not implemented as a focused acquisition post |
| Telegram Bot/Group/Channel roles | links appear in captions | Not implemented as a coherent onboarding sequence |
| Shared-quota expectation | planned in weekly campaign | Not implemented; product-policy gate remains |
| Consent-based customer outcome | planned only | Blocked until real consent and evidence exist |
| Motion adaptations | discussed, prompt-level only | Not implemented |

## Locked product facts for planning

- Free: 300 MB for 24 hours, claimable daily.
- Free: 3 GB for 30 days, claimable every rolling 30 days.
- Paid: 50 GB / 30 days / 3,000 ကျပ်.
- Paid: 100 GB / 30 days / 6,000 ကျပ်.
- My VPN can expose used amount, remaining amount, percentage, expiry, and Key state from the latest available usage data.
- Paid quota warnings are configured at 25%, 10%, and 5% remaining, with a separate quota-reached notice.
- User-corrected target behavior: AI checks uploaded receipts automatically and a successful check clears the review state for near-immediate Key fulfillment. The checked-in implementation still requires staff verification, so the R3 ad has a live-deployment publication gate.
- Accepted transfer methods: KBZPay, WavePay, AYA Pay, uabpay, and CB Pay.
- AuriX independently supplies Keys for the official Outline Client and does not claim an official partnership.

## Priority 1 — Receipt confidence

Audience job: reassure paid-intent buyers who hesitate because receipt submission and verification feel uncertain.

Locked message:

1. `ပြေစာစစ်ပေးမယ့်အချိန် စောင့်နေရသေးလား?`
2. `ပြေစာပုံ ပို့လိုက်ရုံ`
3. `AI က အလိုအလျောက် စစ်ပေး`
4. `အတည်ပြုပြီးတာနဲ့ Key ရပြီ`

Product proof: the target Bot uses AI to check the uploaded receipt and successful checks clear the review state for Key fulfillment. Do not use a hard seconds-level guarantee. Publish only after this automated path is verified live; the current checked-in code still requires human verification.

Visual territories:

- **Selected:** a real paper receipt passes through one cyan reading beam and becomes a sealed AuriX access envelope after a human approval mark. This is a process story, not a scanner UI.
- Alternative: overhead cashier desk with receipt, five wallet tokens, and one prepared Key envelope.
- Reject: fake Telegram chat, OCR dashboard, green check button, stopwatch, or robotic hand.

Format: S1 static first; 6-second process motion later.

Primary metric: paid orders that progress from creation to valid receipt submission.

## Priority 2 — Setup rescue and human help

Audience job: prevent first-use abandonment after the customer receives an Outline Key.

Locked message:

1. `Outline Key ထည့်မရဘူးလား?`
2. `Screenshot ပို့ပြီး`
3. `AuriX Group မှာ မေးလို့ရတယ်`

Visual territories:

- **Selected:** a folded physical setup map bridges a phone and laptop; one unclear segment leads toward a warm human guidance point and the AuriX Group avatar.
- Alternative: documentary desk moment with the user holding a phone while a clear cyan path reconnects to work.
- Reject: fake support chat, distressed stock face, floating help cards, or an unsupported response-time promise.

Format: static plus a saved-comment support template.

Primary metric: resolved setup cases and activation after support contact.

## Priority 3 — Expiry and renewal retention

Audience job: bring active customers back before the Key reaches its end date.

Locked message:

1. `VPN Key သက်တမ်း`
2. `ဘယ်နေ့ကုန်မလဲ?`
3. `AuriX Bot မှာ ဝင်ကြည့်နိုင်`

Visual territories:

- **Selected:** a physical access tag moving toward a clearly marked end notch while a second continuation tag waits nearby. The current path and next step are visible without promising uninterrupted service.
- Alternative: editorial calendar strip attached to an AuriX Key envelope.
- Reject: alarm clock, red countdown, broken connection, or fear-heavy warning symbols.

Format: S1 static; later retargeting Story.

Primary metric: My VPN opens, renewal inquiries, and completed repeat orders.

## Priority 4 — Free-entry ladder

Audience job: give cautious users a low-risk first experience without hiding the paid product behind freebie language.

Locked message:

1. `မဝယ်ခင် အရင်စမ်းမလား?`
2. `Outline VPN Key အခမဲ့ ၂ မျိုး`
3. `နေ့စဉ် 300 MB · ရက် 30 တိုင်း 3 GB`

Visual territories:

- **Selected:** one A-shaped dispenser releases two physically different access passes: a small daily pass and a longer 30-day pass. Their cadence is shown by material rhythm, not UI cards.
- Alternative: two routes from one gateway, one short repeating loop and one longer measured path.
- Reject: gift box, confetti, `FREE` as the only product memory, or mobile-data imagery.

Format: static or an 8-second two-pass motion.

Primary metric: successful free claims that later return to the Bot.

## Priority 5 — Telegram ecosystem onboarding

Audience job: explain why AuriX has a Bot, Group, and Channel without presenting three competing links in one static.

Recommended carousel:

1. Cover: `AuriX Telegram သုံးခု / ဘာအတွက်လဲ?`
2. Bot: `Key ရယူ · ဝယ်ယူ · Quota စစ်`
3. Group: `Admin ကို မေး · အခက်အခဲ ဆွေးနွေး`
4. Channel: `Plan နဲ့ Service Update တွေ ကြည့်`
5. Close: three exact destinations with their approved V4 entity avatars.

Visual mechanism: one cyan route passes through three distinct physical stations. Each slide has one job and one dominant entity avatar. Do not place three equal mini-cards on every slide.

Format: five-slide carousel.

Primary metric: qualified joins and fewer messages sent to the wrong destination.

## Priority 6 — Shared-quota expectation

Audience job: reduce surprise when the same Key is used across more than one device.

Provisional message:

1. `Key တစ်ခုကို Device နှစ်ခုမှာ သုံးရင်`
2. `Quota တစ်ခုထဲက`
3. `အတူလျော့သွားမယ်`

Visual territories:

- **Selected:** one measured reservoir feeds a phone and laptop through two physical cables. Both paths visibly draw from the same source.
- Alternative: one punched access pass opens two device gates while one gauge changes.
- Reject: device-limit numbers, household-sharing promises, or public sharing guidance until abuse and support policy are approved.

Format: education static or three-slide carousel.

Publication gate: verify multi-device behavior, acceptable-use wording, and whether public sharing guidance is commercially intended.

Primary metric: reduced quota-confusion support contacts.

## Upgrade track — existing ideas that deserve S1-era treatment

### Tech-professional quota control

Keep the precision-instrument territory from `quota-control-payments-v1`, but migrate to the approved V4 Bot avatar plus Telegram badge and current Myanmar spacing. Limit the image to usage lookup and 25%/10%/5% warnings; move five-wallet proof to the footer or caption.

Suggested hook:

`Outline VPN Quota ကို / မှန်းသုံးနေရတုန်းလား? / 25% · 10% · 5% ကျန်တိုင်း အသိပေး`

### Transparent plan choice

Use the tactile physical-pass logic from `plan-selector-v1`, not website pricing cards. Refresh the product relationship and Telegram identity, then retain only three choices when the campaign asks for 3 GB free, 50 GB paid, and 100 GB paid.

Suggested hook:

`မဝယ်ခင် / Quota · သက်တမ်း · ဈေးနှုန်း / AuriX မှာ ကြိုသိနိုင်`

### Outline Key explainer and platform guides

Re-audit V8/V10 against current official download links and current app behavior. The cover should answer `Outline VPN Key ဆိုတာ ဘာလဲ?`; each platform post should teach one tested flow. Instructional steps may be numbered, but must not imitate clickable controls.

### Role-based situation series

Extend S1 beyond online sellers without repeating the same packing-desk scene:

- social media manager: Page access and customer replies;
- freelancer/remote professional: client communication and work tools;
- creator: publishing and quota awareness;
- developer: access to required development services, without speed or uptime claims.

Each asset gets a distinct real situation, one proof, and one destination. Do not combine all professions in one collage.

## Motion direction left to implement

The first motion proof should adapt S1 rather than invent a separate visual language:

1. tangled colored routes appear around the seller desk;
2. one cyan route resolves cleanly toward the AuriX Bot avatar;
3. the Telegram badge attaches with a restrained snap;
4. the parcel label prints `Quota ဘယ်လောက်ကျန်လဲ?`;
5. end on the handle for at least 1.5 seconds.

Length: 6–8 seconds, sound-off readable, no fake UI interaction. Deliver 1080 × 1350 feed and 1080 × 1920 Story variants.

## Council judgment

The external language/strategy council ranked free entry highest for raw acquisition and receipt confidence highest for paid conversion. The final priority was adjusted toward customer quality and retention: receipt confidence, setup rescue, and expiry prevention precede broad free acquisition.

Accepted from the council:

- free entry is the strongest reach mechanism;
- receipt friction deserves a dedicated conversion post;
- support and expiry content protect activation and repeat purchase;
- the ecosystem should be explained as a sequence, not three scattered labels.

Rejected from the council:

- `ချက်ချင်း ကူညီပေးမယ်` because no response-time proof is locked;
- `အခုယူ` because the tone is unnecessarily pushy;
- `ရက် ၃၀ သက်တမ်းအပြည့် သုံး` because it may overstate service continuity;
- `KPay` and shortened wallet names because official payment recognition matters;
- placing the full 25%/10%/5% feature set, five wallets, plans, and destination in one tech post.

## Recommended first production sprint

1. Receipt confidence static.
2. Setup rescue static.
3. Expiry/renewal static.
4. Free-entry ladder static.
5. Telegram ecosystem carousel.
6. Shared-quota education after its publication gate is resolved.

The first four should use different scenes and visual mechanisms while inheriting S1 hierarchy and validation. Do not publish them as four consecutive offer posts; use the rhythm `conversion → help → retention → acquisition`.
