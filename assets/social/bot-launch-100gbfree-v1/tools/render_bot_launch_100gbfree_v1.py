#!/usr/bin/env python3
"""Compose the AuriX 100GBFREE Bot-launch ad over its generated plate."""

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

BASE = ROOT / "assets/social/bot-launch-100gbfree-v1"
PLATE = BASE / "plates/aurix-five-pass-launch-generated-r0.png"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = ROOT / "brand/v5/exports/aurix-telegram-bot-avatar-v5-1024.png"
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
) -> str:
    return text_element(x, y, value, size, weight, fill, anchor, family)


def build(round_no: int) -> str:
    refined = round_no >= 1
    final = round_no >= 2
    eyebrow_y = 183 if final else 176
    hero_y = 325 if final else 312
    hero_size = 126 if final else (118 if refined else 112)
    product_y = 417 if final else 397
    scarcity_y = 482 if final else 455
    footer_top = 1040 if final else 1060
    avatar_x = 70
    avatar_y = 1090 if final else 1110
    avatar_size = 168 if final else 154
    copy_x = 276 if final else 258
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="topShade" x1="0" y1="0" x2="1" y2=".18">
        <stop stop-color="#06111D" stop-opacity=".98"/>
        <stop offset=".64" stop-color="#071421" stop-opacity=".72"/>
        <stop offset="1" stop-color="#071421" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="topFade" x1="0" y1="0" x2="0" y2="1">
        <stop stop-color="#FFF"/>
        <stop offset=".75" stop-color="#FFF"/>
        <stop offset="1" stop-color="#000"/>
      </linearGradient>
      <linearGradient id="bottomShade" x1="0" y1="0" x2="0" y2="1">
        <stop stop-color="#06111D" stop-opacity="0"/>
        <stop offset=".16" stop-color="#06111D" stop-opacity=".86"/>
        <stop offset="1" stop-color="#050D16" stop-opacity=".99"/>
      </linearGradient>
      <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
        <stop stop-color="#36E2FF" stop-opacity=".75"/>
        <stop offset="1" stop-color="#7765FF" stop-opacity=".08"/>
      </linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="9" stdDeviation="12" flood-color="#000" flood-opacity=".52"/></filter>
      <mask id="topMask"><rect width="1080" height="650" fill="url(#topFade)"/></mask>
    </defs>
    <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect x="0" y="0" width="1080" height="650" fill="url(#topShade)" mask="url(#topMask)"/>
    <rect x="0" y="{footer_top}" width="1080" height="{1350-footer_top}" fill="url(#bottomShade)"/>

    <image href="{uri(LOGO)}" x="70" y="40" width="274" height="88" preserveAspectRatio="xMinYMid meet"/>
    {t(70, eyebrow_y, 'AuriX Telegram Bot စတင်အသုံးပြုနိုင်ပါပြီ', 34 if final else 32, 820, '#F6F8FC')}

    <text x="68" y="{hero_y}" font-family="Inter, sans-serif" font-size="{hero_size}" font-weight="900" letter-spacing="-6" filter="url(#shadow)"><tspan fill="#FFFFFF">100GB</tspan><tspan fill="#FFC857">FREE</tspan></text>
    <image href="{uri(OUTLINE)}" x="70" y="{product_y-43}" width="62" height="62" filter="url(#shadow)"/>
    {t(152, product_y, 'Outline VPN Key · သက်တမ်း ၃၀ ရက်', 31 if final else 29, 780, '#DDE7EF')}
    {t(70, scarcity_y, 'ပထမဆုံး ၅ ဦးအတွက် အခမဲ့', 42 if final else 38, 900, '#36E2FF')}

    <image href="{uri(BOT)}" x="{avatar_x}" y="{avatar_y}" width="{avatar_size}" height="{avatar_size}" filter="url(#shadow)"/>
    {t(copy_x, 1116 if final else 1138, 'ရယူဖို့', 23, 720, '#C7D3DD')}
    {t(copy_x, 1174 if final else 1190, 'Bot ထဲမှာ', 34 if final else 31, 850, '#FFFFFF')}
    {t(copy_x, 1234 if final else 1242, '100GBFREE', 48 if final else 44, 900, '#FFC857', family='Inter, sans-serif')}
    {t(570 if final else 538, 1234 if final else 1242, 'လို့ ပို့လိုက်ပါ', 34 if final else 31, 850, '#FFFFFF')}
    <rect x="{copy_x}" y="1263" width="{1000-copy_x}" height="2" fill="url(#rule)"/>
    {t(copy_x, 1310, '@aurix_outline_vpn_bot', 28, 850, '#36E2FF', family='Inter, sans-serif')}
    </svg>'''


def caption() -> str:
    return """AuriX Telegram Bot ကို စတင်အသုံးပြုနိုင်ပါပြီ။

Bot စတင်ဖွင့်လှစ်တဲ့ အထိမ်းအမှတ်အဖြစ် ပထမဆုံး အရည်အချင်းပြည့်မီသူ ၅ ဦးကို 100 GB Outline VPN Key တစ်ခုစီ အခမဲ့ ပေးသွားပါမယ်။ Key တစ်ခုကို သက်တမ်း ၃၀ ရက် အသုံးပြုနိုင်ပါတယ်။

ရယူပုံက ရိုးရိုးလေးပါ —
1. AuriX Bot ကို ဖွင့်ပါ။
2. 100GBFREE လို့ အတိအကျ ပို့ပါ။
3. နေရာကျန်သေးပြီး သတ်မှတ်ချက်နဲ့ ကိုက်ညီရင် Bot က Key ကို တစ်ခါတည်း ထုတ်ပေးပါမယ်။ ငွေလွှဲစရာ၊ ပြေစာပို့စရာ မလိုပါဘူး။

ဒီအစီအစဉ်က မဲနှိုက်တာ မဟုတ်ပါဘူး။ သတ်မှတ်ချက်နဲ့ ကိုက်ညီပြီး အရင်ဆုံးရောက်လာတဲ့ Telegram account ၅ ခုသာ ရမှာပါ။ နေရာပြည့်သွားရင် Bot က အသိပေးပါမယ်။

သတိပြုရန် — လက်ရှိ Paid Order ဒါမှမဟုတ် Paid Subscription ရှိနေတဲ့ account တွေ ပါဝင်လို့ မရပါဘူး။ 100GBFREE Key ရသွားတဲ့ account မှာ နောက်ထပ် AuriX Free/Paid Plan၊ Renewal နဲ့ Replacement ရယူလို့ မရတော့ပါဘူး။ တစ်ယောက်ကို Key တစ်ခုသာ ရပါမယ်။

ဒီ 100 GB က Outline VPN Key အသုံးပြုခွင့်ပမာဏပါ။ ဖုန်း SIM/Mobile Data Package မဟုတ်ပါဘူး။ ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းက အသုံးပြုနေတဲ့ Network၊ ISP နဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #100GBFREE #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-bot-launch-100gbfree-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
