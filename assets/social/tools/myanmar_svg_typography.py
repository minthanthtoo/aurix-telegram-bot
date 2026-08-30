"""Shared SVG typography rules for Myanmar-script social graphics."""

from __future__ import annotations

import math
from xml.sax.saxutils import escape


MYANMAR_FONT_STACK = "Noto Sans Myanmar, Myanmar Sangam MN, sans-serif"
LATIN_FONT_STACK = "Inter, Arial, sans-serif"

LINE_HEIGHT = {
    "display": 1.55,
    "headline": 1.50,
    "body": 1.48,
    "small": 1.42,
}


def contains_myanmar(value: str) -> bool:
    return any("\u1000" <= char <= "\u109f" or "\uaa60" <= char <= "\uaa7f" for char in value)


def text_element(
    x: int,
    y: int,
    value: str,
    size: int,
    weight: int = 700,
    fill: str = "#F6F8FC",
    anchor: str = "start",
    family: str | None = None,
) -> str:
    is_myanmar = contains_myanmar(value)
    selected_family = family or (MYANMAR_FONT_STACK if is_myanmar else LATIN_FONT_STACK)
    language = ' xml:lang="my"' if is_myanmar else ""
    shaping = "font-kerning:normal;font-feature-settings:'mark' 1,'mkmk' 1;"
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" '
        f'font-family="{escape(selected_family)}" font-size="{size}" font-weight="{weight}"'
        f'{language} style="{shaping}">{escape(value)}</text>'
    )


def next_myanmar_baseline(current_y: int, size: int, role: str = "body") -> int:
    """Return a safe next baseline for a consecutive Myanmar-script line."""
    factor = LINE_HEIGHT.get(role, LINE_HEIGHT["body"])
    return current_y + math.ceil(size * factor)


def block_clearance(size: int) -> tuple[int, int]:
    """Recommended optical padding above and below a Myanmar text block."""
    return math.ceil(size * 0.32), math.ceil(size * 0.36)
