#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/online-seller-access-v2"
PLATE = ROOT / "assets/social/online-seller-access-v1/plates/online-seller-pastel-generated-r1.png"
LOGO = ROOT / "brand/v2/aurix-logo-mark-v2.svg"
OUTLINE = ROOT / "brand/outline/official/outline-client-icon-1024.png"
BOT = ROOT / "brand/v4/exports/aurix-telegram-bot-avatar-v4-1024.png"
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
    icon = (42, 46, 48, 48, 48)[round_no]
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
    hook_size = (48, 51, 53, 53, 53)[round_no]
    bot_size = (164, 174, 180, 180, 180)[round_no]
    bot_x = 82
    bot_y = 1084 - (bot_size - 164) // 2
    product_y = (838, 828, 818, 818, 818)[round_no]
    product_panel = (
        '<path d="M0 0H450L500 50V218H0Z" fill="#FFFFFF" fill-opacity=".93" stroke="#B8E3E8" stroke-width="2"/>'
        '<path d="M450 0V50H500" fill="#DDF8FA" stroke="#B8E3E8" stroke-width="2"/>'
        if round_no >= 2
        else '<rect x="0" y="0" width="500" height="218" rx="34" fill="#FFFFFF" fill-opacity=".91" stroke="#B8E3E8" stroke-width="2"/>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="topWash" x1="0" y1="0" x2="0" y2="610" gradientUnits="userSpaceOnUse"><stop stop-color="#EEFCFD" stop-opacity=".96"/><stop offset=".72" stop-color="#E7FAFC" stop-opacity=".82"/><stop offset="1" stop-color="#DDF8FA" stop-opacity="0"/></linearGradient>
    <linearGradient id="footer" x1="0" y1="1200" x2="0" y2="1350" gradientUnits="userSpaceOnUse"><stop stop-color="#EAFBFC" stop-opacity=".22"/><stop offset=".32" stop-color="#EAFBFC" stop-opacity=".92"/><stop offset="1" stop-color="#EAFBFC"/></linearGradient>
    <linearGradient id="signal" x1="70" y1="0" x2="890" y2="0" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <linearGradient id="wordX" x1="251" y1="58" x2="292" y2="110" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <filter id="softShadow"><feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#16344A" flood-opacity=".20"/></filter>
    <filter id="badgeShadow"><feDropShadow dx="0" dy="7" stdDeviation="8" flood-color="#16344A" flood-opacity=".24"/></filter>
    <filter id="typeShadow"><feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#0B5278" flood-opacity=".14"/></filter>
  </defs>
  <image href="{uri(PLATE)}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
  <rect width="1080" height="620" fill="url(#topWash)"/>
  <rect y="1190" width="1080" height="160" fill="url(#footer)"/>

  <g aria-label="AuriX compact brand lockup">
    <image href="{uri(LOGO)}" x="62" y="42" width="92" height="92" preserveAspectRatio="xMidYMid meet"/>
    <text x="174" y="103" fill="#071521" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="750" letter-spacing="-2">Auri</text>
    <text x="267" y="103" fill="url(#wordX)" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="800" letter-spacing="-2">X</text>
  </g>
  <g opacity=".88">
    <image href="{uri(OUTLINE)}" x="946" y="56" width="60" height="60" preserveAspectRatio="xMidYMid meet"/>
    <text x="916" y="137" fill="#31576C" font-family="Inter, Arial, sans-serif" font-size="16" font-weight="750">Outline VPN</text>
  </g>

  <g filter="url(#typeShadow)">
    {text(540, 226, 'Customer စာပြန်ချိန်', hook_size, 900, '#10344C', family='Noto Sans Myanmar, sans-serif', anchor='middle', role='display', group='hook')}
    <path d="M289 332C405 350 664 350 790 326" fill="none" stroke="#FFC857" stroke-width="24" stroke-linecap="round" opacity=".72"/>
    {text(540, 321, 'VPN လိုက်စမ်းရတာ', hook_size + 6, 900, '#0A5A88', family='Noto Sans Myanmar SemiCondensed, Noto Sans Myanmar, sans-serif', anchor='middle', role='display', group='hook')}
    {text(540, 423, 'မောနေပြီလား?', hook_size + 3, 900, '#10344C', family='Noto Sans Myanmar, sans-serif', anchor='middle', role='display', group='hook')}
  </g>

  <g transform="translate(95 {product_y})">
    {product_panel}
    <rect x="0" y="30" width="8" height="158" rx="4" fill="url(#signal)"/>
    {text(34, 53, 'AuriX Bot မှာ ဝယ်ထားတဲ့ Key', 29, 850, '#0B5278', family='Inter, Noto Sans Myanmar SemiCondensed, Noto Sans Myanmar UI, sans-serif', role='headline', group='product')}
    {text(34, 112, 'Quota ဘယ်လောက်ကျန်လဲ?', 35, 850, '#183C52', family='Inter, Noto Sans Myanmar SemiCondensed, Noto Sans Myanmar UI, sans-serif', role='headline', group='product')}
    {text(34, 165, 'အချိန်မရွေး ဝင်စစ်နိုင်', 34, 850, '#183C52', family='Noto Sans Myanmar SemiCondensed, Noto Sans Myanmar UI, sans-serif', role='headline', group='product')}
    {text(34, 200, '@aurix_outline_vpn_bot', 20, 800, '#0A6B98', family='Inter, sans-serif')}
  </g>

  <g filter="url(#softShadow)">
    <circle cx="{bot_x + bot_size // 2}" cy="{bot_y + bot_size // 2}" r="{bot_size // 2 + 5}" fill="#FFFFFF" fill-opacity=".94" stroke="#50D7EA" stroke-width="3"/>
    <clipPath id="botClip"><circle cx="{bot_x + bot_size // 2}" cy="{bot_y + bot_size // 2}" r="{bot_size // 2}"/></clipPath>
    <image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}" preserveAspectRatio="xMidYMid slice" clip-path="url(#botClip)"/>
  </g>
  <g aria-label="Telegram platform badge" filter="url(#badgeShadow)" transform="translate(240 1088)">
    <circle r="43" fill="#FFFFFF"/>
    <circle r="35" fill="#229ED9"/>
    <path d="M-22-2 21-19C25-20 28-17 27-13L19 22C18 26 14 27 11 25L-1 16-7 22C-9 24-12 23-12 19L-11 11 12-10-16 7C-20 9-24 6-25 2-25 0-24-1-22-2Z" fill="#FFFFFF"/>
  </g>

  <path d="M378 1204H1008" stroke="#9EDAE2" stroke-width="2"/>
  <path d="M378 1204H540" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>
  {text(392, 1266, 'Wallet ၅ မျိုး', 26, 780, '#183C52', role='headline', group='payment')}
  {payment_row(round_no)}
</svg>'''


def caption() -> str:
    return """Facebook ပေါ်က Customer တွေကို အချိန်မီ စာပြန်ဖို့ VPN တစ်ခုချင်း လိုက်စမ်းနေရတာ အလုပ်ရှုပ်ပါတယ်။

AuriX Telegram Bot မှာ ဝယ်ယူထားတဲ့ VPN Key တိုင်းရဲ့ Quota လက်ကျန်ကို အချိန်မရွေး ကိုယ်တိုင်ဝင်စစ်နိုင်ပါတယ်။

AuriX Bot နဲ့ဆို —
• My VPN ထဲမှာ သုံးပြီးသားပမာဏ၊ Quota လက်ကျန်၊ သက်တမ်းနဲ့ Key အခြေအနေကို တစ်နေရာတည်းမှာ စစ်နိုင်ပါတယ်။
• Quota 25%၊ 10% နဲ့ 5% ကျန်တဲ့အခါ Telegram ကနေ ကြိုအသိပေးပါတယ်။
• ပြေစာပုံတင်လိုက်တာနဲ့ စနစ်က Transaction ID နဲ့ ငွေပမာဏကို အလိုအလျောက်ဖတ်ယူပေးနိုင်ပါတယ်။ ပြီးရင် Admin Team က ငွေလွှဲဝင်ကြောင်း ဆက်စစ်ပြီး အတည်ပြုပေးပါတယ်။ ပြေစာအချက်အလက်တွေ ပြန်ရိုက်စရာမလိုလို့ အော်ဒါတင်ရတာ မြန်ပြီး လွယ်ကူပါတယ်။

ရနိုင်တဲ့ Paid Plan —
• 50 GB · ရက် ၃၀ · 3,000 ကျပ်
• 100 GB · ရက် ၃၀ · 6,000 ကျပ်

အရင်စမ်းသုံးကြည့်ချင်ရင် 24 နာရီသုံး 300 MB Free Plan နဲ့ ရက် ၃၀ တိုင်း ရယူနိုင်တဲ့ 3 GB Free Plan လည်းရှိပါတယ်။

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

Connection speed နဲ့ latency က အသုံးပြုနေတဲ့ Network၊ ISP၊ တည်နေရာနဲ့ လက်ရှိ Server အခြေအနေပေါ် မူတည်နိုင်ပါတယ်။ AuriX ဟာ Outline Foundation သို့မဟုတ် ဖော်ပြထားတဲ့ ငွေလွှဲဝန်ဆောင်မှုတွေရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #OnlineSeller #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3, 4], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    stem = f"{args.stamp}_aurix-online-seller-access-v2_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
