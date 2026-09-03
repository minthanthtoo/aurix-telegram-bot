import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image, ImageDraw

from app import Database
from commerce import (
    CommerceDatabase,
    CommerceError,
    CommerceService,
    JOB_RETRY_DELAY,
    PostgresCommerceDatabase,
    _PostgresConnection,
)
from persistence import open_sqlite_connection


UTC = timezone.utc


class FakePaidOutline:
    def __init__(self):
        self.keys = {}
        self.created = []
        self.deleted = []
        self.limits = []
        self.fail_create = False
        self.transfer = {}

    def list_keys(self):
        return {"accessKeys": list(self.keys.values())}

    def create_key(self, name, limit_bytes):
        if self.fail_create:
            raise CommerceError("Outline unavailable")
        key_id = str(len(self.keys) + 1)
        key = {
            "id": key_id,
            "name": name,
            "accessUrl": f"ss://paid-{key_id}",
        }
        self.keys[key_id] = key
        self.created.append((name, limit_bytes, key))
        return dict(key)

    def set_data_limit(self, key_id, limit_bytes):
        self.limits.append((key_id, limit_bytes))

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": self.transfer}

    def server_info(self):
        return {"version": "fake-outline-1"}

    def delete_key(self, key_id):
        self.deleted.append(key_id)
        self.keys.pop(str(key_id), None)

    def get_key(self, key_id):
        key = self.keys.get(str(key_id))
        return dict(key) if key is not None else None

    def add_key(self, key_id, name, access_url=None):
        self.keys[str(key_id)] = {
            "id": str(key_id),
            "name": name,
            "accessUrl": access_url or f"ss://existing-{key_id}",
        }


class NamedOutlinePool:
    def __init__(self, *server_ids):
        self._server_ids = tuple(server_ids)
        self.default_server_id = self._server_ids[0]

    def server_ids(self):
        return self._server_ids


class CommerceServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tmp.name) / "bot.db")
        self.outline = FakePaidOutline()
        # Existing fixture exercises the pre-screenshot migration path. Public
        # deployments leave this option disabled (the default).
        self.service = CommerceService(
            self.database,
            self.outline,
            Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.service.initialize()
        self.now = datetime(2026, 8, 27, 3, 7, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def _paid_order(self, telegram_id=123):
        order = self.service.create_order(telegram_id, "Min", "basic_50gb", self.now)
        self.service.submit_payment(
            telegram_id, order.order_id, "manual", f"ref-{order.order_id}", self.now
        )
        return order

    @staticmethod
    def _receipt_image(quality: int = 95) -> bytes:
        image = Image.new("RGB", (320, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 210, 65), fill="black")
        draw.rectangle((80, 100, 300, 145), outline="black", width=8)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality)
        return output.getvalue()

    def test_existing_database_adds_reference_column_before_index(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        with open_sqlite_connection(legacy_path) as connection:
            connection.execute(
                """CREATE TABLE payments (
                       id TEXT PRIMARY KEY,
                       provider TEXT NOT NULL,
                       provider_reference TEXT NOT NULL
                   )"""
            )
            connection.execute("INSERT INTO payments VALUES ('payment-1', 'Manual', ' Tx 123 ')")

        CommerceDatabase(legacy_path).initialize()

        with open_sqlite_connection(legacy_path) as connection:
            normalized = connection.execute(
                "SELECT normalized_reference FROM payments WHERE id = 'payment-1'"
            ).fetchone()[0]
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(payments)")}
        self.assertEqual(normalized, "tx123")
        self.assertIn("payments_reference_lookup", indexes)

    def test_server_registration_rejects_provider_id_relabeling(self):
        legacy = CommerceService(
            self.database,
            NamedOutlinePool("primary"),
            Fernet.generate_key(),
        )
        legacy.register_outline_servers(
            {"primary": "Primary"},
            provider_resource_ids={"primary": "595626749"},
        )
        fleet = CommerceService(
            self.database,
            NamedOutlinePool("sg-a", "sg-b"),
            Fernet.generate_key(),
        )

        with self.assertRaisesRegex(CommerceError, "keep that stable server ID"):
            fleet.register_outline_servers(
                {"sg-a": "Singapore A", "sg-b": "Singapore B"},
                provider_resource_ids={"sg-a": "595626749", "sg-b": "595616487"},
            )

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT server_id, provider_resource_id FROM outline_servers ORDER BY server_id"
            ).fetchall()
        self.assertEqual([(row["server_id"], row["provider_resource_id"]) for row in rows], [
            ("primary", "595626749"),
        ])

    def test_existing_database_adds_receipt_media_type(self):
        legacy_path = Path(self.tmp.name) / "legacy-receipts.db"
        database = CommerceDatabase(legacy_path)
        database.initialize()
        with open_sqlite_connection(legacy_path) as connection:
            connection.execute("ALTER TABLE payment_evidence DROP COLUMN telegram_media_type")

        database.initialize()

        with open_sqlite_connection(legacy_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(payment_evidence)")}
        self.assertIn("telegram_media_type", columns)
        self.assertIn("storage_bucket", columns)
        self.assertIn("storage_path", columns)
        self.assertIn("storage_status", columns)
        self.assertIn("storage_error", columns)
        self.assertIn("stored_at", columns)

    def test_reencoded_cross_order_receipt_is_caught_before_model_processing(self):
        first = self.service.create_order(130, "First", "basic_50gb", self.now)
        original = self._receipt_image(quality=95)
        self.service.submit_receipt(
            130,
            first.order_id,
            "kbzpay",
            "receipt-first",
            "receipt-first-unique",
            original,
            "image/jpeg",
            now=self.now,
        )
        second = self.service.create_order(131, "Second", "basic_50gb", self.now)
        recompressed = self._receipt_image(quality=45)

        self.assertEqual(
            self.service.receipt_duplicate_status(
                131,
                second.order_id,
                recompressed,
                "receipt-second-unique",
                provider="kbzpay",
            ),
            "possible_duplicate",
        )
        submitted = self.service.submit_receipt(
            131,
            second.order_id,
            "kbzpay",
            "receipt-second",
            "receipt-second-unique",
            recompressed,
            "image/jpeg",
            now=self.now,
        )
        self.assertEqual(submitted["extraction_status"], "needs_review")
        self.assertIn("duplicate_image_candidate", submitted["flags"])

    def test_short_lived_interaction_state_survives_reopen_and_expires(self):
        future = "2026-08-27T04:07:00+00:00"
        self.database.save_interaction_state(
            123,
            "customer_input",
            {"action": "topup_amount"},
            future,
        )
        reopened = CommerceDatabase(self.database.path)
        self.assertEqual(
            reopened.load_interaction_state(123, "customer_input", "2026-08-27T03:08:00+00:00"),
            {"action": "topup_amount"},
        )
        self.assertIsNone(
            reopened.load_interaction_state(123, "customer_input", "2026-08-27T04:07:00+00:00")
        )
        self.assertEqual(
            reopened.prune_interaction_states("2026-08-27T04:07:00+00:00"), 1
        )

    def test_interaction_state_rejects_oversized_payload(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            self.database.save_interaction_state(
                123,
                "customer_input",
                {"value": "x" * 5000},
                "2026-08-27T04:07:00+00:00",
            )

    def test_payment_is_required_and_approval_is_idempotent(self):
        order = self.service.create_order(123, "Min", "basic_50gb", self.now)
        with self.assertRaises(CommerceError):
            self.service.approve_order(order.order_id, 999, self.now)

        self.assertEqual(
            self.service.submit_payment(123, order.order_id, "manual", "abc123", self.now),
            "submitted",
        )
        self.assertEqual(
            self.service.submit_payment(123, order.order_id, "manual", "abc123", self.now),
            "already_submitted",
        )
        approved = self.service.approve_order(order.order_id, 999, self.now)
        repeated = self.service.approve_order(order.order_id, 999, self.now + timedelta(minutes=1))
        self.assertEqual(approved.subscription_id, repeated.subscription_id)
        self.assertEqual(repeated.status, "already_approved")

        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provisioning_jobs").fetchone()[0], 1
            )

    def test_create_order_reuses_existing_open_order(self):
        first = self.service.create_order(123, "Min", "basic_50gb", self.now)
        second = self.service.create_order(
            123, "Min", "basic_50gb", self.now + timedelta(minutes=1)
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.order_id, second.order_id)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE telegram_id = 123"
                ).fetchone()[0],
                1,
            )

    def test_different_plan_requires_explicit_replacement(self):
        first = self.service.create_order(123, "Min", "basic_50gb", self.now)
        conflict = self.service.create_order(
            123, "Min", "standard_100gb", self.now + timedelta(minutes=1)
        )
        self.assertFalse(conflict.created)
        self.assertTrue(conflict.plan_conflict)
        self.assertEqual(conflict.order_id, first.order_id)
        replacement = self.service.replace_open_order(
            123, "Min", "standard_100gb", self.now + timedelta(minutes=2)
        )
        self.assertNotEqual(replacement.order_id, first.order_id)
        self.assertEqual(self.service.order_detail(first.order_id, 123)["stage"], "cancelled")
        self.assertEqual(
            self.service.order_detail(replacement.order_id, 123)["plan_code"], "standard_100gb"
        )

    def test_plan_replacement_is_blocked_after_payment_activity(self):
        first = self.service.create_order(124, "Min", "basic_50gb", self.now)
        self.service.submit_payment(124, first.order_id, "manual", "replace-block", self.now)
        with self.assertRaises(CommerceError):
            self.service.replace_open_order(124, "Min", "standard_100gb", self.now)

    def test_admin_can_list_and_requeue_terminal_worker_failure(self):
        order = self._paid_order(124)
        self.service.approve_order(order.order_id, 999, self.now)
        with self.service.database.connect() as connection:
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'failed', attempts = 8, last_error = 'Outline unavailable' WHERE operation = 'provision'"
            )
        failures = self.service.failed_jobs()
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["order_id"], order.order_id)
        self.assertEqual(
            self.service.retry_failed_job(order.order_id, 999, self.now),
            "provision",
        )
        failures = self.service.failed_jobs()
        self.assertEqual(failures, [])
        with self.service.database.connect() as connection:
            job = connection.execute(
                "SELECT status, attempts, last_error FROM provisioning_jobs WHERE operation = 'provision'"
            ).fetchone()
        self.assertEqual(tuple(job), ("pending", 0, None))

    def test_paid_duration_starts_when_outline_activation_succeeds(self):
        order = self._paid_order(125)
        self.service.approve_order(order.order_id, 999, self.now)
        activation = self.now + timedelta(hours=3)
        self.assertEqual(self.service.process_jobs(activation), 1)
        with self.service.database.connect() as connection:
            subscription = connection.execute(
                "SELECT starts_at, expires_at, activated_at, status FROM subscriptions WHERE order_id = ?",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(subscription["status"], "active")
        self.assertEqual(subscription["activated_at"], activation.isoformat())
        self.assertEqual(subscription["starts_at"], activation.isoformat())
        self.assertEqual(
            subscription["expires_at"],
            (activation + timedelta(days=30)).isoformat(),
        )

    def test_refund_is_idempotent_wallet_reversal_and_access_revoke(self):
        order = self._paid_order(126)
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        self.assertEqual(
            self.service.refund_order(order.order_id, 999, "customer request", self.now), "refunded"
        )
        self.assertEqual(
            self.service.refund_order(order.order_id, 999, "customer request", self.now),
            "already_refunded",
        )
        self.assertEqual(self.service.wallet_balance(126), 3000)
        detail = self.service.order_detail(order.order_id, 126)
        self.assertEqual(detail["refund_status"], "refunded")
        self.assertEqual(detail["subscription_status"], "revoked")
        self.assertEqual(self.service.list_user_orders(126)[0]["stage"], "refunded")
        with self.service.database.connect() as connection:
            events = connection.execute(
                "SELECT kind, amount_minor FROM wallet_ledger WHERE telegram_id = 126"
            ).fetchall()
            payment = connection.execute(
                "SELECT status FROM payments WHERE order_id = ?", (order.order_id,)
            ).fetchone()
        self.assertEqual(
            sorted((row["kind"], row["amount_minor"]) for row in events),
            [("capture", 3000), ("credit", 3000), ("reserve", 3000), ("reversal", 3000)],
        )
        self.assertEqual(payment["status"], "refunded")

    def test_failed_notification_dead_letters_and_is_reported(self):
        order = self._paid_order(127)
        self.service.reject_order(order.order_id, 999, self.now)
        notification = self.service.pending_notifications(self.now)[0]

        for attempt in range(8):
            self.service.mark_notification_failed(
                notification["id"], self.now + timedelta(minutes=attempt)
            )

        self.assertEqual(self.service.pending_notifications(self.now + timedelta(days=1)), [])
        self.assertEqual(
            self.service.consistency_report(self.now + timedelta(days=1))["dead_notifications"],
            1,
        )

    def test_user_usage_reports_only_owned_paid_key(self):
        order = self._paid_order(123)
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        key_id = self.outline.created[0][2]["id"]
        used = 12 * 1024**3
        self.outline.transfer[key_id] = used
        usage = self.service.user_usage(123, self.outline.transfer)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["tier"], "50 GB")
        self.assertEqual(usage[0]["used_bytes"], used)
        self.assertEqual(usage[0]["remaining_bytes"], 50_000_000_000 - used)
        self.assertEqual(self.service.user_usage(456, self.outline.transfer), [])

    def test_user_and_admin_can_track_order_review_state(self):
        order = self.service.create_order(123, "Min", "basic_50gb", self.now)
        self.service.submit_receipt(
            123,
            order.order_id,
            "manual",
            "track-file",
            "track-unique",
            b"track-receipt",
            "image/jpeg",
            None,
            self.now,
        )
        history = self.service.list_user_orders(123)
        self.assertEqual(history[0]["id"], order.order_id)
        self.assertEqual(history[0]["receipt_status"], "pending")
        self.assertIsNotNone(self.service.order_detail(order.order_id, 123))
        self.assertIsNone(self.service.order_detail(order.order_id, 456))
        self.assertIsNotNone(self.service.order_detail(order.order_id, 999, is_admin=True))

    def test_reconcile_cancels_only_empty_historical_duplicates(self):
        order = self.service.create_order(123, "Min", "basic_50gb", self.now)
        self.service.submit_receipt(
            123,
            order.order_id,
            "manual",
            "keeper-file",
            "keeper-unique",
            b"keeper-evidence",
            "image/jpeg",
            None,
            self.now,
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   SELECT 'empty-duplicate', telegram_id, plan_code, amount_minor,
                          currency, plan_name, quota_bytes_snapshot,
                          duration_days_snapshot, 'awaiting_payment', ?
                   FROM orders WHERE id = ?""",
                ((self.now + timedelta(minutes=1)).isoformat(), order.order_id),
            )
        result = self.service.reconcile_duplicate_open_orders()
        self.assertEqual(result, {"cancelled": 1, "manual_conflicts": 0})
        with self.database.connect() as connection:
            statuses = dict(connection.execute("SELECT id, status FROM orders").fetchall())
        self.assertEqual(statuses[order.order_id], "payment_submitted")
        self.assertEqual(statuses["empty-duplicate"], "cancelled")

    def test_payment_reference_is_unique_and_rejected_orders_are_closed(self):
        first = self.service.create_order(123, "Min", "basic_50gb", self.now)
        second = self.service.create_order(456, "Other", "basic_50gb", self.now)
        self.service.submit_payment(123, first.order_id, "manual", "same-ref", self.now)
        with self.assertRaises(CommerceError):
            self.service.submit_payment(456, second.order_id, "manual", "same-ref", self.now)

        rejected = self.service.create_order(789, "Closed", "basic_50gb", self.now)
        self.service.submit_payment(789, rejected.order_id, "manual", "closed-ref", self.now)
        self.assertEqual(self.service.reject_order(rejected.order_id, 999, self.now), "rejected")
        self.assertEqual(
            self.service.pending_notifications(self.now)[0]["kind"], "payment_rejected"
        )
        with self.assertRaises(CommerceError):
            self.service.submit_payment(789, rejected.order_id, "manual", "late-ref", self.now)

    def test_worker_provisions_once_and_queues_a_deduplicated_notification(self):
        order = self._paid_order()
        approval = self.service.approve_order(order.order_id, 999, self.now)

        self.assertEqual(self.service.process_jobs(self.now), 1)
        self.assertEqual(len(self.outline.created), 1)
        self.assertTrue(self.outline.created[0][0].startswith("123-PAID50GB-30day-202608270307-"))
        subscription = self.service.user_vpn(123)
        self.assertEqual(subscription["status"], "active")
        self.assertEqual(subscription["access_url"], "ss://paid-1")
        self.assertEqual(len(self.service.pending_notifications(self.now)), 1)
        with self.database.connect() as connection:
            stored_url = connection.execute("SELECT access_url FROM paid_vpn_keys").fetchone()[0]
            stored_text = connection.execute("SELECT text FROM notifications").fetchone()[0]
        self.assertNotIn("ss://paid-1", stored_url)
        self.assertNotIn("ss://paid-1", stored_text)
        self.assertEqual(self.service.process_jobs(self.now), 0)

        # Re-running the worker or approval cannot create a second key/subscription.
        repeated = self.service.approve_order(order.order_id, 999, self.now)
        self.assertEqual(repeated.subscription_id, approval.subscription_id)
        self.assertEqual(len(self.outline.created), 1)

    def test_paid_key_name_prefers_tracked_username(self):
        order = self.service.create_order(321, "Min", "basic_50gb", self.now, username="@min_vpn")
        self.service.submit_payment(321, order.order_id, "manual", "username-name-ref", self.now)
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        self.assertTrue(
            self.outline.created[0][0].startswith("min_vpn-PAID50GB-30day-202608270307-")
        )

    def test_user_can_buy_multiple_paid_keys(self):
        first = self._paid_order()
        self.service.approve_order(first.order_id, 999, self.now)
        self.assertEqual(self.service.process_jobs(self.now), 1)

        second = self.service.create_order(123, "Test User", "basic_50gb", self.now)
        self.service.submit_payment(123, second.order_id, "manual", "second-ref", self.now)
        self.service.approve_order(second.order_id, 999, self.now)
        self.assertEqual(self.service.process_jobs(self.now), 1)

        self.assertEqual(len(self.outline.created), 2)
        self.assertNotEqual(self.outline.created[0][0], self.outline.created[1][0])
        subscriptions = self.service.user_vpns(123)
        self.assertEqual(
            len([item for item in subscriptions if item["key_status"] == "active"]),
            2,
        )
        subscription_id = subscriptions[0]["subscription_id"]
        self.assertEqual(
            self.service.user_vpn_detail(123, subscription_id)["subscription_id"],
            subscription_id,
        )
        self.assertIsNone(self.service.user_vpn_detail(999, subscription_id))

    def test_worker_reconciles_a_remote_key_after_a_lost_local_response(self):
        order = self._paid_order()
        approval = self.service.approve_order(order.order_id, 999, self.now)
        self.outline.add_key("remote-7", f"aurix-sub-{approval.subscription_id}", "ss://recovered")

        self.assertEqual(self.service.process_jobs(self.now), 1)
        self.assertEqual(self.outline.created, [])
        self.assertEqual(self.service.user_vpn(123)["access_url"], "ss://recovered")
        self.assertEqual(self.outline.limits[0][0], "remote-7")

    def test_failed_provision_retries_without_duplicate_remote_keys(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.outline.fail_create = True
        self.assertEqual(self.service.process_jobs(self.now), 1)
        with self.database.connect() as connection:
            job = connection.execute("SELECT status, attempts FROM provisioning_jobs").fetchone()
        self.assertEqual(tuple(job), ("pending", 1))

        self.outline.fail_create = False
        self.assertEqual(self.service.process_jobs(self.now + JOB_RETRY_DELAY), 1)
        self.assertEqual(len(self.outline.created), 1)
        self.assertEqual(self.service.user_vpn(123)["status"], "active")

    def test_expiry_revokes_key_and_deduplicates_expiry_notification(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        ready = self.service.pending_notifications(self.now)[0]
        self.service.mark_notification_sent(ready["id"], self.now)

        expiry = self.now + timedelta(days=30, seconds=1)
        self.assertEqual(self.service.expire_and_process(expiry), 1)
        self.assertEqual(self.outline.deleted, ["1"])
        subscription = self.service.user_vpn(123)
        self.assertEqual(subscription["status"], "expired")
        self.assertEqual(subscription["key_status"], "revoked")
        self.assertEqual(len(self.service.pending_notifications(expiry)), 1)

        self.assertEqual(self.service.expire_and_process(expiry + timedelta(days=1)), 0)
        self.assertEqual(self.outline.deleted, ["1"])
        self.assertEqual(len(self.service.pending_notifications(expiry + timedelta(days=1))), 1)

    def test_capacity_snapshot_maps_outline_usage_without_exposing_access_urls(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        self.outline.transfer = {"1": 1234, "unrelated": 999999}

        snapshot = self.service.capacity_snapshot(self.now)
        self.assertEqual(snapshot["active_subscriptions"], 1)
        self.assertEqual(snapshot["outline_version"], "fake-outline-1")
        self.assertEqual(snapshot["active_keys"], 1)
        self.assertEqual(snapshot["pending_jobs"], 0)
        self.assertEqual(snapshot["failed_jobs"], 0)
        self.assertEqual(snapshot["usage"][0]["used_bytes"], 1234)
        self.assertNotIn("access_url", snapshot)

    def test_inventory_tracks_managed_and_untracked_remote_keys_without_secrets(self):
        self.service.register_outline_servers({"default": "Singapore"})
        self.service.refresh_server_inventory(self.now)
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.assertEqual(self.service.process_jobs(self.now), 1)
        self.outline.add_key("legacy-7", "legacy operator key", "ss://must-not-be-stored")

        result = self.service.refresh_server_inventory(self.now)

        self.assertEqual(result[0]["remote_key_count"], 2)
        self.assertEqual(result[0]["remote_orphan_key_count"], 1)
        with self.database.connect() as connection:
            rows = {
                row["outline_key_id"]: dict(row)
                for row in connection.execute(
                    """SELECT outline_key_id, remote_name, managed, status,
                              last_usage_bytes FROM outline_remote_keys
                       WHERE server_id = 'default'"""
                ).fetchall()
            }
            server = connection.execute(
                "SELECT remote_orphan_key_count FROM outline_servers WHERE server_id = 'default'"
            ).fetchone()
        self.assertEqual(rows["1"]["managed"], 1)
        self.assertEqual(rows["legacy-7"]["managed"], 0)
        self.assertEqual(rows["legacy-7"]["status"], "present")
        self.assertEqual(server["remote_orphan_key_count"], 1)
        self.assertNotIn("ss://must-not-be-stored", json.dumps(rows))

        self.outline.keys.pop("legacy-7")
        self.service.refresh_server_inventory(self.now + timedelta(minutes=1))
        with self.database.connect() as connection:
            orphan = connection.execute(
                "SELECT remote_orphan_key_count FROM outline_servers WHERE server_id = 'default'"
            ).fetchone()[0]
            status = connection.execute(
                "SELECT status FROM outline_remote_keys WHERE server_id = 'default' AND outline_key_id = 'legacy-7'"
            ).fetchone()[0]
        self.assertEqual(orphan, 0)
        self.assertEqual(status, "missing")

    def test_remote_key_inventory_filters_audit_rows_without_access_urls(self):
        self.service.register_outline_servers({"default": "Singapore"})
        with self.database.connect() as connection:
            observed = self.now.isoformat()
            connection.execute(
                """INSERT INTO outline_remote_keys
                   (server_id, outline_key_id, remote_name, managed, status,
                    first_seen_at, last_seen_at, last_usage_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", "managed-1", "managed key", 1, "present", observed, observed, 1234),
            )
            connection.execute(
                """INSERT INTO outline_remote_keys
                   (server_id, outline_key_id, remote_name, managed, status,
                    first_seen_at, last_seen_at, last_usage_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", "legacy-1", "legacy key", 0, "present", observed, observed, 5678),
            )
            connection.execute(
                """INSERT INTO outline_remote_keys
                   (server_id, outline_key_id, remote_name, managed, status,
                    first_seen_at, last_seen_at, last_usage_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", "old-1", "old key", 1, "missing", observed, observed, 9),
            )

        present = self.service.remote_key_inventory("default", status="present")
        untracked = self.service.remote_key_inventory("default", status="all", managed=False)
        missing = self.service.remote_key_inventory("default", status="missing")

        self.assertEqual({row["outline_key_id"] for row in present}, {"managed-1", "legacy-1"})
        self.assertEqual([row["outline_key_id"] for row in untracked], ["legacy-1"])
        self.assertEqual([row["outline_key_id"] for row in missing], ["old-1"])
        self.assertNotIn("access_url", json.dumps(present))
        with self.assertRaises(CommerceError):
            self.service.remote_key_inventory("not-configured")

    def test_capacity_snapshot_recommends_assisted_scale_out_without_mutation(self):
        self.service.register_outline_servers({"default": "Singapore"})
        for key_id in range(1, 9):
            self.outline.add_key(str(key_id), f"existing-{key_id}")
        self.service.refresh_server_inventory(self.now)
        self.service.configure_server_capacity(
            "default",
            999,
            max_keys=12,
            reserved_keys=2,
            monthly_traffic_bytes=1_000_000_000_000,
        )

        snapshot = self.service.capacity_snapshot(self.now)

        self.assertEqual(snapshot["scale_advice"]["status"], "prepare")
        self.assertEqual(snapshot["scale_advice"]["utilization_percent"], 80.0)
        self.assertEqual(snapshot["scale_advice"]["remaining_slots"], 2)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM infrastructure_jobs").fetchone()["n"],
                0,
            )

    def test_scale_advice_considers_declared_traffic_commitment(self):
        advice = CommerceService._scale_advice(
            [
                {
                    "enabled": 1,
                    "health_status": "healthy",
                    "saleable_key_capacity": 20,
                    "key_demand": 2,
                    "monthly_traffic_bytes": 1_000,
                    "committed_traffic_bytes": 800,
                }
            ]
        )
        self.assertEqual(advice["status"], "prepare")
        self.assertEqual(advice["traffic_utilization_percent"], 80.0)

    def test_infrastructure_request_records_intent_only(self):
        environment = {
            "AURIX_INFRASTRUCTURE_QUEUE_ENABLED": "1",
            "AURIX_SCALE_OBSERVATION_INTERVAL_SECONDS": "0",
            "AURIX_SCALE_REGION": "sgp1",
            "AURIX_SCALE_DROPLET_SIZE": "s-1vcpu-1gb",
            "AURIX_SCALE_DROPLET_IMAGE": "ubuntu-24-04-x64",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.service.register_outline_servers({"default": "Singapore"})
            for key_id in range(1, 9):
                self.outline.add_key(str(key_id), f"existing-{key_id}")
            self.service.refresh_server_inventory(self.now)
            self.service.configure_server_capacity(
                "default",
                999,
                max_keys=12,
                reserved_keys=2,
                monthly_traffic_bytes=1_000_000_000_000,
            )
            first = self.service.capacity_snapshot(self.now)
            self.assertEqual(first["scale_advice"]["consecutive_observations"], 1)
            self.assertFalse(first["scale_advice"]["observation_ready"])
            with self.assertRaisesRegex(CommerceError, "separate observations"):
                self.service.queue_infrastructure_provision(999, self.now)
            second = self.service.capacity_snapshot(self.now + timedelta(minutes=1))
            self.assertTrue(second["scale_advice"]["observation_ready"])
            job_id = self.service.queue_infrastructure_provision(999, self.now)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT operation, status FROM infrastructure_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        self.assertEqual(tuple(row), ("provision", "pending"))

    def test_plan_allocation_reserves_capacity_before_payment_and_releases_on_cancel(self):
        self.service.register_outline_servers({"default": "Singapore"})
        self.service.refresh_server_inventory(self.now)
        self.service.configure_server_capacity(
            "default",
            999,
            max_keys=3,
            reserved_keys=1,
            monthly_traffic_bytes=1_000_000_000_000,
        )
        self.service.configure_plan_allocation("default", "basic_50gb", 1, 999)

        first = self.service.create_order(101, "One", "basic_50gb", self.now)
        availability = self.service.plan_availability(self.now)["basic_50gb"]
        self.assertFalse(availability["available"])
        self.assertEqual(availability["remaining_slots"], 0)
        with self.assertRaisesRegex(CommerceError, "temporarily full"):
            self.service.create_order(202, "Two", "basic_50gb", self.now)

        self.service.cancel_order(101, first.order_id, self.now)
        second = self.service.create_order(202, "Two", "basic_50gb", self.now)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT server_id, capacity_reserved_until FROM orders WHERE id = ?",
                (second.order_id,),
            ).fetchone()
        self.assertEqual(row["server_id"], "default")
        self.assertIsNotNone(row["capacity_reserved_until"])

    def test_strict_capacity_controls_reject_overallocation_atomically(self):
        self.service.register_outline_servers({"default": "Singapore"})
        self.service.refresh_server_inventory(self.now)
        with patch.dict(os.environ, {"AURIX_FLEET_STRICT_ALLOCATION_VALIDATION": "1"}, clear=False):
            self.service.configure_server_capacity(
                "default", 999, max_keys=3, reserved_keys=1,
                monthly_traffic_bytes=1_000_000_000_000,
            )
            self.service.configure_plan_allocation("default", "basic_50gb", 2, 999)
            with self.assertRaisesRegex(CommerceError, "allocates 3 slots but only 2"):
                self.service.configure_tier_allocation("default", "FREE300MB", 1, 999)

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT slot_limit FROM server_tier_allocations WHERE server_id = ? AND tier_code = ?",
                ("default", "FREE300MB"),
            ).fetchone()
        self.assertIsNone(row)

    def test_strict_fleet_policy_can_migrate_legacy_overallocation_atomically(self):
        self.service.register_outline_servers({"default": "Singapore"})
        self.service.refresh_server_inventory(self.now)
        with patch.dict(os.environ, {"AURIX_FLEET_STRICT_ALLOCATION_VALIDATION": "0"}, clear=False):
            self.service.configure_server_capacity(
                "default", 999, max_keys=10, reserved_keys=2,
                monthly_traffic_bytes=1_000_000_000_000,
            )
            self.service.configure_plan_allocation("default", "basic_50gb", 10, 999)
            self.service.configure_tier_allocation("default", "FREE300MB", 10, 999)

        with patch.dict(os.environ, {"AURIX_FLEET_STRICT_ALLOCATION_VALIDATION": "1"}, clear=False):
            self.service.apply_server_policy(
                "default", 999,
                max_keys=10,
                reserved_keys=2,
                monthly_traffic_bytes=1_000_000_000_000,
                plan_slots={"basic_50gb": 2, "standard_100gb": 0},
                tier_slots={"FREE300MB": 0, "FREE3GB": 0, "PROMO": 0},
            )

        with self.database.connect() as connection:
            total = connection.execute(
                "SELECT COALESCE((SELECT SUM(slot_limit) FROM server_plan_allocations WHERE server_id = ?), 0) + "
                "COALESCE((SELECT SUM(slot_limit) FROM server_tier_allocations WHERE server_id = ?), 0)",
                ("default", "default"),
            ).fetchone()[0]
        self.assertEqual(total, 2)

    def test_stale_server_telemetry_is_not_eligible_for_new_orders(self):
        self.service.register_outline_servers({"default": "Singapore"})
        self.service.refresh_server_inventory(self.now)
        self.service.configure_plan_allocation("default", "basic_50gb", 10, 999)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE outline_servers SET last_synced_at = ? WHERE server_id = 'default'",
                ((self.now - timedelta(hours=1)).isoformat(),),
            )

        with self.assertRaisesRegex(CommerceError, "temporarily full"):
            self.service.create_order(101, "One", "basic_50gb", self.now)


class FakeRawPostgresCursor:
    rowcount = 1

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class FakeRawPostgresConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return FakeRawPostgresCursor()


def postgres_schema_contract(statements):
    tables = {}
    indexes = set()
    for statement in statements:
        table_match = re.match(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\((.*)\)\s*$",
            statement.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if table_match:
            table_name, body = table_match.groups()
            columns = set()
            for line in body.splitlines():
                token = line.strip().split(None, 1)[0].rstrip(",") if line.strip() else ""
                if token.upper() in {"CHECK", "CONSTRAINT", "FOREIGN", "PRIMARY", "UNIQUE"}:
                    continue
                if re.fullmatch(r"[a-z_][a-z0-9_]*", token, re.IGNORECASE):
                    columns.add(token.lower())
            tables[table_name.lower()] = columns
            continue
        alter_match = re.match(
            r"ALTER TABLE\s+([a-z_]+)\s+ADD COLUMN IF NOT EXISTS\s+([a-z_]+)",
            statement.strip(),
            re.IGNORECASE,
        )
        if alter_match:
            table_name, column_name = alter_match.groups()
            tables.setdefault(table_name.lower(), set()).add(column_name.lower())
            continue
        index_match = re.match(
            r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+([a-z_]+)",
            statement.strip(),
            re.IGNORECASE,
        )
        if index_match:
            indexes.add(index_match.group(1).lower())
    return {
        "tables": {name: sorted(columns) for name, columns in sorted(tables.items())},
        "indexes": sorted(indexes),
    }


def sqlite_schema_contract(path):
    Database(path).initialize()
    CommerceDatabase(path).initialize()
    with open_sqlite_connection(path) as connection:
        table_names = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        tables = {
            table_name: sorted(
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
            )
            for table_name in table_names
        }
        indexes = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        )
    return {"tables": tables, "indexes": indexes}


def sqlite_schema_metadata(path):
    with open_sqlite_connection(path) as connection:
        table_names = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        tables = {}
        for table_name in table_names:
            tables[table_name] = {
                "columns": [
                    {
                        "name": row[1],
                        "type": row[2],
                        "not_null": bool(row[3]),
                        "default": row[4],
                        "primary_key": int(row[5]),
                    }
                    for row in connection.execute(f"PRAGMA table_info({table_name})")
                ],
                "foreign_keys": sorted(
                    [
                        {
                            "table": row[2],
                            "from": row[3],
                            "to": row[4],
                            "on_update": row[5],
                            "on_delete": row[6],
                            "match": row[7],
                        }
                        for row in connection.execute(f"PRAGMA foreign_key_list({table_name})")
                    ],
                    key=lambda item: json.dumps(item, sort_keys=True),
                ),
            }
        indexes = {}
        for name, table_name, sql in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            indexes[name] = {
                "table": table_name,
                "columns": [row[2] for row in connection.execute(f"PRAGMA index_info({name})")],
                "sql": re.sub(r"\s+", " ", sql.strip()) if sql else None,
            }
    return {"tables": tables, "indexes": indexes}


def schema_fingerprint(value):
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def postgres_ddl_fingerprint(statements):
    ddl = []
    for statement in statements:
        normalized = re.sub(r"\s+", " ", statement.strip()).lower()
        if normalized.startswith(("create table", "create index", "alter table")):
            ddl.append(normalized)
    return schema_fingerprint(sorted(ddl))


class FakePoolCheckout:
    def __init__(self, raw, events):
        self.raw = raw
        self.events = events

    def __enter__(self):
        self.events.append("checkout")
        return self.raw

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append("return")
        return False


class FakePostgresPool:
    def __init__(self):
        self.raw = FakeRawPostgresConnection()
        self.events = []
        self.closed = False

    def connection(self):
        return FakePoolCheckout(self.raw, self.events)

    def close(self):
        self.closed = True


class PostgresAdapterTest(unittest.TestCase):
    def test_sqlite_and_postgres_schema_contracts_match_frozen_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "contract.db"
            sqlite_contract = sqlite_schema_contract(sqlite_path)
            sqlite_metadata = sqlite_schema_metadata(sqlite_path)

        database = PostgresCommerceDatabase("postgresql://example.invalid/aurix")
        raw = FakeRawPostgresConnection()
        database.connect = lambda: _PostgresConnection(raw)
        database.initialize()
        postgres_contract = postgres_schema_contract([query for query, _params in raw.calls])

        self.assertEqual(postgres_contract, sqlite_contract)
        self.assertEqual(
            schema_fingerprint(sqlite_contract),
            "f49745c3eb4bd2c8fb4a627be014204a9525e88cb4e90b0177280294dda4e41e",
        )
        self.assertEqual(
            schema_fingerprint(sqlite_metadata),
            "d1804762bb0c8750574ff03b42068d25887fe9b1203446701391e624dcc2f979",
        )
        self.assertEqual(
            postgres_ddl_fingerprint([query for query, _params in raw.calls]),
            "cff55ea0bb19ca5ff1fcb8846db921198f923e3631ff99830d377b18b6b0e135",
        )

    def test_qmark_adapter_translates_service_parameters(self):
        raw = FakeRawPostgresConnection()
        connection = _PostgresConnection(raw)
        connection.execute("SELECT * FROM plans WHERE code = ? LIMIT ?", ("basic_50gb", 1))
        self.assertEqual(
            raw.calls[0], ("SELECT * FROM plans WHERE code = %s LIMIT %s", ("basic_50gb", 1))
        )

    def test_postgres_update_dedupe_insert_is_available_to_polling_loop(self):
        raw = FakeRawPostgresConnection()
        database = PostgresCommerceDatabase("postgresql://example.invalid/aurix")
        database.connect = lambda: _PostgresConnection(raw)
        self.assertTrue(database.mark_update_seen(12345))
        self.assertEqual(
            raw.calls[0][0],
            "INSERT INTO telegram_updates (update_id, received_at) VALUES (%s, %s)",
        )

    def test_postgres_begin_write_relies_on_psycopg_transaction_boundary(self):
        raw = FakeRawPostgresConnection()
        PostgresCommerceDatabase.begin_write(_PostgresConnection(raw))
        self.assertEqual(raw.calls, [])

    def test_postgres_database_reuses_pool_and_returns_checked_out_connections(self):
        pool = FakePostgresPool()
        database = PostgresCommerceDatabase("postgresql://example.invalid/aurix")
        create_calls = []

        def create_pool():
            create_calls.append(True)
            return pool

        database._create_pool = create_pool
        for value in (1, 2):
            with database.connect() as connection:
                connection.execute("SELECT ?", (value,))

        self.assertEqual(len(create_calls), 1)
        self.assertEqual(pool.events, ["checkout", "return", "checkout", "return"])
        self.assertEqual(
            pool.raw.calls,
            [("SELECT %s", (1,)), ("SELECT %s", (2,))],
        )
        database.close()
        self.assertTrue(pool.closed)
        self.assertIsNone(database._pool)
        with self.assertRaisesRegex(CommerceError, "pool is closed"):
            database.connect()

    def test_postgres_schema_is_executable_without_sqlite_pragmas(self):
        database = PostgresCommerceDatabase("postgresql://example.invalid/aurix")
        raw = FakeRawPostgresConnection()
        database.connect = lambda: _PostgresConnection(raw)
        database.initialize()
        statements = [query for query, _ in raw.calls]
        self.assertTrue(any("BIGSERIAL PRIMARY KEY" in query for query in statements))
        self.assertFalse(any("PRAGMA" in query for query in statements))
        self.assertTrue(any("ON CONFLICT(code) DO NOTHING" in query for query in statements))


if __name__ == "__main__":
    unittest.main()
