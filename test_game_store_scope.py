import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.game_store_scope import changed_scope, classify
from scripts.run_product_checks import product_test_modules


class GameStoreScopeTests(unittest.TestCase):
    def test_ownership_and_conservative_fallback(self):
        for paths, expected in (
            (["web/game-store/app.js", "test_game_order_status_ui.mjs"], (True, False)),
            (["aurix_vpn/runtime.py"], (False, True)),
            (["uv.lock"], (True, True)),
            (["commerce_models.py"], (True, True)),
            (["config/games/requirements.lock"], (True, False)),
            (["aurix_games/web_api.py", "aurix_ai/config.py"], (True, True)),
            (["docs/games.md"], (False, False)),
            (["unclassified.py"], (True, True)),
        ):
            with self.subTest(paths=paths):
                self.assertEqual(classify(paths), expected)

    def test_git_rename_routes_both_old_and_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

            def commit(message):
                git("-c", "user.name=Games CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", message)
                return git("rev-parse", "HEAD")

            git("init", "-q")
            (repo / "aurix_games").mkdir()
            (repo / "aurix_ai").mkdir()
            (repo / "aurix_games/item.py").write_text("# test\n", encoding="utf-8")
            git("add", ".")
            before = commit("before")
            git("mv", "aurix_games/item.py", "aurix_ai/item.py")
            after = commit("after")
            self.assertEqual(changed_scope(before, after, pull_request=True, root=repo), (True, True))
            self.assertEqual(changed_scope(before, after, pull_request=False, root=repo), (True, True))
            self.assertEqual(changed_scope("0" * 40, after, pull_request=False, root=repo), (True, True))

    def test_product_test_runner_excludes_game_modules_without_importing_them(self):
        self.assertEqual(
            product_test_modules([
                "test_app.py", "test_game_order.py", "nested/__init__.py",
                "nested/test_commerce.py", "nested/test_fazercards_api.py",
                "scratch/test_untracked.py", "not_a_test.py",
            ]),
            ["nested.test_commerce", "test_app"],
        )
