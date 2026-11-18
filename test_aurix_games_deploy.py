import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy import aurix_games_deploy as deploy
from scripts.game_store_scope import SNAPSHOT_REQUIRED_RELEASE_FILES
from scripts.package_game_store import package_snapshot


class GamesDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.base = root / "games"
        self.releases = self.base / "releases"
        self.releases.mkdir(parents=True)
        self.active = self.releases / "legacy-active"
        self.active.mkdir()
        self._write_release(self.active, "existing")
        self.current = self.base / "current"
        self.current.symlink_to(self.active)
        self.stage = self.base / ".deploy-staging"
        self.state = self.base / "deployment-state.json"
        self.journal = self.base / "deployment-journal.json"
        self.lock = root / "lock" / "games.lock"
        self.lock.parent.mkdir()
        for name, value in {
            "BASE": self.base, "RELEASES": self.releases, "CURRENT": self.current,
            "STATE": self.state, "JOURNAL": self.journal, "LOCK": self.lock,
            "STAGE_ROOT": self.stage,
        }.items():
            original = getattr(deploy, name)
            setattr(deploy, name, value)
            self.addCleanup(setattr, deploy, name, original)
        self.active_archive = root / "old.tgz"
        self.active_record = package_snapshot(self.active, self.active_archive)
        self.next_source = root / "next-source"
        self.next_source.mkdir()
        self._write_release(self.next_source, "candidate")
        self.next_archive = root / "candidate.tgz"
        self.next_record = package_snapshot(self.next_source, self.next_archive)
        self.system_active = patch.object(
            deploy, "_active", side_effect=lambda unit: unit in {deploy.WEB_SERVICE, deploy.EDGE_SERVICE},
        )
        self.system_active.start()
        self.addCleanup(self.system_active.stop)
        self.enabled = patch.object(deploy, "_enabled", return_value=True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.commands = patch.object(deploy, "_run", return_value=subprocess.CompletedProcess([], 0))
        self.commands.start()
        self.addCleanup(self.commands.stop)
        self.health = patch.object(deploy, "_health")
        self.health.start()
        self.addCleanup(self.health.stop)
        self.readiness = patch.object(deploy, "_readiness", return_value=None)
        self.readiness.start()
        self.addCleanup(self.readiness.stop)

    @staticmethod
    def _write_release(root: Path, content: str):
        for name in SNAPSHOT_REQUIRED_RELEASE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{content}: {name}\n", encoding="utf-8")

    def _request(self, archive: Path, record: dict, *, operation="deploy", active=None, previous=None):
        header = {
            "operation": operation, "release_id": record["release_id"],
            "source_sha256": record["source_sha256"], "commit_sha": record["commit_sha"],
            "expected_active_release_id": active, "previous_release_id": previous,
        }
        return deploy._install_request(
            header, archive.with_suffix(".manifest.json").read_bytes(), archive,
        )

    def test_preflight_checks_the_pointer_and_leaves_pending_recovery_untouched(self):
        request = b'{"operation":"status"}\n'
        result = io.BytesIO()
        deploy.handle_request(io.BytesIO(request), result)
        self.assertEqual(json.loads(result.getvalue()), {"status": "ok", "state": None})

        self.journal.write_text("{}")
        with self.assertRaisesRegex(deploy.DeployError, "recovery is pending"):
            deploy.handle_request(io.BytesIO(request), io.BytesIO())
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.current.resolve(), self.active.resolve())

    def test_first_deploy_snapshots_unmarked_active_and_keeps_it_as_rollback(self):
        result = self._request(
            self.next_archive, self.next_record,
            previous=self.active_record["release_id"],
        )

        self.assertEqual(result["status"], "deployed")
        self.assertEqual(self.current.resolve(), (self.releases / self.next_record["release_id"]).resolve())
        self.assertEqual(result["rollback"]["release_id"], self.active_record["release_id"])
        self.assertEqual(json.loads(self.state.read_text())["active"]["release_id"], self.next_record["release_id"])
        self.assertEqual([path.name for path in self.releases.iterdir()], [self.next_record["release_id"]])
        self.assertTrue((self.current.resolve() / ".aurix-games-release.json").is_file())
        self.assertEqual(self.current.resolve().stat().st_mode & 0o777, 0o755)

    def test_unhealthy_first_release_restores_live_pointer_and_state(self):
        deploy._health.side_effect = [deploy.DeployError("candidate health failed"), None]
        with self.assertRaisesRegex(deploy.DeployError, "candidate health failed"):
            self._request(self.next_archive, self.next_record, previous=self.active_record["release_id"])
        self.assertEqual(self.current.resolve(), self.active.resolve())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.releases / self.next_record["release_id"]).exists())

    def test_rollback_swaps_active_and_retains_the_previous_package_for_next_recovery(self):
        deployed = self._request(
            self.next_archive, self.next_record,
            previous=self.active_record["release_id"],
        )
        rollback = self._request(
            self.active_archive, self.active_record, operation="rollback",
            active=deployed["active"]["release_id"],
        )

        self.assertEqual(rollback["status"], "deployed")
        self.assertEqual(rollback["active"]["release_id"], self.active_record["release_id"])
        self.assertEqual(rollback["rollback"]["release_id"], self.next_record["release_id"])
        self.assertEqual([path.name for path in self.releases.iterdir()], [self.active_record["release_id"]])

    def test_extract_rejects_archive_path_traversal(self):
        archive = Path(self.temporary.name) / "unsafe.tgz"
        import tarfile
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("../outside")
            member.size = 1
            import io
            bundle.addfile(member, io.BytesIO(b"x"))
        with self.assertRaisesRegex(deploy.DeployError, "unsafe path"):
            deploy._extract_package(archive, Path(self.temporary.name) / "extract", {"../outside": "x"})

    def test_runtime_lock_must_match_installed_packages_and_existing_release_lock(self):
        from scripts.game_store_scope import LOCK_FILE

        source = Path(self.temporary.name) / "locked-source"
        source.mkdir()
        self._write_release(source, "locked")
        lock = source / LOCK_FILE
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("sample-runtime==1.2.3 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
        archive = Path(self.temporary.name) / "locked.tgz"
        record = package_snapshot(source, archive)

        with patch.object(deploy.metadata, "version", return_value="1.2.3"):
            deploy._check_runtime_lock(archive, record, self.active, "deploy")
        with patch.object(deploy.metadata, "version", return_value="1.2.4"):
            with self.assertRaisesRegex(deploy.DeployError, "differs from the server"):
                deploy._check_runtime_lock(archive, record, self.active, "deploy")
        old_lock = self.active / LOCK_FILE
        old_lock.parent.mkdir(parents=True, exist_ok=True)
        old_lock.write_text("old dependency lock\n", encoding="utf-8")
        with self.assertRaisesRegex(deploy.DeployError, "dependency lock changed"):
            deploy._check_runtime_lock(archive, record, self.active, "deploy")


if __name__ == "__main__":
    unittest.main()
