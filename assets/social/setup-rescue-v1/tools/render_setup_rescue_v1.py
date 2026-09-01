#!/usr/bin/env python3
"""
AuriX Setup Rescue v1 — deterministic renderer.

Pipeline (matching receipt-confidence-v1 gold standard):
  1. AI-generated text-free plate as background image.
  2. Actual brand assets (logo mark PNG, Outline icon PNG, bot avatar PNG)
     embedded via base64 data URIs.
  3. Proper Myanmar text rendering via SVG <text> with system fonts.
  4. rsvg-convert to production PNG.

Since image generation is location-blocked, this version reuses the
receipt-confidence editorial plate — same studio palette, different overlay.
Replace PLATE path when a dedicated setup-rescue plate is generated.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/setup-rescue-v1"
# Reuse receipt plate until a dedicated plate can be generated
PLATE = ROOT / "assets/social/receipt-confidence-v1/plates/receipt-confidence-editorial-r0.png"
LOGO = ROOT / "brand/v2/aurix-logo-mark-v2.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
BOT = ROOT / "brand/v4/exports/aurix-telegram-bot-avatar-v4-1024.png"


def uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def text(
    x: int,
    y: int,
    value: str,
    size: int,
    weight: int,
    fill: str,
    *,
    family: str = "Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif",
    anchor: str = "start",
    role: str = "body",
    group: str = "single",
    tracking: int | None = None,
) -> str:
    mm = any("\u1000" <= ch <= "\u109f" for ch in value)
    attrs = [
        f'x="{x}"', f'y="{y}"', f'font-family="{family}"',
        f'font-size="{size}"', f'font-weight="{weight}"', f'fill="{fill}"',
        f'text-anchor="{anchor}"',
    ]
    if tracking is not None:
        attrs.append(f'letter-spacing="{tracking}"')
    if mm:
        attrs.extend([
            'xml:lang="my"',
            'style="font-kerning:normal;font-feature-settings:\'mark\' 1, \'mkmk\' 1"',
            f'data-mm-role="{role}"', f'data-mm-group="{group}"',
        ])
    return f'<text {" ".join(attrs)}>{escape(value)}</text>'


def telegram_badge(x: int, y: int, radius: int = 38) -> str:
    scale = radius / 43
    return f'''<g aria-label="Telegram platform badge" transform="translate({x} {y}) scale({scale:.4f})">
      <circle r="43" fill="#FFFFFF"/>
      <circle r="35" fill="#229ED9"/>
      <path d="M-22-2 21-19C25-20 28-17 27-13L19 22C18 26 14 27 11 25L-1 16-7 22C-9 24-12 23-12 19L-11 11 12-10-16 7C-20 9-24 6-25 2-25 0-24-1-22-2Z" fill="#FFFFFF"/>
    </g>'''


def build() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="topWash" x1="0" y1="0" x2="0" y2="650" gradientUnits="userSpaceOnUse"><stop stop-color="#EAFBFC" stop-opacity=".92"/><stop offset="1" stop-color="#EAFBFC" stop-opacity="0"/></linearGradient>
    <linearGradient id="signal" x1="74" y1="0" x2="1010" y2="0" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <linearGradient id="wordX" x1="250" y1="55" x2="295" y2="112" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <filter id="shadow"><feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#16344A" flood-opacity=".22"/></filter>
    <filter id="typeShadow"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#0B5278" flood-opacity=".12"/></filter>
  </defs>

  <!-- 1. AI-generated plate as full background -->
  <image href="{uri(PLATE)}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>

  <!-- 2. Top wash for headline readability -->
  <rect width="1080" height="650" fill="url(#topWash)"/>

  <!-- 3. AuriX V2 primary lockup (actual logo asset) -->
  <g aria-label="AuriX primary lockup">
    <image href="{uri(LOGO)}" x="62" y="42" width="92" height="92"/>
    <text x="174" y="103" fill="#071521" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="750" letter-spacing="-2">Auri</text>
    <text x="267" y="103" fill="url(#wordX)" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="800" letter-spacing="-2">X</text>
  </g>

  <!-- 4. Outline secondary mark (actual icon asset) -->
  <g opacity=".9">
    <image href="{uri(OUTLINE)}" x="948" y="54" width="60" height="60"/>
    <text x="1010" y="137" fill="#31576C" font-family="Inter, Arial, sans-serif" font-size="16" font-weight="750" text-anchor="end">Outline VPN</text>
  </g>

  <!-- 5. Headline — hook question -->
  <g filter="url(#typeShadow)">
    {text(72, 228, 'Outline Key', 52, 900, '#10344C', family='Noto Serif Myanmar, Noto Sans Myanmar, serif', role='display', group='hook')}
    <path d="M68 337C212 354 404 351 520 329" fill="none" stroke="#FFC857" stroke-width="24" stroke-linecap="round" opacity=".70"/>
    {text(72, 326, 'ထည့်မရဘူးလား?', 56, 900, '#0A5A88', family='Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='display', group='hook')}
    {text(72, 406, 'ဒီအဆင့်လေးပဲ', 44, 700, '#10344C', family='Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='headline', group='hook')}
  </g>

  <!-- 6. Proof card — 2 steps -->
  <g transform="translate(74 612)" filter="url(#shadow)">
    <path d="M0 0H448L500 52V270H0Z" fill="#FFFFFF" fill-opacity=".94" stroke="#B8E3E8" stroke-width="2"/>
    <path d="M448 0V52H500" fill="#DDF8FA" stroke="#B8E3E8" stroke-width="2"/>
    <rect x="0" y="34" width="8" height="190" rx="4" fill="url(#signal)"/>

    {text(34, 72, '❶ Screenshot တစ်ခု ပို့', 34, 850, '#0B5278', role='headline', group='proof')}
    {text(34, 115, 'ဖြစ်နေတဲ့ Screen ကို Capture ပို့ပါ', 24, 600, '#3D6478', role='body', group='proof')}

    <rect x="28" y="140" width="444" height="2" rx="1" fill="#B8E3E8" fill-opacity=".5"/>

    {text(34, 180, '❷ AuriX Group မှာ မေးပါ', 34, 850, '#0B5278', role='headline', group='proof')}
    {text(34, 222, 'Admin က ကိုယ်တိုင် ကူညီပေးမယ်', 24, 600, '#3D6478', role='body', group='proof')}

    {text(34, 256, '@aurix_outline_vpn_bot', 20, 800, '#0A6B98', family='Inter, Arial, sans-serif')}
  </g>

  <!-- 7. Connector line: proof → bot -->
  <path d="M148 884V916" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>

  <!-- 8. Bot avatar (actual V4 avatar asset) with Telegram badge -->
  <g filter="url(#shadow)">
    <circle cx="148" cy="1016" r="100" fill="#FFFFFF" fill-opacity=".94" stroke="#50D7EA" stroke-width="3"/>
    <clipPath id="botClip"><circle cx="148" cy="1016" r="95"/></clipPath>
    <image href="{uri(BOT)}" x="53" y="921" width="190" height="190" clip-path="url(#botClip)"/>
  </g>
  {telegram_badge(230, 940, 42)}

  <!-- 9. Bottom strip -->
  <rect y="1182" width="1080" height="168" fill="#EAFBFC" fill-opacity=".94"/>
  <path d="M354 1206H1008" stroke="#9EDAE2" stroke-width="2"/>
  <path d="M354 1206H536" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>

  {text(74, 1230, 'Group ထဲ ဝင်ပါ', 28, 780, '#183C52', role='headline', group='footer')}
  {text(74, 1266, 'https://t.me/+oA18TDWAD9NiNWU1', 18, 600, '#0A6B98', family='Inter, Arial, sans-serif')}

  <!-- No wallet strip — this is a help/support post, not payment -->
  {text(600, 1240, 'ဘယ် Platform · ဘယ် Error ပဲ ဖြစ်ဖြစ်', 19, 600, '#3D6478', role='body', group='footer')}
  {text(600, 1270, 'Admin ကိုယ်တိုင် ကူညီပေးပါမယ်', 22, 780, '#183C52', role='body', group='footer')}

</svg>'''


def caption() -> str:
    return """Outline Key ထည့်မရဘူးဆိုရင် ဒီ Step ၂ ခုပဲ လိုတာပါ—

📷 ဖြစ်နေတဲ့ Screen ကို Screenshot ရိုက်ပြီး AuriX Group ထဲ ပို့လိုက်ပါ။
💬 Admin က ကိုယ်တိုင် ကြည့်ပြီး ကူညီပေးမယ်။

Outline App မည်သည့် Platform (Android · iOS · Windows · Mac) ပဲသုံးသုံး၊ ဘာ Error ဆိုတာ မသိသေးရင်တောင် မေးနိုင်ပါတယ်။ ကောင်းတဲ့ Question မရှိ၊ မကောင်းတဲ့ Question မရှိ—ကူညီဖို့ ဒီမှာရှိတာပါ။

🔗 AuriX Telegram Group: https://t.me/+oA18TDWAD9NiNWU1
🤖 AuriX Bot: https://t.me/aurix_outline_vpn_bot
📢 Channel: https://t.me/AurixDigitalStore

#AuriXVPN #OutlineVPN #TelegramBot #VPNMyanmar #Setup"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-setup-rescue-v1_r0"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    preview = out / f"{stem}_324x405.png"
    txt = out / f"{stem}.txt"
    svg.write_text(
        "\n".join(line.rstrip() for line in build().splitlines()) + "\n",
        encoding="utf-8",
    )
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(
        ["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)],
        check=True,
    )
    subprocess.run(
        ["rsvg-convert", "-w", "324", "-h", "405", str(svg), "-o", str(preview)],
        check=True,
    )
    print(json.dumps({
        "image": str(png),
        "preview": str(preview),
        "caption": str(txt),
        "source": str(svg),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
