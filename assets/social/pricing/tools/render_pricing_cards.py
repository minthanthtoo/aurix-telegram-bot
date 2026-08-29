#!/usr/bin/env python3
"""Render exact-copy AuriX pricing cards over text-free visual plates."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
PRICING = ROOT / "assets/social/pricing"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = "@aurix_outline_vpn_bot"


def data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def text(x: int, y: int, value: str, size: int, weight: int = 600, fill: str = "#F7FAFC", anchor: str = "start") -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" '
        f'font-family="Noto Sans Myanmar, Inter, sans-serif" font-size="{size}" '
        f'font-weight="{weight}">{escape(value)}</text>'
    )


def paid_card(x: int, y: int, quota: str, price: str, label: str, accent: str, featured: bool = False) -> str:
    border = "#FFC857" if featured else accent
    badge = ""
    if featured:
        badge = (
            '<rect x="24" y="24" width="190" height="42" rx="21" fill="#FFC857"/>'
            + text(119, 53, "စတင်ရွေးချယ်ရန်", 20, 700, "#071421", "middle")
        )
    return f"""
    <g transform="translate({x} {y})">
      <rect width="424" height="310" rx="34" fill="#091928" fill-opacity="0.94" stroke="{border}" stroke-width="{3 if featured else 2}"/>
      {badge}
      {text(28, 100 if featured else 62, label, 25, 600, '#C9D6E2')}
      {text(28, 176 if featured else 145, quota, 66, 800, '#FFFFFF')}
      {text(28, 229 if featured else 198, 'ရက် 30 အသုံးပြုနိုင်', 24, 500, '#B5C6D5')}
      <line x1="28" y1="250" x2="396" y2="250" stroke="#35516B"/>
      {text(28, 291, price, 35, 800, accent)}
    </g>"""


def build(style: str, round_no: int, plate: Path) -> str:
    bg = data_uri(plate)
    logo = data_uri(LOGO)
    if style == "a":
        shade = """
        <linearGradient id="shade" x1="0" x2="1"><stop offset="0" stop-color="#071421" stop-opacity="0.98"/><stop offset="0.55" stop-color="#071421" stop-opacity="0.80"/><stop offset="1" stop-color="#071421" stop-opacity="0.28"/></linearGradient>
        """
        paid = paid_card(76, 530, "50 GB", "3,000 ကျပ်", "ပုံမှန်သုံးသူ", "#36E2FF", round_no < 2) + paid_card(580, 530, "100 GB", "6,000 ကျပ်", "ပိုသုံးသူ", "#9A8BFF")
        free_y = 885
        art_opacity = 0.78 if round_no == 0 else 0.68
    else:
        shade = """
        <linearGradient id="shade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#071421" stop-opacity="0.90"/><stop offset="0.42" stop-color="#071421" stop-opacity="0.48"/><stop offset="1" stop-color="#071421" stop-opacity="0.96"/></linearGradient>
        """
        band_rx = 18 if round_no == 2 else 34
        paid = f"""
        <g transform="translate(76 620)">
          <rect width="928" height="142" rx="{band_rx}" fill="#091928" fill-opacity="0.91" stroke="#36E2FF" stroke-width="2"/>
          {text(30, 46, 'ပုံမှန်သုံးသူ', 22, 600, '#C9D6E2')}
          {text(30, 104, '50 GB', 50, 800)}
          {text(278, 87, 'ရက် 30', 25, 600, '#B5C6D5')}
          {text(890, 88, '3,000 ကျပ်', 36, 800, '#36E2FF', 'end')}
        </g>
        <g transform="translate(76 780)">
          <rect width="928" height="142" rx="{band_rx}" fill="#091928" fill-opacity="0.91" stroke="#9A8BFF" stroke-width="2"/>
          {text(30, 46, 'ပိုသုံးသူ', 22, 600, '#C9D6E2')}
          {text(30, 104, '100 GB', 50, 800)}
          {text(278, 87, 'ရက် 30', 25, 600, '#B5C6D5')}
          {text(890, 88, '6,000 ကျပ်', 36, 800, '#9A8BFF', 'end')}
        </g>"""
        free_y = 950
        art_opacity = 0.86 if round_no == 0 else 0.78

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>{shade}<filter id="shadow"><feDropShadow dx="0" dy="12" stdDeviation="20" flood-color="#000" flood-opacity="0.35"/></filter></defs>
    <rect width="1080" height="1350" fill="#071421"/>
    <image href="{bg}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice" opacity="{art_opacity}"/>
    <rect width="1080" height="1350" fill="url(#shade)"/>
    <image href="{logo}" x="76" y="52" width="250" height="82" preserveAspectRatio="xMinYMid meet"/>
    {text(76, 198, 'AURIX VPN အစီအစဉ်များ', 22, 700, '#36E2FF')}
    {text(76, 270, 'သုံးမယ့်ပမာဏနဲ့', 50, 800)}
    {text(76, 334, 'ကိုက်တာကို ရွေးပါ။', 50, 800)}
    {text(76, 388, 'ပမာဏ · သက်တမ်း · ဈေးနှုန်း ရှင်းရှင်းလင်းလင်း', 24, 500, '#C9D6E2')}
    <g filter="url(#shadow)">{paid}</g>
    <g transform="translate(76 {free_y})">
      <rect width="928" height="116" rx="28" fill="#0D2235" fill-opacity="0.96" stroke="#35516B"/>
      {text(28, 42, 'အရင်စမ်းချင်ရင် အခမဲ့', 22, 700, '#FFC857')}
      {text(28, 87, 'နေ့စဉ် 300 MB', 28, 700)}
      {text(330, 87, '·', 28, 500, '#66829A')}
      {text(370, 87, 'ရက် 30 တိုင်း 3 GB', 28, 700)}
    </g>
    {text(76, 1080 if style == 'a' else 1110, 'AuriX key ကို Official Outline Client ဖြင့် အသုံးပြုရန်', 22, 500, '#B5C6D5')}
    <line x1="76" y1="1160" x2="1004" y2="1160" stroke="#29445C"/>
    {text(76, 1212, 'ဝယ်ယူရန်', 24, 600, '#C9D6E2')}
    {text(76, 1262, BOT, 31, 800, '#36E2FF')}
    {text(1004, 1262, 'AuriX · Clear access', 20, 600, '#8096AA', 'end')}
    </svg>"""


def caption() -> str:
    return """VPN လိုတဲ့အချိန် အဆင်ပြေပြေသုံးနိုင်ဖို့ ကိုယ့်အသုံးနဲ့ကိုက်တဲ့ AuriX အစီအစဉ်ကို ရွေးနိုင်ပါတယ်။

💎 ပုံမှန်သုံးသူ — 50 GB · ရက် 30 · 3,000 ကျပ်
💠 ပိုသုံးသူ — 100 GB · ရက် 30 · 6,000 ကျပ်

အရင်စမ်းချင်ရင်—
🎁 နေ့စဉ် 300 MB အခမဲ့
🚀 ရက် 30 တိုင်း 3 GB အခမဲ့

AuriX Bot ကရတဲ့ access key ကို Official Outline Client ထဲထည့်ပြီး အသုံးပြုရပါတယ်။ ဒီမှာဖော်ပြတဲ့ GB ပမာဏက ဖုန်း SIM ဒေတာပက်ကေ့ချ် မဟုတ်ဘဲ AuriX VPN key အတွက် သတ်မှတ်ထားတဲ့ အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။

🤖 ရယူရန်နှင့် ဝယ်ယူရန် — https://t.me/aurix_outline_vpn_bot
💬 မရှင်းတာမေးရန် — https://t.me/+oA18TDWAD9NiNWU1
📣 သတင်းများ — https://t.me/AurixDigitalStore

အခပေးအစီအစဉ်ကို ငွေလွှဲပြေစာ စစ်ဆေးအတည်ပြုပြီးမှ စတင်ပေးပါတယ်။ ရရှိနိုင်မှုအပေါ် မူတည်ပါတယ်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", choices=["a", "b"], required=True)
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--plate", type=Path, required=True)
    parser.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    target = PRICING / ("exports" if args.final else "iterations")
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_pricing-style-{args.style}_r{args.round}"
    svg_path = target / f"{stem}.svg"
    png_path = target / f"{stem}.png"
    txt_path = target / f"{stem}.txt"
    svg_path.write_text(build(args.style, args.round, args.plate.resolve()), encoding="utf-8")
    txt_path.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg_path), "-o", str(png_path)], check=True)
    print(json.dumps({"style": args.style, "round": args.round, "png": str(png_path), "svg": str(svg_path), "caption": str(txt_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
