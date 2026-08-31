#!/usr/bin/env python3
"""Compose the AuriX quota-control ad over its generated instrument plate."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "assets/social/tools"))

from myanmar_svg_typography import text_element

BASE = ROOT / "assets/social/quota-control-v1"
PLATE = BASE / "plates/aurix-quota-instrument-generated-r1.png"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = ROOT / "brand/v6/exports/aurix-telegram-bot-avatar-v6-1024.png"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"


def uri(path: Path) -> str:
    mime = {".png": "image/png", ".svg": "image/svg+xml"}[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(
    x: int,
    y: int,
    value: str,
    size: int,
    weight: int = 700,
    fill: str = "#F6F8FC",
    anchor: str = "start",
    family: str | None = None,
    role: str | None = None,
    group: str | None = None,
) -> str:
    result = text_element(x, y, value, size, weight, fill, anchor, family)
    attrs = ""
    if role:
        attrs += f' data-mm-role="{role}"'
    if group:
        attrs += f' data-mm-group="{group}"'
    return result.replace(" style=", f"{attrs} style=", 1)


def build(round_no: int) -> str:
    r1 = round_no >= 1
    r2 = round_no >= 2
    hero_size = 47 if r2 else (45 if r1 else 42)
    hero_y1 = 207 if r2 else 198
    hero_y2 = 286 if r2 else 270
    feature_y1 = 470 if r2 else 440
    feature_y2 = 526 if r2 else 502
    feature_y3 = 550 if r2 else 574
    warning_y1 = 742 if r2 else 764
    warning_y2 = 816 if r2 else 830
    avatar_size = 224 if r2 else (194 if r1 else 164)
    avatar_x = 68
    avatar_y = 1054 if r2 else (1080 if r1 else 1100)
    bot_x = 326 if r2 else (292 if r1 else 266)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="leftField" x1="0" y1="0" x2="1" y2="0">
        <stop stop-color="#06111D" stop-opacity=".99"/>
        <stop offset=".58" stop-color="#071421" stop-opacity=".9"/>
        <stop offset=".84" stop-color="#071421" stop-opacity=".36"/>
        <stop offset="1" stop-color="#071421" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="bottomField" x1="0" y1="0" x2="0" y2="1">
        <stop stop-color="#06111D" stop-opacity="0"/>
        <stop offset=".18" stop-color="#06111D" stop-opacity=".9"/>
        <stop offset="1" stop-color="#040C14" stop-opacity=".99"/>
      </linearGradient>
      <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
        <stop stop-color="#36E2FF" stop-opacity=".75"/>
        <stop offset="1" stop-color="#7765FF" stop-opacity=".08"/>
      </linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="10" stdDeviation="13" flood-color="#000" flood-opacity=".55"/></filter>
    </defs>
    <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect width="760" height="1350" fill="url(#leftField)"/>
    <rect x="0" y="990" width="1080" height="360" fill="url(#bottomField)"/>

    <image href="{uri(LOGO)}" x="70" y="42" width="282" height="90" preserveAspectRatio="xMinYMid meet"/>

    {t(70, hero_y1, 'Outline VPN Quota ကို', hero_size, 850, '#F6F8FC', role='headline', group='hero')}
    {t(70, hero_y2, 'မှန်းသုံးနေရတုန်းလား?', 52 if r2 else 49, 900, '#FFC857', role='headline', group='hero')}

    <image href="{uri(OUTLINE)}" x="70" y="338" width="70" height="70" filter="url(#shadow)"/>
    {t(162, 382, 'Outline VPN Key + AuriX Bot', 28, 780, '#DDE7EF', family='Inter, Noto Sans Myanmar, sans-serif')}

    {t(70, feature_y1, 'သုံးထားတဲ့ပမာဏ · လက်ကျန် · သက်တမ်း' if r2 else 'သုံးပြီးပမာဏ · လက်ကျန်', 31 if r2 else 32, 830, '#FFFFFF', role='body', group='features')}
    {'' if r2 else t(70, feature_y2, 'သက်တမ်း · Key အခြေအနေ', 32, 830, '#FFFFFF', role='body', group='features')}
    {t(70, feature_y3, 'My VPN မှာ အချိန်မရွေး စစ်ကြည့်' if r2 else 'My VPN မှာ အချိန်မရွေး ကြည့်နိုင်တယ်', 31 if r2 else 27, 800 if r2 else 760, '#36E2FF', role='body', group='features')}

    <rect x="70" y="{warning_y1-48}" width="8" height="132" rx="4" fill="#FFC857"/>
    {t(102, warning_y1, 'လက်ကျန် 25% · 10% · 5%', 45 if r2 else 42, 900, '#FFC857', family='Inter, Noto Sans Myanmar, sans-serif', role='headline', group='warning')}
    {t(102, warning_y2, 'Telegram မှာ ကြိုတင်အသိပေး' if r2 else 'ရောက်တိုင်း Telegram က သတိပေးတယ်', 33 if r2 else 27, 820 if r2 else 780, '#F6F8FC', role='headline', group='warning')}

    <image href="{uri(BOT)}" x="{avatar_x}" y="{avatar_y}" width="{avatar_size}" height="{avatar_size}" filter="url(#shadow)"/>
    {t(bot_x, 1106 if r2 else 1132, 'AURIX TELEGRAM BOT', 23, 850, '#36E2FF', family='Inter, sans-serif')}
    {t(bot_x, 1172 if r2 else 1194, 'My VPN ကို ဖွင့်ကြည့်', 34 if r2 else 31, 880, '#FFFFFF', role='headline', group='bot')}
    <rect x="{bot_x}" y="1210" width="{1000-bot_x}" height="2" fill="url(#rule)"/>
    {t(bot_x, 1263 if r2 else 1260, '@aurix_outline_vpn_bot', 29 if r2 else 27, 850, '#36E2FF', family='Inter, sans-serif')}
    </svg>'''


def caption() -> str:
    return """အလုပ်တန်းလန်းမှာ VPN Key ရပ်သွားမှ Quota ကုန်သွားတာ သိရတာက အဆင်မပြေပါဘူး။

AuriX က ထုတ်ပေးထားတဲ့ Outline VPN Key ကို သုံးနေရင် Telegram Bot ရဲ့ My VPN မှာ —
• သုံးပြီးပမာဏ
• ကျန်တဲ့ပမာဏနဲ့ ရာခိုင်နှုန်း
• သက်တမ်းကုန်မယ့်အချိန်
• Key ရဲ့ လက်ရှိအခြေအနေ
ကို အချိန်မရွေး ပြန်ကြည့်နိုင်ပါတယ်။

Quota လက်ကျန် 25%၊ 10% နဲ့ 5% အဆင့်တွေကို ရောက်တဲ့အခါ Telegram ကနေ သတိပေးပါတယ်။ Quota ပြည့်သွားတာကို Bot က စစ်တွေ့ရင် Key ရပ်သွားကြောင်းလည်း သီးခြားအသိပေးပါတယ်။

ဒီစာရင်းက Outline ရဲ့ နောက်ဆုံးရရှိထားတဲ့ 30-day rolling transfer metrics ကို အခြေခံတာပါ။ Live speed meter မဟုတ်သလို လဆန်း ၁ ရက်နေ့မှာ reset ဖြစ်တဲ့ Usage စာရင်းလည်း မဟုတ်ပါဘူး။ Refresh လုပ်ပြီး နောက်ဆုံးရရှိထားတဲ့ transfer ပမာဏကို ကြည့်နိုင်ပါတယ်။

ရနိုင်တဲ့ Outline VPN Plan —
• 50 GB · ရက် ၃၀ · 3,000 ကျပ်
• 100 GB · ရက် ၃၀ · 6,000 ကျပ်

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။ ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းက အသုံးပြုနေတဲ့ Network၊ ISP နဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။

#AuriXVPN #OutlineVPN #VPNUsage #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-quota-control-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
