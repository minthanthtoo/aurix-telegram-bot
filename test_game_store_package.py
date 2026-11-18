import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from scripts.game_store_scope import REQUIRED_RELEASE_FILES, SNAPSHOT_REQUIRED_RELEASE_FILES
from scripts.package_game_store import (
    package, package_commit, package_snapshot, verify_commit_package, verify_release_package,
)


class GameStoreReleaseTests(unittest.TestCase):
    def test_isolated_release_bootstraps_without_repo_or_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'release.tgz'
            result = package(archive)
            self.assertIn('migrations.py', result['file_sha256'])
            self.assertIn('commerce_models.py', result['file_sha256'])
            self.assertFalse(any('.local' in name or 'test_token' in name for name in result['file_sha256']))
            isolated = root / 'isolated'
            isolated.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(isolated, filter='data')
            env = {key: value for key, value in os.environ.items() if not key.startswith(('AURIX_', 'FAZERCARDS_', 'PYTHONPATH'))}
            check = subprocess.run([sys.executable, '-c', 'import aurix_games.__main__; print("release_import_ok")'],
                cwd=isolated, env=env, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn('release_import_ok', check.stdout)

    def test_committed_package_excludes_worktree_files_and_verifies_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

            git("init", "-q")
            for name in REQUIRED_RELEASE_FILES:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"committed {name}\n", encoding="utf-8")
            git("add", ".")
            private_policy = repo / "config/games/private-payment-policy.json"
            private_policy.write_text('{"merchant_account":"keep-out"}\n', encoding="utf-8")
            git("add", ".")
            git("-c", "user.name=Games CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "fixture")
            sha = git("rev-parse", "HEAD")
            app = "web/game-store/app.js"
            (repo / app).write_text("uncommitted change", encoding="utf-8")
            (repo / "web/game-store/private.json").write_text("untracked", encoding="utf-8")
            first = base / "first.tgz"
            second = base / "second.tgz"

            manifest = package_commit(first, sha, root=repo)
            package_commit(second, sha, root=repo)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(manifest["file_sha256"][app], hashlib.sha256(f"committed {app}\n".encode()).hexdigest())
            self.assertNotIn("web/game-store/private.json", manifest["file_sha256"])
            self.assertNotIn("config/games/private-payment-policy.json", manifest["file_sha256"])
            self.assertEqual(verify_commit_package(first, sha, manifest["source_sha256"]), manifest)
            with second.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "expected release"):
                verify_commit_package(second, sha, manifest["source_sha256"])

    def test_active_release_snapshot_is_reproducible_and_contains_only_release_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "active"
            root.mkdir()
            for name in SNAPSHOT_REQUIRED_RELEASE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"active {name}\n", encoding="utf-8")
            (root / "web/game-store/.env").write_text("TOKEN=must-not-be-packaged", encoding="utf-8")
            private_policy = root / "config/games/private-payment-policy.json"
            private_policy.parent.mkdir(parents=True, exist_ok=True)
            private_policy.write_text('{"merchant_account":"keep-out"}\n', encoding="utf-8")
            (root / ".local").mkdir()
            (root / ".local/orders.sqlite3").write_bytes(b"customer data")
            first = Path(directory) / "first.tgz"
            second = Path(directory) / "second.tgz"

            manifest = package_snapshot(root, first)
            repeated = package_snapshot(root, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(manifest["release_id"], f"legacy-{manifest['source_sha256']}")
            self.assertEqual(manifest, repeated)
            self.assertNotIn("web/game-store/.env", manifest["file_sha256"])
            self.assertNotIn("config/games/private-payment-policy.json", manifest["file_sha256"])
            self.assertNotIn(".local/orders.sqlite3", manifest["file_sha256"])
            self.assertEqual(
                verify_release_package(first, manifest["source_sha256"], expected_release_id=manifest["release_id"]),
                manifest,
            )
