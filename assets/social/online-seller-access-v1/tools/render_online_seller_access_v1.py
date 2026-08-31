#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/online-seller-access-v1"
PLATE = BASE / "plates/online-seller-pastel-generated-r1.png"
LOGO = ROOT / "brand/aurix-logo-horizontal.svg"
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
    family: str = "Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif",
    anchor: str = "start",
    role: str = "body",
    group: str = "single",
    tracking: int | None = None,
) -> str:
    mm = any("\u1000" <= ch <= "\u109f" for ch in value)
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
            f'data-mm-role="{role}"', f'data-mm-group="{group}"',
        ])
    return f'<text {" ".join(attrs)}>{value}</text>'


def payment_row(round_no: int) -> str:
    icon = (42, 46, 48)[round_no]
    cell = icon + 16
    start = 600
    gap = 84
    result: list[str] = []
    for index, (label, path) in enumerate(PAYMENTS):
        x = start + index * gap
        y = 1212
        inner_x = x + 9
        inner_y = y + 9
        result.append(f'''<g>
          <rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="20" fill="#FFFFFF" fill-opacity=".90" stroke="#A9DCE3" stroke-width="2"/>
          <clipPath id="wallet-{index}"><rect x="{inner_x}" y="{inner_y}" width="{icon}" height="{icon}" rx="15"/></clipPath>
          <image href="{uri(path)}" x="{inner_x}" y="{inner_y}" width="{icon}" height="{icon}" preserveAspectRatio="xMidYMid slice" clip-path="url(#wallet-{index})"/>
          <rect x="{inner_x}" y="{inner_y}" width="{icon}" height="{icon}" rx="15" fill="none" stroke="#FFFFFF" stroke-opacity=".35"/>
          {text(x + cell // 2, y + cell + 21, label, 12, 750, '#24475D', family='Inter, sans-serif', anchor='middle')}
        </g>''')
    return "\n".join(result)


def build(round_no: int) -> str:
    hook_size = (49, 52, 54)[round_no]
    bot_size = (168, 180, 188)[round_no]
    bot_x = 84
    bot_y = 1092 - (bot_size - 168) // 2
    product_y = (866, 850, 836)[round_no]
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="topWash" x1="0" y1="0" x2="0" y2="610" gradientUnits="userSpaceOnUse"><stop stop-color="#EEFCFD" stop-opacity=".96"/><stop offset=".72" stop-color="#E7FAFC" stop-opacity=".82"/><stop offset="1" stop-color="#DDF8FA" stop-opacity="0"/></linearGradient>
    <linearGradient id="footer" x1="0" y1="1200" x2="0" y2="1350" gradientUnits="userSpaceOnUse"><stop stop-color="#EAFBFC" stop-opacity=".22"/><stop offset=".32" stop-color="#EAFBFC" stop-opacity=".92"/><stop offset="1" stop-color="#EAFBFC"/></linearGradient>
    <linearGradient id="signal" x1="70" y1="0" x2="890" y2="0" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <filter id="softShadow"><feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#16344A" flood-opacity=".20"/></filter>
  </defs>
  <image href="{uri(PLATE)}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
  <rect width="1080" height="620" fill="url(#topWash)"/>
  <rect y="1190" width="1080" height="160" fill="url(#footer)"/>

  <image href="{uri(LOGO)}" x="62" y="42" width="230" height="72" preserveAspectRatio="xMinYMid meet"/>
  <g filter="url(#softShadow)">
    <rect x="925" y="44" width="92" height="92" rx="26" fill="#0B3C35" stroke="#42D392" stroke-width="3"/>
    <image href="{uri(OUTLINE)}" x="935" y="54" width="72" height="72" preserveAspectRatio="xMidYMid slice"/>
  </g>

  {text(540, 222, 'Customer စာပြန်ချိန်', hook_size, 900, '#10344C', anchor='middle', role='display', group='hook')}
  <rect x="287" y="265" width="506" height="70" rx="35" fill="#FFE18A" fill-opacity=".88"/>
  {text(540, 322, 'VPN လိုက်စမ်းရတာ', hook_size + 5, 900, '#0A5A88', anchor='middle', role='display', group='hook')}
  {text(540, 425, 'မောနေပြီလား?', hook_size + 3, 900, '#10344C', anchor='middle', role='display', group='hook')}

  <g transform="translate(95 {product_y})">
    <rect x="0" y="0" width="430" height="174" rx="38" fill="#FFFFFF" fill-opacity=".88" stroke="#B8E3E8" stroke-width="2"/>
    {text(34, 55, 'AuriX Outline VPN Key', 29, 850, '#0B5278', family='Inter, Noto Sans Myanmar UI, sans-serif')}
    {text(34, 118, 'Bot ထဲမှာ ဝယ်ယူ · လက်ကျန်စစ်', 29, 760, '#183C52', role='headline', group='product')}
    {text(34, 154, '@aurix_outline_vpn_bot', 20, 800, '#0A6B98', family='Inter, sans-serif')}
  </g>

  <g filter="url(#softShadow)">
    <rect x="{bot_x - 7}" y="{bot_y - 7}" width="{bot_size + 14}" height="{bot_size + 14}" rx="{bot_size // 4 + 7}" fill="#FFFFFF" fill-opacity=".94" stroke="#50D7EA" stroke-width="3"/>
    <clipPath id="botClip"><rect x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}" rx="{bot_size // 4}"/></clipPath>
    <image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}" preserveAspectRatio="xMidYMid slice" clip-path="url(#botClip)"/>
  </g>

  <path d="M378 1204H1008" stroke="#9EDAE2" stroke-width="2"/>
  <path d="M378 1204H540" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>
  {text(392, 1266, 'Wallet ၅ မျိုး', 26, 780, '#183C52', role='headline', group='payment')}
  {payment_row(round_no)}
</svg>'''


def caption() -> str:
    return """Facebook ပေါ်က Customer တွေကို စာပြန်ဖို့ VPN တစ်ခုချင်း လိုက်စမ်းနေရတာ အလုပ်ရှုပ်ပါတယ်။

AuriX Outline VPN Key ကို Telegram Bot ကနေ ဝယ်ယူနိုင်ပါပြီ။

AuriX က ထုတ်ပေးထားတဲ့ Key ဆိုရင် My VPN မှာ —
• သုံးပြီးသား Data ပမာဏ
• လက်ကျန် Data နဲ့ ရာခိုင်နှုန်း
• သက်တမ်းနဲ့ Key အခြေအနေ
ကို ကြိုက်တဲ့အချိန် ကိုယ်တိုင်စစ်နိုင်ပါတယ်။

ရနိုင်တဲ့ Plan —
• 50 GB · ရက် ၃၀ · 3,000 ကျပ်
• 100 GB · ရက် ၃၀ · 6,000 ကျပ်

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

VPN ချိတ်ဆက်နိုင်မှုနဲ့ အမြန်နှုန်းက သုံးနေတဲ့ Network၊ ISP နဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။ AuriX ဟာ Outline Foundation နဲ့ ငွေလွှဲဝန်ဆောင်မှုပေးသူတွေရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #OnlineSeller #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    stem = f"{args.stamp}_aurix-online-seller-access-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
