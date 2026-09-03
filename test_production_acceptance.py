import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.production_acceptance import _summarize, run_acceptance


class ProductionAcceptanceTests(unittest.TestCase):
    def test_warnings_never_summarize_as_pass(self):
        self.assertEqual(_summarize([]), "pass")
        self.assertEqual(_summarize([{"status": "warn"}]), "warn")
        self.assertEqual(_summarize([{"status": "warn"}, {"status": "fail"}]), "fail")

    def test_acceptance_propagates_recovery_warning_and_skips_live_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / "aurix.env"
            env_file.write_text("TELEGRAM_BOT_TOKEN=test\n", encoding="utf-8")

            def fake_runner(command, **kwargs):
                del kwargs
                if command[:4] == ("git", "-C", str(root), "status"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("deploy.production_acceptance.run_audit", return_value={
                "status": "warn", "checks": [{"name": "allocation_policy", "status": "warn"}],
            }), patch("deploy.production_acceptance.shutil.which", return_value="/usr/bin/ruff"):
                report = run_acceptance(
                    env_file=env_file,
                    root=root,
                    runner=fake_runner,
                )
        self.assertEqual(report["status"], "warn")
        self.assertEqual(
            next(item for item in report["checks"] if item["name"] == "live_release")["status"],
            "skip",
        )


if __name__ == "__main__":
    unittest.main()
