#!/usr/bin/env python3
"""Fetch a small, reviewed set of generic Game Icons for local serving.

This is an explicit design-time sync, never a customer-request code path. The
allow-list, source metadata, and SVG sanitizer intentionally make it impossible
to turn arbitrary Iconify API responses into app assets.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "web/game-store/assets/game-icons"
MANIFEST_PATH = ASSET_DIR / "catalog-glyphs.manifest.json"
API_HOSTS = (
    "https://api.iconify.design",
    "https://api.simplesvg.com",
    "https://api.unisvg.com",
)
API_BASE = f"{API_HOSTS[0]}/game-icons"
LICENSE = "CC BY 3.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/3.0/"
SVG_NS = "http://www.w3.org/2000/svg"
MAX_RESPONSE_BYTES = 96 * 1024
MAX_PATHS = 64

# These are generic functional glyphs, not game-publisher artwork.
ICONS: dict[str, dict[str, str]] = {
    "coins": {
        "title": "Coins",
        "author": "Delapouite",
        "source": "https://game-icons.net/1x1/delapouite/coins.html",
    },
    "cut-diamond": {
        "title": "Cut Diamond",
        "author": "Lorc",
        "source": "https://game-icons.net/1x1/lorc/cut-diamond.html",
    },
    "present": {
        "title": "Present",
        "author": "Delapouite",
        "source": "https://game-icons.net/1x1/delapouite/present.html",
    },
    "spell-book": {
        "title": "Spell Book",
        "author": "Delapouite",
        "source": "https://game-icons.net/1x1/delapouite/spell-book.html",
    },
    "boots": {
        "title": "Boots",
        "author": "Lorc",
        "source": "https://game-icons.net/1x1/lorc/boots.html",
    },
    "jungle": {
        "title": "Jungle",
        "author": "Delapouite",
        "source": "https://game-icons.net/1x1/delapouite/jungle.html",
    },
    "footsteps": {
        "title": "Footsteps",
        "author": "Skoll",
        "source": "https://game-icons.net/1x1/skoll/footsteps.html",
    },
}


def sanitize_svg(payload: bytes) -> bytes:
    """Validate an Iconify SVG and return a minimized, deterministic SVG."""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("SVG response is empty or exceeds the size limit")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("SVG declarations and entities are not accepted")

    try:
        upstream = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ValueError("Iconify response is not valid XML") from error

    if upstream.tag != f"{{{SVG_NS}}}svg":
        raise ValueError("Iconify response root is not an SVG")
    allowed_root_attributes = {"width", "height", "viewBox"}
    if set(upstream.attrib) - allowed_root_attributes:
        raise ValueError("SVG contains unsupported root attributes")
    if upstream.attrib.get("viewBox") != "0 0 512 512":
        raise ValueError("SVG viewBox does not match the reviewed Game Icons set")
    children = list(upstream)
    if not children or len(children) > MAX_PATHS:
        raise ValueError("SVG path count is empty or exceeds the limit")

    clean = ET.Element(
        f"{{{SVG_NS}}}svg",
        {"width": "512", "height": "512", "viewBox": "0 0 512 512"},
    )
    for node in children:
        if node.tag != f"{{{SVG_NS}}}path" or set(node.attrib) - {"fill", "d"}:
            raise ValueError("SVG contains an element or attribute outside the path-only policy")
        path_data = node.attrib.get("d", "")
        if not path_data or len(path_data) > 48 * 1024:
            raise ValueError("SVG path data is empty or exceeds the limit")
        if node.attrib.get("fill", "currentColor") != "currentColor":
            raise ValueError("SVG path fill is not the reviewed currentColor value")
        ET.SubElement(clean, f"{{{SVG_NS}}}path", {"fill": "currentColor", "d": path_data})

    ET.register_namespace("", SVG_NS)
    result = ET.tostring(clean, encoding="utf-8", xml_declaration=False)
    if len(result) > MAX_RESPONSE_BYTES:
        raise ValueError("Sanitized SVG exceeds the local asset size limit")
    return result


def fetch_icon(icon_name: str) -> bytes:
    if icon_name not in ICONS:
        raise ValueError(f"Icon is not in the reviewed allow-list: {icon_name}")
    errors: list[Exception] = []
    for host in API_HOSTS:
        expected_url = f"{host}/game-icons/{icon_name}.svg"
        request = Request(
            expected_url,
            headers={"Accept": "image/svg+xml", "User-Agent": "AuriX-GameStore-AssetSync/1.0"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                content_type = response.headers.get_content_type()
                if response.status != 200 or content_type != "image/svg+xml":
                    raise ValueError(
                        f"Unexpected Iconify response: {response.status} {content_type}"
                    )
                if response.geturl() != expected_url:
                    raise ValueError(
                        "Iconify response redirected away from the reviewed icon endpoint"
                    )
                return sanitize_svg(response.read(MAX_RESPONSE_BYTES + 1))
        except (OSError, TimeoutError, URLError, ValueError) as error:
            errors.append(error)
    raise ValueError(
        f"all documented Iconify API hosts failed for {icon_name}: {errors[-1]}"
    ) from errors[-1]


def sync_assets() -> dict[str, Any]:
    # Fetch and validate the entire allow-list before touching the local asset
    # set, so a network/API failure cannot leave a half-refreshed collection.
    payloads = {icon_name: fetch_icon(icon_name) for icon_name in ICONS}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source_api": API_BASE,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "assets": [],
    }
    for icon_name, credit in ICONS.items():
        asset_name = f"{icon_name}.svg"
        svg = payloads[icon_name]
        manifest["assets"].append(
            {
                "file": asset_name,
                "iconify_id": f"game-icons:{icon_name}",
                **credit,
                "license": LICENSE,
                "license_url": LICENSE_URL,
                "modified": "SVG dimensions normalized to 512px; path geometry unchanged.",
                "sha256": hashlib.sha256(svg).hexdigest(),
            }
        )
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for icon_name, svg in payloads.items():
        (ASSET_DIR / f"{icon_name}.svg").write_bytes(svg)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    try:
        manifest = sync_assets()
    except (OSError, URLError, ValueError) as error:
        print(f"Game icon sync failed: {error}", file=sys.stderr)
        return 1
    print(f"Pinned {len(manifest['assets'])} sanitized SVGs in {ASSET_DIR}")
    for asset in manifest["assets"]:
        print(f"  {asset['file']} sha256={asset['sha256']}")
    print(f"Credits and hashes: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
