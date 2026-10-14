"""Role workspaces, inline forms, and live assignment checks."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import ClaimService, Database, TelegramBot
from commerce import CommerceDatabase, CommerceService
from test_app import FakeOutline
from aurix_vpn.staff_access import StaffAccessService


class StaffNavigationTest(unittest.TestCase):
    OWNER = 900
    ADMIN = 901
    SELLER = 902
    CUSTOMER = 12345

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "staff-navigation.db"
        self.db = Database(path)
        self.db.initialize()
        self.outline = FakeOutline()
        self.commerce_db = CommerceDatabase(path)
        self.commerce = CommerceService(
            self.commerce_db,
            self.outline,
            Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.commerce.initialize()
        self.access = StaffAccessService(
            self.commerce_db,
            admin_ids={self.ADMIN},
            owner_ids={self.OWNER},
            mode="roles",
        )
        self.access.initialize()
        self.bot = TelegramBot(
            "synthetic-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {self.ADMIN},
            set(),
            staff_access=self.access,
        )
        self.messages = {}
        self.calls = []
        self.next_id = 1

        def request(method, payload):
            self.calls.append((method, copy.deepcopy(payload)))
            if method in {"sendMessage", "sendPhoto", "sendDocument"}:
                message_id = self.next_id
                self.next_id += 1
                self.messages[message_id] = copy.deepcopy(payload)
                return {"message_id": message_id}
            if method == "editMessageText":
                self.messages[payload["message_id"]].update(copy.deepcopy(payload))
            elif method == "editMessageReplyMarkup":
                self.messages.setdefault(payload["message_id"], {}).update(
                    copy.deepcopy(payload)
                )
            return True

        self.bot.request = request

    def tearDown(self):
        self.bot._http.clear()
        self.tmp.cleanup()

    def callback(self, data, message_id=None, actor=CUSTOMER):
        self.bot.handle_callback(
            {
                "id": "synthetic-query",
                "from": {"id": actor, "first_name": "Test"},
                "message": {
                    "chat": {"id": actor, "type": "private"},
                    "message_id": message_id,
                },
                "data": data,
            }
        )

    def buttons(self, message_id):
        return [
            button
            for row in self.messages[message_id]
            .get("reply_markup", {})
            .get("inline_keyboard", [])
            for button in row
        ]

    def click(self, message_id, label, actor):
        button = next(
            item for item in self.buttons(message_id) if label in item["text"]
        )
        self.callback(button["callback_data"], message_id, actor)

    def reply(self, message_id, actor, text):
        self.bot.handle(
            {
                "chat": {"id": actor, "type": "private"},
                "from": {"id": actor, "first_name": "Test"},
                "reply_to_message": {"message_id": message_id},
                "text": text,
            }
        )

    def test_admin_and_owner_have_distinct_staff_workspaces(self):
        self.callback("u:h:staff", actor=self.ADMIN)
        admin_screen = self.messages[max(self.messages)]
        admin_text = admin_screen["text"]
        self.assertIn("Admin workspace", admin_text)
        self.assertFalse(
            any(
                "Staff roles" in item["text"]
                for item in self.buttons(max(self.messages))
            )
        )

        self.callback("u:h:staff", actor=self.OWNER)
        owner_screen = self.messages[max(self.messages)]
        self.assertIn("Owner workspace", owner_screen["text"])
        self.assertTrue(
            any(
                "Staff roles" in item["text"]
                for item in self.buttons(max(self.messages))
            )
        )

    def test_bot_loads_explicit_owner_environment_without_promoting_other_admins(self):
        with patch.dict(
            os.environ,
            {
                "OWNER_TELEGRAM_IDS": str(self.OWNER),
                "SELLER_TELEGRAM_IDS": "",
                "AURIX_STAFF_AUTHORIZATION_MODE": "roles",
            },
        ):
            bot = TelegramBot(
                "synthetic-token",
                ClaimService(self.db, self.outline),
                self.commerce,
                {self.OWNER, self.ADMIN},
                set(),
            )
        try:
            self.assertEqual(bot._staff_role(self.OWNER), "owner")
            self.assertEqual(bot._staff_role(self.ADMIN), "admin")
            self.assertTrue(bot._is_admin(self.ADMIN))
        finally:
            bot._http.clear()

    def test_owner_role_change_requires_inline_confirmation_and_is_durable(self):
        self.callback("u:h:staff", actor=self.OWNER)
        message_id = max(self.messages)
        self.click(message_id, "Staff roles", self.OWNER)
        self.click(message_id, "Grant or change role", self.OWNER)
        self.reply(message_id, self.OWNER, f"{self.SELLER} seller")
        self.assertIsNone(self.access.role_for(self.SELLER))
        self.assertTrue(
            any("Confirm change" in item["text"] for item in self.buttons(message_id))
        )
        self.click(message_id, "Confirm change", self.OWNER)
        self.assertEqual(self.access.role_for(self.SELLER), "seller")
        with self.commerce_db.connect() as connection:
            audit = connection.execute(
                "SELECT action FROM staff_access_audit WHERE target_id = ? ORDER BY created_at DESC LIMIT 1",
                (self.SELLER,),
            ).fetchone()
        self.assertEqual(audit[0], "role_granted")

    def test_back_cancels_an_unconfirmed_role_change(self):
        self.callback("u:h:staff", actor=self.OWNER)
        message_id = max(self.messages)
        self.click(message_id, "Staff roles", self.OWNER)
        self.click(message_id, "Grant or change role", self.OWNER)
        self.reply(message_id, self.OWNER, f"{self.SELLER} seller")
        self.click(message_id, "Back", self.OWNER)
        self.assertIsNone(self.access.role_for(self.SELLER))
        self.assertIn("Staff roles & customer cases", self.messages[message_id]["text"])

    def test_seller_sees_only_assigned_order_summary_without_sensitive_records(self):
        order = self.commerce.create_order(self.CUSTOMER, "Customer", "basic_50gb")
        self.commerce.submit_payment(
            self.CUSTOMER,
            order.order_id,
            "kbzpay",
            "PRIVATE-REFERENCE-DO-NOT-LEAK",
        )
        self.access.grant_role(self.OWNER, self.SELLER, "seller")
        self.access.assign_customer(self.OWNER, self.SELLER, self.CUSTOMER)
        self.callback("u:h:staff", actor=self.SELLER)
        message_id = max(self.messages)
        self.assertIn("Seller workspace", self.messages[message_id]["text"])
        self.click(message_id, "Assigned customers", self.SELLER)
        self.click(message_id, str(self.CUSTOMER), self.SELLER)
        self.click(message_id, "50 GB", self.SELLER)
        screen = self.messages[message_id]["text"]
        self.assertIn("Assigned order review", screen)
        self.assertIn("50 GB", screen)
        self.assertIn("review_pending", screen)
        self.assertNotIn("PRIVATE-REFERENCE-DO-NOT-LEAK", screen)
        self.assertNotIn("access key", screen.lower())
        self.assertNotIn("wallet balance", screen.lower())

    def test_stale_seller_button_is_rejected_after_assignment_revocation(self):
        self.access.grant_role(self.OWNER, self.SELLER, "seller")
        self.access.assign_customer(self.OWNER, self.SELLER, self.CUSTOMER)
        self.callback("u:h:staff", actor=self.SELLER)
        message_id = max(self.messages)
        self.click(message_id, "Assigned customers", self.SELLER)
        customer_button = next(
            item
            for item in self.buttons(message_id)
            if str(self.CUSTOMER) in item["text"]
        )
        self.access.unassign_customer(self.OWNER, self.SELLER, self.CUSTOMER)
        self.callback(customer_button["callback_data"], message_id, self.SELLER)
        self.assertEqual(
            self.messages[message_id]["text"], self.bot.UNKNOWN_ACTION_TEXT
        )

    def test_owner_confirmation_expires_when_actor_loses_owner_role(self):
        self.callback("u:h:staff", actor=self.OWNER)
        message_id = max(self.messages)
        self.click(message_id, "Staff roles", self.OWNER)
        self.click(message_id, "Grant or change role", self.OWNER)
        self.reply(message_id, self.OWNER, f"{self.SELLER} seller")
        confirm = next(
            item
            for item in self.buttons(message_id)
            if "Confirm change" in item["text"]
        )
        self.access.grant_role(self.OWNER, 905, "owner")
        self.access.owner_ids = frozenset()
        self.access.revoke_role(905, self.OWNER)
        self.callback(confirm["callback_data"], message_id, self.OWNER)
        self.assertIsNone(self.access.role_for(self.SELLER))

    def test_owner_can_review_staff_access_audit_inside_the_workspace(self):
        self.access.grant_role(self.OWNER, self.SELLER, "seller")
        self.callback("u:h:staff", actor=self.OWNER)
        message_id = max(self.messages)
        self.click(message_id, "Staff roles", self.OWNER)
        self.click(message_id, "Access changes", self.OWNER)
        self.assertIn("Staff access history", self.messages[message_id]["text"])
        self.assertIn("role_granted", self.messages[message_id]["text"])


if __name__ == "__main__":
    unittest.main()
