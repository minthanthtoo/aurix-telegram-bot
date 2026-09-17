# Auri mascot sample content

2026-09-16. Four sample creatives, not posted or live-offer verified.

## Selection and plan review

“Chosen ones first” interpreted as quota-control/retention and receipt-to-key
conversion, the two established directions earlier in the conversation. Follow
with two additional executions: setup help and daily free trial. These are new
mascot treatments of previously discussed themes, not invented product features.

Audience jobs: existing buyer checks remaining VPN allowance; paid buyer wants
receipt review without waiting for a seller; beginner needs setup help; cautious
prospect wants to try before paying. One job per image. No full pricing tables,
unlimited claims, fake screens/buttons, meeting-disruption story or speed guarantee.

Plan correction: do not repeat conflicting old reminder percentages; omit exact
thresholds. Key delivery is conditional on successful verification. Daily free
300 MB is user-provided/history-backed, not freshly verified on the live service.
Wallet options appear in the receipt caption, not a cramped bottom icon strip.

Visual directions: quota gauge; receipt-to-key symbolic sequence; question/help
composition; oversized 300 MB typographic offer. Use existing 3D mascot as a guide,
not a new generated image or pretend staff member. These are deliberately simple
white-background samples, not four fully bespoke character scenes.

## Assets and production

- Primary identity derives from `brand/v2/aurix-logo-horizontal-v2.svg`.
- `aurix-compact-derived.svg` removes the tiny tagline and unused right whitespace;
  logo geometry/colors are not redrawn and canonical master stays untouched.
- Official Outline icon: `brand/outline/official/outline-client-icon-1024.png`.
- Mascot: `brand/mascot-v2-3d/20260916_aurix-auri-3d-hero-r2.png`.
- Installed Bold faces: Noto Sans Myanmar and Noto Serif Myanmar, verified by fc-match.
- Rendering: deterministic SVG through rsvg-convert. No generated Burmese text.
- Rebuild: `python3 assets/social/mascot-samples-v1/render.py 2`.

## Two refinement passes

R0: inspected all four feed previews. Core reading order works; headline leading
too tight; free-plan character reaches footer; primary logo contains unreadable tagline.

R1: clearer quota hook; fewer duplicate Outline marks in receipt post; free-plan
character repositioned and scaled down. Preview reviewed; logo still wastes width.

R2: compact official lockup, baseline gap increased to 102px, daily-free display
lines separated by 142px. All four final images visually inspected at full size.
All SVGs pass strict Myanmar audit. Four-final manifest passes bundle validation.
1080x1350 final PNG, 324x405 feed PNG, editable SVG, UTF-8 caption per sample.

Remaining design limitation: reused welcome pose, mild tonal rectangle around
the studio plate on white, and intentionally simple authored symbols. For a broader
campaign, commission specific checking/help/celebration poses from a locked model.
The quota ring is illustrative, not actual customer usage or a percentage claim.

## External language review

Genuine per-asset calls returned `gemini-3.8-flash-tiered`, requested via
`ag/gemini-3.8-flash-tiered(high)`. Initial and final exact-copy reviews are saved.
Final exact audits extract visible SVG strings and read the exported TXT, not drafts.
Model scores: quota 9; receipt 8.5; help 8.5; daily free 8.5. These are language
judgments, not operational verification or human-native approval.

Adopted: quota hook `အချိန်မရွေး စစ်ကြည့်လိုက်ပါ`; prospective `Key ရမယ်`;
caption distinguishes unable to verify from unable to inspect; support CTA corrected.

Rejected: formal `ရပါမည်`, removal of every friendly `လေး`/`နော်`, reintroducing
`VPN ဒေတာ` ambiguity, instructions to merge short headline beats into a long
sentence. Reviewer treated the large question symbol and quota-gauge label as
linear prose; retained because their visual roles are explicit. Duplicate Outline
category text on free post retained to keep large offer grounded in VPN category.
No claim of unanimous external approval.

## Delivery and publication gates

Use `manifest.json` for PNG/TXT/SVG paths and sequence. Keep all four final PNGs
locally, but exclude generated exports and iterations from Git; retain compact
sources, captions and review evidence. The source mascot is an essential shared asset.
No external publishing or GitHub push authorized or performed.

Before publishing: confirm current daily-free availability/eligibility and current
receipt-automation behavior. No numeric latency promise is made. Group help is not
described as instant or 24/7. Setup caption says to hide keys/private payment data.
