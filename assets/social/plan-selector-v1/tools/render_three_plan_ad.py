#!/usr/bin/env python3
"""Render a reference-aware, non-interactive AuriX three-plan social ad."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/plan-selector-v1"
AURIX = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
GROUP = "https://t.me/+oA18TDWAD9NiNWU1"


def uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def text(x: int, y: int, value: str, size: int, weight: int = 600, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif", opacity: float = 1.0) -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" opacity="{opacity}" '
        f'font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def plan_pass(x: int, y: int, number: str, cue: str, quota: str, price: str, accent: str, term: str, round_no: int, featured: bool = False) -> str:
    width = 292
    height = 548
    border = 4 if featured and round_no >= 1 else 2
    lift = -18 if featured and round_no >= 1 else 0
    angle = 0
    if round_no >= 1:
        angle = -1.15 if number == "01" else (0.8 if number == "03" else 0)
    notches = ""
    if round_no >= 1:
        notches = (
            f'<circle cx="0" cy="274" r="{8 if round_no >= 2 else 14}" fill="#071421"/>'
            f'<circle cx="292" cy="274" r="{8 if round_no >= 2 else 14}" fill="#071421"/>'
            f'<line x1="18" y1="274" x2="274" y2="274" stroke="#557086" stroke-dasharray="7 12" opacity="{0.25 if round_no >= 2 else 0.42}"/>'
        )
    price_size = 38 if len(price) < 9 else 34
    return f"""
    <g transform="translate({x} {y + lift}) rotate({angle} 146 274)" filter="url(#shadow)">
      <path d="M0 0 H236 L292 56 V548 H0 Z" fill="#0C1C2B" stroke="{accent}" stroke-width="{border}"/>
      <path d="M236 0 L292 56 H236 Z" fill="{accent}" opacity="0.95"/>
      <rect x="0" y="0" width="12" height="548" fill="{accent}"/>
      {notches}
      {text(26, 58, number, 70, 800, accent, family='Inter, sans-serif', opacity=0.33)}
      {text(26, 112, cue, 25, 700, '#F6F8FC')}
      <line x1="26" y1="138" x2="260" y2="138" stroke="{accent}" stroke-width="2" opacity="0.65"/>
      {text(146, 252, quota, 72 if len(quota) <= 5 else 62, 900, '#FFFFFF', 'middle', 'Inter, sans-serif')}
      {text(146, 300, 'AuriX VPN key', 19, 600, '#9DB1C3', 'middle', 'Inter, sans-serif')}
      {text(146, 350, 'ရက် 30', 27, 700, '#D6E0E8', 'middle')}
      <line x1="26" y1="382" x2="266" y2="382" stroke="#35516B"/>
      {text(146, 448, price, price_size, 900, accent, 'middle')}
      {text(146, 498, term, 20, 600, '#B9C8D5', 'middle')}
    </g>"""


def telegram_mark(x: int, y: int, size: int) -> str:
    # Simple official-color destination mark; drawn locally so it remains crisp in SVG.
    return f"""
    <g transform="translate({x} {y})">
      <circle cx="{size/2}" cy="{size/2}" r="{size/2}" fill="#229ED9"/>
      <path d="M{size*0.21:.1f} {size*0.48:.1f} L{size*0.79:.1f} {size*0.24:.1f} L{size*0.66:.1f} {size*0.79:.1f} L{size*0.47:.1f} {size*0.62:.1f} L{size*0.36:.1f} {size*0.72:.1f} L{size*0.38:.1f} {size*0.57:.1f} Z" fill="#FFFFFF"/>
    </g>"""


def build(round_no: int) -> str:
    aurix = uri(AURIX)
    outline = uri(OUTLINE)
    relation_y = 390 if round_no == 0 else 405
    cards_y = 458 if round_no == 0 else 448
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#071421"/><stop offset="1" stop-color="#0D2235"/></linearGradient>
      <linearGradient id="signal" x1="0" x2="1"><stop stop-color="#36E2FF"/><stop offset="0.55" stop-color="#5BA7FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="18" stdDeviation="18" flood-color="#000" flood-opacity="0.38"/></filter>
    </defs>
    <rect width="1080" height="1350" fill="url(#bg)"/>
    <path d="M-80 1060 C190 840 175 390 540 310 C830 246 912 70 1160 122" fill="none" stroke="url(#signal)" stroke-width="3" opacity="0.18"/>
    <path d="M-50 1120 C220 920 250 500 560 400 C855 305 930 154 1130 182" fill="none" stroke="#FFC857" stroke-width="1.5" opacity="0.12"/>
    <circle cx="913" cy="147" r="82" fill="#36E2FF" opacity="0.035"/>

    <image href="{aurix}" x="70" y="46" width="300" height="100" preserveAspectRatio="xMinYMid meet"/>
    <image href="{outline}" x="915" y="52" width="96" height="96"/>
    {text(963, 173, 'OUTLINE CLIENT', 14, 800, '#F6F8FC', 'middle', 'Inter, sans-serif')}

    {text(70, 234, 'AURIX VPN အစီအစဉ်များ', 22, 800, '#36E2FF')}
    {text(70, 302, 'ကိုယ့်အသုံးနဲ့ ကိုက်တာ', 47, 800)}
    {text(70, 360, 'ဘယ်တစ်ခုလဲ။', 47, 800)}
    {text(70 if round_no >= 1 else 1010, relation_y, 'AuriX key · Outline Client ဖြင့် အသုံးပြုရန်', 20, 500, '#B9C8D5', 'start' if round_no >= 1 else 'end')}

    {plan_pass(70, cards_y, '01', 'အရင်စမ်းမယ်', '3 GB', 'အခမဲ့', '#FFC857', 'ရက် 30 တိုင်း ရယူနိုင်', round_no)}
    {plan_pass(394, cards_y, '02', 'ပုံမှန်သုံးမယ်', '50 GB', '3,000 ကျပ်', '#36E2FF', 'ရက် 30 အသုံးပြုနိုင်', round_no, True)}
    {plan_pass(718, cards_y, '03', 'ပိုသုံးမယ်', '100 GB', '6,000 ကျပ်', '#9A8BFF', 'ရက် 30 အသုံးပြုနိုင်', round_no)}

    <line x1="70" y1="1050" x2="1010" y2="1050" stroke="#35516B"/>
    {telegram_mark(72, 1103, 82)}
    {text(178, 1137, 'မေးရန် · ရယူရန် · ဝယ်ယူရန်', 24, 700, '#F6F8FC')}
    {text(178, 1185, 'AuriX Telegram အကူအညီအဖွဲ့', 21, 500, '#9DB1C3')}
    {text(178, 1243, 't.me/+oA18TDWAD9NiNWU1', 31, 800, '#36E2FF', family='Inter, sans-serif')}
    {text(1010, 1243, 'Clear access. Human help.', 19, 600, '#71889C', 'end', 'Inter, sans-serif')}
    </svg>"""


def caption() -> str:
    return f"""ကိုယ့်အသုံးနဲ့ ကိုက်တဲ့ AuriX VPN အစီအစဉ်ကို ရွေးနိုင်ပါတယ်။

အရင်စမ်းချင်ရင်
🎁 3 GB · ရက် 30 · အခမဲ့
ရက် 30 တိုင်း တစ်ကြိမ် ရယူနိုင်ပါတယ်။

ပုံမှန်သုံးမယ်ဆိုရင်
💎 50 GB · ရက် 30 · 3,000 ကျပ်

ပိုသုံးမယ်ဆိုရင်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

AuriX Bot ကရတဲ့ access key ကို Official Outline Client ထဲထည့်ပြီး အသုံးပြုရပါတယ်။ ဒီ GB ပမာဏတွေက ဖုန်း SIM ဒေတာပက်ကေ့ချ် မဟုတ်ဘဲ AuriX VPN key အတွက် သတ်မှတ်ထားတဲ့ အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။

မေးမြန်းရန်၊ ရယူရန်နှင့် ဝယ်ယူရန် AuriX Telegram အကူအညီအဖွဲ့ထဲ ဝင်နိုင်ပါတယ်—
{GROUP}

အခပေးအစီအစဉ်ကို ငွေလွှဲပြေစာ စစ်ဆေးအတည်ပြုပြီးမှ စတင်ပေးပါတယ်။ ရရှိနိုင်မှုအပေါ် မူတည်ပါတယ်။

AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ AuriX က Outline-compatible access key ကို သီးခြားဝန်ဆောင်မှုပေးပြီး Official Outline Client ဖြင့် အသုံးပြုရပါသည်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-three-plans_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text(build(args.round), encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"round": args.round, "image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
