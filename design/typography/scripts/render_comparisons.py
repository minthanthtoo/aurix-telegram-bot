#!/usr/bin/env python3
"""Build reproducible Burmese/Latin type comparison boards for AuriX."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
PROOFS = ROOT / "proofs"
FONT_DIR = ROOT / "fonts"
FONTCONFIG = ROOT / ".fontconfig.local.xml"
WIDTH = 1080
BG = "#F6F8FC"
INK = "#071421"
DEEP = "#0D2235"
CYAN = "#36E2FF"
VIOLET = "#7765FF"
GOLD = "#FFC857"
SLATE = "#526273"
WHITE = "#FFFFFF"
MUTED = "#D9E3EE"


def esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def text(x: int, y: int, value: str, *, size: int, family: str,
         fill: str = INK, weight: int = 400, lang: str = "en",
         anchor: str = "start", extra: str = "",
         features: tuple[tuple[str, int], ...] = ()) -> str:
    lang_attr = f' xml:lang="{lang}"' if lang != "en" else ""
    feature_values = (("kern", 1), ("mark", 1), ("mkmk", 1), *features)
    feature_string = ", ".join(f'&quot;{name}&quot; {value}' for name, value in feature_values)
    return (
        f'<text x="{x}" y="{y}" font-family="{esc(family)}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}" font-kerning="normal" '
        f'style="font-feature-settings: {feature_string};"'
        f'{lang_attr}{extra}>{esc(value)}</text>'
    )


def rect(x: int, y: int, w: int, h: int, fill: str, radius: int = 24,
         stroke: str = "none", stroke_width: int = 0, extra: str = "") -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}" {extra}/>'
    )


def shell(title: str, subtitle: str, height: int, content: str) -> str:
    has_myanmar_title = any(0x1000 <= ord(char) <= 0x109F for char in title)
    title_family = "Noto Sans Myanmar" if has_myanmar_title else "Inter"
    title_lang = "my" if has_myanmar_title else "en"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
      xmlns:xml="http://www.w3.org/XML/1998/namespace" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">
      <defs>
        <linearGradient id="accent" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{CYAN}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>
        <filter id="soft-shadow" x="-20%" y="-25%" width="140%" height="160%"><feGaussianBlur in="SourceAlpha" stdDeviation="7"/><feOffset dy="7"/><feComponentTransfer><feFuncA type="linear" slope=".17"/></feComponentTransfer><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      </defs>
      <rect width="{WIDTH}" height="{height}" fill="{BG}"/>
      {rect(0, 0, WIDTH, 20, DEEP, 0)}
      {rect(64, 56, 12, 94, "url(#accent)", 6)}
      {text(104, 104, title, size=42, family=title_family, fill=INK, weight=750, lang=title_lang)}
      {text(104, 148, subtitle, size=21, family="Inter", fill=SLATE, weight=450)}
      {content}
      {text(64, height - 38, "AuriX  /  TYPE STUDY  ·  NOT A FINISHED AD", size=16, family="Inter", fill=SLATE, weight=550)}
      {text(WIDTH - 64, height - 38, "1080 px master", size=16, family="Inter", fill=SLATE, weight=450, anchor="end")}
    </svg>'''


def board_myanmar() -> str:
    title = "မြန်မာစာဖောင့် — ပုံစံနှိုင်းယှဉ်ချက်"
    subtitle = "Same copy, same scale. Compare voice, weight, and mark clearance."
    families = [
        ("Noto Sans Myanmar", "Noto Sans Myanmar", 650, "Brand workhorse · neutral, sturdy, broad weight range"),
        ("Noto Sans Myanmar UI", "Noto Sans Myanmar UI", 650, "Compact interface voice · efficient, less expressive at display size"),
        ("Noto Serif Myanmar", "Noto Serif Myanmar", 700, "Editorial/display contrast · distinctive for a short hook"),
        ("Padauk 6.000", "Padauk", 700, "Open-source alternative · warm, readable; compare local shaping carefully"),
        ("Myanmar MN · system-only", "Myanmar MN", 500, "Compatibility specimen only · platform-dependent, do not build a portable identity on it"),
    ]
    parts = []
    y = 184
    for i, (label, family, weight, note) in enumerate(families):
        parts.append(rect(64, y, 952, 282, WHITE, 20, stroke=MUTED, stroke_width=2))
        parts.append(rect(64, y, 10, 282, [CYAN, VIOLET, GOLD, "#32B981", SLATE][i], 5))
        parts.append(text(100, y + 46, label, size=23, family="Inter", fill=DEEP, weight=700))
        parts.append(text(100, y + 91, note, size=18, family="Inter", fill=SLATE, weight=450))
        parts.append(text(100, y + 167, "Outline VPN Key ရဲ့ Quota လက်ကျန်", size=49, family=family, fill=INK, weight=weight, lang="my"))
        parts.append(text(100, y + 232, "ကို အချိန်မရွေး ကိုယ်တိုင် စစ်ကြည့်နိုင်ပါတယ်", size=35, family=family, fill=DEEP, weight=max(400, weight - 200), lang="my"))
        y += 298
    return shell(title, subtitle, y + 60, "".join(parts))


def board_pairings() -> str:
    title = "Bilingual pairings — compare the brand voice"
    subtitle = "Myanmar leads the reading; Latin adds a controlled brand accent."
    samples = [
        ("Inter + Noto Sans Myanmar", "Inter", "Noto Sans Myanmar", 750, 650, "RECOMMENDED DEFAULT · consistent, contemporary, clear"),
        ("Avenir Next + Noto Sans Myanmar", "Avenir Next", "Noto Sans Myanmar", 700, 650, "WARMER SERVICE FEEL · use only if platform licensing/rendering is settled"),
        ("Oswald + Noto Sans Myanmar", "Oswald", "Noto Sans Myanmar", 650, 650, "CAMPAIGN DISPLAY · short Latin numerals/labels only; never squeeze Burmese"),
        ("Fraunces + Noto Serif Myanmar", "Fraunces", "Noto Serif Myanmar", 700, 700, "EDITORIAL / HUMAN STORY · a short one-off campaign, not default UI"),
        ("JetBrains Mono + Noto Sans Myanmar", "JetBrains Mono", "Noto Sans Myanmar", 650, 650, "TECHNICAL MICRO-DETAIL · IDs/specs only, not the headline"),
    ]
    parts = []
    y = 184
    for i, (label, latin, mm, lw, mw, note) in enumerate(samples):
        parts.append(rect(64, y, 952, 282, WHITE, 20, stroke=MUTED, stroke_width=2))
        parts.append(rect(64, y, 10, 282, [CYAN, VIOLET, GOLD, "#32B981", SLATE][i], 5))
        parts.append(text(100, y + 43, label, size=22, family="Inter", fill=DEEP, weight=700))
        parts.append(text(100, y + 77, note, size=17, family="Inter", fill=SLATE, weight=450))
        parts.append(text(100, y + 148, "Clear access, on your terms.", size=43, family=latin, fill=INK, weight=lw))
        parts.append(text(100, y + 207, "အချိန်မရွေး ကိုယ်တိုင် စစ်ကြည့်နိုင်ပါတယ်", size=32, family=mm, fill=DEEP, weight=mw, lang="my"))
        parts.append(text(100, y + 257, "300 MB daily    ·    3 GB monthly", size=24, family=latin, fill=SLATE, weight=600))
        y += 298
    return shell(title, subtitle, y + 60, "".join(parts))


def board_treatments() -> str:
    title = "Type treatments — one clear move"
    subtitle = "Same copy and scale. Let one controlled treatment clarify meaning."
    x_positions = [64, 548]
    y_positions = [184, 566, 948]
    cards = [
        ("01  WEIGHT + SCALE", "Quiet authority", "အချိန်မရွေး ကိုယ်တိုင်", "စစ်ကြည့်နိုင်ပါတယ်", "plain"),
        ("02  WORD-LEVEL COLOR", "One meaning gets focus", "Quota လက်ကျန်ကို", "အချိန်မရွေး စစ်ကြည့်ပါ", "highlight"),
        ("03  SERIF / SANS ROLES", "Editorial hook, useful detail", "Quota လက်ကျန်", "ကိုယ်တိုင် အချိန်မရွေး စစ်နိုင်ပါတယ်", "contrast"),
        ("04  SEMANTIC UNDERLINE", "Rule sits below the complete phrase", "အချိန်မရွေး", "ကိုယ်တိုင် ဝင်စစ်နိုင်ပါတယ်", "underline"),
        ("05  GROUP SHADOW", "Only to separate type from photography", "အချိန်မရွေး ကိုယ်တိုင်", "စစ်ကြည့်နိုင်ပါတယ်", "shadow"),
        ("06  OUTLINE / GLOW", "Reject for Burmese text", "Quota လက်ကျန်", "အချိန်မရွေး စစ်ကြည့်ပါ", "reject"),
    ]
    parts = []
    for idx, (label, sub, line1, line2, treatment) in enumerate(cards):
        x = x_positions[idx % 2]
        y = y_positions[idx // 2]
        parts.append(rect(x, y, 468, 350, WHITE, 22, stroke=MUTED, stroke_width=2))
        parts.append(text(x + 30, y + 44, label, size=17, family="Inter", fill=SLATE, weight=700))
        parts.append(text(x + 30, y + 82, sub, size=20, family="Inter", fill=DEEP, weight=650))
        fill = INK
        extra = ""
        if treatment == "shadow":
            extra = ' filter="url(#soft-shadow)"'
        if treatment == "reject":
            fill = "#FFFFFF"
            extra = ' stroke="#071421" stroke-width="1.4" paint-order="stroke"'
        if treatment == "highlight":
            parts.append(rect(x + 27, y + 132, 414, 58, "#FFF3CD", 12))
        font1 = "Noto Serif Myanmar" if treatment == "contrast" else "Noto Sans Myanmar"
        weight1 = 700 if treatment in {"plain", "contrast", "reject"} else 650
        parts.append(text(x + 36, y + 180, line1, size=38, family=font1, fill=VIOLET if treatment == "highlight" else fill, weight=weight1, lang="my", extra=extra))
        parts.append(text(x + 36, y + 239, line2, size=30, family="Noto Sans Myanmar", fill=DEEP, weight=550, lang="my", extra=extra if treatment == "shadow" else ""))
        if treatment == "underline":
            parts.append(f'<path d="M{x + 36} {y + 256} C{x + 98} {y + 264}, {x + 175} {y + 263}, {x + 292} {y + 255}" fill="none" stroke="{GOLD}" stroke-width="7" stroke-linecap="round"/>')
        if treatment == "reject":
            parts.append(text(x + 36, y + 303, "No contrast rescue · marks get noisy", size=16, family="Inter", fill="#B42318", weight=650))
        else:
            parts.append(text(x + 36, y + 303, "Accent is attached to meaning, not decoration", size=15, family="Inter", fill=SLATE, weight=500))
    return shell(title, subtitle, 1390, "".join(parts))


def board_motion() -> str:
    title = "Motion type — preserve shaped reading units"
    subtitle = "7-second feature-ad storyboard. Reveal complete Burmese words/phrases, never individual marks."
    panels = [
        ("00.0–01.8 s  /  HOOK", "Quota လက်ကျန်", "ကိုယ်တိုင် စစ်နိုင်ပြီ", CYAN),
        ("01.8–03.4 s  /  BENEFIT", "အချိန်မရွေး", "ဝင်စစ်ကြည့်ပါ", GOLD),
        ("03.4–05.4 s  /  PROOF", "Outline VPN Key", "တစ်ခုချင်းစီရဲ့ အသုံးပြုမှုကို စစ်နိုင်", VIOLET),
        ("05.4–07.0 s  /  BRAND + NEXT STEP", "AuriX Telegram Bot", "သုံးကြည့်ရန် · @aurix_outline_vpn_bot", CYAN),
    ]
    parts = []
    for idx, (tag, headline, support, accent) in enumerate(panels):
        y = 184 + idx * 266
        parts.append(rect(64, y, 952, 232, DEEP if idx == 0 else WHITE, 22, stroke=DEEP if idx == 0 else MUTED, stroke_width=2))
        color = WHITE if idx == 0 else INK
        subcolor = "#C9D5E4" if idx == 0 else SLATE
        parts.append(rect(94, y + 34, 8, 54, accent, 4))
        parts.append(text(124, y + 59, tag, size=19, family="Inter", fill=subcolor, weight=700))
        parts.append(text(124, y + 128, headline, size=44, family="Noto Sans Myanmar", fill=color, weight=700, lang="my"))
        support_fill = CYAN if idx == 0 else DEEP
        parts.append(text(124, y + 202, support, size=26, family="Noto Sans Myanmar", fill=support_fill, weight=550, lang="my"))
        parts.append(text(975, y + 209, ["SETTLE", "WORD GROUP", "HOLD", "BRAND LOCK"][idx], size=13, family="Inter", fill=subcolor, weight=650, anchor="end"))
    return shell(title, subtitle, 1320, "".join(parts))


def board_mobile_stress_test() -> str:
    title = "Feed-scale composite — type hierarchy"
    subtitle = "A compact study, not a finished ad. Check the line rhythm at full and reduced size."
    content = f'''
      <defs>
        <radialGradient id="glow"><stop stop-color="{VIOLET}" stop-opacity=".25"/><stop offset="1" stop-color="{DEEP}" stop-opacity="0"/></radialGradient>
      </defs>
      <rect x="0" y="178" width="1080" height="1110" fill="{DEEP}"/>
      <circle cx="910" cy="290" r="410" fill="url(#glow)"/>
      <path d="M80 222 H154" stroke="{CYAN}" stroke-width="8" stroke-linecap="round"/>
      {text(80, 405, "Quota လက်ကျန်", size=82, family="Noto Serif Myanmar", fill=WHITE, weight=700, lang="my")}
      {text(80, 534, "ကိုယ်တိုင် အချိန်မရွေး စစ်နိုင်ပြီ", size=49, family="Noto Sans Myanmar", fill=WHITE, weight=650, lang="my")}
      <path d="M82 578 C196 586 314 584 461 575" fill="none" stroke="{GOLD}" stroke-width="7" stroke-linecap="round"/>
      {text(80, 671, "Outline VPN Key တစ်ခုချင်းစီရဲ့", size=47, family="Noto Sans Myanmar", fill="#E1E8F0", weight=500, lang="my")}
      {text(80, 744, "Quota ကို AuriX Bot မှာ စစ်နိုင်ပါတယ်", size=47, family="Noto Sans Myanmar", fill="#E1E8F0", weight=500, lang="my")}
      {text(80, 853, "50%", size=74, family="Inter", fill=CYAN, weight=800)}
      <path d="M371 807 V870" stroke="#416078" stroke-width="2"/>
      {text(430, 853, "10%", size=74, family="Inter", fill=GOLD, weight=800)}
      <path d="M714 807 V870" stroke="#416078" stroke-width="2"/>
      {text(782, 853, "0%", size=74, family="Inter", fill="#FF8A80", weight=800)}
      {text(80, 929, "Quota သတ်မှတ်အဆင့်တိုင်းမှာ သတိပေးမယ်", size=42, family="Noto Sans Myanmar", fill=WHITE, weight=550, lang="my")}
      {text(80, 1122, "@aurix_outline_vpn_bot", size=40, family="Inter", fill=CYAN, weight=700)}
    '''
    return shell(title, subtitle, 1350, content)


def board_numerals() -> str:
    title = "Numerals — scan, align, keep units attached"
    subtitle = "Sample figures only—not current offers. Compare numeral shape, column alignment, and script choice."
    cards = [
        (64, 184, "01  PROPORTIONAL FIGURES", "Natural line flow", "proportional"),
        (548, 184, "02  TABULAR FIGURES", "Aligned value column", "tabular"),
        (64, 652, "03  CONDENSED DISPLAY", "Short Latin quota labels only", "condensed"),
        (548, 652, "04  MYANMAR / LATIN DIGITS", "Choose one convention per post", "digits"),
    ]
    parts = []
    for x, y, label, note, mode in cards:
        parts.append(rect(x, y, 468, 430, WHITE, 22, stroke=MUTED, stroke_width=2))
        parts.append(text(x + 30, y + 47, label, size=17, family="Inter", fill=SLATE, weight=700))
        parts.append(text(x + 30, y + 83, note, size=18, family="Inter", fill=DEEP, weight=500))
        if mode in {"proportional", "tabular"}:
            features = (("tnum", 1), ("lnum", 1)) if mode == "tabular" else ()
            parts.append(text(x + 30, y + 156, "Plan A", size=21, family="Inter", fill=SLATE, weight=550))
            parts.append(text(x + 343, y + 156, "3,000", size=34, family="Inter", fill=INK, weight=700, anchor="end", features=features))
            parts.append(text(x + 362, y + 156, "MMK", size=20, family="Inter", fill=SLATE, weight=600))
            parts.append(text(x + 30, y + 232, "Plan B", size=21, family="Inter", fill=SLATE, weight=550))
            parts.append(text(x + 343, y + 232, "6,000", size=34, family="Inter", fill=INK, weight=700, anchor="end", features=features))
            parts.append(text(x + 362, y + 232, "MMK", size=20, family="Inter", fill=SLATE, weight=600))
            parts.append(text(x + 30, y + 308, "Plan C", size=21, family="Inter", fill=SLATE, weight=550))
            parts.append(text(x + 343, y + 308, "10,000", size=34, family="Inter", fill=INK, weight=700, anchor="end", features=features))
            parts.append(text(x + 362, y + 308, "MMK", size=20, family="Inter", fill=SLATE, weight=600))
            parts.append(text(x + 30, y + 380, "Use `tnum` where a real column should align.", size=15, family="Inter", fill=SLATE, weight=450))
        elif mode == "condensed":
            parts.append(text(x + 30, y + 191, "50 GB", size=72, family="Oswald", fill=DEEP, weight=700))
            parts.append(text(x + 30, y + 291, "100 GB", size=72, family="Oswald", fill=VIOLET, weight=700))
            parts.append(text(x + 30, y + 365, "Digits stay Latin; Burmese never gets squeezed.", size=15, family="Inter", fill=SLATE, weight=450))
        else:
            parts.append(text(x + 30, y + 187, "တစ်လလျှင် ၃,၀၀၀ ကျပ်", size=37, family="Noto Sans Myanmar", fill=INK, weight=650, lang="my"))
            parts.append(text(x + 30, y + 267, "တစ်လလျှင် 3,000 ကျပ်", size=37, family="Noto Sans Myanmar", fill=DEEP, weight=650, lang="my"))
            parts.append(text(x + 30, y + 353, "Myanmar numerals / Latin numerals", size=16, family="Inter", fill=SLATE, weight=500))
    return shell(title, subtitle, 1200, "".join(parts))


def board_app_style_gallery() -> str:
    title = "App-style type scan"
    subtitle = "24 display directions + one Burmese companion line."
    entries = [
        ("Inter", "neutral / platform", "Clear access", 700, CYAN),
        ("Manrope", "humanist / clean", "Clear access", 700, CYAN),
        ("Space Grotesk", "tech / contemporary", "FAST SETUP", 700, VIOLET),
        ("Rubik", "rounded / practical", "Clear access", 700, GOLD),
        ("Avenir Next", "warm / polished", "Clear access", 700, CYAN),
        ("Arial Rounded MT Bold", "friendly / native", "HELLO AuriX", 700, GOLD),
        ("Oswald", "condensed / utility", "100 GB", 700, VIOLET),
        ("Bebas Neue", "condensed / impact", "1 MIN", 400, GOLD),
        ("Anton", "heavy / poster", "START NOW", 400, CYAN),
        ("Arial Narrow", "compact / system", "3 GB MONTHLY", 700, VIOLET),
        ("Comfortaa", "rounded / soft", "Hello AuriX", 700, CYAN),
        ("Fredoka", "bubble / friendly", "TRY FREE", 700, GOLD),
        ("Baloo 2", "playful / regional", "AuriX", 700, VIOLET),
        ("Lilita One", "bold / playful", "GO", 400, CYAN),
        ("Bungee", "sticker / display", "ON", 400, GOLD),
        ("Fraunces", "soft / editorial", "Clear access", 700, CYAN),
        ("DM Serif Display", "editorial / classic", "AuriX", 400, VIOLET),
        ("Playfair Display", "luxury / editorial", "Clear access", 700, GOLD),
        ("Bodoni Moda", "high contrast / fashion", "AuriX", 800, CYAN),
        ("Space Mono", "mono / data", "50 GB", 700, VIOLET),
        ("JetBrains Mono", "mono / technical", "quota 50%", 700, GOLD),
        ("Caveat", "handwritten / note", "try AuriX", 700, CYAN),
        ("Pacifico", "script / celebratory", "Hello!", 400, VIOLET),
        ("Permanent Marker", "marker / sticker", "FREE", 400, GOLD),
    ]
    left = 64
    card_w = 226
    gap = 16
    card_h = 218
    row_gap = 14
    parts = []
    for idx, (family, category, sample, weight, accent) in enumerate(entries):
        col = idx % 4
        row = idx // 4
        x = left + col * (card_w + gap)
        y = 184 + row * (card_h + row_gap)
        parts.append(rect(x, y, card_w, card_h, WHITE, 18, stroke=MUTED, stroke_width=2))
        parts.append(rect(x, y, 8, card_h, accent, 4))
        parts.append(text(x + 22, y + 31, family, size=15, family="Inter", fill=DEEP, weight=700))
        parts.append(text(x + 22, y + 56, category, size=12, family="Inter", fill=SLATE, weight=550))
        parts.append(text(x + 22, y + 122, sample, size=28, family=family, fill=INK, weight=weight))
        parts.append(text(x + 22, y + 166, "AuriX / access", size=15, family=family, fill=accent, weight=weight))
        parts.append(text(x + 22, y + 198, "မြန်မာစာ companion", size=15, family="Noto Sans Myanmar", fill=SLATE, weight=500, lang="my"))
    height = 184 + 6 * (card_h + row_gap) + 54
    return shell(title, subtitle, height, "".join(parts))


def board_burmese_roles() -> str:
    title = "Burmese role scan"
    subtitle = "Character in the hook; facts in tested Myanmar faces."
    entries = [
        ("Noto Sans Myanmar / 400", "Noto Sans Myanmar", 400, "Body / explanation", CYAN),
        ("Noto Sans Myanmar / 650", "Noto Sans Myanmar", 650, "Headline / benefit", VIOLET),
        ("Noto Sans Myanmar / 800", "Noto Sans Myanmar", 800, "Hook / large number", GOLD),
        ("Noto Sans Myanmar UI", "Noto Sans Myanmar UI", 650, "Compact UI label only", CYAN),
        ("Noto Serif Myanmar", "Noto Serif Myanmar", 700, "Short editorial hook", VIOLET),
        ("Padauk / Regular", "Padauk", 400, "Open alternative / retest", GOLD),
        ("Padauk / Bold", "Padauk", 700, "Warm display alternative", CYAN),
        ("Myanmar MN / system", "Myanmar MN", 500, "Platform contrast only", SLATE),
    ]
    parts = []
    y = 184
    for idx, (label, family, weight, role, accent) in enumerate(entries):
        parts.append(rect(64, y, 952, 174, WHITE, 20, stroke=MUTED, stroke_width=2))
        parts.append(rect(64, y, 10, 174, accent, 5))
        parts.append(text(102, y + 40, label, size=21, family="Inter", fill=DEEP, weight=700))
        parts.append(text(102, y + 71, role, size=17, family="Inter", fill=SLATE, weight=550))
        parts.append(text(102, y + 124, "Quota လက်ကျန်ကို အချိန်မရွေး စစ်နိုင်ပြီ", size=35, family=family, fill=INK, weight=weight, lang="my"))
        y += 190
    return shell(title, subtitle, y + 58, "".join(parts))


BOARDS = {
    "01-myanmar-families": board_myanmar,
    "02-bilingual-pairings": board_pairings,
    "03-static-treatments": board_treatments,
    "04-motion-storyboard": board_motion,
    "05-feed-scale-composite": board_mobile_stress_test,
    "06-numeral-systems": board_numerals,
    "07-app-style-display-scan": board_app_style_gallery,
    "08-burmese-role-scan": board_burmese_roles,
}


def main() -> None:
    PROOFS.mkdir(parents=True, exist_ok=True)
    cache_dir = ROOT / ".fontcache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    system_configs = [
        Path("/opt/homebrew/etc/fonts/fonts.conf"),
        Path("/usr/local/etc/fonts/fonts.conf"),
        Path("/etc/fonts/fonts.conf"),
    ]
    system_config = next((path for path in system_configs if path.exists()), None)
    if system_config is None:
        raise RuntimeError("No base Fontconfig config found; install Fontconfig or use installed fonts.")
    extra_dirs = [
        Path(raw_path)
        for raw_path in os.environ.get("AURIX_EXTRA_FONT_DIRS", "").split(os.pathsep)
        if raw_path
    ]
    font_dirs = [FONT_DIR, *extra_dirs]
    dir_xml = "".join(f"  <dir>{escape(str(path))}</dir>\n" for path in font_dirs)
    FONTCONFIG.write_text(
        "<?xml version=\"1.0\"?>\n"
        "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">\n"
        "<fontconfig>\n"
        f"  <cachedir>{escape(str(cache_dir))}</cachedir>\n"
        f"  <include>{escape(str(system_config))}</include>\n"
        + dir_xml
        + "</fontconfig>\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(FONTCONFIG)
    subprocess.run(["fc-cache", "-f", *[str(path) for path in font_dirs]], check=True, env=env)
    for name, make_svg in BOARDS.items():
        svg_path = PROOFS / f"{name}.svg"
        png_path = PROOFS / f"{name}.png"
        svg_path.write_text(make_svg(), encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "--width", str(WIDTH), "--output", str(png_path), str(svg_path)],
            check=True,
            env=env,
        )
        print(f"rendered {png_path}")


if __name__ == "__main__":
    main()
