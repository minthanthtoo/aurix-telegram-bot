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
    outline_size = 78 if round_no == 0 else 92
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
    {t(70, hero_y+72, '၅ ယောက်အတွက် အခမဲ့', 48 if round_no == 0 else 52, 850, '#FFC857')}
    {t(70, hero_y+126, 'Outline VPN Key · ရက် 30', 29, 700, '#D6E0E8')}

    <image href="{uri(OUTLINE)}" x="{540-outline_size/2}" y="575" width="{outline_size}" height="{outline_size}" filter="url(#shadow)"/>

    {t(540, cta_y, '“100GB စမ်းမယ်”', 54 if round_no == 0 else 60, 900, '#FFFFFF', 'middle')}
    {t(540, cta_y+64, 'လို့ Comment ရေးပါ', 36 if round_no == 0 else 42, 800, '#36E2FF', 'middle')}
    </svg>"""


def caption() -> str:
    return """AuriX Giveaway မှာ ကံထူးရှင် ၅ ယောက်ကို ရက် 30 အသုံးပြုနိုင်တဲ့ 100 GB Outline-compatible VPN Key တစ်ခုစီ ပေးပါမယ်။

ပါဝင်ဖို့ ဘာမှဝယ်စရာ မလိုပါဘူး။ ဒီ Post အောက်မှာ “100GB စမ်းမယ်” လို့ Comment တစ်ကြိမ်ပဲ ရေးပေးပါ။ Facebook account တစ်ခုကို entry တစ်ခုသာ ထည့်တွက်ပါမယ်။ Share လုပ်ဖို့၊ သူငယ်ချင်း Tag တွဲဖို့၊ Page Follow လုပ်ထားဖို့ မလိုပါဘူး။

ဒီ Post တင်ပြီး ၇ ရက်အကြာ မြန်မာစံတော်ချိန် ည ၈ နာရီမှာ စာရင်းပိတ်ပါမယ်။ သတ်မှတ်ချက်နဲ့ကိုက်ညီတဲ့ Comment တွေထဲက ၅ ယောက်ကို ကျပန်းရွေးပြီး AuriX Facebook Page နဲ့ Telegram Channel မှာ ကြေညာပေးပါမယ်။

100 GB ဆိုတာ VPN Key အသုံးပြုခွင့်ပမာဏဖြစ်ပြီး ဖုန်း SIM/Mobile Data မဟုတ်ပါဘူး။ AuriX က ကံထူးရှင်တွေဆီက ငွေ၊ OTP ဒါမှမဟုတ် Payment PIN ကို ဘယ်တော့မှ မတောင်းပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Winner ကြေညာချက်နဲ့ သတင်းများ — https://t.me/AurixDigitalStore

ဒီ Giveaway ကို AuriX က စီစဉ်တာဖြစ်ပြီး Facebook က sponsor၊ ထောက်ခံသူ ဒါမှမဟုတ် စီမံသူ မဟုတ်ပါဘူး။ ပါဝင်သူတွေက ဒီအစီအစဉ်နဲ့သက်ဆိုင်တဲ့ တာဝန်ကနေ Facebook ကို ကင်းလွတ်ခွင့်ပေးပါတယ်။ AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriX #OutlineVPN #GiveawayMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1], required=True)
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
