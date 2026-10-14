"""Durable staff-policy checks using an isolated SQLite database."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Database
from aurix_vpn.commerce_repositories import CommerceDatabase
from aurix_vpn.staff_access import StaffAccessError, StaffAccessService
from aurix_vpn.telegram_admin import AdminOperations


class StaffAccessServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "staff.db"
        Database(path).initialize()
        self.database = CommerceDatabase(path)
        self.database.initialize()
        self.access = StaffAccessService(
            self.database,
            admin_ids={901, 902},
            owner_ids={900},
            seller_ids=(),
            mode="roles",
        )
        self.access.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_environment_roles_bootstrap_idempotently_without_promoting_admins(self):
        self.assertEqual(self.access.role_for(900), "owner")
        self.assertEqual(self.access.role_for(901), "admin")
        self.assertEqual(self.access.role_for(902), "admin")
        self.assertFalse(self.access.is_owner(901))
        self.access.initialize()
        self.assertEqual(len(self.access.staff_members(900)), 3)

    def test_environment_owner_can_coexist_with_multiple_configured_admins(self):
        with patch.dict(
            os.environ,
            {
                "OWNER_TELEGRAM_IDS": "910",
                "SELLER_TELEGRAM_IDS": "913",
                "AURIX_STAFF_AUTHORIZATION_MODE": "roles",
            },
            clear=True,
        ):
            access = StaffAccessService.from_environment(self.database, {911, 912})
        self.assertEqual(access.role_for(910), "owner")
        self.assertEqual(access.role_for(911), "admin")
        self.assertEqual(access.role_for(912), "admin")
        self.assertEqual(access.role_for(913), "seller")

    def test_seller_case_access_is_assignment_scoped_and_revoked_live(self):
        self.access.grant_role(900, 903, "seller")
        self.access.assign_customer(900, 903, 12345)
        self.assertTrue(self.access.can_view_customer_case(903, 12345))
        self.assertFalse(self.access.can_view_customer_case(903, 12346))
        self.assertTrue(self.access.can_view_customer_case(901, 12346))
        self.access.revoke_role(900, 903)
        self.assertFalse(self.access.can_view_customer_case(903, 12345))

    def test_customer_case_reassignment_keeps_only_one_active_seller(self):
        self.access.grant_role(900, 903, "seller")
        self.access.grant_role(900, 904, "seller")
        self.access.assign_customer(900, 903, 12345)
        self.access.assign_customer(900, 904, 12345)
        self.assertFalse(self.access.can_view_customer_case(903, 12345))
        self.assertTrue(self.access.can_view_customer_case(904, 12345))
        self.assertEqual(
            self.access.assignments(900),
            [
                {
                    "seller_telegram_id": 904,
                    "customer_telegram_id": 12345,
                    "assigned_by": 900,
                    "assigned_at": self.access.assignments(900)[0]["assigned_at"],
                }
            ],
        )

    def test_admin_operations_recheck_capability_on_each_call(self):
        class CommerceProbe:
            def list_pending_orders(self):
                return ["pending"]

        operations = AdminOperations(
            CommerceProbe(), {901, 902}, staff_access=self.access
        )
        self.assertEqual(operations.call(901, "list_pending_orders"), ["pending"])
        self.access.grant_role(900, 903, "seller")
        with self.assertRaises(StaffAccessError):
            operations.call(903, "list_pending_orders")
        self.access.set_capability(900, 901, "sales.read", "deny")
        with self.assertRaises(StaffAccessError):
            operations.call(901, "list_pending_orders")
        self.assertEqual(operations.call(900, "list_pending_orders"), ["pending"])

    def test_every_allowlisted_operation_has_a_capability_mapping(self):
        operations = AdminOperations(object(), set())
        self.assertEqual(
            operations.COMMERCE_OPERATIONS | operations.SERVICE_OPERATIONS,
            set(operations.OPERATION_CAPABILITIES),
        )

    def test_only_owner_can_change_roles_and_owner_cannot_lock_out_the_last_owner(self):
        with self.assertRaises(StaffAccessError):
            self.access.grant_role(901, 905, "admin")
        self.access.grant_role(900, 905, "owner")
        self.access.owner_ids = frozenset()
        self.access.revoke_role(900, 900)
        self.assertEqual(self.access.role_for(900), None)
        with self.assertRaisesRegex(StaffAccessError, "last active Owner"):
            self.access.revoke_role(905, 905)
        self.assertTrue(self.access.is_owner(905))

    def test_admin_capability_overrides_are_audited_and_role_bounded(self):
        self.access.set_capability(900, 901, "fleet.manage", "deny")
        self.assertFalse(self.access.has_capability(901, "fleet.manage"))
        self.assertTrue(self.access.has_capability(901, "fleet.read"))
        self.access.set_capability(900, 901, "fleet.manage", None)
        self.assertTrue(self.access.has_capability(901, "fleet.manage"))
        self.access.grant_role(900, 903, "seller")
        with self.assertRaisesRegex(ValueError, "only to Admins"):
            self.access.set_capability(900, 903, "fleet.manage", "allow")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT action FROM staff_access_audit ORDER BY created_at, audit_id"
            ).fetchall()
        self.assertGreaterEqual(len(rows), 3)
        self.assertIn("capability_changed", [row[0] for row in rows])

    def test_revoked_staff_cannot_keep_assigned_cases(self):
        self.access.grant_role(900, 903, "seller")
        self.access.assign_customer(900, 903, 12345)
        self.access.revoke_role(900, 903)
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM staff_customer_assignments WHERE seller_telegram_id = 903 AND active = 1"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
