from __future__ import annotations

import io
import tarfile
import unittest

from deploy.fleet_backup import validate_archive
from deploy.fleet_reconcile import FleetError


def archive_with(names: list[str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in names:
            payload = b"test"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


class FleetBackupTests(unittest.TestCase):
    def test_accepts_complete_archive(self) -> None:
        validate_archive(archive_with(["access.txt", "persisted-state/config.yml"]))

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(FleetError):
            validate_archive(archive_with(["access.txt", "persisted-state/../../root/.ssh/key"]))

    def test_rejects_incomplete_archive(self) -> None:
        with self.assertRaises(FleetError):
            validate_archive(archive_with(["access.txt"]))


if __name__ == "__main__":
    unittest.main()
