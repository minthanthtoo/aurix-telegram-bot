# AuriX Quota Control V1 — Creative Plan

## Locked commercial brief

- Brand/product: AuriX Telegram Bot for Outline VPN keys.
- Platform/format: Facebook portrait static, 1080 × 1350.
- Audience: technically confident Myanmar professionals, freelancers, online operators, and existing Outline users who care about measurable quota and predictable interruption.
- Audience tension: a quota-limited VPN can stop at the worst time when the user cannot see usage or approaching exhaustion.
- Asset job: position AuriX as the visibility and warning layer around an Outline key.
- Promise: customers can open My VPN/Usage at any time to see used allowance, remaining allowance, percentage, expiry, and key state; AuriX queues advance Telegram warnings as remaining quota crosses configured thresholds.
- Product memory: Outline VPN Key + AuriX Telegram Bot = visible usage and advance quota warnings.
- CTA: open `@aurix_outline_vpn_bot` and use My VPN.
- Primary metric: qualified Bot starts and My VPN opens from existing/returning users.

## Evidence and claim state

| Claim | State | Evidence / restriction |
|---|---|---|
| Usage lookup at any time | implemented, not production-live-verified | `/myvpn`, `/usage`, and Usage button converge on customer dashboard |
| Fields shown | implemented, not production-live-verified | used, remaining, percentage, expiry, and state per owned key |
| Usage source | verified in code/docs | Outline trailing-30-day transfer metrics; not live speed or a calendar-month ledger |
| Advance warnings | implemented, not production-live-verified | deduplicated thresholds at 25%, 10%, and 5% remaining |
| Quota stop notice | implemented, not production-live-verified | separate termination notification when observed quota is reached |
| Requested 50% / 10% / 0% sequence | blocked for publication | current code is 25% / 10% / 5%, with a separate 0% termination notice |

## Locked reading path

`Outline quota uncertainty → usage visibility → warning thresholds → AuriX Bot destination`

Exact visible copy:

1. `Outline VPN Quota ကို မှန်းသုံးနေရတုန်းလား?`
2. `သုံးပြီးပမာဏ · လက်ကျန် · သက်တမ်း · Key အခြေအနေ`
3. `My VPN မှာ အချိန်မရွေး ကြည့်နိုင်တယ်`
4. `လက်ကျန် 25% · 10% · 5% ရောက်တိုင်း Telegram က သတိပေးတယ်`
5. `@aurix_outline_vpn_bot`

## Visual territories explored

- Phone screenshot of the Bot dashboard — rejected; immediately becomes UI documentation rather than an acquisition ad.
- Floating usage cards — rejected; resembles a SaaS dashboard and repeats the project's earlier failure mode.
- Generic speedometer — rejected; could advertise telecom, hosting, finance, or automotive products.
- Data reservoir visibly draining — rejected; memorable but risks recreating the mobile-data-package ambiguity.
- Physical access key with a glowing fill bar — rejected; quota is clearer, but the connection to AuriX and warning checkpoints is weak.
- Precision A-shaped quota instrument with one calibrated signal path and three warning notches — selected; combines AuriX geometry, measurement, interruption prevention, and professional engineering character in one mechanism.

## Plan council and improvement

Gemini approved the audience tension and verified feature set. Its main concern was that a loose access-key sculpture would be too abstract at mobile size. The plan was improved to a readable precision instrument: one A-shaped measured path, a large remaining-allowance arc, and three explicit warning notches. Exact percentages and language remain deterministic overlays.

## Brand and composition rules

- Midnight Ink dominates; Cloud carries information; cyan means measured connection; restrained violet closes the signal path; amber marks only warning thresholds.
- No warning coral unless showing a true failure state.
- Official AuriX logo is primary. Official Outline icon is a recognition anchor, clearly secondary.
- The AuriX Bot destination badge must be large, clean, and professional. Replace the small cartoon robot badge with a restrained `BOT` status seal.
- No fake controls, phone screens, rounded dashboard cards, payment logos, speed claims, security superlatives, or generated copy.

## Publication gates

1. Live-test usage refresh, all three advance warnings, and quota termination delivery in production.
2. Keep the exact 25% / 10% / 5% values unless implementation and tests are changed.
3. Do not imply real-time telemetry; the figures are last-observed Outline transfer metrics.
