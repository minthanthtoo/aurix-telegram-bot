#!/usr/bin/env python3
"""Render AuriX × Outline education and retention assets from reusable SVG templates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
STAMP = "20260828-173953"
VERSION = "v8"
SOURCE_DIR = ROOT / "assets/social/campaign/outline-v8-sources" / STAMP
EXPORT_DIR = ROOT / "assets/social/campaign/exports" / f"{STAMP}-outline-cross-channel-{VERSION}"
TEMP_DIR = Path("/private/tmp") / f"aurix-outline-{VERSION}-{STAMP}"
MARK = ROOT / "brand/v2/aurix-logo-mark-v2.svg"
OUTLINE_ICON = ROOT / "brand/outline/official/outline-client-icon-1024.png"
PLATES = {
    "setup": ROOT / "assets/social/campaign/v4-plates/setup-help-v4-plate.png",
    "allowance": ROOT / "assets/social/campaign/v4-plates/data-finished-v4-plate.png",
    "expiry": ROOT / "assets/social/campaign/v4-plates/renewal-v4-plate.png",
}

BOT = "https://t.me/aurix_outline_vpn_bot"
GROUP = "https://t.me/+oA18TDWAD9NiNWU1"
CHANNEL = "https://t.me/AurixDigitalStore"
OFFICIAL_DOWNLOADS = "https://developer.getoutline.org/download-links/"
DISCLAIMER = (
    "AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ "
    "AuriX က Outline-compatible access key ကို သီးခြားဝန်ဆောင်မှုပေးပြီး "
    "official Outline Client ဖြင့် အသုံးပြုရပါသည်။"
)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def tspans(lines: list[str], x: int, dy: int) -> str:
    return "".join(
        f'<tspan x="{x}"' + ("" if i == 0 else f' dy="{dy}"') + f'>{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )


POSTS = [
    {
        "id": "retention-setup",
        "kind": "retention",
        "plate": "setup",
        "series": "AURIX KEY → OUTLINE CLIENT",
        "headline": ["Outline VPN", "ချိတ်ဆက်မရဘူးလား။"],
        "support": ["AuriX access key ကို Outline Client မှာ", "ထည့်ပြီး ချိတ်ဆက်အသုံးပြုတာပါ။"],
        "caption": f"""AuriX VPN က ဘယ် app နဲ့ သုံးရတာလဲဆိုရင်—official Outline Client နဲ့ သုံးရတာပါ။

AuriX Bot ကရတဲ့ access key အပြည့်အစုံကို Copy လုပ်ပြီး Outline Client ထဲထည့်ပါ။ Key က `ss://` နဲ့ စရပါမယ်။ Outline app တစ်ခုတည်းသွင်းထားရုံနဲ့ မချိတ်ဆက်နိုင်ပါဘူး—AuriX access key လည်း လိုပါတယ်။

ချိတ်ဆက်မရရင် ဖုန်းအမျိုးအစား၊ ဘယ်အဆင့်မှာ ရပ်နေလဲနဲ့ အမှားပြစာမျက်နှာပုံကို အဖွဲ့ထဲမှာ ပို့ပြီး အကူအညီတောင်းနိုင်ပါတယ်။ Key အပြည့်အစုံကို public group သို့မဟုတ် screenshot ထဲ မဖော်ပြပါနဲ့။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီအဖွဲ့ — {GROUP}
📥 Official Outline Client — {OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

{DISCLAIMER}

#AuriXVPN #OutlineClient #VPNSetup""",
    },
    {
        "id": "retention-allowance",
        "kind": "retention",
        "plate": "allowance",
        "series": "AURIX KEY → OUTLINE CLIENT",
        "headline": ["VPN လိုတဲ့အချိန်", "သုံးခွင့်မကုန်ပါစေနဲ့။"],
        "support": ["Outline Client မှာ သုံးနေတဲ့ AuriX key လက်ကျန်ကို", "Bot ရဲ့ Usage မှာ ကြိုစစ်ပါ။"],
        "fact": "50 GB · 3,000 ကျပ်  |  100 GB · 6,000 ကျပ်",
        "caption": f"""ဒီမှာပြောတဲ့ “လက်ကျန်” က ဖုန်း SIM ဒေတာ ဒါမှမဟုတ် မိုဘိုင်းအင်တာနက်ပက်ကေ့ချ် မဟုတ်ပါဘူး။ Outline Client မှာ သုံးနေတဲ့ AuriX access key အတွက် သတ်မှတ်ထားတဲ့ VPN ဒေတာလက်ကျန်ကို ဆိုလိုတာပါ။

VPN လိုတဲ့အချိန် သုံးခွင့်ကုန်မသွားအောင် AuriX Bot ရဲ့ 📶 Usage မှာ ကြိုစစ်ထားပါ။ လက်ကျန်နည်းလာရင် နောက်အစီအစဉ်ကို ရွေးနိုင်ပါတယ်။

🎁 နေ့စဉ် 300 MB အခမဲ့
🚀 လစဉ် 3 GB အခမဲ့
💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 လက်ကျန်စစ်ရန်နှင့် ဝယ်ယူရန် — {BOT}
💬 မရှင်းတာမေးရန် — {GROUP}
📥 Official Outline Client — {OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

{DISCLAIMER}

#AuriXVPN #OutlineClient #VPNလက်ကျန်""",
    },
    {
        "id": "retention-expiry",
        "kind": "retention",
        "plate": "expiry",
        "series": "AURIX KEY → OUTLINE CLIENT",
        "headline": ["Outline VPN သုံးနေရင်း", "AuriX key သက်တမ်း", "မကုန်ပါစေနဲ့။"],
        "support": ["Bot ရဲ့ Status မှာ သက်တမ်းကုန်ရက်ကို", "ကြိုစစ်ပြီး နောက်အစီအစဉ်ကို ရွေးထားပါ။"],
        "caption": f"""Outline Client မှာ AuriX VPN သုံးနေရင်း access key သက်တမ်းကုန်သွားတာမျိုး မဖြစ်ရအောင် ကြိုတင်စီစဉ်ထားပါ။

AuriX Bot ရဲ့ 📊 Status မှာ လက်ရှိ key သက်တမ်းကုန်ဆုံးမယ့်ရက်ကို စစ်နိုင်ပါတယ်။ ဆက်သုံးဖို့လိုရင် နောက်ဆုံးနေ့မရောက်ခင် နောက်အစီအစဉ်ကို ရွေးထားပါ။

💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 သက်တမ်းစစ်ရန်နှင့် ဝယ်ယူရန် — {BOT}
💬 အကူအညီတောင်းရန် — {GROUP}
📥 Official Outline Client — {OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

ငွေလွှဲပြေစာကို စစ်ဆေးအတည်ပြုပြီးမှ အခပေးအစီအစဉ် စတင်နိုင်ပါတယ်။

{DISCLAIMER}

#AuriXVPN #OutlineClient #VPNသက်တမ်း""",
    },
    {
        "id": "guide-overview",
        "kind": "guide",
        "device": "overview",
        "series": "OUTLINE KEY အသုံးပြုနည်း",
        "headline": ["AuriX Key ကို", "Outline Client မှာ သုံးပါ။"],
        "support": ["App သွင်းထားရုံနဲ့ မရသေးပါ။", "AuriX access key အပြည့်အစုံလည်း လိုပါတယ်။"],
        "steps": ["Official Outline Client ကိုသွင်း", "ss:// နဲ့စတဲ့ key အပြည့်ကိုထည့်", "Connect လုပ်ပြီး စသုံး"],
        "caption": f"""AuriX VPN စသုံးဖို့ လိုတာ နှစ်ခုရှိပါတယ်—official Outline Client app နဲ့ AuriX access key ပါ။

၁။ ကိုယ့်စက်အတွက် official Outline Client ကို သွင်းပါ။
၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ key အပြည့်အစုံကို Copy လုပ်ပါ။
၃။ Outline Client ထဲမှာ key ကိုထည့်ပြီး Connect လုပ်ပါ။

App သွင်းထားရုံနဲ့ VPN မချိတ်ဆက်နိုင်ပါဘူး။ Access key နဲ့ Outline server နှစ်ခုလုံး လိုအပ်ပါတယ်။

🤖 AuriX key ရယူရန် — {BOT}
📥 Official downloads — {OFFICIAL_DOWNLOADS}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineClient #OutlineKey""",
    },
    {
        "id": "guide-android",
        "kind": "guide",
        "device": "mobile",
        "series": "ANDROID · OUTLINE CLIENT",
        "headline": ["Android မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Official Outline Client · Android 10 နှင့်အထက်"],
        "steps": ["Google Play မှ Outline Client သွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""Android ဖုန်းမှာ AuriX VPN သုံးနည်း—

၁။ Google Play မှ official Outline Client ကို သွင်းပါ။
https://play.google.com/store/apps/details?id=org.outline.android.client

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပြီး Android က VPN ချိတ်ဆက်ခွင့်တောင်းရင် ခွင့်ပြုပါ။

Official minimum requirement: Android 10 နှင့်အထက်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineAndroid #VPNSetup""",
    },
    {
        "id": "guide-ios",
        "kind": "guide",
        "device": "mobile",
        "series": "IPHONE & IPAD · OUTLINE CLIENT",
        "headline": ["iPhone / iPad မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Official Outline Client · iOS 15.5 နှင့်အထက်"],
        "steps": ["App Store မှ Outline Client သွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""iPhone သို့မဟုတ် iPad မှာ AuriX VPN သုံးနည်း—

၁။ App Store မှ official Outline Client ကို သွင်းပါ။
https://itunes.apple.com/us/app/outline-app/id1356177741

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပြီး iOS က VPN configuration ထည့်ခွင့်တောင်းရင် ခွင့်ပြုပါ။

Official minimum requirement: iOS 15.5 နှင့်အထက်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineiOS #VPNSetup""",
    },
    {
        "id": "guide-windows",
        "kind": "guide",
        "device": "desktop",
        "series": "WINDOWS · OUTLINE CLIENT",
        "headline": ["Windows မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Official Outline Client · Windows 10 နှင့်အထက်"],
        "steps": ["Official Windows Client ကိုသွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""Windows ကွန်ပျူတာမှာ AuriX VPN သုံးနည်း—

၁။ Official Outline Client for Windows ကို Download လုပ်ပြီး သွင်းပါ။
https://s3.amazonaws.com/outline-releases/client/windows/stable/Outline-Client.exe

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပါ။ Firewall သို့မဟုတ် antivirus က Outline traffic ကိုပိတ်ထားရင် ခွင့်ပြုချက်ကို စစ်ပါ။

Official minimum requirement: Windows 10 နှင့်အထက်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineWindows #VPNSetup""",
    },
    {
        "id": "guide-macos",
        "kind": "guide",
        "device": "desktop",
        "series": "macOS · OUTLINE CLIENT",
        "headline": ["Mac မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Official Outline Client · macOS 12 နှင့်အထက်"],
        "steps": ["Mac App Store မှ Outline Client သွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""Mac မှာ AuriX VPN သုံးနည်း—

၁။ Mac App Store မှ official Outline Client ကို သွင်းပါ။
https://itunes.apple.com/us/app/outline-app/id1356178125

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပြီး macOS က VPN configuration ခွင့်ပြုချက်တောင်းရင် အတည်ပြုပါ။

Official minimum requirement: macOS 12 နှင့်အထက်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlinemacOS #VPNSetup""",
    },
    {
        "id": "guide-linux",
        "kind": "guide",
        "device": "desktop",
        "series": "LINUX · OUTLINE CLIENT",
        "headline": ["Linux မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Debian-based Linux · Ubuntu 20.04 နှင့်အထက်"],
        "steps": ["Official repository သို့ .deb မှသွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""Linux မှာ AuriX VPN သုံးနည်း—

၁။ Official Linux installation guide အတိုင်း Outline Client ကို repository သို့မဟုတ် `.deb` package ကနေ သွင်းပါ။
https://support.getoutline.org/en-GB/client/getting-started/install-linux/

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပါ။ Linux Client မှာ in-app auto-update မရှိတဲ့အတွက် package update ကို ကိုယ်တိုင်စစ်ပါ။

Official minimum requirement: Ubuntu 20.04 နှင့်အထက်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineLinux #VPNSetup""",
    },
    {
        "id": "guide-chromeos",
        "kind": "guide",
        "device": "laptop",
        "series": "CHROMEOS · OUTLINE CLIENT",
        "headline": ["Chromebook မှာ", "AuriX Key ထည့်သုံးနည်း"],
        "support": ["Google Play မှ official Android Outline Client"],
        "steps": ["Google Play မှ Outline Client သွင်း", "AuriX ss:// key အပြည့်ကို Copy", "Outline ထဲထည့်ပြီး Connect"],
        "caption": f"""Google Play သုံးနိုင်တဲ့ Chromebook မှာ AuriX VPN သုံးနည်း—

၁။ Google Play မှ official Android Outline Client ကို သွင်းပါ။
https://play.google.com/store/apps/details?id=org.outline.android.client

၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်ကို Copy လုပ်ပါ။
၃။ Outline Client ကိုဖွင့်ပြီး key ကို Add လုပ်ပါ။
၄။ Connect လုပ်ပါ။

Chromebook မှာ Google Play/Android app support ရှိရပါမယ်။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီ — {GROUP}

{DISCLAIMER}

#AuriXVPN #OutlineChromeOS #VPNSetup""",
    },
]


def identity_lockup() -> str:
    return """
      <text x="158" y="113" fill="#f7f9fc" font-family="Avenir Next, Arial, sans-serif" font-size="35" font-weight="900">Auri<tspan fill="#36e2ff">X</tspan> KEY</text>
      <path d="M327 96h34m-12-12 12 12-12 12" fill="none" stroke="#9fb1bf" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <text x="454" y="107" fill="#e7eef5" font-family="Avenir Next, Arial, sans-serif" font-size="23" font-weight="800" letter-spacing="1.6">OUTLINE CLIENT</text>
    """


def retention_svg(post: dict) -> str:
    headline_size = 54 if len(post["headline"]) == 3 else 60
    support_y = 638 if len(post["headline"]) == 3 else 565
    fact = ""
    if post.get("fact"):
        fact = f'<text x="76" y="690" fill="#ffc857" font-family="Noto Sans Myanmar" font-size="27" font-weight="800">{escape(post["fact"])}</text>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
      <defs><linearGradient id="shade" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#05111d" stop-opacity=".98"/><stop offset=".39" stop-color="#05111d" stop-opacity=".92"/><stop offset=".63" stop-color="#05111d" stop-opacity=".55"/><stop offset="1" stop-color="#05111d" stop-opacity=".08"/></linearGradient><linearGradient id="gold" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#ffc857"/><stop offset="1" stop-color="#ff9d34"/></linearGradient></defs>
      <rect width="1080" height="1350" fill="url(#shade)"/>
      {identity_lockup()}
      <rect x="76" y="230" width="43" height="4" rx="2" fill="url(#gold)"/>
      <text x="134" y="248" fill="#ffc857" font-family="Avenir Next, Noto Sans Myanmar, sans-serif" font-size="20" font-weight="800">{escape(post['series'])}</text>
      <text x="76" y="355" fill="#f7f9fc" font-family="Noto Sans Myanmar" font-size="{headline_size}" font-weight="900">{tspans(post['headline'], 76, 88)}</text>
      <text x="76" y="{support_y}" fill="#dbe6ef" font-family="Noto Sans Myanmar" font-size="27" font-weight="600">{tspans(post['support'], 76, 47)}</text>
      {fact}
      <g transform="translate(76 1260)"><rect y="13" width="5" height="7" rx="2.5" fill="#36e2ff"/><rect x="10" y="7" width="5" height="13" rx="2.5" fill="#36e2ff"/><rect x="20" width="5" height="20" rx="2.5" fill="#7765ff"/><text x="40" y="18" fill="#bfd0dd" font-family="Avenir Next, Arial, sans-serif" font-size="18" font-weight="700" letter-spacing="2.2">AURIX KEY · OFFICIAL OUTLINE CLIENT</text></g>
    </svg>"""


def device_art(device: str) -> str:
    if device == "mobile":
        return '<rect x="720" y="355" width="260" height="530" rx="48" fill="#0a1b29" stroke="#46637a" stroke-width="5"/><rect x="750" y="405" width="200" height="350" rx="28" fill="#102b3e"/><circle cx="850" cy="820" r="18" fill="#526b7f"/>'
    if device == "desktop":
        return '<rect x="620" y="365" width="390" height="420" rx="30" fill="#091824" stroke="#46637a" stroke-width="5"/><rect x="655" y="405" width="320" height="290" rx="20" fill="#102b3e"/><path d="M575 800h480l-48 65H623z" fill="#17344a" stroke="#46637a" stroke-width="4"/>'
    if device == "laptop":
        return '<rect x="650" y="380" width="350" height="355" rx="28" fill="#091824" stroke="#46637a" stroke-width="5"/><rect x="680" y="415" width="290" height="240" rx="18" fill="#102b3e"/><path d="M605 750h440l-52 62H657z" fill="#17344a" stroke="#46637a" stroke-width="4"/>'
    return '<path d="M600 650C680 550 735 520 825 510" fill="none" stroke="#36e2ff" stroke-width="16" stroke-linecap="round" opacity=".52"/><path d="M600 690C710 620 770 610 860 600" fill="none" stroke="#7765ff" stroke-width="14" stroke-linecap="round" opacity=".5"/>'


def guide_svg(post: dict) -> str:
    step_blocks = []
    for index, step in enumerate(post["steps"], 1):
        y = 760 + (index - 1) * 108
        step_blocks.append(f'<text x="76" y="{y}" fill="#ffc857" font-family="Noto Sans Myanmar" font-size="35" font-weight="900">{index}</text><text x="126" y="{y}" fill="#e5edf4" font-family="Noto Sans Myanmar" font-size="27" font-weight="650">{escape(step)}</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
      <defs><radialGradient id="bg" cx="68%" cy="40%" r="82%"><stop stop-color="#143a4d"/><stop offset=".52" stop-color="#071521"/><stop offset="1" stop-color="#040c14"/></radialGradient><linearGradient id="line" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#ffc857"/><stop offset=".5" stop-color="#36e2ff"/><stop offset="1" stop-color="#7765ff"/></linearGradient></defs>
      <rect width="1080" height="1350" fill="url(#bg)"/>
      <circle cx="900" cy="250" r="280" fill="none" stroke="#36e2ff" stroke-width="4" opacity=".12"/><circle cx="900" cy="250" r="220" fill="none" stroke="#7765ff" stroke-width="4" opacity=".12"/>
      {identity_lockup()}
      <rect x="76" y="218" width="43" height="4" rx="2" fill="#ffc857"/><text x="134" y="236" fill="#ffc857" font-family="Avenir Next, Noto Sans Myanmar, sans-serif" font-size="20" font-weight="800">{escape(post['series'])}</text>
      <text x="76" y="345" fill="#f7f9fc" font-family="Noto Sans Myanmar" font-size="58" font-weight="900">{tspans(post['headline'], 76, 88)}</text>
      <text x="76" y="560" fill="#dbe6ef" font-family="Noto Sans Myanmar" font-size="27" font-weight="600">{tspans(post['support'], 76, 46)}</text>
      <path d="M80 674h520" stroke="url(#line)" stroke-width="5" stroke-linecap="round" opacity=".75"/>
      {''.join(step_blocks)}
      {device_art(post['device'])}
      <g transform="translate(76 1260)"><rect y="13" width="5" height="7" rx="2.5" fill="#36e2ff"/><rect x="10" y="7" width="5" height="13" rx="2.5" fill="#36e2ff"/><rect x="20" width="5" height="20" rx="2.5" fill="#7765ff"/><text x="40" y="18" fill="#bfd0dd" font-family="Avenir Next, Arial, sans-serif" font-size="18" font-weight="700" letter-spacing="2.2">AURIX KEY · OUTLINE SETUP GUIDE</text></g>
    </svg>"""


def render() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mark_png = TEMP_DIR / "aurix-mark.png"
    outline_top = TEMP_DIR / "outline-top-70.png"
    outline_mobile = TEMP_DIR / "outline-hero-150.png"
    outline_desktop = TEMP_DIR / "outline-hero-160.png"
    outline_overview = TEMP_DIR / "outline-hero-220.png"
    run("rsvg-convert", "-w", "70", "-h", "70", "-o", str(mark_png), str(MARK))
    run("magick", str(OUTLINE_ICON), "-resize", "70x70", str(outline_top))
    run("magick", str(OUTLINE_ICON), "-resize", "150x150", str(outline_mobile))
    run("magick", str(OUTLINE_ICON), "-resize", "160x160", str(outline_desktop))
    run("magick", str(OUTLINE_ICON), "-resize", "220x220", str(outline_overview))

    manifest_posts = []
    for post in POSTS:
        stem = f"{STAMP}_{post['id']}_{VERSION}"
        svg_path = SOURCE_DIR / f"{stem}.svg"
        png_path = EXPORT_DIR / f"{stem}.png"
        txt_path = EXPORT_DIR / f"{stem}.txt"
        overlay_path = TEMP_DIR / f"{stem}-overlay.png"
        svg_path.write_text(retention_svg(post) if post["kind"] == "retention" else guide_svg(post), encoding="utf-8")
        caption = post["caption"].strip()
        if post["kind"] == "guide" and post["id"] != "guide-overview":
            caption += "\n\n🔐 AuriX access key အပြည့်အစုံကို public post၊ comment၊ group သို့မဟုတ် screenshot ထဲ မဖော်ပြပါနဲ့။"
        txt_path.write_text(caption + "\n", encoding="utf-8")
        run("rsvg-convert", "-w", "1080", "-h", "1350", "-o", str(overlay_path), str(svg_path))

        if post["kind"] == "retention":
            run("magick", str(PLATES[post["plate"]]), "-resize", "1080x1350^", "-gravity", "center", "-extent", "1080x1350", str(overlay_path), "-compose", "over", "-composite", str(mark_png), "-gravity", "northwest", "-geometry", "+76+58", "-compose", "over", "-composite", str(png_path))
        else:
            run("magick", str(overlay_path), str(mark_png), "-gravity", "northwest", "-geometry", "+76+58", "-compose", "over", "-composite", str(png_path))

        run("magick", str(png_path), str(outline_top), "-gravity", "northwest", "-geometry", "+373+58", "-compose", "over", "-composite", str(png_path))
        if post["kind"] == "guide":
            if post["device"] == "mobile":
                hero_icon = outline_mobile
                offset = "+775+475"
            elif post["device"] in {"desktop", "laptop"}:
                hero_icon = outline_desktop
                offset = "+735+465"
            else:
                hero_icon = outline_overview
                offset = "+735+410"
            run("magick", str(png_path), str(hero_icon), "-gravity", "northwest", "-geometry", offset, "-compose", "over", "-composite", str(png_path))

        run("magick", str(png_path), "-background", "#071421", "-alpha", "remove", "-alpha", "off", str(png_path))
        manifest_posts.append({"id": post["id"], "kind": post["kind"], "image": png_path.name, "caption": txt_path.name, "source": str(svg_path.relative_to(ROOT))})

    manifest = {
        "campaign": f"aurix-outline-cross-channel-{VERSION}",
        "created_at": "2026-08-28T17:39:53+06:30",
        "publish_ready": True,
        "format": "1080x1350 PNG plus UTF-8 Burmese TXT",
        "distribution": ["Facebook Page", "Telegram public channel", "Telegram community group", "Telegram bot onboarding"],
        "outline_positioning": "AuriX independently provides Outline-compatible access keys for use in the official Outline Client; no official partnership is claimed.",
        "posts": manifest_posts,
    }
    (EXPORT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    render()
