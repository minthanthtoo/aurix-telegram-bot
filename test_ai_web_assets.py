import unittest
from pathlib import Path


class AIWebAssetsTest(unittest.TestCase):
    def test_deployable_ai_shell_contains_every_runtime_asset(self):
        root = Path(__file__).resolve().parent / "web" / "ai-app"
        required = (
            "index.html",
            "styles.css",
            "shared.js",
            "app.js",
            "admin.html",
            "admin.js",
            "api-guide.html",
            "api-guide.css",
            "api-guide.js",
        )
        missing = [name for name in required if not (root / name).is_file()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
