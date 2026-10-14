"""Activity navigation exercised through real dispatch with mocked Telegram I/O."""

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


class TelegramNavigationTest(unittest.TestCase):
    def setUp(self):
        staff_environment = patch.dict(
            os.environ,
            {
                "AURIX_STAFF_AUTHORIZATION_MODE": "legacy",
                "OWNER_TELEGRAM_IDS": "",
                "SELLER_TELEGRAM_IDS": "",
            },
        )
        staff_environment.start()
        self.addCleanup(staff_environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        self.db.initialize()
        self.outline = FakeOutline()
        self.commerce = CommerceService(
            CommerceDatabase(self.db.path),
            self.outline,
            Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.commerce.initialize()
        self.bot = TelegramBot(
            "synthetic-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
        )
        self.messages = {}
        self.calls = []
        self.next_id = 1
        self.fail_edit = False

        def request(method, payload):
            self.calls.append((method, copy.deepcopy(payload)))
            if method in {"sendMessage", "sendPhoto", "sendDocument"}:
                message_id = self.next_id
                self.next_id += 1
                self.messages[message_id] = copy.deepcopy(payload)
                return {"message_id": message_id}
            if method == "editMessageText":
                if self.fail_edit:
                    raise RuntimeError("message unavailable")
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

    def callback(self, data, message_id=None, actor=123):
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
        return max(self.messages)

    def buttons(self, message_id):
        return [
            b
            for row in self.messages[message_id]
            .get("reply_markup", {})
            .get("inline_keyboard", [])
            for b in row
        ]

    def click(self, message_id, label):
        button = next(b for b in self.buttons(message_id) if label in b["text"])
        self.callback(button["callback_data"], message_id)
        return max(self.messages)

    def financial_state(self):
        with self.db.connect() as connection:
            return {
                table: [
                    tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
                ]
                for table in (
                    "orders",
                    "payments",
                    "wallets",
                    "wallet_ledger",
                    "wallet_reservations",
                    "payment_evidence",
                    "subscriptions",
                )
            }

    def order(self):
        return self.commerce.create_order(123, "Test", "basic_50gb").order_id

    def test_home_migrates_keyboard_in_one_message_without_business_effects(self):
        before = self.financial_state()
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "text": "/start",
            }
        )
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.calls[0][1]["reply_markup"], {"remove_keyboard": True})
        self.assertTrue(self.buttons(1))
        self.assertNotIn("keyboard", self.messages[1]["reply_markup"])
        self.assertEqual(before, self.financial_state())
        self.assertFalse(self.outline.created)

    def test_new_activity_keeps_home_and_child_views_edit_same_island(self):
        self.callback("u:h:home")
        home = copy.deepcopy(self.messages[1])
        buy = self.click(1, "Buy VPN")
        count = len(self.messages)
        self.click(buy, "Paid plans")
        self.assertEqual(len(self.messages), count)
        self.click(buy, "Back")
        self.assertIn("Buy VPN", self.messages[buy]["text"])
        self.assertEqual(home, self.messages[1])

    def test_root_branch_back_returns_home_in_the_same_activity(self):
        self.callback("u:h:home")
        branch = self.click(1, "Buy VPN")
        message_count = len(self.messages)
        self.click(branch, "Back")
        self.assertEqual(len(self.messages), message_count)
        self.assertIn("Welcome to AuriX VPN", self.messages[branch]["text"])
        self.assertNotIn("Back", {b["text"] for b in self.buttons(branch)})

    def test_home_button_reuses_an_editable_activity(self):
        self.callback("u:h:home")
        branch = self.click(1, "Buy VPN")
        message_count = len(self.messages)
        self.click(branch, "Home")
        self.assertEqual(len(self.messages), message_count)
        self.assertIn("Welcome to AuriX VPN", self.messages[branch]["text"])

    def test_home_keeps_a_protected_wallet_record_unchanged(self):
        self.callback("u:h:home")
        wallet = self.click(1, "Wallet")
        wallet_message = copy.deepcopy(self.messages[wallet])
        self.click(wallet, "Home")
        self.assertEqual(wallet_message, self.messages[wallet])
        self.assertGreater(len(self.messages), 2)

    def test_plan_review_and_duplicate_commit_keep_single_order(self):
        self.callback("u:h:buy")
        self.click(1, "Paid plans")
        self.click(1, "3,000")
        self.assertEqual(self.commerce.list_user_orders(123), [])
        confirm = next(
            b["callback_data"] for b in self.buttons(1) if "Create order" in b["text"]
        )
        self.callback(confirm, 1)
        after = self.financial_state()
        self.callback(confirm, 1)
        self.assertEqual(len(self.commerce.list_user_orders(123)), 1)
        self.assertEqual(after, self.financial_state())

    def test_back_restores_order_filter_and_page(self):
        for _ in range(7):
            order_id = self.order()
            self.commerce.cancel_order(123, order_id)
        self.callback("u:h:orders")
        self.click(1, "Cancelled")
        self.click(1, "⏭")
        expected = self.messages[1]["text"]
        self.click(1, "Open #7")
        self.assertIn("Order ·", self.messages[1]["text"])
        self.click(1, "Back")
        self.assertEqual(expected, self.messages[1]["text"])

    def test_customer_summary_has_full_details_without_scrolling(self):
        order_id = self.order()
        self.callback("u:o:" + order_id)
        self.assertLess(len(self.messages[1]["text"].splitlines()), 10)
        self.assertNotIn("Provisioning:", self.messages[1]["text"])
        self.click(1, "Full details")
        self.assertIn("Provisioning:", self.messages[1]["text"])
        self.click(1, "Back")
        self.assertNotIn("Provisioning:", self.messages[1]["text"])

    def test_wallet_statement_survives_topup_navigation_and_back(self):
        self.callback("u:h:wallet")
        statement = copy.deepcopy(self.messages[1])
        before = self.financial_state()
        topup = self.click(1, "Top up")
        self.assertNotEqual(topup, 1)
        self.click(topup, "Other amount")
        self.click(topup, "Back")
        self.assertEqual(statement, self.messages[1])
        self.assertEqual(before, self.financial_state())

    def test_wallet_review_is_read_only_and_confirm_reserves_once(self):
        self.commerce.credit_wallet(123, 6000, "fixture-credit", 999)
        order_id = self.order()
        self.callback("u:o:" + order_id)
        before = self.financial_state()
        self.click(1, "Pay Wallet")
        self.assertEqual(before, self.financial_state())
        confirm = next(
            b["callback_data"] for b in self.buttons(1) if "Confirm wallet" in b["text"]
        )
        self.callback(confirm, 1)
        record = max(self.messages)
        after = self.financial_state()
        self.callback(confirm, 1)
        self.assertEqual(after, self.financial_state())
        saved = copy.deepcopy(self.messages[record])
        self.click(record, "Review order")
        self.assertEqual(saved, self.messages[record])

    def test_expiry_restart_and_other_actor_cannot_commit_old_activity(self):
        self.callback("u:h:buy")
        self.click(1, "Paid plans")
        self.click(1, "3,000")
        data = next(
            b["callback_data"] for b in self.buttons(1) if "Create order" in b["text"]
        )
        before = self.financial_state()
        self.callback(data, 1, actor=456)
        self.bot._activities.clear()
        self.callback(data, 1)
        self.assertEqual(before, self.financial_state())

    def test_staff_role_is_rechecked_after_menu_was_opened(self):
        self.callback("u:h:staff", actor=999)
        self.click_as_staff(1, "Sales")
        button = next(
            b["callback_data"] for b in self.buttons(1) if "Pending" in b["text"]
        )
        self.bot.admin_ids.clear()
        before = self.financial_state()
        self.callback(button, 1, actor=999)
        self.assertEqual(before, self.financial_state())
        self.assertEqual(self.messages[1]["text"], self.bot.UNKNOWN_ACTION_TEXT)

    def click_as_staff(self, message_id, label):
        data = next(
            b["callback_data"] for b in self.buttons(message_id) if label in b["text"]
        )
        return self.callback(data, message_id, actor=999)

    def test_close_only_retires_buttons_never_deletes_record(self):
        self.callback("u:h:wallet")
        text = self.messages[1]["text"]
        before = self.financial_state()
        self.callback("n:close", 1)
        self.assertEqual(self.messages[1]["text"], text)
        self.assertFalse(any(method == "deleteMessage" for method, _ in self.calls))
        self.assertEqual(before, self.financial_state())

    def test_edit_failure_recovers_view_without_replaying_financial_action(self):
        order_id = self.order()
        self.callback("u:o:" + order_id)
        before = self.financial_state()
        self.fail_edit = True
        self.click(1, "Full details")
        self.assertEqual(before, self.financial_state())
        self.assertIn("Provisioning:", self.messages[max(self.messages)]["text"])

    def test_custom_input_is_bound_to_screen_and_back_cancels_only_prompt(self):
        self.callback("u:h:wallet")
        activity_id = self.click(1, "Top up")
        self.click(activity_id, "Other amount")
        self.click(activity_id, "Back")
        before = self.financial_state()
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "text": "7500",
                "reply_to_message": {"message_id": activity_id},
            }
        )
        self.assertEqual(before, self.financial_state())

    def test_receipt_reply_targets_its_order_not_a_newer_order(self):
        old = self.order()
        self.callback("u:o:" + old)
        self.commerce.cancel_order(123, old)
        newer = self.order()
        received = []
        self.bot._handle_receipt = lambda message, *_: received.append(
            message["caption"]
        )
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "photo": [{"file_id": "synthetic"}],
                "reply_to_message": {"message_id": 1},
            }
        )
        self.assertEqual(received, ["/paid " + old])
        self.assertNotEqual(old, newer)

    def test_receipt_reply_to_retained_payment_message_keeps_order_binding(self):
        order_id = self.order()
        self.callback("u:o:" + order_id)
        activity = next(iter(self.bot._activities.values()))
        with self.bot._activity_context(activity, preserve=True):
            self.bot.send(123, "Payment acknowledgement")
        self.assertNotEqual(activity["message_id"], 1)
        received = []
        self.bot._handle_receipt = lambda message, *_: received.append(
            message["caption"]
        )
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "photo": [{"file_id": "synthetic"}],
                "reply_to_message": {"message_id": 1},
            }
        )
        self.assertEqual(received, ["/paid " + order_id])

    def test_expired_or_restarted_receipt_reply_never_uses_newest_order(self):
        self.order()
        self.callback("u:h:orders")
        received = []
        self.bot._handle_receipt = lambda message, *_: received.append(message)
        message = {
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 123},
            "photo": [{"file_id": "synthetic"}],
            "reply_to_message": {"message_id": 1},
        }
        for restart in (False, True):
            if restart:
                self.bot._activities.clear()
            else:
                for a in self.bot._activities.values():
                    a["updated_at"] -= self.bot.NAVIGATION_TTL + 1
            self.bot.handle(message)
        self.assertEqual(received, [])
        self.assertIn("/paid <order-id>", self.messages[max(self.messages)]["text"])

    def test_explicit_receipt_caption_takes_precedence_over_reply(self):
        old = self.order()
        self.callback("u:o:" + old)
        self.commerce.cancel_order(123, old)
        newer = self.order()
        received = []
        self.bot._handle_receipt = lambda message, *_: received.append(
            message["caption"]
        )
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "photo": [{"file_id": "synthetic"}],
                "caption": "/paid " + newer,
                "reply_to_message": {"message_id": 1},
            }
        )
        self.assertEqual(received, ["/paid " + newer])

    def test_home_buttons_recover_when_markup_edit_fails(self):
        request = self.bot.request

        def failed_markup(method, payload):
            if method == "editMessageReplyMarkup":
                raise RuntimeError("Telegram unavailable")
            return request(method, payload)

        self.bot.request = failed_markup
        self.callback("u:h:home")
        self.assertTrue(self.buttons(max(self.messages)))

    def test_key_is_revealed_only_in_owned_detail_view(self):
        self.bot.service.claim(123, "Test")
        self.callback("u:h:vpn")
        self.assertNotIn("ss://secret", self.messages[1]["text"])
        self.click(1, "Open #")
        self.assertIn("ss://secret", self.messages[1]["text"])
        self.click(1, "Back")
        self.assertNotIn("ss://secret", self.messages[1]["text"])

    def test_all_history_browses_beyond_previous_fifty_record_cap(self):
        for _ in range(52):
            self.commerce.cancel_order(123, self.order())
        self.callback("u:h:orders")
        self.click(1, "Full order history")
        for _ in range(17):
            self.click(1, "Next")
        self.assertIn("Page 18", self.messages[1]["text"])
        self.assertTrue(any("cancelled" in b["text"] for b in self.buttons(1)))
        self.assertEqual(self.commerce.list_user_orders(456, offset=50), [])

    def test_replied_custom_topup_creates_order_without_crediting_wallet(self):
        self.callback("u:h:wallet")
        screen = self.click(1, "Top up")
        self.click(screen, "Other amount")
        before = self.commerce.wallet_balance(123)
        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "text": "7500",
                "reply_to_message": {"message_id": screen},
            }
        )
        orders = self.commerce.list_user_orders(123)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["amount_minor"], 7500)
        self.assertEqual(orders[0]["stage"], "awaiting_payment")
        self.assertEqual(self.commerce.wallet_balance(123), before)

    def test_staff_rejection_keeps_existing_single_use_confirmation(self):
        order_id = self.order()
        self.callback("u:h:sales", actor=999)
        self.click_as_staff(1, "Pending")
        self.click_as_staff(1, order_id[:8])
        self.click_as_staff(1, "Reject…")
        self.assertEqual(
            self.commerce.order_detail(order_id, 999, is_admin=True)["status"],
            "awaiting_payment",
        )
        confirm = next(
            b["callback_data"] for b in self.buttons(1) if "Confirm" in b["text"]
        )
        self.callback(confirm, 1, actor=999)
        after = self.financial_state()
        self.callback(confirm, 1, actor=999)
        self.assertEqual(after, self.financial_state())
        self.assertEqual(
            self.commerce.order_detail(order_id, 999, is_admin=True)["status"],
            "rejected",
        )

    def test_interleaved_order_activities_keep_their_entity(self):
        first = self.order()
        self.commerce.cancel_order(123, first)
        second = self.order()
        self.callback("u:o:" + first)
        self.callback("u:o:" + second)
        self.click(1, "Full details")
        self.assertIn(first, self.messages[1]["text"])
        self.click(2, "Full details")
        self.assertIn(second, self.messages[2]["text"])

    def test_key_refresh_rechecks_status_before_revealing(self):
        self.bot.service.claim(123, "Test")
        self.callback("u:h:vpn")
        self.click(1, "Open #")
        snapshot = self.bot._collect_customer_vpn_state(123)
        snapshot["all_items"][0]["status"] = "revoked"
        self.bot._collect_customer_vpn_state = lambda _: snapshot
        self.click(1, "Refresh")
        self.assertNotIn("ss://secret", self.messages[1]["text"])

    def test_expired_activity_cannot_change_payment_records(self):
        self.callback("u:o:" + self.order())
        data = next(
            b["callback_data"] for b in self.buttons(1) if "Pay Wallet" in b["text"]
        )
        activity = next(iter(self.bot._activities.values()))
        activity["updated_at"] -= self.bot.NAVIGATION_TTL + 1
        before = self.financial_state()
        self.callback(data, 1)
        self.assertEqual(before, self.financial_state())
        self.assertIn("expired", self.messages[max(self.messages)]["text"])

    def test_payment_button_reloads_review_pending_state(self):
        order_id = self.order()
        self.callback("u:o:" + order_id)
        self.commerce.submit_payment(123, order_id, "manual", "fixture-reference")
        self.click(1, "Send Receipt")
        self.assertIn("Do not pay again", self.messages[1]["text"])
        self.assertFalse(any("KBZPay" in b["text"] for b in self.buttons(1)))

    def test_staff_next_pending_opens_another_owned_review_target(self):
        first = self.order()
        self.commerce.create_order(456, "Other", "basic_50gb")
        self.callback("u:h:sales", actor=999)
        self.click_as_staff(1, "Pending")
        self.click_as_staff(1, first[:8])
        self.assertIn(first, self.messages[1]["text"])
        self.click_as_staff(1, "Next pending")
        self.assertNotIn(first, self.messages[1]["text"])
        self.assertIn("Customer: 456", self.messages[1]["text"])


if __name__ == "__main__":
    unittest.main()
