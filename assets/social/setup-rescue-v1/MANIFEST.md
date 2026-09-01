# AuriX Setup Rescue v1 — Bundle Manifest

## Asset identifiers

| File | Description |
|---|---|
| `setup-rescue-v1.svg` | R1 editable source (1080 × 1350) |
| `setup-rescue-v1-r2.svg` | R2 editable source — production master |
| `exports/20260901-172500_aurix-setup-rescue-v1_r2.png` | R2 production PNG (1080 × 1350) |
| `exports/20260901-172500_aurix-setup-rescue-v1_r2_preview.png` | R2 mobile-feed preview (324 × 405) |
| `exports/20260901-172500_aurix-setup-rescue-v1_r2.txt` | Burmese caption |

## Brand assets used

| Role | Asset |
|---|---|
| Primary mark | `brand/v2/aurix-logo-horizontal-v2.svg` (inlined dark variant) |
| Secondary mark | Outline VPN icon (constructed, official green palette `#00BF72`) |
| Bot avatar | V2 mark + Telegram badge at ~38% avatar diameter |
| Wallet marks | Not used — this post is Help pillar, not payment |

## Standard applied

`AURIX-SOCIAL-S1` — Situation-led Telegram conversion static.

Reading path:
```
Outline Key ထည့်မရဘူးလား? → Screenshot တစ်ခုပို့ → AuriX Group မှာ မေး → @aurix_outline_vpn_bot
```

## Content pillar

**Human operations — 20%** (Setup rescue & support confidence)

## S1 rejection checklist — R2 result

| Gate | Status |
|---|---|
| Does not resemble a webpage, dashboard, or fake Telegram interface | ✅ Pass |
| Viewer remembers AuriX more strongly than Outline | ✅ Pass (Outline is secondary, top-right) |
| Telegram Bot identity is unambiguous | ✅ Pass (handle shown, badge on avatar) |
| No competing reading starts from scattered labels | ✅ Pass (linear top-to-bottom path) |
| Scene is specific to the audience occasion (setup failure) | ✅ Pass (map+laptop+phone, error state) |
| Burmese copy is natural, not translated English | ✅ Pass |
| No unverified live-operation claim | ✅ Pass |

## Claims present

- AuriX Group Admin ကိုယ်တိုင် ကူညီပေးမယ် — requires Group to be staffed (safe to assert once support is active)
- No speed, latency, uptime, receipt-automation, or price claim

## Publication gate

**SAFE TO PUBLISH** once the AuriX Telegram Group and Admin support path are staffed and monitored.

No payment, receipt-automation, or technical-performance claim requires pre-verification.

## Myanmar typography audit

- Font: Noto Sans Myanmar (rsvg-convert system font fallback; production render should verify Noto is installed)
- Leading: headline × 1.50, body × 1.48 (matching `AURIX_MYANMAR_GRAPHICS_RENDERING_RULES.md`)
- `xml:lang="my"` and `font-feature-settings:'mark' 1,'mkmk' 1` applied to all Myanmar runs
- Visual inspection: no overlapping marks detected in r2 production render

## Iterations

| Version | Summary |
|---|---|
| R1 | Initial composition. Dead zone in middle, laptop screen too dark, bot cluster too small. |
| R2 | Tighter layout, brighter laptop error state, larger headline, cleaner bot cluster, sub-note added to proof card. Production master. |

## Git

Committed to local repository only. No GitHub push.
