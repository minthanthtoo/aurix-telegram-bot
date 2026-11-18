from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from scripts import sync_game_icon_assets as icon_sync


class GameIconAssetTests(unittest.TestCase):
    SAMPLE_SVG = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" '
        b'viewBox="0 0 512 512"><path fill="currentColor" d="M1 2h3z"/></svg>'
    )

    def test_allow_list_is_small_and_credits_each_generic_symbol(self) -> None:
        self.assertEqual(
            set(icon_sync.ICONS),
            {"coins", "cut-diamond", "present", "spell-book", "boots", "jungle", "footsteps"},
        )
        self.assertTrue(all(entry["author"] and entry["source"].startswith("https://game-icons.net/") for entry in icon_sync.ICONS.values()))

    def test_sanitizer_keeps_only_path_geometry_and_normalizes_dimensions(self) -> None:
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" '
            b'viewBox="0 0 512 512"><path fill="currentColor" d="M1 2h3z"/></svg>'
        )
        actual = icon_sync.sanitize_svg(svg)
        root = ET.fromstring(actual)
        self.assertEqual(root.attrib, {"width": "512", "height": "512", "viewBox": "0 0 512 512"})
        self.assertEqual([child.tag for child in root], [f"{{{icon_sync.SVG_NS}}}path"])
        self.assertEqual(root[0].attrib, {"fill": "currentColor", "d": "M1 2h3z"})
        self.assertNotIn(b"script", actual.lower())

    def test_sanitizer_rejects_active_or_unreviewed_svg_content(self) -> None:
        payloads = [
            b'<!DOCTYPE svg [<!ENTITY x "bad">]><svg/>',
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><script/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><image href="https://example.test/x"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0"/></svg>',
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:48]), self.assertRaises(ValueError):
                icon_sync.sanitize_svg(payload)

    def test_unknown_icon_id_is_rejected_before_network_access(self) -> None:
        with patch.object(icon_sync, "urlopen") as open_url:
            with self.assertRaises(ValueError):
                icon_sync.fetch_icon("not-reviewed")
        open_url.assert_not_called()

    def test_icon_fetch_falls_back_to_documented_api_host(self) -> None:
        expected_url = f"{icon_sync.API_HOSTS[1]}/game-icons/coins.svg"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.get_content_type.return_value = "image/svg+xml"
        response.geturl.return_value = expected_url
        response.read.return_value = self.SAMPLE_SVG
        with patch.object(
            icon_sync, "urlopen", side_effect=[URLError("primary offline"), response]
        ) as open_url:
            self.assertEqual(
                icon_sync.fetch_icon("coins"), icon_sync.sanitize_svg(self.SAMPLE_SVG)
            )
        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(
            open_url.call_args_list[0].args[0].full_url,
            f"{icon_sync.API_HOSTS[0]}/game-icons/coins.svg",
        )
        self.assertEqual(open_url.call_args_list[1].args[0].full_url, expected_url)

    def test_failed_refresh_does_not_partially_replace_local_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_dir = Path(temp_dir) / "game-icons"
            asset_dir.mkdir()
            manifest = asset_dir / "catalog-glyphs.manifest.json"
            old_icon = asset_dir / "coins.svg"
            old_icon.write_bytes(b"previous-icon")
            manifest.write_text("previous-manifest", encoding="utf-8")
            with patch.object(icon_sync, "ASSET_DIR", asset_dir), patch.object(icon_sync, "MANIFEST_PATH", manifest):
                with patch.object(icon_sync, "fetch_icon", side_effect=[self.SAMPLE_SVG, ValueError("offline")]):
                    with self.assertRaisesRegex(ValueError, "offline"):
                        icon_sync.sync_assets()
            self.assertEqual(old_icon.read_bytes(), b"previous-icon")
            self.assertEqual(manifest.read_text(encoding="utf-8"), "previous-manifest")

    def test_checked_in_assets_match_their_attribution_manifest(self) -> None:
        manifest_path = Path(__file__).parent / "web/game-store/assets/game-icons/catalog-glyphs.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["license"], "CC BY 3.0")
        self.assertEqual({entry["file"] for entry in manifest["assets"]}, {f"{name}.svg" for name in icon_sync.ICONS})
        for entry in manifest["assets"]:
            data = (manifest_path.parent / entry["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
            icon_sync.sanitize_svg(data)


if __name__ == "__main__":
    unittest.main()
