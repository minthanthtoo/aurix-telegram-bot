import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch

from scripts import aurix_games_release_transport as transport
from scripts.game_store_scope import SNAPSHOT_REQUIRED_RELEASE_FILES
from scripts.package_game_store import package_snapshot


class _Input(io.BytesIO):
    def close(self):
        self.was_closed = True


class _Process:
    def __init__(self, response):
        self.stdin = _Input()
        self.stdout = io.BytesIO(response)
        self.stderr = io.BytesIO()

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None

    def kill(self):
        raise AssertionError("unexpected SSH process kill")


class GamesReleaseTransportTests(unittest.TestCase):
    def test_ssh_configuration_rejects_host_command_injection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GAMES_DEPLOY_HOST": "games.example; touch /tmp/pwned",
            "GAMES_DEPLOY_PORT": "22",
            "GAMES_DEPLOY_PRIVATE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n",
            "GAMES_DEPLOY_KNOWN_HOSTS": "games.example ssh-ed25519 AAAA",
        }):
            with self.assertRaisesRegex(ValueError, "DNS name"):
                transport._configuration(Path(directory))

    def test_send_streams_verified_archive_and_nulls_empty_optional_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            for name in SNAPSHOT_REQUIRED_RELEASE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture {name}\n", encoding="utf-8")
            archive = Path(directory) / "release.tgz"
            release = package_snapshot(root, archive)
            process = _Process(b'{"status":"deployed"}\n')
            args = Namespace(
                operation="deploy", archive=archive, release_id=release["release_id"],
                digest=release["source_sha256"], commit="", expected_active="", previous_release="",
            )
            with patch.object(transport, "_start", return_value=process):
                result = transport.send([], args)

            self.assertEqual(result, {"status": "deployed"})
            payload = process.stdin.getvalue()
            header_line, body = payload.split(b"\n", 1)
            header = json.loads(header_line)
            self.assertIsNone(header["commit_sha"])
            self.assertIsNone(header["expected_active_release_id"])
            self.assertEqual(len(body), header["manifest_bytes"] + header["archive_bytes"])
            self.assertTrue(process.stdin.was_closed)


if __name__ == "__main__":
    unittest.main()
