#!/usr/bin/env python3
"""Render V9 retention ads over approved text-free narrative masters."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[4]
STAMP = "20260828-202131"
SOURCE_DIR = ROOT / "assets/social/campaign/retention-v9-sources"
GRAPHICS_DIR = ROOT / "assets/social/campaign/retention-v9-graphics-only"
EXPORT_DIR = ROOT / "assets/social/campaign/exports/20260828-202131-retention-v9"
TEMP_DIR = Path("/private/tmp/aurix-retention-v9-20260828-202131")
MARK = ROOT / "brand/v2/aurix-logo-mark-v2.svg"
OUTLINE_ICON = ROOT / "brand/outline/official/outline-client-icon-1024.png"

BOT = "https://t.me/aurix_outline_vpn_bot"
GROUP = "https://t.me/+oA18TDWAD9NiNWU1"
CHANNEL = "https://t.me/AurixDigitalStore"
DOWNLOADS = "https://developer.getoutline.org/download-links/"
DISCLOSURE = (
    "AuriX သည် Outline Foundation ၏ တရားဝင်မိတ်ဖက် မဟုတ်ပါ။ "
    "AuriX က Outline-compatible access key ကို သီးခြားဝန်ဆောင်မှုပေးပြီး "
    "official Outline Client ဖြင့် အသုံးပြုရပါသည်။"
)


POSTS = [
    {
        "id": "setup",
        "graphic": f"{STAMP}_setup-human-help_graphics-only.png",
        "series": "AURIX VPN အကူအညီ",
        "headline": ["Outline VPN ချိတ်မရလို့", "စိတ်ညစ်နေလား။"],
        "support": ["AuriX key ကို Outline Client မှာ ထည့်သုံးတာပါ။", "မရရင် အဖွဲ့ထဲမှာ ကူညီပေးမယ်။"],
        "caption": f"""Outline VPN ချိတ်မရလို့ အကြိမ်ကြိမ်စမ်းနေရသလား။ တစ်ယောက်တည်း စမ်းရင်း အချိန်ကုန်မနေပါနဲ့။

AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်အစုံကို official Outline Client ထဲ ထည့်ပြီး Connect လုပ်ရပါတယ်။ Outline app သွင်းထားရုံနဲ့ မရသေးပါဘူး—AuriX key လည်း လိုပါတယ်။

မချိတ်ဆက်နိုင်သေးရင် ဘယ်စက်သုံးနေလဲ၊ ဘယ်အဆင့်မှာ ရပ်နေလဲနဲ့ အမှားပြစာမျက်နှာပုံကို AuriX အဖွဲ့ထဲမှာ ပို့ပြီး အကူအညီတောင်းနိုင်ပါတယ်။ Key အပြည့်အစုံကို public group သို့မဟုတ် screenshot ထဲ မဖော်ပြပါနဲ့။

🤖 AuriX key ရယူရန် — {BOT}
💬 အကူအညီတောင်းရန် — {GROUP}
📥 Official Outline Client — {DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

{DISCLOSURE}

#AuriXVPN #OutlineClient #VPNSetup""",
    },
    {
        "id": "allowance",
        "graphic": f"{STAMP}_allowance-worry_graphics-only.png",
        "series": "AURIX VPN အစီအစဉ်",
        "headline": ["AuriX VPN ဒေတာ", "မလောက်မှာ စိုးရိမ်နေလား။"],
        "support": ["Bot ရဲ့ Usage မှာ key လက်ကျန်ကို စစ်ပြီး", "ကိုယ်နဲ့ကိုက်တဲ့ အစီအစဉ်ကို ရွေးနိုင်တယ်။"],
        "fact": "50 GB · 3,000 ကျပ်  |  100 GB · 6,000 ကျပ်",
        "caption": f"""လိုအပ်တဲ့အချိန်မှာ AuriX VPN ဒေတာ မလောက်တော့မှာ စိုးရိမ်နေရသလား။

ဒီမှာပြောတဲ့ VPN ဒေတာက ဖုန်း SIM ဒေတာ ဒါမှမဟုတ် မိုဘိုင်းအင်တာနက်ပက်ကေ့ချ် မဟုတ်ပါဘူး။ Outline Client မှာ သုံးနေတဲ့ AuriX access key အတွက် သတ်မှတ်ထားတဲ့ အသုံးပြုနိုင်သည့်ပမာဏကို ဆိုလိုတာပါ။

AuriX Bot ရဲ့ 📶 Usage မှာ key လက်ကျန်ကို ကြိုစစ်နိုင်ပါတယ်။ လက်ကျန်နည်းလာရင် ကိုယ်နဲ့ကိုက်တဲ့ နောက်အစီအစဉ်ကို ရွေးပါ။

🎁 နေ့စဉ် 300 MB အခမဲ့
🚀 လစဉ် 3 GB အခမဲ့
💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 လက်ကျန်စစ်ရန်နှင့် ဝယ်ယူရန် — {BOT}
💬 မရှင်းတာမေးရန် — {GROUP}
📥 Official Outline Client — {DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

{DISCLOSURE}

#AuriXVPN #OutlineClient #VPNဒေတာ""",
    },
    {
        "id": "expiry",
        "graphic": f"{STAMP}_expiry-worry_graphics-only.png",
        "series": "AURIX VPN သက်တမ်း",
        "headline": ["VPN သက်တမ်းကုန်တော့မှာ", "စိုးရိမ်နေလား။"],
        "support": ["Bot ရဲ့ Status မှာ ကုန်မယ့်ရက်ကို ကြိုစစ်ပြီး", "ဆက်သုံးမယ့် အစီအစဉ်ကို ရွေးနိုင်တယ်။"],
        "caption": f"""AuriX VPN လိုအပ်နေတဲ့အချိန်မှာ key သက်တမ်းကုန်သွားမှာ စိုးရိမ်နေရသလား။

AuriX Bot ရဲ့ 📊 Status မှာ လက်ရှိ key ကုန်ဆုံးမယ့်ရက်ကို ကြိုစစ်နိုင်ပါတယ်။ ဆက်သုံးဖို့လိုရင် နောက်ဆုံးနေ့မရောက်ခင် နောက်အစီအစဉ်ကို ရွေးထားပါ။

💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 သက်တမ်းစစ်ရန်နှင့် ဝယ်ယူရန် — {BOT}
💬 အကူအညီတောင်းရန် — {GROUP}
📥 Official Outline Client — {DOWNLOADS}
📣 AuriX သတင်းများ — {CHANNEL}

ငွေလွှဲပြေစာကို စစ်ဆေးအတည်ပြုပြီးမှ အခပေးအစီအစဉ် စတင်နိုင်ပါတယ်။

{DISCLOSURE}

#AuriXVPN #OutlineClient #VPNသက်တမ်း""",
    },
]


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def tspans(lines: list[str], x: int, dy: int) -> str:
    return "".join(
        f'<tspan x="{x}"' + ("" if i == 0 else f' dy="{dy}"') + f'>{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )


def overlay_svg(post: dict) -> str:
    fact = ""
    if post.get("fact"):
        fact = f'<text x="76" y="650" fill="#ffc857" font-family="Noto Sans Myanmar" font-size="26" font-weight="800">{escape(post["fact"])}</text>'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
      <defs>
        <linearGradient id="shade" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#04101b" stop-opacity=".96"/><stop offset=".40" stop-color="#04101b" stop-opacity=".88"/><stop offset=".62" stop-color="#04101b" stop-opacity=".38"/><stop offset="1" stop-color="#04101b" stop-opacity=".02"/></linearGradient>
        <linearGradient id="bottom" x1="0" y1="1" x2="0" y2="0"><stop stop-color="#04101b" stop-opacity=".44"/><stop offset=".35" stop-color="#04101b" stop-opacity="0"/></linearGradient>
      </defs>
      <rect width="1080" height="1350" fill="url(#shade)"/><rect width="1080" height="1350" fill="url(#bottom)"/>
      <text x="158" y="113" fill="#f7f9fc" font-family="Avenir Next, Arial, sans-serif" font-size="35" font-weight="900">Auri<tspan fill="#36e2ff">X</tspan> KEY</text>
      <path d="M327 96h34m-12-12 12 12-12 12" fill="none" stroke="#9fb1bf" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <text x="454" y="107" fill="#e7eef5" font-family="Avenir Next, Arial, sans-serif" font-size="23" font-weight="800" letter-spacing="1.6">OUTLINE CLIENT</text>
      <rect x="76" y="220" width="43" height="4" rx="2" fill="#ffc857"/><text x="134" y="238" fill="#ffc857" font-family="Avenir Next, Noto Sans Myanmar, sans-serif" font-size="20" font-weight="800">{escape(post['series'])}</text>
      <text x="76" y="350" fill="#f7f9fc" font-family="Noto Sans Myanmar" font-size="56" font-weight="900">{tspans(post['headline'], 76, 88)}</text>
      <text x="76" y="555" fill="#dbe6ef" font-family="Noto Sans Myanmar" font-size="27" font-weight="600">{tspans(post['support'], 76, 46)}</text>
      {fact}
      <g transform="translate(76 1260)"><rect y="13" width="5" height="7" rx="2.5" fill="#36e2ff"/><rect x="10" y="7" width="5" height="13" rx="2.5" fill="#36e2ff"/><rect x="20" width="5" height="20" rx="2.5" fill="#7765ff"/><text x="40" y="18" fill="#bfd0dd" font-family="Avenir Next, Arial, sans-serif" font-size="18" font-weight="700" letter-spacing="2.2">AURIX KEY · OFFICIAL OUTLINE CLIENT</text></g>
    </svg>"""


def main() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mark_png = TEMP_DIR / "aurix-mark.png"
    outline_png = TEMP_DIR / "outline-70.png"
    run("rsvg-convert", "-w", "70", "-h", "70", "-o", str(mark_png), str(MARK))
    run("magick", str(OUTLINE_ICON), "-resize", "70x70", str(outline_png))
    manifest_posts = []
    for post in POSTS:
        stem = f"{STAMP}_retention-{post['id']}_v9"
        source = SOURCE_DIR / f"{stem}.svg"
        overlay = TEMP_DIR / f"{stem}-overlay.png"
        image = EXPORT_DIR / f"{stem}.png"
        caption = EXPORT_DIR / f"{stem}.txt"
        source.write_text(overlay_svg(post), encoding="utf-8")
        caption.write_text(post["caption"].strip() + "\n", encoding="utf-8")
        run("rsvg-convert", "-w", "1080", "-h", "1350", "-o", str(overlay), str(source))
        run("magick", str(GRAPHICS_DIR / post["graphic"]), str(overlay), "-compose", "over", "-composite", str(mark_png), "-gravity", "northwest", "-geometry", "+76+58", "-compose", "over", "-composite", str(outline_png), "-geometry", "+373+58", "-compose", "over", "-composite", "-background", "#071421", "-alpha", "remove", "-alpha", "off", str(image))
        manifest_posts.append({"id": post["id"], "image": image.name, "caption": caption.name, "graphics_only": post["graphic"], "source": str(source.relative_to(ROOT))})
    manifest = {
        "campaign": "aurix-retention-v9-natural-hooks",
        "created_at": "2026-08-28T20:21:31+06:30",
        "publish_ready": True,
        "format": "1080x1350 labeled PNG + TXT caption + separate graphics-only 1080x1350 PNG",
        "posts": manifest_posts,
    }
    (EXPORT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
