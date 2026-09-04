import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deploy.digitalocean_autodeploy import (
    VERSIONED_UNIT_NAMES,
    ci_conclusion,
    github_slug,
    make_release_traversable,
    missing_release_configuration,
)


class DigitalOceanDeployTest(unittest.TestCase):
    def test_operational_units_are_versioned_with_the_release(self):
        self.assertIn("aurix-infrastructure-worker.service", VERSIONED_UNIT_NAMES)
        self.assertIn("aurix-infrastructure-worker.timer", VERSIONED_UNIT_NAMES)
        self.assertIn("aurix-fleet-registration.service", VERSIONED_UNIT_NAMES)
        self.assertIn("aurix-dns-sync.service", VERSIONED_UNIT_NAMES)
        self.assertIn("aurix-dns-sync.timer", VERSIONED_UNIT_NAMES)
    def test_receipt_smoke_is_directly_executable_outside_repository(self):
        script = Path(__file__).resolve().parent / "deploy/receipt_pipeline_smoke.py"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=temporary,
                env={"PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_built_release_is_traversable_by_service_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            release.mkdir(mode=0o700)
            private_directory = release / ".venv" / "bin"
            private_directory.mkdir(parents=True, mode=0o700)
            private_file = private_directory / "config"
            private_file.write_text("configuration")
            private_file.chmod(0o600)
            executable = private_directory / "python"
            executable.write_text("executable")
            executable.chmod(0o700)

            make_release_traversable(release)

            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(private_directory.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)

    def test_release_gate_reports_names_without_secret_values(self):
        environment = {
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "secret-value",
            "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
            "RECEIPT_LLM_MODEL": "vision-model",
            "RECEIPT_LLM_API_KEY": "vision-secret",
            "PAYMENT_RECIPIENTS_JSON": "configured-secret",
            "RECEIPT_STORAGE_REQUIRED": "1",
        }
        self.assertEqual(missing_release_configuration(environment), [])
        environment.pop("SUPABASE_SERVICE_ROLE_KEY")
        environment["RECEIPT_STORAGE_REQUIRED"] = "0"
        self.assertEqual(
            missing_release_configuration(environment),
            ["SUPABASE_SERVICE_ROLE_KEY", "RECEIPT_STORAGE_REQUIRED=1"],
        )

    def test_github_slug_accepts_only_normal_https_repository(self):
        self.assertEqual(
            github_slug("https://github.com/minthanthtoo/aurix-telegram-bot.git"),
            "minthanthtoo/aurix-telegram-bot",
        )
        self.assertIsNone(github_slug("ssh://github.com/minthanthtoo/aurix.git"))
        self.assertIsNone(github_slug("https://example.com/minthanthtoo/aurix.git"))

    def test_ci_gate_requires_named_completed_success(self):
        self.assertEqual(ci_conclusion({"check_runs": []}), "pending")
        self.assertEqual(
            ci_conclusion(
                {"check_runs": [{"name": "safety-net", "status": "in_progress"}]}
            ),
            "pending",
        )
        self.assertEqual(
            ci_conclusion(
                {
                    "check_runs": [
                        {
                            "name": "safety-net",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
            "failed",
        )
        self.assertEqual(
            ci_conclusion(
                {
                    "check_runs": [
                        {
                            "name": "safety-net",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ),
            "success",
        )


if __name__ == "__main__":
    unittest.main()
