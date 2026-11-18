"""Read-only strategy-guide contracts for the Games storefront.

The guide layer deliberately separates game knowledge from commerce, identity,
and fulfillment.  MLBB can be refreshed from the public MLBBDex JSON API when
an operator explicitly runs a refresh; the storefront never fetches that API
on a customer request.  The Myanmar battle-royale guide is an offline,
localized strategy reference because the official PUBG API does not expose
PUBG Mobile data.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping
from urllib.request import Request, urlopen


GUIDE_SCHEMA_VERSION = 1
MLBBDex_BASE_URL = "https://mlbbdex.com/api/v1"
MLBBDex_RANKINGS_SYNC_WINDOW_DAYS = 7
_MYANMAR_TIMEZONE = timezone(timedelta(hours=6, minutes=30))
DEFAULT_MLBB_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "config" / "games" / "mlbb-strategy.json"
MLBBDex_SOURCE = {
    "name": "MLBBDex public API",
    "url": "https://mlbbdex.com/en/api-doc",
    "data_source": "Mobile Legends Wiki",
    "license": "CC BY-SA",
    "license_scope": "Underlying Mobile Legends Wiki static game facts; ranking provenance is described separately.",
    "attribution_required": True,
    "rankings_provider": "Rone Arena community API, as disclosed by MLBBDex",
    "rankings_provider_url": "https://arena.rone.dev/api/docs",
    "rankings_terms_url": "https://github.com/ridwaanhall/rone-arena-api#license--attribution",
    "rankings_license_status": "no separate ranking-data license stated in the MLBBDex or Rone docs",
    "rankings_commercial_use_status": "not verified; Rone documents educational, analytical, and community purposes only",
    "player_data": False,
}


class StrategyGuideInputError(ValueError):
    """A guide identifier or snapshot is invalid."""


_MLBB_HEROES = (
    {"slug": "freya", "name": "Freya", "roles": ["Fighter"], "lanes": ["Exp Lane", "Jungle"]},
    {"slug": "gloo", "name": "Gloo", "roles": ["Tank"], "lanes": ["Exp Lane", "Roam"]},
    {"slug": "harley", "name": "Harley", "roles": ["Mage", "Assassin"], "lanes": ["Mid Lane", "Jungle"]},
    {"slug": "marcel", "name": "Marcel", "roles": ["Support"], "lanes": ["Roam"]},
    {"slug": "sora", "name": "Sora", "roles": ["Fighter"], "lanes": ["Exp Lane"]},
    {"slug": "estes", "name": "Estes", "roles": ["Support"], "lanes": ["Roam"]},
    {"slug": "guinevere", "name": "Guinevere", "roles": ["Fighter"], "lanes": ["Exp Lane"]},
    {"slug": "helcurt", "name": "Helcurt", "roles": ["Assassin"], "lanes": ["Jungle"]},
    {"slug": "zhuxin", "name": "Zhuxin", "roles": ["Mage"], "lanes": ["Mid Lane"]},
)

_MLBB_ITEMS = (
    {"slug": "berserkers-fury", "name": "Berserker's Fury", "category": "Attack", "price": 2390, "stats": ["+65 Physical ATK", "+25% Crit Chance"]},
    {"slug": "blade-of-despair", "name": "Blade of Despair", "category": "Attack", "price": 3010, "stats": ["+160 Physical ATK", "+5 Movement SPD"]},
    {"slug": "blade-of-the-heptaseas", "name": "Blade of the Heptaseas", "category": "Attack", "price": 1950, "stats": ["+70 Physical ATK", "+250 HP"]},
    {"slug": "corrosion-scythe", "name": "Corrosion Scythe", "category": "Attack", "price": 2050, "stats": ["+30 Physical ATK", "+5% Attack Speed"]},
)

_MYANMAR_BATTLE_ROYALE_SECTIONS = (
    {
        "id": "squad-roles",
        "title": "အဖွဲ့တာဝန်များ",
        "title_en": "Squad roles",
        "cards": [
            {"title": "IGL / Shot-caller", "body": "ဝိုင်းပိတ်ရာလမ်း၊ အချိန်နှင့် ပြန်ဆုတ်မည့်နေရာကို အဖွဲ့လိုက်ဆုံးဖြတ်ပါ။"},
            {"title": "Entry", "body": "ပထမဆုံးမြင်ကွင်းဖွင့်သူဖြစ်ပြီး အဖွဲ့နှင့် အကွာအဝေးမကျော်ပါနှင့်။"},
            {"title": "Support", "body": "Smoke၊ heal နှင့် ammo ကို စောင့်ရှောက်ပြီး knock ပြန်ယူရာတွင် ကူညီပါ။"},
            {"title": "Scout", "body": "ကား၊ တောင်ကြောနှင့် ရန်သူတည်နေရာကို ကြိုတင်သတင်းပို့ပါ။"},
        ],
    },
    {
        "id": "circle-plan",
        "title": "ဝိုင်းပိတ်စီမံချက်",
        "title_en": "Circle plan",
        "cards": [
            {"title": "Early", "body": "Loot ကို မလိုအပ်ဘဲ မရှည်ကြာစေဘဲ ပထမဝိုင်းမပိတ်မီ ကားနှင့် compound ရွေးပါ။"},
            {"title": "Mid", "body": "အလယ်ဝိုင်းကို လိုက်ပြီး high ground နှင့် cover နှစ်ခုလုံး ရှိသောနေရာကို ဦးစားပေးပါ။"},
            {"title": "Late", "body": "Smoke အရေအတွက်၊ လူအရေအတွက်နှင့် နောက်ဆုံး cover ကို အဖွဲ့လိုက်ရေတွက်ပြီးမှ ဝင်ပါ။"},
        ],
    },
    {
        "id": "myanmar-latency",
        "title": "Myanmar connection checklist",
        "title_en": "Myanmar connection checklist",
        "cards": [
            {"title": "Before queue", "body": "Wi‑Fi နှင့် mobile data latency ကို တစ်ကြိမ်စမ်းပြီး packet loss ရှိလျှင် ranked မဝင်ပါနှင့်။"},
            {"title": "During match", "body": "Ping spike ဖြစ်လျှင် push မလုပ်ဘဲ cover ပြန်ယူပြီး voice call ကို လျှော့ပါ။"},
            {"title": "Fair play", "body": "Account login၊ cheat၊ third-party injector သို့မဟုတ် မသေချာသော player API မသုံးပါနှင့်။"},
        ],
    },
)

_FREE_FIRE_MYANMAR_SECTIONS = (
    {
        "id": "team-play",
        "title": "အဖွဲ့လိုက်ကစားနည်း",
        "title_en": "Squad play",
        "cards": [
            {"title": "Shot-caller", "body": "ဆင်းမည့်နေရာ၊ လှည့်မည့်လမ်းကြောင်းနှင့် တိုက်မည်/ရှောင်မည်ကို တစ်ယောက်တည်းက ရှင်းရှင်းလင်းလင်း ခေါ်ပါ။ အဖွဲ့သဘောတူချက်ကို စောင့်ပါ။"},
            {"title": "Scout", "body": "ရှေ့လမ်း၊ အဖုံးအကွယ်နှင့် ရန်သူအရေအတွက်ကို တိုတိုတုတ်တုတ် သတင်းပို့ပါ။ သတင်းပို့ရန် အဖွဲ့နှင့်ဝေးမသွားပါနှင့်။"},
            {"title": "Entry", "body": "ပစ်ခတ်မှုစမည့်အချိန်ကို အသင်းဖော်များ ပြင်ဆင်ပြီးမှ စတင်ပါ။ ပထမဆုံး knock မရလျှင် တစ်ယောက်တည်း ဆက်မလိုက်ပါနှင့်။"},
            {"title": "Support", "body": "ကုသရေး၊ ကျည်နှင့် လုံခြုံသောပြန်စုရာနေရာကို အသင်းဖော်များနှင့် မျှဝေပါ။ ပြန်ကူညီရန် အဖုံးအကွယ်မရှိဘဲ မထွက်ပါနှင့်။"},
        ],
    },
    {
        "id": "drop-and-rotate",
        "title": "ဆင်းသက်ခြင်းနှင့် လှည့်ရွှေ့ခြင်း",
        "title_en": "Drop & rotate",
        "cards": [
            {"title": "အဖွဲ့လိုက်ဆင်းပါ", "body": "တစ်ယောက်ချင်း ခွဲဆင်းမည့်အစား အချင်းချင်းကူညီနိုင်သည့် အကွာအဝေးကို ရွေးပြီး ပထမမိနစ်တွင် လိုအပ်သောပစ္စည်းကိုသာ စုပါ။"},
            {"title": "အချိန်မီလှည့်ပါ", "body": "လုံခြုံဇုန်နှင့် အကွာအဝေးကို စောစောစစ်ပါ။ နောက်ဆုံးအချိန်မှ ပြေးဝင်ရသည့် အန္တရာယ်ကို လျှော့ရန် အဖုံးအကွယ်ရှိသော လမ်းကြောင်းကို အဖွဲ့လိုက်ရွေးပါ။"},
            {"title": "တိုက်ပွဲရွေးပါ", "body": "လူအင်အား၊ ကျည်နှင့် ကုသရေးမပြည့်သေးပါက ရှောင်လွှဲခြင်းသည်လည်း ရွေးချယ်မှုကောင်းတစ်ခုဖြစ်သည်။ အကြောင်းမဲ့လိုက်လံတိုက်ခိုက်ခြင်းကို ရှောင်ပါ။"},
        ],
    },
    {
        "id": "fight-and-endgame",
        "title": "တိုက်ပွဲနှင့် နောက်ဆုံးအဆင့်",
        "title_en": "Fight & endgame",
        "cards": [
            {"title": "တစ်ယောက်တည်း မဝင်ပါနှင့်", "body": "ပစ်မှတ်နှင့် တိုက်မည့်ဘက်ကို ကြိုတင်ခေါ်ပါ။ တစ်ယောက်ထဲ ဝင်တိုက်ခြင်းထက် အဖွဲ့၏ပစ်အားကို တစ်နေရာတည်း စုစည်းပါ။"},
            {"title": "Gloo Wall ကို အဖုံးအကွယ်အဖြစ်သုံးပါ", "body": "ပြန်စုခြင်း၊ အနေအထားပြောင်းခြင်း သို့မဟုတ် လုံခြုံသည့်အခါမှ ပြန်ကူညီခြင်းအတွက် အဖုံးဖန်တီးပါ။ နေရာနှင့် လက်ကျန်ကို သတိထားပြီး အသုံးပြုပါ။"},
            {"title": "နောက်ဆုံးဝိုင်း", "body": "ကျန်အသင်းဖော်၊ လုံခြုံဇုန်နှင့် အနီးဆုံးအဖုံးကို ပြန်ရေတွက်ပါ။ အဖွဲ့ကွဲမသွားစေဘဲ လှည့်ဝင်ပြီး မလိုအပ်သောပြေးလွှားမှုကို ရှောင်ပါ။"},
        ],
    },
    {
        "id": "myanmar-play-checklist",
        "title": "မြန်မာကစားသမား စစ်ဆေးရန်",
        "title_en": "Myanmar player checklist",
        "cards": [
            {"title": "တည်ငြိမ်သောလိုင်းကိုရွေးပါ", "body": "ပွဲမစမီ ကိုယ်သုံးမည့် Wi‑Fi သို့မဟုတ် မိုဘိုင်းဒေတာ၏ ping တည်ငြိမ်မှုကို စစ်ပါ။ အမြန်နှုန်းမြင့်တစ်ခုတည်းထက် မကြာခဏမပြတ်သောလိုင်းကို ဦးစားပေးပါ။"},
            {"title": "နောက်ခံဒေါင်းလုဒ်များ ပိတ်ပါ", "body": "ပွဲမတိုင်မီ အလိုအလျောက်ဒေါင်းလုဒ်နှင့် မလိုအပ်သောနောက်ခံအက်ပ်များကို ပိတ်ပြီး ဘက်ထရီနှင့် ဖုန်းအပူချိန်ကို စစ်ဆေးပါ။"},
            {"title": "အကောင့်နှင့် fair play", "body": "တရားဝင်ဂိမ်းဗားရှင်းကိုသာ သုံးပါ။ Password၊ OTP သို့မဟုတ် login ကို မမျှဝေပါနှင့်။ Cheat၊ ပြင်ဆင်ထားသော APK နှင့် အခမဲ့ diamond ပေးမည်ဆိုသည့် မသိသော link များကို ရှောင်ပါ။"},
        ],
    },
    {
        "id": "review-loop",
        "title": "တိုးတက်ရန် အတိုချုံး",
        "title_en": "One-match review",
        "cards": [
            {"title": "အမှားတစ်ခုကို ရွေးပါ", "body": "ပွဲပြီးလျှင် ကိုယ်ပထမဆုံးကွဲထွက်သည့်အချိန်၊ အချိန်မီမလှည့်နိုင်သည့်အကြောင်း သို့မဟုတ် အဖွဲ့နှင့်မဆက်သွယ်နိုင်သည့်အကြောင်းထဲမှ တစ်ခုကိုသာ ပြန်စဉ်းစားပါ။"},
            {"title": "နောက်ပွဲအတွက် တစ်ချက်ပြင်ပါ", "body": "နောက်ပွဲတွင် စမ်းမည့် အဖွဲ့ဆက်သွယ်ရေး သို့မဟုတ် လှည့်ရွှေ့မှုတစ်ချက်ကို သတ်မှတ်ပါ။ ဤအကြံပြုချက်သည် ကိုယ်ပိုင် match analysis မဟုတ်ပါ။"},
        ],
    },
    {
        "id": "patch-watch",
        "title": "OB55 patch ပြင်ဆင်မှု",
        "title_en": "OB55 patch watch",
        "cards": [
            {"title": "Garena ၏ စီစဉ်ထားသောရက်စွဲ", "body": "Garena ၏ OB55 မှတ်စုတွင် M7၊ Skorp၊ RPK နှင့် Hawk လက်နက်များ၊ weapon balance ပြောင်းလဲမှုအချို့ကို ၂၀၂၆-၁၀-၀၁ တွင် ရောက်ရှိရန် စီစဉ်ထားသည်ဟု ဖော်ပြထားသည်။ ဤလမ်းညွှန်က update သည် သင့် game client တွင် အမှန်တကယ်ရောက်ရှိပြီး/မရောက်ရှိသေးကို အတည်မပြုပါ။"},
            {"title": "Client တွင် အတည်ပြုပြီးမှသာ စမ်းပါ", "body": "Game version နှင့် patch အပြောင်းအလဲကို ကိုယ့် client တွင် စစ်ဆေးပါ။ Update ရောက်ရှိကြောင်း အတည်ပြုပြီးမှ Training Ground သို့မဟုတ် ပုံမှန်ပွဲတွင် recoil နှင့် အသုံးပြုမှုကို အရင်စမ်းပါ။ ဤလမ်းညွှန်တွင် player statistics သို့မဟုတ် ရာသီအလိုက်အဆင့်သတ်မှတ်ချက်များ မပါဝင်ပါ။"},
        ],
    },
)


def _validate_slug(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in value):
        raise StrategyGuideInputError("guide slug is invalid")
    return value


def _rankings_freshness(
    measured_at: Any,
    *,
    captured_at: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Classify the source-reported ranking date without implying a live feed.

    MLBBDex describes its data as following a weekly sync.  This is a
    conservative display hint for an operator snapshot, not a provider health
    check: the successful local snapshot capture and source measurement date
    remain separate fields.
    """

    result: dict[str, Any] = {
        "status": "unknown",
        "last_measured_at": measured_at if isinstance(measured_at, str) else None,
        "source_reported_measured_date": None,
        "last_successful_snapshot_capture_at": captured_at if isinstance(captured_at, str) else None,
        "age_days": None,
        "freshness_window_days": MLBBDex_RANKINGS_SYNC_WINDOW_DAYS,
        "time_basis": "UTC calendar date",
        "notice": "Snapshot age only; this is not live game data or a provider sync-health check.",
    }
    if not isinstance(measured_at, str) or not measured_at.strip():
        return result

    try:
        normalized = measured_at.strip().replace("Z", "+00:00")
        if "T" not in normalized and " " not in normalized:
            measured_date = date.fromisoformat(normalized)
        else:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return result
            measured_date = parsed.astimezone(timezone.utc).date()
    except (TypeError, ValueError):
        return result

    current_date = today or datetime.now(timezone.utc).date()
    if measured_date > current_date:
        result["status"] = "future_dated"
        return result

    age_days = (current_date - measured_date).days
    result["source_reported_measured_date"] = measured_date.isoformat()
    result["age_days"] = age_days
    result["status"] = (
        "fresh" if age_days < MLBBDex_RANKINGS_SYNC_WINDOW_DAYS else "stale"
    )
    return result


def _mlbb_guide(
    snapshot: Mapping[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    heroes = deepcopy(list(_MLBB_HEROES))
    items = deepcopy(list(_MLBB_ITEMS))
    freshness = "reference"
    snapshot_meta: dict[str, Any] = {"loaded": False}
    ranking_freshness = _rankings_freshness(None, today=today)
    if snapshot is not None:
        if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != GUIDE_SCHEMA_VERSION:
            raise StrategyGuideInputError("MLBB guide snapshot schema is invalid")
        candidate_heroes = snapshot.get("heroes")
        candidate_items = snapshot.get("items")
        if isinstance(candidate_heroes, list) and candidate_heroes:
            # Ranking fields from the captured community feed are not part of
            # the public guide contract: commercial terms are unverified and
            # the current ranking snapshot is stale. Keep only the directory
            # facts used by role filters and deterministic lane matching.
            heroes = [
                {key: deepcopy(hero[key]) for key in ("slug", "name", "roles", "lanes") if key in hero}
                for hero in candidate_heroes
                if isinstance(hero, Mapping)
            ]
        if isinstance(candidate_items, list) and candidate_items:
            items = deepcopy(candidate_items)
        freshness = "operator_snapshot"
        snapshot_meta = {
            "loaded": True,
            "captured_at": snapshot.get("captured_at"),
            "patch": snapshot.get("patch"),
            "rankings_measured_at": snapshot.get("rankings_measured_at"),
        }
        ranking_freshness = _rankings_freshness(
            snapshot.get("rankings_measured_at"),
            captured_at=snapshot.get("captured_at"),
            today=today,
        )
    return {
        "schema_version": GUIDE_SCHEMA_VERSION,
        "slug": "mlbb",
        "game": "Mobile Legends: Bang Bang",
        "locale": "en-MM",
        "status": "reference_only" if freshness == "reference" else "snapshot",
        "freshness": freshness,
        "player_lookup": False,
        "sections": [
            {"id": "hero-tiers", "title": "Hero Reference", "filters": {"roles": ["Tank", "Fighter", "Assassin", "Mage", "Marksman", "Support"], "lanes": ["Gold Lane", "Exp Lane", "Mid Lane", "Roam", "Jungle"]}, "heroes": heroes},
            {"id": "items", "title": "Item List", "categories": ["All", "Attack", "Magic", "Defense", "Movement", "Jungling", "Roaming"], "items": items},
            {"id": "draft", "title": "Draft Strategy", "features": ["ally_team", "enemy_team", "synergy_score", "counter_score", "draft_threat"], "status": "reference_shell"},
            {"id": "counters", "title": "Counter Picks", "features": ["enemy_team", "suggested_counters"], "status": "reference_shell"},
            {"id": "emblems", "title": "Emblems & Talents", "status": "reference_shell"},
        ],
        "source": deepcopy(MLBBDex_SOURCE),
        "snapshot": snapshot_meta,
        "rankings_freshness": ranking_freshness,
        "rankings_display_status": "withheld_unverified_terms_and_freshness",
        "notice": "No player login, account lookup, or live purchase action is available in this guide.",
    }


def _pubg_myanmar_guide() -> dict[str, Any]:
    return {
        "schema_version": GUIDE_SCHEMA_VERSION,
        "slug": "pubg-mobile-myanmar",
        "game": "PUBG Mobile",
        "locale": "my-MM",
        "status": "offline_reference",
        "freshness": "curated",
        "player_lookup": False,
        "sections": deepcopy(list(_MYANMAR_BATTLE_ROYALE_SECTIONS)),
        "source": {
            "name": "AuriX Myanmar guide reference",
            "url": "https://developer.pubg.com/faq",
            "official_api_mobile_data": False,
            "notice": "The official PUBG Developer API documents that PUBG Mobile data is not available.",
        },
        "notice": "Myanmar-localized strategy reference; no PUBG Mobile player API, match history, or account lookup is used.",
    }


def _free_fire_patch_status(effective_on: str, *, today: date | None = None) -> str:
    try:
        effective_date = date.fromisoformat(effective_on)
    except (TypeError, ValueError):
        return "unknown"
    current_date = today or datetime.now(_MYANMAR_TIMEZONE).date()
    return "scheduled_unverified" if current_date < effective_date else "verification_required"


def _free_fire_myanmar_guide(*, today: date | None = None) -> dict[str, Any]:
    patch_effective_on = "2026-10-01"
    patch_status = _free_fire_patch_status(patch_effective_on, today=today)
    return {
        "schema_version": GUIDE_SCHEMA_VERSION,
        "slug": "free-fire-myanmar",
        "game": "Garena Free Fire",
        "locale": "my-MM",
        "status": "offline_reference",
        "freshness": "official_patch_snapshot",
        "reviewed_on": "2026-09-24",
        "season_specific": True,
        "patch_version": "OB55",
        "patch_changes_effective_on": patch_effective_on,
        "patch_status": patch_status,
        "patch_verification_required": patch_status != "scheduled_unverified",
        "player_lookup": False,
        "sections": deepcopy(list(_FREE_FIRE_MYANMAR_SECTIONS)),
        "source": {
            "name": "AuriX-authored practice guide with official Garena patch-note watch",
            "url": "https://ff.garena.com/en/article/1712/",
            "patch_notes_url": "https://ff.garena.com/en/article/1712/",
            "patch_notes_published_on": "2026-09-10",
            "patch_changes_effective_on": patch_effective_on,
            "official_api_mobile_data": False,
            "craftland_api_url": "https://ffcraftland.garena.com/en/docs/api",
            "craftland_api_scope": "Craftland creation/editor scripting; not a general player-stat, live-meta, or account-lookup API.",
            "notice": "Core gameplay tips are AuriX-authored practice guidance; the OB55 patch watch is paraphrased from Garena's official notes. The listed date is a schedule only; current live-client availability is not verified. After the date, confirm the patch in the game client before relying on the changes. No Free Fire player API, account lookup, or player ranking data is used.",
        },
        "notice": "မြန်မာကစားသမားများအတွက် Free Fire အဖွဲ့လိုက်ကစားနည်း ကိုးကားချက်။ OB55 patch အချက်အလက်ကို Garena ၏ တရားဝင်မှတ်စုမှ စုစည်းထားပြီး ပွဲဒေတာ၊ အကောင့်နှင့် player statistics များကို မရယူပါ။",
    }


def strategy_guides(*, mlbb_snapshot_path: str | Path | None = None) -> list[dict[str, Any]]:
    snapshot = _read_snapshot(mlbb_snapshot_path) if mlbb_snapshot_path else None
    return [_mlbb_guide(snapshot), _pubg_myanmar_guide(), _free_fire_myanmar_guide()]


def strategy_guide(slug: str, *, mlbb_snapshot_path: str | Path | None = None) -> dict[str, Any] | None:
    normalized = _validate_slug(slug)
    for guide in strategy_guides(mlbb_snapshot_path=mlbb_snapshot_path):
        if guide["slug"] == normalized:
            return guide
    return None


def mlbbdex_url(resource: str, *, base_url: str = MLBBDex_BASE_URL) -> str:
    """Build an allow-listed MLBBDex GET URL for an operator refresh."""

    if resource not in {"heroes", "items", "patches", "rankings"}:
        raise StrategyGuideInputError("unsupported MLBBDex resource")
    return f"{base_url.rstrip('/')}/{resource}"


def fetch_mlbbdex(resource: str, *, timeout: float = 10.0, base_url: str = MLBBDex_BASE_URL) -> dict[str, Any]:
    """Fetch one public MLBBDex response; never called by the storefront."""

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 30:
        raise StrategyGuideInputError("timeout must be between 1 and 30 seconds")
    request = Request(
        mlbbdex_url(resource, base_url=base_url),
        headers={"Accept": "application/json", "User-Agent": "AuriX-Games-guide-refresh/1"},
        method="GET",
    )
    with urlopen(request, timeout=float(timeout)) as response:  # nosec B310 - fixed HTTPS default, operator-only use
        if response.status != 200:
            raise StrategyGuideInputError(f"MLBBDex returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise StrategyGuideInputError("MLBBDex response shape is invalid")
    data = payload.get("data")
    if resource == "rankings":
        valid_data = isinstance(data, dict) and isinstance(data.get("heroes"), list)
    else:
        valid_data = isinstance(data, list)
    if not valid_data:
        raise StrategyGuideInputError("MLBBDex response shape is invalid")
    return payload


def write_mlbb_snapshot(
    path: str | Path,
    *,
    heroes: list[Any],
    items: list[Any],
    patch: str | None = None,
    captured_at: str | None = None,
    rankings_measured_at: str | None = None,
) -> None:
    """Persist a small operator-approved snapshot without artwork or player data."""

    if not isinstance(heroes, list) or not isinstance(items, list):
        raise StrategyGuideInputError("snapshot collections must be lists")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if target.is_symlink():
        raise StrategyGuideInputError("snapshot destination cannot be a symlink")
    payload = json.dumps(
        {
            "schema_version": GUIDE_SCHEMA_VERSION,
            "captured_at": captured_at,
            "patch": patch,
            "rankings_measured_at": rankings_measured_at,
            "heroes": heroes,
            "items": items,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    destination_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), destination_mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def _read_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyGuideInputError("MLBB guide snapshot could not be read") from exc
    if not isinstance(payload, dict):
        raise StrategyGuideInputError("MLBB guide snapshot must be an object")
    return payload


__all__ = [
    "GUIDE_SCHEMA_VERSION",
    "DEFAULT_MLBB_SNAPSHOT_PATH",
    "MLBBDex_BASE_URL",
    "MLBBDex_SOURCE",
    "StrategyGuideInputError",
    "fetch_mlbbdex",
    "mlbbdex_url",
    "strategy_guide",
    "strategy_guides",
    "write_mlbb_snapshot",
]
