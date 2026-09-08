import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app import Database
from commerce import (
    CommerceDatabase,
    CommerceError,
    CommerceService,
    JOB_RETRY_DELAY,
    PostgresCommerceDatabase,
    _PostgresConnection,
)
from connectivity import EndpointRegistry
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


class CommerceServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.tmp.name) / "bot.db")
        self.outline = FakePaidOutline()
        # Existing fixture exercises the pre-screenshot migration path. Public
        # deployments leave this option disabled (the default).
        self.service = CommerceService(
            self.database, self.outline, Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.service.initialize()
        self.now = datetime(2026, 8, 27, 3, 7, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def _paid_order(self, telegram_id=123):
        order = self.service.create_order(telegram_id, "Min", "basic_50gb", self.now)
        self.service.submit_payment(telegram_id, order.order_id, "manual", f"ref-{order.order_id}", self.now)
        return order

    def _receipt_extraction(self, transaction_id="TX-100", **overrides):
        value = {
            "provider": "KBZPay",
            "transaction_id": transaction_id,
            "amount_minor": 6000,
            "currency": "MMK",
            "timestamp": self.now.isoformat(),
            "recipient": "AuriX",
            "confidence": 0.99,
            "flags": [],
            "notes": [],
        }
        value.update(overrides)
        return value

    def test_wallet_topup_credits_exact_verified_deposit_without_vpn_job(self):
        order = self.service.create_wallet_topup(123, "Min", 6000, self.now)
        self.service.select_payment_provider(123, order.order_id, "kpay")
        evidence = self.service.submit_receipt(
            123,
            order.order_id,
            "KBZPay",
            "file-topup",
            "unique-topup",
            b"topup-receipt",
            "image/jpeg",
            self._receipt_extraction(),
            self.now,
        )
        self.assertEqual(evidence["flags"], [])
        self.service.verify_receipt(
            evidence["evidence_id"], 999, "TX-100", 6000, "MMK", self.now
        )

        approved = self.service.approve_order(order.order_id, 999, self.now)
        repeated = self.service.approve_order(
            order.order_id, 999, self.now + timedelta(minutes=1)
        )

        self.assertEqual(approved.status, "wallet_credited")
        self.assertEqual(repeated.status, "already_credited")
        self.assertEqual(self.service.wallet_balance(123), 6000)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE order_id = ?", (order.order_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provisioning_jobs").fetchone()[0], 0
            )

    def test_wallet_topup_requires_exact_verified_amount(self):
        order = self.service.create_wallet_topup(123, "Min", 6000, self.now)
        self.service.select_payment_provider(123, order.order_id, "kpay")
        evidence = self.service.submit_receipt(
            123,
            order.order_id,
            "KBZPay",
            "file-overpay",
            "unique-overpay",
            b"overpay-receipt",
            "image/jpeg",
            self._receipt_extraction(amount_minor=7000),
            self.now,
        )
        self.assertIn("amount_mismatch", evidence["flags"])
        with self.assertRaisesRegex(CommerceError, "must match exactly"):
            self.service.verify_receipt(
                evidence["evidence_id"], 999, "TX-100", 7000, "MMK", self.now
            )

    def test_receipt_risk_flags_stale_timestamp_and_blocks_cross_order_image_reuse(self):
        first = self.service.create_wallet_topup(123, "Min", 6000, self.now)
        self.service.select_payment_provider(123, first.order_id, "kpay")
        stale = self.service.submit_receipt(
            123,
            first.order_id,
            "KBZPay",
            "file-stale",
            "unique-stale",
            b"same-receipt",
            "image/jpeg",
            self._receipt_extraction(timestamp=(self.now - timedelta(hours=2)).isoformat()),
            self.now,
        )
        self.assertIn("receipt_older_than_1_hour", stale["flags"])

        second = self.service.create_wallet_topup(124, "Other", 6000, self.now)
        self.service.select_payment_provider(124, second.order_id, "kpay")
        with self.assertRaisesRegex(CommerceError, "already submitted"):
            self.service.submit_receipt(
                124,
                second.order_id,
                "KBZPay",
                "file-copy",
                "unique-copy",
                b"same-receipt",
                "image/jpeg",
                self._receipt_extraction(transaction_id="TX-200"),
                self.now,
            )

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
            connection.execute(
                "INSERT INTO payments VALUES ('payment-1', 'Manual', ' Tx 123 ')"
            )

        CommerceDatabase(legacy_path).initialize()

        with open_sqlite_connection(legacy_path) as connection:
            normalized = connection.execute(
                "SELECT normalized_reference FROM payments WHERE id = 'payment-1'"
            ).fetchone()[0]
            indexes = {
                row[1] for row in connection.execute("PRAGMA index_list(payments)")
            }
        self.assertEqual(normalized, "tx123")
        self.assertIn("payments_reference_lookup", indexes)

    def test_existing_database_adds_receipt_media_type(self):
        legacy_path = Path(self.tmp.name) / "legacy-receipts.db"
        database = CommerceDatabase(legacy_path)
        database.initialize()
        with open_sqlite_connection(legacy_path) as connection:
            connection.execute(
                "ALTER TABLE payment_evidence DROP COLUMN telegram_media_type"
            )

        database.initialize()

        with open_sqlite_connection(legacy_path) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(payment_evidence)")
            }
        self.assertIn("telegram_media_type", columns)
        self.assertIn("storage_bucket", columns)
        self.assertIn("storage_path", columns)
        self.assertIn("storage_status", columns)
        self.assertIn("storage_error", columns)
        self.assertIn("stored_at", columns)

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
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM provisioning_jobs").fetchone()[0], 1)

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
        conflict = self.service.create_order(123, "Min", "standard_100gb", self.now + timedelta(minutes=1))
        self.assertFalse(conflict.created)
        self.assertTrue(conflict.plan_conflict)
        self.assertEqual(conflict.order_id, first.order_id)
        replacement = self.service.replace_open_order(
            123, "Min", "standard_100gb", self.now + timedelta(minutes=2)
        )
        self.assertNotEqual(replacement.order_id, first.order_id)
        self.assertEqual(self.service.order_detail(first.order_id, 123)["stage"], "cancelled")
        self.assertEqual(self.service.order_detail(replacement.order_id, 123)["plan_code"], "standard_100gb")

    def test_customer_server_preference_flows_into_subscription(self):
        self.service.connectivity = SimpleNamespace(
            validate_customer_endpoint=lambda endpoint_id, plan_code: {
                "id": endpoint_id,
                "eligible": True,
            }
        )
        order = self.service.create_order(
            123, "Min", "basic_50gb", self.now, requested_endpoint_id="sgp-02"
        )
        with self.database.connect() as connection:
            requested = connection.execute(
                "SELECT requested_endpoint_id FROM orders WHERE id = ?",
                (order.order_id,),
            ).fetchone()["requested_endpoint_id"]
        self.assertEqual(requested, "sgp-02")
        self.service.submit_payment(123, order.order_id, "manual", "server-pref", self.now)
        self.service.approve_order(order.order_id, 999, self.now)
        with self.database.connect() as connection:
            preferred = connection.execute(
                "SELECT preferred_endpoint_id FROM subscriptions WHERE order_id = ?",
                (order.order_id,),
            ).fetchone()["preferred_endpoint_id"]
        self.assertEqual(preferred, "sgp-02")

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
        self.assertEqual(self.service.refund_order(order.order_id, 999, "customer request", self.now), "refunded")
        self.assertEqual(self.service.refund_order(order.order_id, 999, "customer request", self.now), "already_refunded")
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

        self.assertEqual(
            self.service.pending_notifications(self.now + timedelta(days=1)), []
        )
        self.assertEqual(
            self.service.consistency_report(self.now + timedelta(days=1))[
                "dead_notifications"
            ],
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
        self.assertEqual(usage[0]["remaining_bytes"], 38 * 1024**3)
        self.assertEqual(self.service.user_usage(456, self.outline.transfer), [])

    def test_admin_can_pause_endpoint_and_allocate_plan_slots(self):
        registry = EndpointRegistry(self.database, Fernet.generate_key())
        registry.configure_bootstrap(
            "https://outline.invalid:1234/secret", "0" * 64, now=datetime.now(UTC)
        )
        self.service.connectivity = registry
        endpoint = self.service.configure_endpoint_capacity(
            "legacy-default",
            999,
            max_active_keys=50,
            accepts_new_assignments=False,
            now=self.now,
        )
        self.assertEqual(endpoint["max_active_keys"], 50)
        self.assertFalse(bool(endpoint["accepts_new_assignments"]))
        self.service.configure_endpoint_plan_limit(
            "legacy-default",
            "basic_50gb",
            999,
            max_active_assignments=10,
            enabled=True,
            now=self.now,
        )
        plans = self.service.endpoint_plan_capacity("legacy-default")
        basic = next(item for item in plans if item["plan_code"] == "basic_50gb")
        self.assertEqual(basic["max_active_assignments"], 10)

    def test_unavailable_endpoint_metrics_do_not_reset_last_paid_usage(self):
        order = self._paid_order(128)
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        key_id = self.outline.created[0][2]["id"]
        self.service.enforce_quotas(
            self.now,
            metrics={"byEndpoint": {"legacy-default": {key_id: 54321}}, "errors": {}},
        )
        self.service.enforce_quotas(
            self.now + timedelta(minutes=1),
            metrics={"byEndpoint": {}, "errors": {"legacy-default": "OutlineError"}},
        )
        with self.database.connect() as connection:
            observed = connection.execute(
                "SELECT last_usage_bytes FROM paid_vpn_keys WHERE outline_key_id = ?", (key_id,)
            ).fetchone()["last_usage_bytes"]
        self.assertEqual(observed, 54321)

    def test_user_and_admin_can_track_order_review_state(self):
        order = self.service.create_order(123, "Min", "basic_50gb", self.now)
        self.service.submit_receipt(
            123, order.order_id, "manual", "track-file", "track-unique",
            b"track-receipt", "image/jpeg", None, self.now,
        )
        history = self.service.list_user_orders(123)
        self.assertEqual(history[0]["id"], order.order_id)
        self.assertEqual(history[0]["receipt_status"], "pending")
        self.assertIsNotNone(self.service.order_detail(order.order_id, 123))
        self.assertIsNone(self.service.order_detail(order.order_id, 456))
        self.assertIsNotNone(
            self.service.order_detail(order.order_id, 999, is_admin=True)
        )

    def test_reconcile_cancels_only_empty_historical_duplicates(self):
        order = self.service.create_order(123, "Min", "basic_50gb", self.now)
        self.service.submit_receipt(
            123, order.order_id, "manual", "keeper-file", "keeper-unique",
            b"keeper-evidence", "image/jpeg", None, self.now,
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
            statuses = dict(
                connection.execute("SELECT id, status FROM orders").fetchall()
            )
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
        self.assertTrue(
            self.outline.created[0][0].startswith("123-PAID50GB-30day-202608270307-")
        )
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
        order = self.service.create_order(
            321, "Min", "basic_50gb", self.now, username="@min_vpn"
        )
        self.service.submit_payment(
            321, order.order_id, "manual", "username-name-ref", self.now
        )
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        self.assertTrue(
            self.outline.created[0][0].startswith(
                "min_vpn-PAID50GB-30day-202608270307-"
            )
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
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
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
                        for row in connection.execute(
                            f"PRAGMA foreign_key_list({table_name})"
                        )
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
                "columns": [
                    row[2] for row in connection.execute(f"PRAGMA index_info({name})")
                ],
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
        postgres_contract = postgres_schema_contract(
            [query for query, _params in raw.calls]
        )

        self.assertEqual(postgres_contract, sqlite_contract)
        self.assertEqual(
            schema_fingerprint(sqlite_contract),
            "0fc7ca6a1dde889f0dfd9e644750de21fd433f2ab69c7838d10653fc45e4140e",
        )
        self.assertEqual(
            schema_fingerprint(sqlite_metadata),
            "f50d6297d25ea35ac125b667ba1e2b00bb676cfdb2263bc21995849b113e5158",
        )
        self.assertEqual(
            postgres_ddl_fingerprint([query for query, _params in raw.calls]),
            "6a3aae494dd93489399790c93d86d27c7b6600c7bd55296c48e9516bbb6cce5d",
        )

    def test_qmark_adapter_translates_service_parameters(self):
        raw = FakeRawPostgresConnection()
        connection = _PostgresConnection(raw)
        connection.execute("SELECT * FROM plans WHERE code = ? LIMIT ?", ("basic_50gb", 1))
        self.assertEqual(raw.calls[0], ("SELECT * FROM plans WHERE code = %s LIMIT %s", ("basic_50gb", 1)))

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
