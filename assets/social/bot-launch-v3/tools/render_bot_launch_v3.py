#!/usr/bin/env python3
"""Compose the AuriX public-launch static over a generated gateway plate."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/bot-launch-v3"
PLATE = BASE / "plates/aurix-monumental-gateway-generated.png"
LOGO = ROOT / "brand/v2/aurix-logo-horizontal-reverse-v2.svg"
BOT = ROOT / "brand/v5/exports/aurix-telegram-bot-avatar-v5-1024.png"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
PAYMENTS = [
    ("KBZPay", ROOT / "brand/payments/official/kbzpay-app-icon.png"),
    ("WavePay", ROOT / "brand/payments/official/wavepay-app-icon.png"),
    ("AYA Pay", ROOT / "brand/payments/official/ayapay-app-icon.png"),
    ("uabpay", ROOT / "brand/payments/official/uabpay-app-icon.jpg"),
    ("CB Pay", ROOT / "brand/payments/official/cbpay-app-icon.jpg"),
]


def uri(path: Path) -> str:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}[path.suffix.lower()]
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def t(x: int, y: int, value: str, size: int, weight: int = 700, fill: str = "#F6F8FC", anchor: str = "start", family: str = "Noto Sans Myanmar, Inter, sans-serif") -> str:
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" font-family="{family}" font-size="{size}" font-weight="{weight}">{escape(value)}</text>'


def build(round_no: int) -> str:
    if round_no >= 3:
        payment_marks = []
        for index, (name, path) in enumerate(PAYMENTS):
            x = 410 + index * 126
            payment_marks.append(f'<image href="{uri(path)}" x="{x}" y="1190" width="78" height="78" preserveAspectRatio="xMidYMid slice" clip-path="url(#paymentClip{index})"/>')
            payment_marks.append(t(x + 39, 1295, name, 17, 700, "#D6E0E8", "middle", "Inter, sans-serif"))
        payment_clips = "".join(f'<clipPath id="paymentClip{i}"><rect x="{410 + i * 126}" y="1190" width="78" height="78" rx="18"/></clipPath>' for i in range(5))
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
        <defs>
          <linearGradient id="leftShade" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#071421" stop-opacity=".98"/><stop offset=".66" stop-color="#071421" stop-opacity=".76"/><stop offset="1" stop-color="#071421" stop-opacity="0"/></linearGradient>
          <linearGradient id="bottomShade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity="0"/><stop offset=".55" stop-color="#071421" stop-opacity=".84"/><stop offset="1" stop-color="#071421" stop-opacity=".98"/></linearGradient>
          <filter id="shadow"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#000" flood-opacity=".5"/></filter>
          {payment_clips}
        </defs>
        <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
        <rect x="0" y="0" width="820" height="1350" fill="url(#leftShade)"/>
        <rect x="0" y="790" width="1080" height="560" fill="url(#bottomShade)"/>
        <image href="{uri(LOGO)}" x="68" y="42" width="300" height="96" preserveAspectRatio="xMinYMid meet"/>

        {t(70, 220, 'အလုပ်အတွက် VPN လိုတဲ့အခါ', 46, 850)}
        {t(70, 292, 'AuriX Telegram Bot ကနေ', 52, 900, '#FFC857')}
        {t(70, 360, 'စလိုက်ပါ', 54, 900, '#FFC857')}
        {t(70, 425, 'Official Outline Client နဲ့ ချိတ်သုံးနိုင်တဲ့', 27, 700, '#D6E0E8')}
        {t(70, 475, 'VPN Key · ရက် 30', 35, 850, '#36E2FF')}
        <image href="{uri(OUTLINE)}" x="360" y="438" width="72" height="72"/>

        <image href="{uri(BOT)}" x="68" y="882" width="210" height="210" filter="url(#shadow)"/>
        {t(318, 925, 'AURIX TELEGRAM BOT', 24, 850, '#36E2FF', family='Inter, sans-serif')}
        {t(318, 985, 'ပြေစာပို့ပြီး', 28, 750, '#D6E0E8')}
        {t(318, 1047, 'ပုံမှန် ၁ မိနစ်မပြည့်ခင်', 40, 900, '#FFFFFF')}
        {t(318, 1095, 'စစ်ပေးပါတယ်*', 31, 800, '#FFFFFF')}
        {t(318, 1142, '@aurix_outline_vpn_bot', 28, 850, '#36E2FF', family='Inter, sans-serif')}
        {t(70, 1180, '*အော်ဒါများတဲ့အချိန် အနည်းငယ်ပိုကြာနိုင်ပါတယ်။', 17, 550, '#B2C0CC')}

        {t(70, 1243, 'ငွေပေးချေနိုင်တဲ့ နည်းလမ်းများ', 24, 750, '#F6F8FC')}
        {''.join(payment_marks)}
        </svg>"""

    final = round_no >= 1
    headline = 52 if final else 48
    supporting = 31 if final else 29
    bot_size = 118 if final else 108
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
    <defs>
      <linearGradient id="leftShade" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#071421" stop-opacity=".98"/><stop offset=".63" stop-color="#071421" stop-opacity=".72"/><stop offset="1" stop-color="#071421" stop-opacity="0"/></linearGradient>
      <linearGradient id="bottomShade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#071421" stop-opacity="0"/><stop offset="1" stop-color="#071421" stop-opacity=".92"/></linearGradient>
      <filter id="shadow"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#000" flood-opacity=".5"/></filter>
    </defs>
    <image href="{uri(PLATE)}" x="0" y="0" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
    <rect x="0" y="0" width="760" height="1350" fill="url(#leftShade)"/>
    <rect x="0" y="980" width="1080" height="370" fill="url(#bottomShade)"/>
    <image href="{uri(LOGO)}" x="68" y="44" width="300" height="96" preserveAspectRatio="xMinYMid meet"/>

    {t(70, 245, 'AuriX Outline VPN Key များ', 48 if round_no >= 2 else headline, 850)}
    {t(70, 315, 'ယနေ့ စတင်ရယူနိုင်ပါပြီ', headline+2, 900, '#FFC857')}
    {t(70, 397, 'Official Outline Client နဲ့', supporting, 750, '#D6E0E8')}
    {t(70, 446, 'ချိတ်သုံးနိုင်တဲ့ VPN Key', supporting, 750, '#D6E0E8')}

    <image href="{uri(BOT)}" x="70" y="1010" width="{bot_size}" height="{bot_size}" filter="url(#shadow)"/>
    {t(222, 1048, 'ပြေစာစစ်ဆေးချိန်', 27, 700, '#D6E0E8')}
    {t(222, 1124, 'ပုံမှန် ၁ မိနစ်အောက်*', 52 if round_no >= 2 else (61 if final else 56), 900, '#FFFFFF')}
    {t(222, 1190, '@aurix_outline_vpn_bot', 32 if final else 29, 850, '#36E2FF', family='Inter, sans-serif')}
    {t(222, 1240, '*အော်ဒါများတဲ့အချိန် အနည်းငယ်ပိုကြာနိုင်ပါတယ်။', 18, 550, '#B2C0CC')}
    <image href="{uri(OUTLINE)}" x="850" y="1030" width="130" height="130"/>
    </svg>"""


def caption() -> str:
    return """အလုပ်လုပ်နေတုန်း VPN လိုလာရင် Free VPN တစ်ခုပြီးတစ်ခု လိုက်စမ်းနေဖို့ အချိန်မရှိပါဘူး။

Freelancer၊ Remote Worker၊ Online Seller၊ Content Creator၊ Researcher — အလုပ်အတွက် VPN မကြာခဏလိုအပ်သူတွေအတွက် Outline VPN Key ကို AuriX Telegram Bot ကနေ အခု ဝယ်ယူနိုင်ပါပြီ။

ရလာတဲ့ Key ကို Official Outline Client ထဲထည့်ပြီး ချိတ်သုံးရတာပါ။

Plan များ—
• 50 GB · ရက် 30 · 3,000 ကျပ်
• 100 GB · ရက် 30 · 6,000 ကျပ်

KBZPay၊ WavePay၊ AYA Pay၊ uabpay၊ CB Pay တို့နဲ့ ငွေပေးချေနိုင်ပါတယ်။

ဝယ်ယူပုံကလည်း ရိုးရိုးလေးပါ။ Bot မှာ Plan ရွေး၊ ငွေလွှဲပြီး ပြေစာပို့ပါ။ ပြေစာကို ဝန်ထမ်းက စစ်ပြီး အတည်ပြုပေးပါတယ်။ ပုံမှန်ဆို ၁ မိနစ်မပြည့်ခင် စစ်ပေးနိုင်ပြီး အော်ဒါများတဲ့အချိန်မှာတော့ အနည်းငယ်စောင့်ရနိုင်ပါတယ်။

မဝယ်ခင် စမ်းကြည့်ချင်သေးရင် 24 နာရီတစ်ကြိမ် 300 MB အခမဲ့ Key ကို Bot မှာ ရယူနိုင်ပါတယ်။ 300 MB နဲ့ Plan တွေမှာပါတဲ့ GB ပမာဏက VPN Key အသုံးပြုခွင့်ပမာဏပါ။ ဖုန်း SIM/Mobile Data Package မဟုတ်ပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

AuriX ဟာ Outline Foundation ရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။ ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းက အသုံးပြုနေတဲ့ Network၊ ISP နဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။

#AuriXVPN #OutlineVPN #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3, 4], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-public-launch-v3_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
