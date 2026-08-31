#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/quota-control-payments-v1"
LOGO = ROOT / "brand/aurix-logo-horizontal-reverse.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
BOT = ROOT / "brand/v7/exports/aurix-telegram-bot-avatar-v7-1024.png"
PAYMENTS = [
    ("KBZPay", ROOT / "brand/payments/official/kbzpay-app-icon.png"),
    ("WavePay", ROOT / "brand/payments/official/wavepay-app-icon.png"),
    ("AYA Pay", ROOT / "brand/payments/official/ayapay-app-icon.png"),
    ("uabpay", ROOT / "brand/payments/official/uabpay-app-icon.jpg"),
    ("CB Pay", ROOT / "brand/payments/official/cbpay-app-icon.jpg"),
]


def uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def text(
    x: int,
    y: int,
    value: str,
    size: int,
    weight: int,
    fill: str,
    *,
    family: str = "Noto Sans Myanmar, sans-serif",
    anchor: str = "start",
    role: str | None = None,
    group: str | None = None,
    tracking: int | None = None,
) -> str:
    mm = any("\u1000" <= char <= "\u109f" for char in value)
    attrs = [
        f'x="{x}"', f'y="{y}"', f'font-family="{family}"',
        f'font-size="{size}"', f'font-weight="{weight}"', f'fill="{fill}"',
        f'text-anchor="{anchor}"',
    ]
    if tracking is not None:
        attrs.append(f'letter-spacing="{tracking}"')
    if mm:
        attrs.extend([
            'xml:lang="my"',
            'style="font-kerning:normal;font-feature-settings:\'mark\' 1, \'mkmk\' 1"',
            f'data-mm-role="{role or "body"}"',
            f'data-mm-group="{group or "single"}"',
        ])
    return f'<text {" ".join(attrs)}>{value}</text>'


def payment_cells(round_no: int) -> str:
    cell = 112 if round_no == 0 else 118
    inner = 84 if round_no < 2 else 88
    gap = 68 if round_no == 0 else 65
    x0 = 72
    y = 1065
    result = []
    for index, (label, path) in enumerate(PAYMENTS):
        x = x0 + index * (cell + gap)
        pad = (cell - inner) // 2
        result.append(
            f'''<g>
              <rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="28" fill="#0D2235" stroke="#33506A" stroke-width="2"/>
              <clipPath id="payClip{index}"><rect x="{x+pad}" y="{y+pad}" width="{inner}" height="{inner}" rx="20"/></clipPath>
              <image href="{uri(path)}" x="{x+pad}" y="{y+pad}" width="{inner}" height="{inner}" preserveAspectRatio="xMidYMid slice" clip-path="url(#payClip{index})"/>
              <rect x="{x+pad}" y="{y+pad}" width="{inner}" height="{inner}" rx="20" fill="none" stroke="#FFFFFF" stroke-opacity=".18" stroke-width="2"/>
              {text(x+cell//2, y+cell+31, label, 17, 720, '#DDE7EF', family='Inter, sans-serif', anchor='middle')}
            </g>'''
        )
    return "\n".join(result)


def build(round_no: int) -> str:
    r = min(round_no, 2)
    natural_copy = round_no >= 3
    sample_copy = round_no >= 4
    bot_size = (290, 342, 410)[r]
    bot_x = 1080 - 70 - bot_size
    bot_y = (105, 90, 62)[r]
    outline_size = (112, 138, 152)[r]
    outline_x = bot_x - 34
    outline_y = bot_y + 54
    hero_size = 50 if sample_copy else (55, 60, 64)[r]
    feature_gap_x = (495, 550, 590)[r]
    hero_one = 'Meeting ဝင်နေတုန်း' if sample_copy else ('ဘယ်လောက်' if natural_copy else 'မှန်းသုံးစရာ')
    hero_two = 'Quota ကုန်သွားဖူးလား?' if sample_copy else ('ကျန်သေးလဲ?' if natural_copy else 'မလိုတော့ဘူး')
    feature_one_main = 'လိုတဲ့အချိန် ဝင်စစ်နိုင်' if sample_copy else ('My VPN မှာ စစ်နိုင်တယ်' if natural_copy else 'အချိန်မရွေး စစ်ကြည့်')
    feature_one_support = 'သုံးပြီးသား % · လက်ကျန်' if sample_copy else ('သုံးထားတဲ့ပမာဏ · လက်ကျန်' if natural_copy else 'သုံးထားတာ · လက်ကျန်')
    feature_two_main = '25% · 10% · 5%'
    feature_two_support = 'ကျန်ရင် Noti ကြိုပို့' if sample_copy else ('ကျန်ရင် Telegram warning' if natural_copy else 'ရောက်ရင် Telegram က')
    feature_two_end = '' if sample_copy else ('ပို့ပေးတယ်' if natural_copy else 'ကြိုသတိပေး')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1080" y2="1350" gradientUnits="userSpaceOnUse"><stop stop-color="#071421"/><stop offset=".62" stop-color="#081827"/><stop offset="1" stop-color="#0D2235"/></linearGradient>
    <linearGradient id="cyan" x1="80" y1="0" x2="1010" y2="0" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <linearGradient id="gold" x1="70" y1="250" x2="560" y2="440" gradientUnits="userSpaceOnUse"><stop stop-color="#FFD36A"/><stop offset="1" stop-color="#FF9D35"/></linearGradient>
    <radialGradient id="halo"><stop stop-color="#36E2FF" stop-opacity=".14"/><stop offset="1" stop-color="#36E2FF" stop-opacity="0"/></radialGradient>
    <filter id="shadow"><feDropShadow dx="0" dy="18" stdDeviation="22" flood-color="#000" flood-opacity=".48"/></filter>
  </defs>
  <rect width="1080" height="1350" fill="url(#bg)"/>
  <circle cx="920" cy="110" r="350" fill="url(#halo)"/>
  <path d="M620 72C848 162 969 356 1009 572" fill="none" stroke="#18354A" stroke-width="2"/>
  <path d="M681 65C871 170 962 342 990 521" fill="none" stroke="#36E2FF" stroke-opacity=".17" stroke-width="2"/>
  <path d="M70 498H620" stroke="#28465E" stroke-width="2"/>

  <image href="{uri(LOGO)}" x="70" y="50" width="240" height="70" preserveAspectRatio="xMinYMid meet"/>

  {text(70, 190, 'OUTLINE VPN QUOTA', 22, 820, '#36E2FF', family='Inter, sans-serif', tracking=2)}
  {text(70, 302, hero_one, hero_size, 900, 'url(#gold)', family='Noto Serif Myanmar, serif', role='display', group='hero')}
  {text(70, 416, hero_two, hero_size, 900, '#F6F8FC', family='Noto Serif Myanmar, serif', role='display', group='hero')}

  <g filter="url(#shadow)"><image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}"/></g>
  <path d="M{outline_x+outline_size-8} {outline_y+outline_size//2} C{outline_x+outline_size+34} {outline_y+outline_size//2-12} {bot_x-8} {bot_y+bot_size//2-30} {bot_x+18} {bot_y+bot_size//2-18}" fill="none" stroke="url(#cyan)" stroke-width="8" stroke-linecap="round"/>
  <g filter="url(#shadow)"><rect x="{outline_x-8}" y="{outline_y-8}" width="{outline_size+16}" height="{outline_size+16}" rx="36" fill="#0C2B28" stroke="#42D392" stroke-width="3"/><image href="{uri(OUTLINE)}" x="{outline_x}" y="{outline_y}" width="{outline_size}" height="{outline_size}" preserveAspectRatio="xMidYMid slice"/></g>
  <g transform="translate(70 565)">
    <path d="M0 0H{375 if r < 2 else 440}" stroke="#2B4A62" stroke-width="2"/>
    <circle cx="18" cy="65" r="17" fill="none" stroke="#36E2FF" stroke-width="6"/>
    <circle cx="18" cy="65" r="5" fill="#36E2FF"/>
    <path d="M18 42V22" stroke="#36E2FF" stroke-width="6" stroke-linecap="round"/>
    {text(52, 77, feature_one_main, 34 if sample_copy else (36 if r < 2 else 38), 850, '#F6F8FC', role='headline', group='feature-one')}
    {text(0, 160, feature_one_support, 27 if natural_copy else 29, 680, '#AFC0CE', role='body', group='feature-one')}
  </g>
  <g transform="translate({feature_gap_x} 565)">
    <path d="M0 0H{1010-feature_gap_x}" stroke="#2B4A62" stroke-width="2"/>
    {text(0, 78, feature_two_main, 48 if r < 2 else 51, 900, '#FFC857', family='Inter, Noto Sans Myanmar, sans-serif')}
    {text(0, 160, feature_two_support, 31 if sample_copy else (27 if natural_copy else 28), 760, '#DDE7EF', role='body', group='feature-two')}
    {'' if sample_copy else text(0, 222, feature_two_end, 35 if r < 2 else 38, 850, '#F6F8FC', role='headline', group='feature-two')}
  </g>

  <path d="M70 875H1010" stroke="#315069" stroke-width="2"/>
  <path d="M70 875H290" stroke="url(#cyan)" stroke-width="7" stroke-linecap="round"/>
  {text(70, 958, 'Wallet ၅ မျိုးနဲ့ ငွေလွှဲနိုင်', 31 if r < 2 else 33, 780, '#F6F8FC', role='headline', group='payments')}
  {payment_cells(r)}

  <path d="M70 1278H1010" stroke="#27465E" stroke-width="2"/>
  <circle cx="90" cy="1313" r="17" fill="#229ED9"/>
  <path d="M81 1312l19-8-5 18-6-5-4 3 1-6z" fill="#FFFFFF"/>
  {text(122, 1323, '@aurix_outline_vpn_bot', 27, 850, '#36E2FF', family='Inter, sans-serif')}
  {text(1010, 1321, 'AuriX · Clear access. Human help.', 17, 650, '#71889C', family='Inter, sans-serif', anchor='end')}
</svg>'''


def caption() -> str:
    return """Meeting ဝင်နေတုန်း Outline VPN Quota ကုန်သွားမှာ စိတ်ပူနေရလား?

AuriX က ထုတ်ပေးထားတဲ့ Outline VPN Key တွေအတွက် Telegram Bot ရဲ့ My VPN မှာ —
• သုံးပြီးသားပမာဏ
• လက်ကျန်ပမာဏနဲ့ ရာခိုင်နှုန်း
• သက်တမ်းနဲ့ Key အခြေအနေ
ကို လိုတဲ့အချိန် ဝင်စစ်နိုင်ပါတယ်။

Quota 25%၊ 10% နဲ့ 5% ကျန်တဲ့အခါ Telegram ကနေ Noti ပို့ပေးပါတယ်။ Quota ပြည့်သွားရင် Key ရပ်သွားကြောင်းလည်း သီးခြားအသိပေးပါတယ်။

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

မှတ်ချက် — My VPN မှာပြတဲ့ Usage က Outline ဆီက နောက်ဆုံးရထားတဲ့ rolling 30-day transfer metrics ကို အခြေခံထားတာပါ။ Live speed meter မဟုတ်သလို လဆန်းတိုင်း reset ဖြစ်တဲ့ စာရင်းလည်း မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #VPNUsage #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3, 4], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-quota-control-payments-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
