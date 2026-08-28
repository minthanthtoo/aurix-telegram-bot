#!/usr/bin/env python3
"""Render the focused four-post V10 set using the approved V8 visual system."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BASE_PATH = Path(__file__).with_name("render_outline_cross_channel_v8.py")
SPEC = importlib.util.spec_from_file_location("aurix_outline_v8", BASE_PATH)
assert SPEC and SPEC.loader
v8 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v8)

STAMP = "20260828-212752"
EXPORT_DIR = ROOT / "assets/social/campaign/exports/20260828-212752-outline-core-v10"


def get(post_id: str) -> dict:
    return deepcopy(next(post for post in v8.POSTS if post["id"] == post_id))


setup = get("retention-setup")
setup["headline"] = ["Outline VPN", "ချိတ်ဆက်မရဘူးလား။"]
setup["support"] = ["AuriX key ကို Outline Client ထဲ ထည့်သုံးတာပါ။", "မရရင် အဖွဲ့ထဲမှာ ကူညီပေးမယ်။"]
setup["caption"] = f"""Outline VPN ချိတ်ဆက်မရလို့ အကြိမ်ကြိမ်စမ်းနေရသလား။ တစ်ယောက်တည်း စမ်းရင်း အချိန်ကုန်မနေပါနဲ့။

AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ access key အပြည့်အစုံကို official Outline Client ထဲ ထည့်ပြီး Connect လုပ်ရပါတယ်။ Outline app သွင်းထားရုံနဲ့ မရသေးပါဘူး—AuriX key လည်း လိုပါတယ်။

မချိတ်ဆက်နိုင်သေးရင် ဘယ်စက်သုံးနေလဲ၊ ဘယ်အဆင့်မှာ ရပ်နေလဲနဲ့ အမှားပြစာမျက်နှာပုံကို AuriX အဖွဲ့ထဲမှာ ပို့ပြီး အကူအညီတောင်းနိုင်ပါတယ်။ Key အပြည့်အစုံကို public group သို့မဟုတ် screenshot ထဲ မဖော်ပြပါနဲ့။

🤖 AuriX key ရယူရန် — {v8.BOT}
💬 အကူအညီတောင်းရန် — {v8.GROUP}
📥 Official Outline Client — {v8.OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {v8.CHANNEL}

{v8.DISCLAIMER}

#AuriXVPN #OutlineClient #VPNSetup"""

allowance = get("retention-allowance")
allowance["headline"] = ["AuriX VPN ဒေတာ", "လောက်ပါ့မလား။"]
allowance["support"] = ["Bot ရဲ့ Usage မှာ key လက်ကျန်ကို ကြိုစစ်ပြီး", "လိုအပ်ရင် နောက်အစီအစဉ်ကို ရွေးနိုင်တယ်။"]
allowance["caption"] = f"""AuriX VPN ဒေတာ လောက်ပါ့မလားလို့ စိုးရိမ်နေရသလား။

ဒီမှာပြောတဲ့ VPN ဒေတာက ဖုန်း SIM ဒေတာ ဒါမှမဟုတ် မိုဘိုင်းအင်တာနက်ပက်ကေ့ချ် မဟုတ်ပါဘူး။ Outline Client မှာ သုံးနေတဲ့ AuriX access key အတွက် သတ်မှတ်ထားတဲ့ အသုံးပြုနိုင်သည့်ပမာဏကို ဆိုလိုတာပါ။

AuriX Bot ရဲ့ 📶 Usage မှာ key လက်ကျန်ကို ကြိုစစ်နိုင်ပါတယ်။ လက်ကျန်နည်းလာရင် ကိုယ်နဲ့ကိုက်တဲ့ နောက်အစီအစဉ်ကို ရွေးပါ။

🎁 နေ့စဉ် 300 MB အခမဲ့
🚀 လစဉ် 3 GB အခမဲ့
💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 လက်ကျန်စစ်ရန်နှင့် ဝယ်ယူရန် — {v8.BOT}
💬 မရှင်းတာမေးရန် — {v8.GROUP}
📥 Official Outline Client — {v8.OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {v8.CHANNEL}

{v8.DISCLAIMER}

#AuriXVPN #OutlineClient #VPNဒေတာ"""

expiry = get("retention-expiry")
expiry["headline"] = ["VPN သက်တမ်းကုန်တော့မှာ", "စိုးရိမ်နေလား။"]
expiry["support"] = ["Bot ရဲ့ Status မှာ ကုန်မယ့်ရက်ကို ကြိုစစ်ပြီး", "ဆက်သုံးမယ့် အစီအစဉ်ကို ရွေးနိုင်တယ်။"]
expiry["caption"] = f"""AuriX VPN သက်တမ်းကုန်တော့မှာ စိုးရိမ်နေရသလား။

AuriX Bot ရဲ့ 📊 Status မှာ လက်ရှိ key ကုန်ဆုံးမယ့်ရက်ကို ကြိုစစ်နိုင်ပါတယ်။ ဆက်သုံးဖို့လိုရင် နောက်ဆုံးနေ့မရောက်ခင် နောက်အစီအစဉ်ကို ရွေးထားပါ။

💎 50 GB · ရက် 30 · 3,000 ကျပ်
💠 100 GB · ရက် 30 · 6,000 ကျပ်

🤖 သက်တမ်းစစ်ရန်နှင့် ဝယ်ယူရန် — {v8.BOT}
💬 အကူအညီတောင်းရန် — {v8.GROUP}
📥 Official Outline Client — {v8.OFFICIAL_DOWNLOADS}
📣 AuriX သတင်းများ — {v8.CHANNEL}

ငွေလွှဲပြေစာကို စစ်ဆေးအတည်ပြုပြီးမှ အခပေးအစီအစဉ် စတင်နိုင်ပါတယ်။

{v8.DISCLAIMER}

#AuriXVPN #OutlineClient #VPNသက်တမ်း"""

guide = get("guide-overview")
guide["headline"] = ["AuriX Key ကို", "ဘယ်လိုသုံးမလဲ။"]
guide["support"] = ["Official Outline Client သွင်း၊ key ထည့်ပြီး", "Connect လုပ်ရုံပါပဲ။"]
guide["steps"] = ["Official Outline Client ကိုသွင်း", "ss:// နဲ့စတဲ့ AuriX key အပြည့်ကိုထည့်", "Connect လုပ်ပြီး စသုံး"]
guide["caption"] = f"""AuriX VPN စသုံးဖို့ official Outline Client app နဲ့ AuriX access key—နှစ်ခုလုံး လိုပါတယ်။

၁။ ကိုယ့်စက်အတွက် official Outline Client ကို သွင်းပါ။
၂။ AuriX Bot ကရတဲ့ `ss://` နဲ့စတဲ့ key အပြည့်အစုံကို Copy လုပ်ပါ။
၃။ Outline Client ထဲမှာ key ကို ထည့်ပါ။
၄။ Connect လုပ်ပြီး စသုံးပါ။

Outline app သွင်းထားရုံနဲ့ VPN မချိတ်ဆက်နိုင်ပါဘူး။ AuriX access key လည်း လိုပါတယ်။ Key အပြည့်အစုံကို public post၊ comment၊ group သို့မဟုတ် screenshot ထဲ မဖော်ပြပါနဲ့။

🤖 AuriX key ရယူရန် — {v8.BOT}
📥 Official Outline Client — {v8.OFFICIAL_DOWNLOADS}
💬 အကူအညီ — {v8.GROUP}
📣 AuriX သတင်းများ — {v8.CHANNEL}

{v8.DISCLAIMER}

#AuriXVPN #OutlineClient #OutlineKey"""


def main() -> None:
    v8.STAMP = STAMP
    v8.VERSION = "v10"
    v8.SOURCE_DIR = ROOT / "assets/social/campaign/outline-v10-sources" / STAMP
    v8.EXPORT_DIR = EXPORT_DIR
    v8.TEMP_DIR = Path("/private/tmp/aurix-outline-v10-20260828-212752")
    v8.POSTS = [setup, allowance, expiry, guide]
    v8.render()
    manifest_path = EXPORT_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["campaign"] = "aurix-outline-core-v10"
    manifest["created_at"] = "2026-08-28T21:27:52+06:30"
    manifest["scope"] = "Three approved V8 retention concepts with Burmese nuance only, plus one Outline key guide."
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
