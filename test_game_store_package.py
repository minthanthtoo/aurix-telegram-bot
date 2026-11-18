import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from scripts.game_store_scope import REQUIRED_RELEASE_FILES
from scripts.package_game_store import package, package_commit, verify_commit_package


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
            self.assertEqual(verify_commit_package(first, sha, manifest["source_sha256"]), manifest)
            with second.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "expected release"):
                verify_commit_package(second, sha, manifest["source_sha256"])
