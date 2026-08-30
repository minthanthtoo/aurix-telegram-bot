#!/usr/bin/env python3
"""Compose exact AuriX Bot launch content over a generated documentary plate."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/bot-paid-intent-v2"
PLATE = BASE / "plates/bot-work-continuity-documentary-source.png"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = ROOT / "brand/v4/exports/aurix-telegram-bot-avatar-v4-1024.png"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"


def uri(path: Path) -> str:
    mime = {".png": "image/png", ".svg": "image/svg+xml"}[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(x: int, y: int, value: str, size: int, weight: int = 700, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif") -> str:
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'


def build(round_no: int) -> str:
    headline = 44 if round_no == 0 else 48
    avatar = 146 if round_no == 0 else 158
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="top" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity=".94"/><stop offset="1" stop-color="#071421" stop-opacity=".08"/></linearGradient>
      <linearGradient id="bottom" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity=".05"/><stop offset="1" stop-color="#071421" stop-opacity=".96"/></linearGradient>
      <linearGradient id="signal" x1="0" x2="1"><stop stop-color="#FFC857"/><stop offset=".5" stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#000" flood-opacity=".5"/></filter>
    </defs>
    <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect x="0" y="0" width="1080" height="470" fill="url(#top)"/>
    <rect x="0" y="965" width="1080" height="385" fill="#071421" opacity=".91"/>
    <image href="{uri(LOGO)}" x="66" y="40" width="265" height="86" preserveAspectRatio="xMinYMid meet"/>

    {t(68, 208, 'အလုပ်လုပ်ဖို့ VPN ရှာနေရင်း', headline, 850)}
    {t(68, 268, 'အချိန်ကုန်နေလား?', headline+4, 900, '#FFC857')}
    {t(68, 338, 'AuriX Bot ကနေ စလိုက်ပါ', 38 if round_no == 0 else 42, 800, '#FFFFFF')}

    <image href="{uri(BOT)}" x="72" y="1022" width="{avatar}" height="{avatar}" filter="url(#shadow)"/>
    {t(258, 1052, 'ပုံမှန်ဆို ပြေစာစစ်ချိန်', 28, 700, '#D6E0E8')}
    {t(258, 1132, '၁ မိနစ်အောက်*', 58 if round_no == 0 else 64, 900, '#FFFFFF')}
    {t(258, 1195, '@aurix_outline_vpn_bot', 31 if round_no == 0 else 34, 850, '#36E2FF', family='Inter, sans-serif')}
    {t(258, 1250, '*အလုပ်များချိန် စစ်ဆေးချိန် ကွာနိုင်ပါတယ်။', 18, 550, '#B2C0CC')}
    <path d="M762 1108 H830" stroke="url(#signal)" stroke-width="5" stroke-linecap="round"/>
    <path d="M820 1096 L838 1108 L820 1120" fill="none" stroke="#36E2FF" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    <image href="{uri(OUTLINE)}" x="862" y="1054" width="108" height="108"/>
    </svg>"""


def caption() -> str:
    return """အလုပ်လုပ်ဖို့၊ စာလေ့လာဖို့ ဒါမှမဟုတ် Project အတွက် VPN ရှာရင်း အချိန်ကုန်နေပြီလား။

AuriX Telegram Bot ကနေ Outline-compatible VPN Key ကို အလွယ်တကူ ဝယ်ယူနိုင်ပါတယ်။ Plan ရွေး၊ ငွေလွှဲပြီး ပြေစာပို့လိုက်ရုံပါပဲ။ ပုံမှန်ဆိုရင် ၁ မိနစ်မပြည့်ခင် ပြေစာစစ်ဆေးအတည်ပြုပေးပါတယ်။ ဝယ်ယူသူများတဲ့အချိန်မျိုးမှာတော့ စစ်ဆေးချိန် အနည်းငယ်ပိုကြာနိုင်ပါတယ်။

Plan များ—
• 50 GB · ရက် 30 · 3,000 ကျပ်
• 100 GB · ရက် 30 · 6,000 ကျပ်

KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay တို့နဲ့ ငွေပေးချေနိုင်ပါတယ်။

အရင်စမ်းသုံးကြည့်ချင်တယ်ဆိုရင်လည်း 24 နာရီတစ်ကြိမ် 300 MB အခမဲ့ Key ရယူနိုင်ပါတယ်။ ဒီ 300 MB ဟာ VPN Key အသုံးပြုခွင့်ပမာဏသာဖြစ်ပြီး ဖုန်း SIM/Mobile Data မဟုတ်ပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။ ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းကတော့ အသုံးပြုနေတဲ့ Network၊ ISP နဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-bot-launch-v2_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
