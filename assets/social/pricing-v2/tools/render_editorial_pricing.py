#!/usr/bin/env python3
"""Render non-UI editorial pricing posters with exact Burmese copy."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/pricing-v2"
BOT = "@aurix_outline_vpn_bot"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"


def uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(x: int, y: int, s: str, size: int, weight: int = 600, fill: str = "#F7FAFC", anchor: str = "start", rotate: float = 0, family: str = "Noto Sans Myanmar, Inter, sans-serif") -> str:
    transform = f' transform="rotate({rotate} {x} {y})"' if rotate else ""
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" font-family="{family}" font-size="{size}" font-weight="{weight}"{transform}>{escape(s)}</text>'


def caption() -> str:
    return """VPN သုံးတာ ဘယ်လောက်များလဲ။ ကိုယ့်အသုံးနဲ့ ကိုက်တာကိုပဲ ရွေးပါ။

ပုံမှန်သုံးသူအတွက်
50 GB · ရက် 30 · 3,000 ကျပ်

ပိုသုံးသူအတွက်
100 GB · ရက် 30 · 6,000 ကျပ်

အရင်စမ်းချင်ရင်—
နေ့စဉ် 300 MB အခမဲ့
ရက် 30 တိုင်း 3 GB အခမဲ့

AuriX Bot ကရတဲ့ key ကို Official Outline Client ထဲထည့်ပြီး အသုံးပြုရပါတယ်။ ဖော်ပြထားတဲ့ GB က ဖုန်း SIM ဒေတာမဟုတ်ဘဲ AuriX VPN key အတွက် အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။

ရယူရန်နှင့် ဝယ်ယူရန် — https://t.me/aurix_outline_vpn_bot
မရှင်းတာမေးရန် — https://t.me/+oA18TDWAD9NiNWU1
သတင်းများ — https://t.me/AurixDigitalStore

အခပေးအစီအစဉ်ကို ငွေလွှဲပြေစာ စစ်ဆေးအတည်ပြုပြီးမှ စတင်ပေးပါတယ်။ ရရှိနိုင်မှုအပေါ် မူတည်ပါတယ်။

AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ AuriX က Outline-compatible access key ကို သီးခြားဝန်ဆောင်မှုပေးပြီး Official Outline Client ဖြင့် အသုံးပြုရပါသည်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def street(plate: Path, round_no: int) -> str:
    logo = uri(ROOT / "brand/v2/aurix-logo-horizontal-v2.svg")
    outline = uri(OUTLINE)
    bg = uri(plate)
    rough = 0.0 if round_no == 0 else 0.8
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <filter id="ink"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="7" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="{rough}"/></filter>
    </defs>
    <image href="{bg}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <image href="{logo}" x="62" y="52" width="250" height="82" preserveAspectRatio="xMinYMid meet"/>
    <g transform="rotate(-4 952 122)">
      <rect x="884" y="42" width="136" height="166" fill="#F3EBDD" stroke="#071421" stroke-width="3"/>
      <image href="{outline}" x="900" y="56" width="104" height="104"/>
      {t(952, 190, 'OUTLINE CLIENT', 15, 800, '#071421', 'middle', family='Inter, sans-serif')}
    </g>
    <g filter="url(#ink)">
      {t(66, 198, 'VPN သုံးတာ', 50, 800, '#071421')}
      {t(66, 258, 'ဘယ်လောက်များလဲ။', 50, 800, '#071421')}
      {t(68, 314, 'ကိုယ့်အသုံးနဲ့ ကိုက်တာကိုပဲ ရွေးပါ။', 23, 600, '#10334A')}

      {t(68, 438, 'ပုံမှန်သုံးသူ', 25, 700, '#071421')}
      {t(54, 650, '50', 220, 900, '#071421', family='Inter, sans-serif')}
      {t(325, 645, 'GB', 52, 800, '#071421', family='Inter, sans-serif')}
      {t(68, 718, 'ရက် 30', 28, 700, '#10334A')}
      {t(68, 790, '3,000 ကျပ်', 54, 900, '#071421')}

      {t(598, 515, 'ပိုသုံးသူ', 25, 700, '#071421')}
      {t(570, 720, '100', 190, 900, '#071421', family='Inter, sans-serif')}
      {t(901, 714, 'GB', 48, 800, '#071421', family='Inter, sans-serif')}
      {t(598, 782, 'ရက် 30', 28, 700, '#526273')}
      {t(598, 854, '6,000 ကျပ်', 54, 900, '#071421')}

      <line x1="595" y1="907" x2="986" y2="907" stroke="#071421" stroke-width="3"/>
      {t(598, 954, 'အရင်စမ်းချင်ရင် အခမဲ့', 23, 800, '#7765FF')}
      {t(598, 1003, 'နေ့စဉ် 300 MB', 27, 700, '#071421')}
      {t(598, 1047, 'ရက် 30 တိုင်း 3 GB', 27, 700, '#071421')}
      {t(598, 1092, 'AuriX key · Official Outline Client', 18, 600, '#526273')}
      {t(598, 1122, 'ဖြင့် အသုံးပြုရန်', 20, 600, '#526273')}
      {t(598, 1216, 'ရယူရန် / ဝယ်ယူရန်', 20, 700, '#071421')}
      {t(598, 1263, BOT, 27, 900, '#071421')}
    </g>
    </svg>"""


def documentary(plate: Path, round_no: int) -> str:
    logo = uri(ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg")
    outline = uri(OUTLINE)
    bg = uri(plate)
    split = 540
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="top" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#071421" stop-opacity="0.98"/><stop offset="1" stop-color="#071421" stop-opacity="0"/></linearGradient>
      <linearGradient id="bottom" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#071421" stop-opacity="0"/><stop offset="1" stop-color="#071421" stop-opacity="0.96"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#000" flood-opacity="0.9"/></filter>
    </defs>
    <image href="{bg}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect width="1080" height="440" fill="url(#top)"/>
    <rect y="690" width="1080" height="660" fill="url(#bottom)"/>
    <image href="{logo}" x="64" y="48" width="250" height="82" preserveAspectRatio="xMinYMid meet"/>
    <image href="{outline}" x="910" y="52" width="104" height="104"/>
    {t(962, 181, 'OUTLINE CLIENT', 15, 800, '#F7FAFC', 'middle', family='Inter, sans-serif')}
    <g filter="url(#shadow)">
      {t(64, 190, 'VPN သုံးတာ', 49, 800)}
      {t(64, 250, 'ဘယ်လောက်များလဲ။', 49, 800)}
      {t(64, 302, 'ကိုယ့်အသုံးနဲ့ ကိုက်တာကိုပဲ ရွေးပါ။', 23, 500, '#D5DFE8')}

      {t(58, 820, 'ပုံမှန်သုံးသူ', 24, 700, '#FFC857')}
      {t(55, 955, '50', 138, 900, '#FFFFFF', family='Inter, sans-serif')}
      {t(238, 950, 'GB', 40, 800, '#FFFFFF', family='Inter, sans-serif')}
      {t(58, 1004, 'ရက် 30', 25, 600)}
      {t(58, 1065, '3,000 ကျပ်', 40, 900, '#36E2FF')}

      {t(590, 820, 'ပိုသုံးသူ', 24, 700, '#FFC857')}
      {t(585, 955, '100', 132, 900, '#FFFFFF', family='Inter, sans-serif')}
      {t(858, 950, 'GB', 40, 800, '#FFFFFF', family='Inter, sans-serif')}
      {t(590, 1004, 'ရက် 30', 25, 600)}
      {t(590, 1065, '6,000 ကျပ်', 40, 900, '#9A8BFF')}
    </g>
    <line x1="{split}" y1="760" x2="{split}" y2="1085" stroke="#F6F8FC" stroke-opacity="0.35"/>
    <line x1="58" y1="1110" x2="1022" y2="1110" stroke="#F6F8FC" stroke-opacity="0.3"/>
    {t(58, 1155, 'အရင်စမ်းချင်ရင် · နေ့စဉ် 300 MB · ရက် 30 တိုင်း 3 GB အခမဲ့', 23, 700, '#FFC857')}
    {t(58, 1202, 'AuriX key · Official Outline Client ဖြင့် အသုံးပြုရန်', 20, 500, '#D5DFE8')}
    {t(58, 1270, BOT, 28, 900, '#36E2FF')}
    {t(1022, 1270, 'ရယူရန် / ဝယ်ယူရန်', 19, 700, '#D5DFE8', 'end')}
    </svg>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", choices=["street", "real-life"], required=True)
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3], required=True)
    parser.add_argument("--plate", type=Path, required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_pricing-{args.style}_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    source = street(args.plate, args.round) if args.style == "street" else documentary(args.plate, args.round)
    svg.write_text(source, encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"style": args.style, "round": args.round, "png": str(png), "svg": str(svg), "caption": str(txt)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
