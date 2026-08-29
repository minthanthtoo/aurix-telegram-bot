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
PAYMENTS = [
    ("KBZPay", ROOT / "brand/payments/official/kbzpay-app-icon.png"),
    ("WavePay", ROOT / "brand/payments/official/wavepay-app-icon.png"),
    ("AYA Pay", ROOT / "brand/payments/official/ayapay-app-icon.png"),
    ("uabpay", ROOT / "brand/payments/official/uabpay-app-icon.jpg"),
    ("CB Pay", ROOT / "brand/payments/official/cbpay-app-icon.jpg"),
]
GROUP = "https://t.me/+oA18TDWAD9NiNWU1"


def uri(path: Path) -> str:
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }[path.suffix.lower()]
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
    cue_size = 22 if len(cue) > 14 else 25
    return f"""
    <g transform="translate({x} {y + lift}) rotate({angle} 146 274)" filter="url(#shadow)">
      <path d="M0 0 H236 L292 56 V548 H0 Z" fill="#0C1C2B" stroke="{accent}" stroke-width="{border}"/>
      <path d="M236 0 L292 56 H236 Z" fill="{accent}" opacity="0.95"/>
      <rect x="0" y="0" width="12" height="548" fill="{accent}"/>
      {notches}
      {text(26, 58, number, 70, 800, accent, family='Inter, sans-serif', opacity=0.33)}
      {text(26, 112, cue, cue_size, 700, '#F6F8FC')}
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


def payment_rail(round_no: int) -> str:
    icon_size = 66 if round_no == 0 else 76
    panel_y = 1038
    panel_height = 146 if round_no == 0 else 142
    border_opacity = 0.55 if round_no == 0 else 0.8
    separators = ""
    if round_no == 0:
        separators = "".join(
            f'<line x1="{x}" y1="1062" x2="{x}" y2="1161" stroke="#35516B" opacity="0.6"/>'
            for x in (258, 446, 634, 822)
        )
    elif round_no == 1:
        separators = '<path d="M88 1170 H992" stroke="#35516B" stroke-dasharray="5 12" opacity="0.55"/>'
    else:
        separators = (
            '<circle cx="70" cy="1110" r="9" fill="#071421"/>'
            '<circle cx="1010" cy="1110" r="9" fill="#071421"/>'
            '<path d="M92 1167 H988" stroke="#35516B" stroke-dasharray="4 14" opacity="0.38"/>'
        )
    icons = []
    centers = (164, 352, 540, 728, 916)
    for (label, path), center in zip(PAYMENTS, centers):
        icons.append(
            f'<image href="{uri(path)}" x="{center - icon_size / 2}" y="1054" width="{icon_size}" height="{icon_size}" '
            f'preserveAspectRatio="xMidYMid slice"/>'
            + text(center, 1160, label, 17, 700, '#DDE6ED', 'middle', 'Inter, sans-serif')
        )
    return f"""
    {text(70, 1014, 'Wallet ၅ မျိုးနဲ့ ငွေလွှဲနိုင်ပါတယ်', 23, 700, '#FFC857')}
    <path d="M70 {panel_y} H982 L1010 {panel_y + 28} V{panel_y + panel_height} H70 Z" fill="#0C1C2B" stroke="#35516B" stroke-width="2" opacity="0.98"/>
    <path d="M982 {panel_y} L1010 {panel_y + 28} H982 Z" fill="#36E2FF" opacity="0.7"/>
    {separators}
    {''.join(icons)}
    """


def build(round_no: int, variant: str = "original") -> str:
    aurix = uri(AURIX)
    outline = uri(OUTLINE)
    relation_y = 390 if round_no == 0 else 405
    cards_y = 438 if variant == "payment" else (458 if round_no == 0 else 448)
    if variant in {"natural", "payment"}:
        series = "OUTLINE VPN · တစ်လသုံး KEY များ"
        headline_1 = "စိတ်ကြိုက် Plan"
        headline_2 = "ရွေးယူနိုင်ပါပြီ။"
        cue_1 = "အစမ်းသုံး Free Plan"
        cue_2 = "ပုံမှန် တစ်လသုံး"
        cue_3 = "အဝသုံး တစ်လ Plan"
        footer_action = "စုံစမ်းမေးမြန်းရန် · ဝယ်ယူရန်"
        footer_name = "AuriX Telegram Chat Group"
    else:
        series = "AURIX VPN အစီအစဉ်များ"
        headline_1 = "ကိုယ့်အသုံးနဲ့ ကိုက်တာ"
        headline_2 = "ဘယ်တစ်ခုလဲ။"
        cue_1 = "အရင်စမ်းမယ်"
        cue_2 = "ပုံမှန်သုံးမယ်"
        cue_3 = "ပိုသုံးမယ်"
        footer_action = "မေးရန် · ရယူရန် · ဝယ်ယူရန်"
        footer_name = "AuriX Telegram အကူအညီအဖွဲ့"
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

    {text(70, 234, series, 22, 800, '#36E2FF')}
    {text(70, 302, headline_1, 47, 800)}
    {text(70, 360, headline_2, 47, 800)}
    {text(70 if round_no >= 1 else 1010, relation_y, 'AuriX key · Outline Client ဖြင့် အသုံးပြုရန်', 20, 500, '#B9C8D5', 'start' if round_no >= 1 else 'end')}

    {plan_pass(70, cards_y, '01', cue_1, '3 GB', 'အခမဲ့', '#FFC857', 'ရက် 30 တိုင်း ရယူနိုင်', round_no)}
    {plan_pass(394, cards_y, '02', cue_2, '50 GB', '3,000 ကျပ်', '#36E2FF', 'ရက် 30 အသုံးပြုနိုင်', round_no, True)}
    {plan_pass(718, cards_y, '03', cue_3, '100 GB', '6,000 ကျပ်', '#9A8BFF', 'ရက် 30 အသုံးပြုနိုင်', round_no)}

    {payment_rail(round_no) if variant == 'payment' else ''}
    <line x1="70" y1="{1208 if variant == 'payment' else 1050}" x2="1010" y2="{1208 if variant == 'payment' else 1050}" stroke="#35516B"/>
    {telegram_mark(72, 1228 if variant == 'payment' else 1103, 58 if variant == 'payment' else 82)}
    {text(150 if variant == 'payment' else 178, 1252 if variant == 'payment' else 1137, footer_action, 21 if variant == 'payment' else 24, 700, '#F6F8FC')}
    {text(150 if variant == 'payment' else 178, 1297 if variant == 'payment' else 1185, 't.me/+oA18TDWAD9NiNWU1' if variant == 'payment' else footer_name, 27 if variant == 'payment' else 21, 800 if variant == 'payment' else 500, '#36E2FF' if variant == 'payment' else '#9DB1C3', family='Inter, sans-serif' if variant == 'payment' else 'Noto Sans Myanmar, Inter, sans-serif')}
    {text(178, 1243, 't.me/+oA18TDWAD9NiNWU1', 31, 800, '#36E2FF', family='Inter, sans-serif') if variant != 'payment' else ''}
    {text(1010, 1297 if variant == 'payment' else 1243, 'Clear access. Human help.', 17 if variant == 'payment' else 19, 600, '#71889C', 'end', 'Inter, sans-serif')}
    </svg>"""


def caption(variant: str = "original") -> str:
    if variant in {"natural", "payment"}:
        payment_copy = "" if variant == "natural" else """

အောက်ပါ Wallet ၅ မျိုးနဲ့ ငွေလွှဲနိုင်ပါတယ်—
KBZPay · WavePay · AYA Pay · uabpay · CB Pay

ငွေလွှဲပြီးပါက ပြေစာကို AuriX Telegram Chat Group ထဲ ပို့ပေးပါ။ ဝန်ထမ်းက ပြေစာစစ်ဆေးအတည်ပြုပြီးမှ Key အသုံးပြုခွင့်ကို စတင်ပေးပါတယ်။ ဒီ Wallet များဟာ AuriX နဲ့ တရားဝင်ပူးပေါင်းထားတဲ့ payment partner များလို့ မဆိုလိုပါဘူး။
"""
        return f"""Outline VPN တစ်လသုံး Key များကို စိတ်ကြိုက် Plan ရွေးယူနိုင်ပါပြီ။

အစမ်းသုံး Free Plan
🎁 3 GB · ရက် 30 · အခမဲ့
ရက် 30 တိုင်း တစ်ကြိမ် ရယူနိုင်ပါတယ်။

ပုံမှန် တစ်လသုံး Plan
💎 50 GB · ရက် 30 · 3,000 ကျပ်

အဝသုံး တစ်လ Plan
💠 100 GB · ရက် 30 · 6,000 ကျပ်

“အဝသုံး” Plan မှာလည်း VPN key အသုံးပြုခွင့်ပမာဏကို 100 GB သတ်မှတ်ထားပါတယ်။ Unlimited Plan မဟုတ်ပါဘူး။ ဖော်ပြထားတဲ့ GB ပမာဏတွေက ဖုန်း SIM ဒေတာမဟုတ်ဘဲ AuriX VPN key အတွက် အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။

AuriX Bot ကရတဲ့ access key ကို Official Outline Client ထဲထည့်ပြီး အသုံးပြုရပါတယ်။
{payment_copy}

စုံစမ်းမေးမြန်းရန်နှင့် ဝယ်ယူရန်—
AuriX Telegram Chat Group
{GROUP}

အခပေးအစီအစဉ်ကို ငွေလွှဲပြေစာ စစ်ဆေးအတည်ပြုပြီးမှ စတင်ပေးပါတယ်။ ရရှိနိုင်မှုအပေါ် မူတည်ပါတယ်။

AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ AuriX က Outline-compatible access key ကို သီးခြားဝန်ဆောင်မှုပေးပြီး Official Outline Client ဖြင့် အသုံးပြုရပါသည်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""
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
    parser.add_argument("--variant", choices=["original", "natural", "payment"], default="original")
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    suffix = {"original": "", "natural": "-natural-copy", "payment": "-payment-methods"}[args.variant]
    stem = f"{args.stamp}_aurix-three-plans{suffix}_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text(build(args.round, args.variant), encoding="utf-8")
    txt.write_text(caption(args.variant) + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"round": args.round, "image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
