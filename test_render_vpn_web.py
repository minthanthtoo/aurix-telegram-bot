import subprocess
import sys
import unittest
from pathlib import Path


class RenderVpnWebEntrypointTest(unittest.TestCase):
    def test_entrypoint_imports_repository_modules_when_run_by_path(self):
        repository_root = Path(__file__).resolve().parent
        entrypoint = repository_root / "deploy" / "render_vpn_web.py"
        probe = f"import runpy; runpy.run_path({str(entrypoint)!r}, run_name='render_entrypoint_probe')"
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repository_root.parent,
            env={},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
