#!/usr/bin/env python3
"""Render the AuriX Bot paid-intent Facebook static campaign."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/bot-paid-intent-v1"
AURIX = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = ROOT / "brand/v4/exports/aurix-telegram-bot-avatar-v4-1024.png"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
PAYMENTS = [
    ("KBZPay", ROOT / "brand/payments/official/kbzpay-app-icon.png"),
    ("WavePay", ROOT / "brand/payments/official/wavepay-app-icon.png"),
    ("AYA Pay", ROOT / "brand/payments/official/ayapay-app-icon.png"),
    ("uabpay", ROOT / "brand/payments/official/uabpay-app-icon.jpg"),
    ("CB Pay", ROOT / "brand/payments/official/cbpay-app-icon.jpg"),
]


def uri(path: Path) -> str:
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
    }[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(x: int, y: int, value: str, size: int, weight: int = 600, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif", opacity: float = 1.0) -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" opacity="{opacity}" '
        f'font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def telegram_mark(x: int, y: int, size: int) -> str:
    return f"""
    <g transform="translate({x} {y})">
      <circle cx="{size / 2}" cy="{size / 2}" r="{size / 2}" fill="#229ED9"/>
      <path d="M{size*.21:.1f} {size*.48:.1f} L{size*.79:.1f} {size*.24:.1f} L{size*.66:.1f} {size*.79:.1f} L{size*.47:.1f} {size*.62:.1f} L{size*.36:.1f} {size*.72:.1f} L{size*.38:.1f} {size*.57:.1f} Z" fill="#FFFFFF"/>
    </g>"""


def payment_row(round_no: int) -> str:
    centers = (140, 330, 520, 710, 900)
    size = 72 if round_no >= 3 else (58 if round_no == 0 else 64)
    marks = []
    for (label, path), center in zip(PAYMENTS, centers):
        marks.append(
            f'<image href="{uri(path)}" x="{center-size/2}" y="1014" width="{size}" height="{size}" preserveAspectRatio="xMidYMid slice"/>'
            + ('' if round_no >= 3 else t(center, 1105, label, 16, 700, '#DDE6ED', 'middle', 'Inter, sans-serif'))
        )
    border = '<path d="M70 990 H1010" stroke="#35516B"/>' if round_no == 0 else ''
    return f"""
    {'' if round_no >= 3 else t(70, 972, 'ငွေလွှဲနိုင်တဲ့ Wallet များ', 20, 700, '#9DB1C3')}
    {border}
    {''.join(marks)}
    """


def build(round_no: int) -> str:
    aurix = uri(AURIX)
    bot = uri(BOT)
    outline = uri(OUTLINE)
    panel = (
        '<path d="M70 400 H970 L1010 440 V744 H70 Z" fill="#0C1C2B" stroke="#35516B" stroke-width="2"/>'
        if round_no == 0
        else '<path d="M70 744 H1010" stroke="#35516B"/>'
    )
    speed_size = 92 if round_no < 2 else 102
    speed_x = 410 if round_no == 0 else 390
    free_fill = '#D6E0E8' if round_no < 2 else '#FFC857'
    process_y = 735 if round_no == 0 else 722
    clean = round_no >= 3
    headline_y = 240 if clean else 282
    header_outline = "" if round_no >= 2 else f"""
    <image href="{outline}" x="930" y="52" width="78" height="78"/>
    {t(969, 158, 'OUTLINE CLIENT', 13, 800, '#F6F8FC', 'middle', 'Inter, sans-serif')}
    """
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#071421"/><stop offset="1" stop-color="#0D2235"/></linearGradient>
      <linearGradient id="signal" x1="0" x2="1"><stop stop-color="#36E2FF"/><stop offset=".58" stop-color="#5BA7FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#000" flood-opacity=".34"/></filter>
    </defs>
    <rect width="1080" height="1350" fill="url(#bg)"/>
    <path d="M-120 1050 C230 830 150 480 520 350 C820 244 930 105 1180 138" fill="none" stroke="url(#signal)" stroke-width="3" opacity=".18"/>
    <path d="M-80 1120 C210 940 260 560 570 438 C850 328 930 215 1140 225" fill="none" stroke="#FFC857" stroke-width="1.5" opacity=".11"/>

    <image href="{aurix}" x="70" y="45" width="285" height="92" preserveAspectRatio="xMinYMid meet"/>
    {header_outline}

    {'' if clean else t(70, 215, 'AURIX BOT · OUTLINE KEY', 21, 800, '#36E2FF', family='Inter, sans-serif')}
    {t(70, headline_y, 'အလုပ်အတွက် VPN လိုတဲ့အခါ', 46 if clean else 44, 800)}
    {t(70, headline_y+62, 'AuriX Bot ကနေ စလိုက်ပါ။', 46 if clean else 44, 800)}

    {panel}
    <image href="{bot}" x="88" y="438" width="240" height="240" filter="url(#shadow)"/>
    {'' if clean else t(208, 706, '@aurix_outline_vpn_bot', 16, 700, '#9DB1C3', 'middle', 'Inter, sans-serif')}

    {t(speed_x, 470, 'ပုံမှန်ဆို', 24, 700, '#FFC857')}
    {t(speed_x, 590, '< 1 MIN*', speed_size, 900, '#FFFFFF', family='Inter, sans-serif')}
    {t(speed_x, 642, 'ငွေလွှဲပြေစာ စစ်ဆေးချိန်', 25, 700, '#D6E0E8')}
    <path d="M760 526 H846" stroke="url(#signal)" stroke-width="5" stroke-linecap="round"/>
    <path d="M838 515 L856 526 L838 537" fill="none" stroke="#36E2FF" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    <image href="{outline}" x="870" y="468" width="104" height="104"/>
    {'' if clean else t(922, 610, 'Official Outline Client', 15, 700, '#9DB1C3', 'middle', 'Inter, sans-serif')}

    {t(150, process_y, 'PLAN ရွေး', 17, 800, '#FFC857', 'middle', 'Inter, sans-serif')}
    <path d="M230 {process_y-6} H370" stroke="#35516B" stroke-width="2"/>
    {t(450, process_y, 'ပြေစာပို့', 20, 700, '#F6F8FC', 'middle')}
    <path d="M530 {process_y-6} H670" stroke="#35516B" stroke-width="2"/>
    {t(760, process_y, 'KEY ရ', 17, 800, '#36E2FF', 'middle', 'Inter, sans-serif')}

    {'' if clean else t(70, 800, 'အလုပ်သုံး Paid Plan', 20, 700, '#9DB1C3')}
    {t(70, 865, '50 GB', 50, 900, '#FFFFFF', family='Inter, sans-serif')}
    {t(285, 865, '3,000 ကျပ်', 32, 800, '#36E2FF')}
    <line x1="70" y1="894" x2="500" y2="894" stroke="#35516B"/>
    {t(560, 865, '100 GB', 50, 900, '#FFFFFF', family='Inter, sans-serif')}
    {t(820, 865, '6,000 ကျပ်', 32, 800, '#7765FF')}
    {t(70, 925, 'Plan တစ်ခုစီ · ရက် 30', 19, 600, '#9DB1C3')}

    {payment_row(round_no)}

    <line x1="70" y1="1142" x2="1010" y2="1142" stroke="#35516B"/>
    {'' if clean else t(70, 1185, 'အရင်စမ်းချင်ရင်', 19, 600, '#9DB1C3')}
    {t(70, 1228, 'လူတိုင်းအတွက် · နေ့စဉ် 300 MB အခမဲ့', 25, 700, free_fill)}
    {telegram_mark(654, 1174, 54)}
    {'' if clean else t(727, 1197, 'Bot မှာ စတင်ပါ', 20, 700, '#F6F8FC')}
    {t(727, 1232, '@aurix_outline_vpn_bot', 22, 800, '#36E2FF', family='Inter, sans-serif')}
    {t(70, 1300, '*အလုပ်များချိန် စစ်ဆေးချိန် ကွာနိုင်ပါတယ်။', 17, 500, '#71889C')}
    {'' if clean else t(1010, 1300, 'Clear access. Human help.', 16, 600, '#71889C', 'end', 'Inter, sans-serif')}
    </svg>"""


def caption() -> str:
    return """အလုပ်အတွက် VPN လိုတဲ့အခါ အချိန်မကြာဘဲ စသုံးနိုင်ဖို့ AuriX Bot ကနေ တိုက်ရိုက်ဝယ်ယူနိုင်ပါတယ်။

လုပ်ဆောင်ပုံက ရိုးရိုးလေးပါ—

၁။ @aurix_outline_vpn_bot ထဲဝင်ပြီး Plan ရွေးပါ။
၂။ KBZPay၊ WavePay၊ AYA Pay၊ uabpay သို့မဟုတ် CB Pay နဲ့ ငွေလွှဲပြီး ပြေစာပို့ပါ။
၃။ ဝန်ထမ်းက ပြေစာကို စစ်ဆေးအတည်ပြုပြီးရင် AuriX Outline-compatible Key ရပါမယ်။ ပုံမှန်ဆို ပြေစာပို့ပြီး ၁ မိနစ်မပြည့်ခင် စစ်ပေးပါတယ်။ အလုပ်များချိန်မှာတော့ စစ်ဆေးချိန် ကွာနိုင်ပါတယ်။
၄။ ရလာတဲ့ Key ကို Official Outline Client ထဲထည့်ပြီး စသုံးပါ။

အခပေး Plan များ—
• 50 GB · ရက် 30 · 3,000 ကျပ်
• 100 GB · ရက် 30 · 6,000 ကျပ်

မဝယ်ခင် စမ်းချင်သူတိုင်းအတွက် နေ့စဉ် 300 MB အခမဲ့လည်း ရှိပါတယ်။ တစ်ကြိမ်ရယူပြီးနောက် နောက်တစ်ကြိမ်ကို 24 နာရီပြည့်မှ ထပ်ယူနိုင်ပါတယ်။ ဒီ 300 MB က ဖုန်း SIM ဒေတာမဟုတ်ဘဲ AuriX VPN Key အတွက် အသုံးပြုခွင့်ပမာဏ ဖြစ်ပါတယ်။

Bot — @aurix_outline_vpn_bot

Key ထည့်သုံးတာ မရှင်းတာရှိရင် AuriX Telegram Chat Group မှာ Admin နဲ့ မေးနိုင်ပါတယ်—
https://t.me/+oA18TDWAD9NiNWU1

ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းက အသုံးပြုနေတဲ့ network၊ ISP နဲ့ လက်ရှိ server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။ AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ AuriX က Official Outline Client နဲ့ တွဲသုံးနိုင်တဲ့ access key ကို သီးခြားဝန်ဆောင်မှုပေးတာ ဖြစ်ပါတယ်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()

    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-bot-paid-intent_r{args.round}"
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
