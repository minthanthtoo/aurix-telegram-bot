import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.production_acceptance import _default_env_file, _summarize, _tool_checks, run_acceptance


class ProductionAcceptanceTests(unittest.TestCase):
    def test_default_env_prefers_managed_host_file(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "deploy.production_acceptance.Path.is_file", return_value=True
        ):
            self.assertEqual(_default_env_file(), "/etc/aurix-bot/aurix.env")

    def test_default_env_keeps_explicit_override(self):
        with patch.dict("os.environ", {"AURIX_FLEET_ENV_FILE": "/tmp/test.env"}, clear=True):
            self.assertEqual(_default_env_file(), "/tmp/test.env")

    def test_linked_worktree_git_file_is_checked_for_cleanliness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").write_text("gitdir: /tmp/common-git\n", encoding="utf-8")

            def runner(command, **kwargs):
                del kwargs
                return subprocess.CompletedProcess(command, 0, "", "")

            from deploy.production_acceptance import _git_clean

            check = _git_clean(root, runner)
        self.assertEqual(check["status"], "pass")

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

    def test_runtime_release_does_not_fail_when_ruff_is_ci_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(command, **kwargs):
                del kwargs
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("deploy.production_acceptance.shutil.which", return_value=None):
                checks = _tool_checks(root, runner)
        self.assertEqual(next(item for item in checks if item["name"] == "ruff")["status"], "skip")

    def test_outline_check_reports_partial_fleet_without_leaking_details(self):
        from deploy.production_acceptance import _outline_check

        with patch(
            "deploy.production_acceptance.run_outline_diagnostics",
            return_value={
                "status": "degraded",
                "healthy_servers": 1,
                "server_count": 3,
                "servers": [{"server_id": "sg-a", "status": "healthy"}],
            },
        ) as diagnostic:
            check, report = _outline_check(Path("/tmp/aurix.env"))
        diagnostic.assert_called_once_with(Path("/tmp/aurix.env"))
        self.assertEqual(check["name"], "outline_endpoints")
        self.assertEqual(check["status"], "warn")
        self.assertEqual(report["healthy_servers"], 1)

    def test_acceptance_includes_outline_diagnostic_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / "aurix.env"
            env_file.write_text("TELEGRAM_BOT_TOKEN=test\n", encoding="utf-8")

            def fake_runner(command, **kwargs):
                del kwargs
                if command[:4] == ("git", "-C", str(root), "status"):
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "deploy.production_acceptance.run_audit",
                return_value={"status": "pass", "checks": []},
            ), patch(
                "deploy.production_acceptance.shutil.which", return_value="/usr/bin/ruff"
            ), patch(
                "deploy.production_acceptance.run_outline_diagnostics",
                return_value={"status": "healthy", "healthy_servers": 1, "server_count": 1},
            ) as diagnostic:
                report = run_acceptance(env_file=env_file, root=root, runner=fake_runner, outline=True)

        diagnostic.assert_called_once_with(env_file)
        self.assertEqual(
            next(item for item in report["checks"] if item["name"] == "outline_endpoints")["status"],
            "pass",
        )
        self.assertEqual(report["outline_diagnostics"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
