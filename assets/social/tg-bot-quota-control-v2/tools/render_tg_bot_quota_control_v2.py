#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/tg-bot-quota-control-v2"
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


def telegram_badge(x: int, y: int, radius: int = 48) -> str:
    scale = radius / 43
    return f'''<g aria-label="Telegram platform badge" transform="translate({x} {y}) scale({scale:.4f})">
      <circle r="43" fill="#FFFFFF"/>
      <circle r="35" fill="#229ED9"/>
      <path d="M-22-2 21-19C25-20 28-17 27-13L19 22C18 26 14 27 11 25L-1 16-7 22C-9 24-12 23-12 19L-11 11 12-10-16 7C-20 9-24 6-25 2-25 0-24-1-22-2Z" fill="#FFFFFF"/>
    </g>'''


def build(round_no: int) -> str:
    hook_size = (54, 58, 61, 56)[round_no]
    bot_size = (250, 280, 300, 320)[round_no]
    bot_x = (742, 720, 700, 676)[round_no]
    bot_y = (500, 480, 466, 476)[round_no]
    proof_y = (990, 972, 990, 988)[round_no]
    checkpoints_y = (790, 774, 760, 742)[round_no]
    warning_y = checkpoints_y + (140, 140, 186, 190)[round_no]
    instrument_bottom = 874 if round_no == 3 else 906
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1080" y2="1350" gradientUnits="userSpaceOnUse"><stop stop-color="#071421"/><stop offset=".58" stop-color="#091A2A"/><stop offset="1" stop-color="#10283A"/></linearGradient>
    <linearGradient id="signal" x1="120" y1="500" x2="870" y2="930" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset=".58" stop-color="#4EC5F7"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <linearGradient id="gold" x1="70" y1="160" x2="640" y2="360" gradientUnits="userSpaceOnUse"><stop stop-color="#FFD36A"/><stop offset="1" stop-color="#FF9D35"/></linearGradient>
    <linearGradient id="wordX" x1="250" y1="55" x2="295" y2="112" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <radialGradient id="halo"><stop stop-color="#36E2FF" stop-opacity=".15"/><stop offset="1" stop-color="#36E2FF" stop-opacity="0"/></radialGradient>
    <filter id="shadow"><feDropShadow dx="0" dy="16" stdDeviation="20" flood-color="#000000" flood-opacity=".42"/></filter>
    <filter id="glow"><feGaussianBlur stdDeviation="10" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <rect width="1080" height="1350" fill="url(#bg)"/>
  <circle cx="830" cy="590" r="430" fill="url(#halo)"/>
  <path d="M52 430H1028" stroke="#29475E" stroke-width="2"/>
  <path d="M52 1210H1028" stroke="#29475E" stroke-width="2"/>

  <g aria-label="AuriX primary lockup">
    <image href="{uri(LOGO)}" x="62" y="42" width="92" height="92"/>
    <text x="174" y="103" fill="#F6F8FC" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="750" letter-spacing="-2">Auri</text>
    <text x="267" y="103" fill="url(#wordX)" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="800" letter-spacing="-2">X</text>
  </g>
  <g opacity=".92">
    <image href="{uri(OUTLINE)}" x="946" y="52" width="64" height="64"/>
    <text x="1010" y="140" fill="#AFC0CE" font-family="Inter, Arial, sans-serif" font-size="16" font-weight="750" text-anchor="end">Outline VPN</text>
  </g>

  {text(70, 220, 'Outline VPN Key ရဲ့' if round_no == 3 else 'Outline VPN Quota', hook_size, 900, '#36E2FF', family='Inter, Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='display', group='hook')}
  <path d="M68 335C210 353 478 351 626 329" fill="none" stroke="#FFC857" stroke-width="24" stroke-linecap="round" opacity=".68"/>
  {text(70, 326, 'Quota ဘယ်လောက်ကျန်သေးလဲ?' if round_no == 3 else 'ဘယ်လောက်ကျန်သေးလဲ?', hook_size + (0 if round_no == 3 else 3), 900, '#F6F8FC', family='Noto Serif Myanmar, serif', role='display', group='hook')}

  <g aria-label="A-shaped calibrated quota instrument">
    <path d="M126 {instrument_bottom} 390 500 636 {instrument_bottom}" fill="none" stroke="#17344B" stroke-width="54" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M126 {instrument_bottom} 390 500 636 {instrument_bottom}" fill="none" stroke="url(#signal)" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M222 {checkpoints_y}H548" stroke="#17344B" stroke-width="44" stroke-linecap="round"/>
    <path d="M222 {checkpoints_y}H548" stroke="url(#signal)" stroke-width="12" stroke-linecap="round"/>
    <circle cx="278" cy="{checkpoints_y}" r="24" fill="#FFC857" stroke="#071521" stroke-width="8"/>
    <circle cx="389" cy="{checkpoints_y}" r="24" fill="#FFC857" stroke="#071521" stroke-width="8"/>
    <circle cx="500" cy="{checkpoints_y}" r="24" fill="#FFC857" stroke="#071521" stroke-width="8"/>
    {text(278, checkpoints_y + 74, '25%', 37, 900, '#FFD36A', family='Inter, Arial, sans-serif', anchor='middle')}
    {text(389, checkpoints_y + 74, '10%', 37, 900, '#FFD36A', family='Inter, Arial, sans-serif', anchor='middle')}
    {text(500, checkpoints_y + 74, '5%', 37, 900, '#FFD36A', family='Inter, Arial, sans-serif', anchor='middle')}
    {text(389, warning_y, 'ကျန်တိုင်း Telegram ကနေ ကြိုသတိပေး' if round_no == 3 else 'ကျန်တိုင်း Telegram က ကြိုအသိပေး', (29, 31, 35, 32)[round_no], 800, '#DDE7EF', family='Inter, Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', anchor='middle', role='headline', group='instrument-warning')}
    <path d="M524 {checkpoints_y-10}C632 {checkpoints_y-112} 690 {bot_y+bot_size//2-44} {bot_x+12} {bot_y+bot_size//2-36}" fill="none" stroke="#36E2FF" stroke-width="8" stroke-linecap="round" stroke-dasharray="2 22"/>
  </g>

  <g filter="url(#shadow)">
    <circle cx="{bot_x + bot_size//2}" cy="{bot_y + bot_size//2}" r="{bot_size//2 + 7}" fill="#071521" stroke="#50D7EA" stroke-width="4"/>
    <clipPath id="botClip"><circle cx="{bot_x + bot_size//2}" cy="{bot_y + bot_size//2}" r="{bot_size//2}"/></clipPath>
    <image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}" clip-path="url(#botClip)"/>
  </g>
  {telegram_badge(bot_x + bot_size - 4, bot_y + 48, (44, 49, 52, 56)[round_no])}

  <g transform="translate(70 {proof_y})">
    <path d="M0 0H940" stroke="#315069" stroke-width="2"/>
    <path d="M0 0H220" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>
    {text(0, 72, 'AuriX Bot မှာ ဝယ်ထားတဲ့ VPN Key', 34 if round_no < 3 else 32, 800, '#AFC0CE', family='Inter, Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='headline', group='proof')}
    {text(0, 142, 'My VPN မှာ အချိန်မရွေး ဝင်စစ်', 39 if round_no < 3 else 37, 900, '#F6F8FC', family='Inter, Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='headline', group='proof')}
  </g>

  <g transform="translate(70 1262)">
    <circle cx="24" cy="20" r="20" fill="#229ED9"/>
    <path d="M11 19 35 9C39 8 41 11 39 15L35 31C34 35 30 35 27 33L21 29 17 33C14 35 12 32 13 28L14 25 28 14 11 23C7 25 5 21 11 19Z" fill="#FFFFFF"/>
    <text x="62" y="31" fill="#36E2FF" font-family="Inter, Arial, sans-serif" font-size="28" font-weight="850">@aurix_outline_vpn_bot</text>
    <text x="940" y="30" fill="#71889C" font-family="Inter, Arial, sans-serif" font-size="17" font-weight="650" text-anchor="end">AuriX · Clear access. Human help.</text>
  </g>
</svg>'''


def caption() -> str:
    return """Outline VPN Key သုံးနေရင်း Quota ဘယ်လောက်ကျန်သေးလဲ ခန့်မှန်းသုံးနေရတာ စိတ်မအေးရဘူးလား။

AuriX Telegram Bot မှာ ဝယ်ထားတဲ့ VPN Key ဆိုရင် My VPN ထဲကနေ —
• သုံးပြီးသားပမာဏ
• Quota လက်ကျန်နဲ့ ရာခိုင်နှုန်း
• သက်တမ်းနဲ့ Key အခြေအနေ
ကို အချိန်မရွေး ဝင်စစ်နိုင်ပါတယ်။

Quota 25%၊ 10% နဲ့ 5% ကျန်တဲ့အခါတိုင်း Telegram ကနေ ကြိုသတိပေးပါတယ်။ Quota ကုန်သွားရင်လည်း သီးခြားအသိပေးလို့ လိုအပ်တဲ့အချိန်မတိုင်ခင် ကြိုတင်စီစဉ်နိုင်ပါတယ်။

ရနိုင်တဲ့ Paid Plan —
• 50 GB · ရက် 30 · 3,000 ကျပ်
• 100 GB · ရက် 30 · 6,000 ကျပ်

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

My VPN မှာပြတဲ့ Usage က Outline ဆီက နောက်ဆုံးရထားတဲ့ rolling 30-day transfer metrics ကို အခြေခံထားတာပါ။ Live speed meter မဟုတ်သလို လဆန်းတိုင်း reset ဖြစ်တဲ့ စာရင်းလည်း မဟုတ်ပါဘူး။ AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #TelegramBot #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-tg-bot-quota-control-v2_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
