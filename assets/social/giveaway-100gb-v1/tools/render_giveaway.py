#!/usr/bin/env python3
"""Render the AuriX five-key 100 GB giveaway campaign."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/giveaway-100gb-v1"
AURIX = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"


def uri(path: Path) -> str:
    mime = {".png": "image/png", ".svg": "image/svg+xml"}[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def text(x: int, y: int, value: str, size: int, weight: int = 700, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif") -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" '
        f'font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def telegram_mark(x: int, y: int, size: int = 38) -> str:
    return f"""
    <g transform="translate({x} {y})">
      <circle cx="{size/2}" cy="{size/2}" r="{size/2}" fill="#229ED9"/>
      <path d="M{size*.20:.1f} {size*.49:.1f} L{size*.80:.1f} {size*.24:.1f} L{size*.66:.1f} {size*.80:.1f} L{size*.47:.1f} {size*.62:.1f} L{size*.35:.1f} {size*.72:.1f} L{size*.38:.1f} {size*.57:.1f} Z" fill="#FFFFFF"/>
    </g>"""


def pass_shape(x: int, y: int, rotation: int, color: str, front: bool, outline: str) -> str:
    mark = f'<image href="{outline}" x="77" y="28" width="114" height="114"/>' if front else ''
    gleam = '<path d="M34 24 H234" stroke="#FFFFFF" stroke-width="3" opacity=".32"/>'
    return f"""
    <g transform="translate({x} {y}) rotate({rotation} 134 250)" filter="url(#shadow)">
      <rect x="0" y="0" width="268" height="500" rx="34" fill="{color}"/>
      <path d="M0 356 L268 246 V500 H0 Z" fill="#071421" opacity=".28"/>
      {gleam}
      {mark}
      {text(134, 300 if front else 278, '100', 96 if front else 76, 900, '#FFFFFF', 'middle', 'Inter, sans-serif')}
      {text(134, 354 if front else 330, 'GB', 34, 900, '#FFFFFF', 'middle', 'Inter, sans-serif')}
    </g>"""


def passes(round_no: int, outline: str) -> str:
    if round_no == 0:
        specs = [(145, 480, -15, '#E7A93F', False), (270, 435, -8, '#36BFD8', False), (405, 410, 0, '#7765FF', True), (545, 435, 8, '#2CA7C8', False), (665, 480, 15, '#C89038', False)]
    elif round_no == 1:
        specs = [(175, 500, -12, '#D89C37', False), (300, 455, -6, '#2CBACF', False), (410, 418, 0, '#7765FF', True), (520, 455, 6, '#32B6D2', False), (645, 500, 12, '#D89C37', False)]
    else:
        specs = [(220, 506, -10, '#C98E34', False), (318, 466, -5, '#2AAFC6', False), (406, 390, 0, '#7765FF', True), (494, 466, 5, '#2AAFC6', False), (592, 506, 10, '#C98E34', False)]
    return ''.join(pass_shape(*spec, outline) for spec in specs)


def build_hierarchy(round_no: int) -> str:
    aurix = uri(AURIX)
    outline = uri(OUTLINE)
    final = round_no >= 4
    hero_y = 270 if final else 282
    card_y = 500 if final else 520
    envelope_top = 735 if final else 755
    envelope_peak = envelope_top - (100 if final else 150)
    envelope_bottom = 960 if final else 1015
    action_y = 1060 if final else 1150
    specs = [
        (220, card_y + 78, -10, '#C98E34', False),
        (318, card_y + 38, -5, '#2AAFC6', False),
        (406, card_y - 12, 0, '#7765FF', True),
        (494, card_y + 38, 5, '#2AAFC6', False),
        (592, card_y + 78, 10, '#C98E34', False),
    ]
    pass_group = ''.join(pass_shape(*spec, outline) for spec in specs)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#071421"/><stop offset="1" stop-color="#0D2235"/></linearGradient>
      <linearGradient id="signal" x1="0" x2="1"><stop stop-color="#FFC857"/><stop offset=".45" stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="18" stdDeviation="18" flood-color="#000000" flood-opacity=".42"/></filter>
      <radialGradient id="halo"><stop stop-color="#36E2FF" stop-opacity=".20"/><stop offset="1" stop-color="#36E2FF" stop-opacity="0"/></radialGradient>
      <clipPath id="passesClip"><rect x="0" y="0" width="1080" height="{envelope_bottom}"/></clipPath>
    </defs>
    <rect width="1080" height="1350" fill="url(#bg)"/>
    <circle cx="540" cy="690" r="470" fill="url(#halo)"/>
    <path d="M-150 1010 C220 760 240 360 640 210 C830 140 1010 155 1180 42" fill="none" stroke="url(#signal)" stroke-width="3" opacity=".16"/>
    <image href="{aurix}" x="72" y="46" width="270" height="86" preserveAspectRatio="xMinYMid meet"/>

    {text(72, hero_y, '100 GB', 132 if final else 124, 900, '#FFFFFF', family='Inter, sans-serif')}
    {text(72, hero_y+64, 'Outline VPN Key · ရက် 30', 34, 750, '#D6E0E8')}
    {text(72, hero_y+136, '၅ ယောက်အတွက် အခမဲ့', 52 if final else 48, 850, '#FFC857')}

    <g clip-path="url(#passesClip)">{pass_group}</g>
    <g filter="url(#shadow)">
      <path d="M120 {envelope_top} L540 {envelope_peak} L960 {envelope_top} V{envelope_bottom} H120 Z" fill="#102A40" stroke="#36536B" stroke-width="3"/>
      <path d="M120 {envelope_top} L540 {envelope_top+182} L960 {envelope_top}" fill="#0A1A28" stroke="#35516B" stroke-width="3"/>
      <path d="M120 {envelope_bottom} L430 811 Q540 747 650 811 L960 {envelope_bottom} Z" fill="#0D2235"/>
      <path d="M150 {envelope_bottom-20} L467 789 Q540 745 613 789 L930 {envelope_bottom-20}" fill="none" stroke="url(#signal)" stroke-width="5" opacity=".85"/>
    </g>

    {text(540, action_y, '“100GB စမ်းမယ်”', 60 if final else 46, 900, '#FFFFFF', 'middle')}
    {text(540, action_y+66, 'လို့ Comment ရေးပါ', 42 if final else 32, 750, '#36E2FF', 'middle')}
    </svg>"""


def build(round_no: int) -> str:
    if round_no >= 3:
        return build_hierarchy(round_no)
    aurix = uri(AURIX)
    outline = uri(OUTLINE)
    hero_y = 270 if round_no == 0 else 282
    instruction_y = 990 if round_no == 0 else 970
    free_size = 46 if round_no < 2 else 50
    footer_top = 1090 if round_no == 0 else 1080
    envelope = f"""
    <g filter="url(#shadow)">
      <path d="M120 735 L540 585 L960 735 V1012 H120 Z" fill="#102A40" stroke="#36536B" stroke-width="3"/>
      <path d="M120 735 L540 917 L960 735" fill="#0A1A28" stroke="#35516B" stroke-width="3"/>
      <path d="M120 1012 L430 808 Q540 744 650 808 L960 1012 Z" fill="#0D2235"/>
      <path d="M150 992 L467 786 Q540 742 613 786 L930 992" fill="none" stroke="url(#signal)" stroke-width="5" opacity=".8"/>
    </g>"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#071421"/><stop offset="1" stop-color="#0D2235"/></linearGradient>
      <linearGradient id="signal" x1="0" x2="1"><stop stop-color="#FFC857"/><stop offset=".45" stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="18" stdDeviation="18" flood-color="#000000" flood-opacity=".42"/></filter>
      <radialGradient id="halo"><stop stop-color="#36E2FF" stop-opacity=".20"/><stop offset="1" stop-color="#36E2FF" stop-opacity="0"/></radialGradient>
    </defs>
    <rect width="1080" height="1350" fill="url(#bg)"/>
    <circle cx="540" cy="650" r="470" fill="url(#halo)"/>
    <path d="M-120 870 C220 620 290 270 650 170 C835 120 1010 120 1190 20" fill="none" stroke="url(#signal)" stroke-width="3" opacity=".18"/>
    <image href="{aurix}" x="72" y="46" width="270" height="86" preserveAspectRatio="xMinYMid meet"/>
    {text(72, hero_y, '100 GB သုံးကြည့်မလား?', 62, 900)}
    {text(72, hero_y+72, '၅ ယောက်အတွက် အခမဲ့', free_size, 800, '#FFC857')}
    {text(72, hero_y+126, 'Outline VPN Key · ရက် 30', 28, 700, '#D6E0E8')}
    {passes(round_no, outline)}
    {envelope}
    {text(540, instruction_y, '“100GB စမ်းမယ်” လို့ Comment ရေးပါ', 35 if round_no < 2 else 38, 800, '#FFFFFF', 'middle')}
    <line x1="72" y1="{footer_top}" x2="1008" y2="{footer_top}" stroke="#35516B"/>
    {telegram_mark(74, footer_top+34)}
    {text(132, footer_top+64, 't.me/aurix_outline_vpn_bot', 28, 750, '#36E2FF', family='Inter, sans-serif')}
    {telegram_mark(74, footer_top+99)}
    {text(132, footer_top+129, 't.me/+oA18TDWAD9NiNWU1', 28, 750, '#F6F8FC', family='Inter, sans-serif')}
    {telegram_mark(74, footer_top+164)}
    {text(132, footer_top+194, 't.me/AurixDigitalStore', 28, 750, '#9A8BFF', family='Inter, sans-serif')}
    </svg>"""


def caption() -> str:
    return """100 GB သုံးကြည့်မလား?

AuriX Outline VPN Key 100 GB ကို ၅ ယောက်အတွက် အခမဲ့ပေးပါမယ်။ Key တစ်ခုစီကို ရက် 30 အသုံးပြုနိုင်ပါတယ်။ လူတိုင်း ပါဝင်နိုင်ပြီး ဝယ်ယူထားဖို့ မလိုပါဘူး။

ပါဝင်ဖို့—
ဒီ Post အောက်မှာ “100GB စမ်းမယ်” လို့ Comment တစ်ကြိမ်ပဲ ရေးပေးပါ။ Facebook account တစ်ခုကို entry တစ်ခုသာ ထည့်တွက်ပါမယ်။ Share လုပ်ဖို့၊ သူငယ်ချင်း Tag တွဲဖို့၊ Page Follow လုပ်ဖို့ မလိုပါဘူး။

Post တင်ပြီး ၇ ရက်အကြာ မြန်မာစံတော်ချိန် ည ၈:၀၀ မှာ ပိတ်ပါမယ်။ အကျုံးဝင်တဲ့ Comment တွေထဲက ၅ ယောက်ကို ကျပန်းရွေးပြီး AuriX Page နဲ့ Telegram Channel မှာ ကြေညာပါမယ်။

ဒီ 100 GB က ဖုန်း SIM ဒေတာမဟုတ်ပါဘူး။ Official Outline Client ထဲမှာ Key ထည့်ပြီး အသုံးပြုရတဲ့ VPN အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။ ဆုရသူဆီက ငွေ၊ OTP သို့မဟုတ် payment PIN မတောင်းပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Winner ကြေညာချက်နဲ့ သတင်းများ — https://t.me/AurixDigitalStore

ဤအစီအစဉ်ကို AuriX က စီစဉ်တာဖြစ်ပြီး Facebook က sponsor၊ စီမံသူ သို့မဟုတ် အတည်ပြုသူ မဟုတ်ပါဘူး။ ပါဝင်သူများက ဤအစီအစဉ်နှင့်သက်ဆိုင်သည့် တာဝန်မှ Facebook ကို ကင်းလွတ်ခွင့်ပြုကြောင်း သဘောတူပါသည်။ AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။

#AuriXVPN #OutlineVPN #GiveawayMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3, 4], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-100gb-five-key-giveaway_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg_text = "\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n"
    svg.write_text(svg_text, encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"round": args.round, "image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
