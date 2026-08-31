import tempfile
import unittest
from pathlib import Path

from access_control import StaffAccessControl, StaffAccessError
from commerce import CommerceDatabase
from free_repository import Database


class StaffAccessControlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "staff.db"
        self.database = Database(self.path)
        self.database.initialize()
        CommerceDatabase(self.path).initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_bootstrap_excludes_bots_and_preserves_creator_as_owner(self):
        access = StaffAccessControl(self.database)
        result = access.bootstrap(
            owner_id=None,
            group_owner={"id": 10, "display_name": "Owner"},
            group_admins=[
                {"id": 20, "username": "human"},
                {"id": 30, "username": "helperbot", "is_bot": True},
            ],
        )
        self.assertEqual(result["owner_id"], 10)
        self.assertEqual(result["admin_ids"], {10, 20})
        self.assertTrue(access.is_owner(10))
        self.assertFalse(access.is_admin(30))

    def test_environment_owner_conflict_fails_closed(self):
        access = StaffAccessControl(self.database, 10)
        access.bootstrap(owner_id=10)
        with self.assertRaisesRegex(StaffAccessError, "conflicts"):
            access.bootstrap(owner_id=11)

    def test_owner_adds_and_immediately_revokes_tracked_admin(self):
        access = StaffAccessControl(self.database, 10)
        access.bootstrap(owner_id=10)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, username, created_at) VALUES (20, 'Alice', 'alice', '2026-01-01T00:00:00+00:00')"
            )
        added = access.add_admin(20, 10)
        self.assertEqual(added["effective_username"], "alice")
        self.assertTrue(access.is_admin(20))
        access.remove_admin(20, 10)
        self.assertFalse(access.is_admin(20))
        with self.assertRaises(PermissionError):
            access.require_admin(20)

    def test_owner_cannot_be_removed(self):
        access = StaffAccessControl(self.database, 10)
        access.bootstrap(owner_id=10)
        with self.assertRaisesRegex(StaffAccessError, "owner cannot"):
            access.remove_admin(10, 10)

    def test_single_legacy_admin_becomes_recoverable_owner(self):
        access = StaffAccessControl(self.database)
        result = access.bootstrap(owner_id=None, admin_ids={10})
        self.assertEqual(result["owner_id"], 10)
        self.assertTrue(access.is_owner(10))


if __name__ == "__main__":
    unittest.main()
