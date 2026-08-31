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
    bot_size = (290, 342, 410)[round_no]
    bot_x = 1080 - 70 - bot_size
    bot_y = (105, 90, 62)[round_no]
    outline_size = (112, 138, 152)[round_no]
    outline_x = bot_x - 34
    outline_y = bot_y + 54
    hero_size = (55, 60, 64)[round_no]
    feature_gap_x = (495, 550, 590)[round_no]
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
  {text(70, 302, 'မှန်းသုံးစရာ', hero_size, 900, 'url(#gold)', family='Noto Serif Myanmar, serif', role='display', group='hero')}
  {text(70, 416, 'မလိုတော့ဘူး', hero_size, 900, '#F6F8FC', family='Noto Serif Myanmar, serif', role='display', group='hero')}

  <g filter="url(#shadow)"><image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}"/></g>
  <path d="M{outline_x+outline_size-8} {outline_y+outline_size//2} C{outline_x+outline_size+34} {outline_y+outline_size//2-12} {bot_x-8} {bot_y+bot_size//2-30} {bot_x+18} {bot_y+bot_size//2-18}" fill="none" stroke="url(#cyan)" stroke-width="8" stroke-linecap="round"/>
  <g filter="url(#shadow)"><rect x="{outline_x-8}" y="{outline_y-8}" width="{outline_size+16}" height="{outline_size+16}" rx="36" fill="#0C2B28" stroke="#42D392" stroke-width="3"/><image href="{uri(OUTLINE)}" x="{outline_x}" y="{outline_y}" width="{outline_size}" height="{outline_size}" preserveAspectRatio="xMidYMid slice"/></g>
  <g transform="translate(70 565)">
    <path d="M0 0H{375 if round_no < 2 else 440}" stroke="#2B4A62" stroke-width="2"/>
    <circle cx="18" cy="65" r="17" fill="none" stroke="#36E2FF" stroke-width="6"/>
    <circle cx="18" cy="65" r="5" fill="#36E2FF"/>
    <path d="M18 42V22" stroke="#36E2FF" stroke-width="6" stroke-linecap="round"/>
    {text(52, 77, 'အချိန်မရွေး စစ်ကြည့်', 36 if round_no < 2 else 38, 850, '#F6F8FC', role='headline', group='feature-one')}
    {text(0, 160, 'သုံးထားတာ · လက်ကျန်', 29, 680, '#AFC0CE', role='body', group='feature-one')}
  </g>
  <g transform="translate({feature_gap_x} 565)">
    <path d="M0 0H{1010-feature_gap_x}" stroke="#2B4A62" stroke-width="2"/>
    {text(0, 78, '25% · 10% · 5%', 48 if round_no < 2 else 51, 900, '#FFC857', family='Inter, Noto Sans Myanmar, sans-serif')}
    {text(0, 160, 'ရောက်ရင် Telegram က', 28, 700, '#DDE7EF', role='body', group='feature-two')}
    {text(0, 222, 'ကြိုသတိပေး', 35 if round_no < 2 else 38, 850, '#F6F8FC', role='headline', group='feature-two')}
  </g>

  <path d="M70 875H1010" stroke="#315069" stroke-width="2"/>
  <path d="M70 875H290" stroke="url(#cyan)" stroke-width="7" stroke-linecap="round"/>
  {text(70, 958, 'Wallet ၅ မျိုးနဲ့ ငွေလွှဲနိုင်', 31 if round_no < 2 else 33, 780, '#F6F8FC', role='headline', group='payments')}
  {payment_cells(round_no)}

  <path d="M70 1278H1010" stroke="#27465E" stroke-width="2"/>
  <circle cx="90" cy="1313" r="17" fill="#229ED9"/>
  <path d="M81 1312l19-8-5 18-6-5-4 3 1-6z" fill="#FFFFFF"/>
  {text(122, 1323, '@aurix_outline_vpn_bot', 27, 850, '#36E2FF', family='Inter, sans-serif')}
  {text(1010, 1321, 'AuriX · Clear access. Human help.', 17, 650, '#71889C', family='Inter, sans-serif', anchor='end')}
</svg>'''


def caption() -> str:
    return """Outline VPN Key သုံးရင်း Quota ဘယ်လောက်ကျန်သေးလဲ မှန်းနေစရာမလိုတော့ပါဘူး။

AuriX က ထုတ်ပေးထားတဲ့ Outline VPN Key ကို Telegram Bot ရဲ့ My VPN မှာ —
• သုံးထားတဲ့ပမာဏ
• ကျန်တဲ့ပမာဏနဲ့ ရာခိုင်နှုန်း
• သက်တမ်းနဲ့ Key အခြေအနေ
ကို အချိန်မရွေး စစ်ကြည့်နိုင်ပါတယ်။

Quota လက်ကျန် 25%၊ 10% နဲ့ 5% အဆင့်တွေကို ရောက်တဲ့အခါ Telegram ကနေ ကြိုတင်သတိပေးပါတယ်။ Quota ပြည့်သွားရင် Key ရပ်သွားကြောင်းလည်း သီးခြားအသိပေးပါတယ်။

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။ Wallet အမှတ်တံဆိပ်တွေကို လက်ခံနိုင်တဲ့ ငွေလွှဲနည်း သိသာစေဖို့သာ ဖော်ပြထားတာဖြစ်ပြီး မိတ်ဖက်ဖြစ်ကြောင်း မဆိုလိုပါဘူး။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

Usage စာရင်းက Outline ရဲ့ နောက်ဆုံးရရှိထားတဲ့ 30-day rolling transfer metrics ကို အခြေခံတာပါ။ Live speed meter မဟုတ်သလို လဆန်း ၁ ရက်နေ့မှာ reset ဖြစ်တဲ့ Usage စာရင်းလည်း မဟုတ်ပါဘူး။

AuriX ဟာ Outline Foundation နဲ့ ငွေလွှဲဝန်ဆောင်မှုပေးသူတွေရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #VPNUsage #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-quota-control-payments-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text(build(args.round) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
