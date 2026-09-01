#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / "assets/social/receipt-confidence-v1"
PLATE = BASE / "plates/receipt-confidence-editorial-r0.png"
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
    return f'<text {" ".join(attrs)}>{escape(value)}</text>'


def telegram_badge(x: int, y: int, radius: int = 38) -> str:
    scale = radius / 43
    return f'''<g aria-label="Telegram platform badge" transform="translate({x} {y}) scale({scale:.4f})">
      <circle r="43" fill="#FFFFFF"/>
      <circle r="35" fill="#229ED9"/>
      <path d="M-22-2 21-19C25-20 28-17 27-13L19 22C18 26 14 27 11 25L-1 16-7 22C-9 24-12 23-12 19L-11 11 12-10-16 7C-20 9-24 6-25 2-25 0-24-1-22-2Z" fill="#FFFFFF"/>
    </g>'''


def payment_row(round_no: int) -> str:
    icon = (44, 48, 50, 50)[round_no]
    cell = icon + 18
    gap = 24
    x0 = 586
    y = 1218
    out: list[str] = []
    for index, (label, path) in enumerate(PAYMENTS):
        x = x0 + index * (cell + gap)
        pad = 9
        out.append(f'''<g>
          <rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="20" fill="#FFFFFF" fill-opacity=".94" stroke="#A9DCE3" stroke-width="2"/>
          <clipPath id="pay-{index}"><rect x="{x+pad}" y="{y+pad}" width="{icon}" height="{icon}" rx="14"/></clipPath>
          <image href="{uri(path)}" x="{x+pad}" y="{y+pad}" width="{icon}" height="{icon}" preserveAspectRatio="xMidYMid slice" clip-path="url(#pay-{index})"/>
          <rect x="{x+pad}" y="{y+pad}" width="{icon}" height="{icon}" rx="14" fill="none" stroke="#FFFFFF" stroke-opacity=".35"/>
          {text(x + cell // 2, y + cell + 19, label, 11, 750, '#24475D', family='Inter, Arial, sans-serif', anchor='middle')}
        </g>''')
    return "\n".join(out)


def build(round_no: int) -> str:
    hook_size = (48, 52, 55, 52)[round_no]
    proof_x = (74, 74, 74, 74)[round_no]
    proof_y = (640, 624, 612, 612)[round_no]
    proof_w = (470, 486, 500, 500)[round_no]
    bot_size = (160, 178, 190, 190)[round_no]
    bot_x = 74
    bot_y = (1008, 944, 928, 928)[round_no]
    connector = (
        '' if round_no == 0 else
        f'<path d="M{bot_x + bot_size//2} {proof_y + 252}V{bot_y - 12}" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>'
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1350" role="img">
  <defs>
    <linearGradient id="topWash" x1="0" y1="0" x2="0" y2="650" gradientUnits="userSpaceOnUse"><stop stop-color="#EAFBFC" stop-opacity=".92"/><stop offset="1" stop-color="#EAFBFC" stop-opacity="0"/></linearGradient>
    <linearGradient id="signal" x1="74" y1="0" x2="1010" y2="0" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <linearGradient id="wordX" x1="250" y1="55" x2="295" y2="112" gradientUnits="userSpaceOnUse"><stop stop-color="#36E2FF"/><stop offset="1" stop-color="#7765FF"/></linearGradient>
    <filter id="shadow"><feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#16344A" flood-opacity=".22"/></filter>
    <filter id="typeShadow"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#0B5278" flood-opacity=".12"/></filter>
  </defs>
  <image href="{uri(PLATE)}" width="1080" height="1350" preserveAspectRatio="xMidYMid slice"/>
  <rect width="1080" height="650" fill="url(#topWash)"/>
  <rect y="1182" width="1080" height="168" fill="#EAFBFC" fill-opacity=".94"/>

  <g aria-label="AuriX primary lockup">
    <image href="{uri(LOGO)}" x="62" y="42" width="92" height="92"/>
    <text x="174" y="103" fill="#071521" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="750" letter-spacing="-2">Auri</text>
    <text x="267" y="103" fill="url(#wordX)" font-family="Inter, Arial, sans-serif" font-size="50" font-weight="800" letter-spacing="-2">X</text>
  </g>
  <g opacity=".9">
    <image href="{uri(OUTLINE)}" x="948" y="54" width="60" height="60"/>
    <text x="1010" y="137" fill="#31576C" font-family="Inter, Arial, sans-serif" font-size="16" font-weight="750" text-anchor="end">Outline VPN</text>
  </g>

  <g filter="url(#typeShadow)">
    {text(72, 228, 'Outline VPN Key ဝယ်ပြီး' if round_no == 3 else 'VPN Key ဝယ်ဖို့', hook_size, 900, '#10344C', family='Noto Serif Myanmar, serif', role='display', group='hook')}
    <path d="M68 337C212 354 404 351 520 329" fill="none" stroke="#FFC857" stroke-width="24" stroke-linecap="round" opacity=".70"/>
    {text(72, 326, 'ပြေစာစစ်ပေးမယ့်အချိန်' if round_no == 3 else 'TxID ပြန်ရိုက်နေရလို့', hook_size + 1, 900, '#0A5A88', family='Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='display', group='hook')}
    {text(72, 426, 'စောင့်နေရသေးလား?' if round_no == 3 else 'အလုပ်ရှုပ်နေလား?', hook_size, 900, '#10344C', family='Noto Serif Myanmar, serif', role='display', group='hook')}
  </g>

  <g transform="translate({proof_x} {proof_y})" filter="url(#shadow)">
    <path d="M0 0H{proof_w-52}L{proof_w} 52V252H0Z" fill="#FFFFFF" fill-opacity=".94" stroke="#B8E3E8" stroke-width="2"/>
    <path d="M{proof_w-52} 0V52H{proof_w}" fill="#DDF8FA" stroke="#B8E3E8" stroke-width="2"/>
    <rect x="0" y="34" width="8" height="174" rx="4" fill="url(#signal)"/>
    {text(34, 64, 'ပြေစာပုံ ပို့လိုက်ရုံ', 35, 850, '#0B5278', role='headline', group='proof')}
    {text(34, 126, 'AI က အလိုအလျောက် စစ်ပေး' if round_no == 3 else 'TxID ပြန်ရိုက်စရာ မလို', 32 if round_no == 3 else 34, 850, '#183C52', role='headline', group='proof')}
    {text(34, 190, 'အတည်ပြုပြီးတာနဲ့ Key ရပြီ' if round_no == 3 else 'Admin စစ်ပြီးရင် Key ရပြီ', 29 if round_no == 3 else 30, 800, '#183C52', family='Inter, Noto Sans Myanmar UI, Noto Sans Myanmar, sans-serif', role='headline', group='proof')}
    {text(34, 231, '@aurix_outline_vpn_bot', 20, 800, '#0A6B98', family='Inter, Arial, sans-serif')}
  </g>

  {connector}
  <g filter="url(#shadow)">
    <circle cx="{bot_x + bot_size//2}" cy="{bot_y + bot_size//2}" r="{bot_size//2 + 5}" fill="#FFFFFF" fill-opacity=".94" stroke="#50D7EA" stroke-width="3"/>
    <clipPath id="botClip"><circle cx="{bot_x + bot_size//2}" cy="{bot_y + bot_size//2}" r="{bot_size//2}"/></clipPath>
    <image href="{uri(BOT)}" x="{bot_x}" y="{bot_y}" width="{bot_size}" height="{bot_size}" clip-path="url(#botClip)"/>
  </g>
  {telegram_badge(bot_x + bot_size - 2, bot_y + 30, 38 if round_no < 2 else 42)}

  <path d="M354 1206H1008" stroke="#9EDAE2" stroke-width="2"/>
  <path d="M354 1206H536" stroke="url(#signal)" stroke-width="7" stroke-linecap="round"/>
  {text(360, 1264, 'Wallet ၅ မျိုး', (24, 26, 27, 27)[round_no], 780, '#183C52', role='headline', group='payments')}
  {payment_row(round_no)}
</svg>'''


def caption() -> str:
    return """Outline VPN Key ဝယ်ပြီး ပြေစာပို့ထားပေမယ့် ဆိုင်ဘက်က စစ်ပေးမယ့်အချိန်ကို ထိုင်စောင့်နေရတာမျိုး မလိုတော့ပါဘူး။

AuriX Telegram Bot မှာ ပြေစာပုံတင်လိုက်ရုံပါပဲ။ Bot က AI နဲ့ အလိုအလျောက် စစ်ပေးပြီး စစ်ဆေးမှုအောင်မြင်တာနဲ့ Outline VPN Key ကို ခဏလေးအတွင်း ထုတ်ပေးပါတယ်။ ဆိုင်ဘက်က ပြန်စစ်ပေးမယ့်အချိန်ကို စောင့်စရာမလိုတော့ဘူးနော်။

ဝယ်ယူနိုင်တဲ့ Plan —
• 50 GB · ရက် 30 · 3,000 ကျပ်
• 100 GB · ရက် 30 · 6,000 ကျပ်

ငွေလွှဲနိုင်တဲ့ Wallet — KBZPay၊ WavePay၊ AYA Pay၊ uabpay နဲ့ CB Pay။

Bot — https://t.me/aurix_outline_vpn_bot
Admin နဲ့ Group Chat — https://t.me/+oA18TDWAD9NiNWU1
Channel — https://t.me/AurixDigitalStore

ပြေစာပုံမရှင်းတာ၊ အချက်အလက်မကိုက်တာလို စစ်ဆေးလို့မရတဲ့ အော်ဒါတွေကိုတော့ Admin Team က ဆက်လက်ကူညီပေးပါမယ်။ AuriX ဟာ Outline Foundation သို့မဟုတ် ဖော်ပြထားတဲ့ ငွေလွှဲဝန်ဆောင်မှုတွေရဲ့ တရားဝင်မိတ်ဖက် မဟုတ်ပါဘူး။

#AuriXVPN #OutlineVPN #TelegramBot #VPNMyanmar"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=[0, 1, 2, 3], required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    out = BASE / ("exports" if args.final else "iterations")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.stamp}_aurix-receipt-confidence-v1_r{args.round}"
    svg = out / f"{stem}.svg"
    png = out / f"{stem}.png"
    txt = out / f"{stem}.txt"
    svg.write_text("\n".join(line.rstrip() for line in build(args.round).splitlines()) + "\n", encoding="utf-8")
    txt.write_text(caption() + "\n", encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1080", "-h", "1350", str(svg), "-o", str(png)], check=True)
    print(json.dumps({"image": str(png), "caption": str(txt), "source": str(svg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
