#!/usr/bin/env python3
"""Compose exact AuriX giveaway content over a generated editorial plate."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/giveaway-100gb-v2"
PLATE = BASE / "plates/giveaway-five-pass-editorial-r0.png"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"


def uri(path: Path) -> str:
    mime = {".png": "image/png", ".svg": "image/svg+xml"}[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(x: int, y: int, value: str, size: int, weight: int = 700, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif") -> str:
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'


def build(round_no: int) -> str:
    cta_y = 1110 if round_no == 0 else 1080
    hero_y = 248 if round_no == 0 else 238
    outline_size = 78 if round_no == 0 else (92 if round_no == 1 else 128)
    outline_y = 575 if round_no < 2 else 548
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="topShade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity=".92"/><stop offset="1" stop-color="#071421" stop-opacity="0"/></linearGradient>
      <linearGradient id="bottomShade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity="0"/><stop offset="1" stop-color="#071421" stop-opacity=".86"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="5" stdDeviation="8" flood-color="#000" flood-opacity=".45"/></filter>
    </defs>
    <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect x="0" y="0" width="1080" height="470" fill="url(#topShade)"/>
    <rect x="0" y="850" width="1080" height="500" fill="url(#bottomShade)"/>
    <image href="{uri(LOGO)}" x="68" y="42" width="264" height="86" preserveAspectRatio="xMinYMid meet"/>

    {t(70, hero_y, '100 GB', 124 if round_no == 0 else 132, 900, '#FFFFFF', family='Inter, sans-serif')}
    {t(70, hero_y+72, 'ကံထူးရှင် ၅ ယောက်', 48 if round_no == 0 else 52, 850, '#FFC857')}
    {t(70, hero_y+126, 'Outline VPN Key · ရက် 30', 29, 700, '#D6E0E8')}

    <image href="{uri(OUTLINE)}" x="{540-outline_size/2}" y="{outline_y}" width="{outline_size}" height="{outline_size}" filter="url(#shadow)"/>

    {t(540, cta_y, '“100GB စမ်းမယ်”', 54 if round_no == 0 else 60, 900, '#FFFFFF', 'middle')}
    {t(540, cta_y+64, 'Comment ရေးပြီး ပါဝင်ပါ', 36 if round_no == 0 else 42, 800, '#36E2FF', 'middle')}
    </svg>"""


def caption() -> str:
    return """AuriX Giveaway မှာ ကံထူးရှင် ၅ ယောက်ကို ရက် 30 သုံးနိုင်တဲ့ 100 GB Outline VPN Key တစ်ခုစီ မဲဖောက်ပေးမယ်။

ပါဝင်ချင်ရင် ဒီ Post အောက်မှာ “100GB စမ်းမယ်” လို့ Comment တစ်ကြိမ်ပဲ ရေးပေးရုံပါပဲ။ ဝယ်စရာမလိုပါဘူး။ Share လုပ်တာ၊ သူငယ်ချင်း Tag တွဲတာ၊ Page Follow လုပ်တာလည်း မလိုပါဘူး။ Facebook account တစ်ခုကို Comment တစ်ခုသာ ထည့်တွက်ပါမယ်။

Post တင်တဲ့နေ့ကနေ ၇ ရက်ပြည့်တဲ့နေ့ ည ၈ နာရီ (မြန်မာစံတော်ချိန်) မှာ Comment ပိတ်ပါမယ်။ စည်းကမ်းနဲ့ကိုက်ညီတဲ့ Comment တွေထဲက ၅ ယောက်ကို ကျပန်းရွေးပြီး AuriX Facebook Page နဲ့ Telegram Channel မှာ ကြေညာပါမယ်။

ဒီ 100 GB က Outline VPN Key အသုံးပြုခွင့်ပမာဏပါ။ ဖုန်း SIM/Mobile Data Package မဟုတ်ပါဘူး။ ကံထူးရှင်ဆီက ငွေ၊ OTP ဒါမှမဟုတ် Payment PIN ကို AuriX က ဘယ်တော့မှ မတောင်းပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
ကံထူးရှင်ကြေညာချက်နဲ့ သတင်းများ — https://t.me/AurixDigitalStore

ဒီ Giveaway ကို AuriX က စီစဉ်တာပါ။ Facebook က ကမကထလုပ်သူ၊ ထောက်ခံသူ ဒါမှမဟုတ် စီမံခန့်ခွဲသူ မဟုတ်ပါဘူး။ ဒီ Giveaway နဲ့ပတ်သက်ပြီး Facebook မှာ တာဝန်မရှိပါဘူး။ AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriX #OutlineVPN #GiveawayMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-100gb-giveaway-v2_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
