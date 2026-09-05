import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from access_control import StaffAccessControl
from app import ClaimService, Database, OutlineClient, OutlineError, TelegramBot
from commerce import CommerceDatabase, CommerceService
from persistence import open_sqlite_connection
from telegram_transport import TelegramAPIError


UTC = timezone.utc
LIMIT_BYTES = 100 * 1024 * 1024


class FakeOutline:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.fail_create = False
        self.create_lock = threading.Lock()
        self.create_delay = 0.0
        self.transfer = {}
        self.data_limits = []

    def create_key(self, name, limit_bytes):
        if self.create_delay:
            time.sleep(self.create_delay)
        with self.create_lock:
            if self.fail_create:
                raise OutlineError("staging unavailable")
            key = {"id": str(len(self.created) + 1), "accessUrl": "ss://secret"}
            self.created.append((name, limit_bytes, key))
            return key

    def delete_key(self, key_id):
        self.deleted.append(key_id)

    def set_data_limit(self, key_id, limit_bytes):
        self.data_limits.append((str(key_id), int(limit_bytes)))

    def get_key(self, key_id):
        if str(key_id) in self.deleted:
            return None
        for _name, _limit, key in self.created:
            if key["id"] == str(key_id):
                return key
        return None

    def list_keys(self):
        return {
            "accessKeys": [
                key for _name, _limit, key in self.created if key["id"] not in self.deleted
            ]
        }

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": self.transfer}

    def server_info(self):
        return {"version": "test"}


class AmbiguousDeterministicOutline(FakeOutline):
    """Simulate a PUT that commits remotely before its response is lost."""

    def __init__(self):
        super().__init__()
        self.deterministic_ids = []

    def create_key_with_id(self, key_id, name, limit_bytes):
        self.deterministic_ids.append(str(key_id))
        key = {
            "id": str(key_id),
            "name": name,
            "accessUrl": f"ss://{key_id}",
        }
        self.created.append((name, limit_bytes, key))
        raise OutlineError("response lost after remote commit")


class FakeOutlinePool:
    def __init__(self):
        self.clients = {"sg-a": FakeOutline(), "sg-b": FakeOutline()}
        self.default_server_id = "sg-a"

    def server_ids(self):
        return tuple(self.clients)

    def client(self, server_id=None):
        return self.clients[str(server_id or self.default_server_id)]


class FakeReceiptStorage:
    configured = True
    bucket = "payment-receipts"

    def signed_url(self, path, expires_in=300):
        return f"https://storage.example/{path}?ttl={expires_in}"

    def download(self, path):
        return b"stored-receipt"


class ClaimServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        self.db.initialize()
        self.outline = FakeOutline()
        self.service = ClaimService(self.db, self.outline)
        self.now = datetime(2026, 8, 27, 3, 7, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_claim_creates_300_mb_key_for_24_hours(self):
        result = self.service.claim(123, "Min", self.now)
        self.assertEqual(result.access_url, "ss://secret")
        self.assertEqual(result.expires_at, self.now + timedelta(hours=24))
        self.assertEqual(self.outline.created[0][1], 300_000_000)
        self.assertEqual(self.outline.created[0][0], "123-FREE300MB-24hr-202608270307")

    def test_free_remote_create_runs_after_reservation_commit(self):
        observed = []
        database = self.db

        class ObservingOutline(FakeOutline):
            def create_key(self, name, limit_bytes):
                with database.connect() as connection:
                    row = connection.execute(
                        """SELECT status FROM free_provisioning_intents
                           WHERE telegram_id = 123 ORDER BY created_at DESC LIMIT 1"""
                    ).fetchone()
                    observed.append(row["status"] if row else None)
                return super().create_key(name, limit_bytes)

        service = ClaimService(self.db, ObservingOutline())
        result = service.claim(123, "Min", self.now)

        self.assertEqual(result.access_url, "ss://secret")
        self.assertEqual(observed, ["running"])
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM free_provisioning_intents WHERE telegram_id = 123"
                ).fetchone()[0],
                "done",
            )

    def test_free_intent_worker_recovers_a_request_crash_without_duplicate_key(self):
        with self.db.connect() as connection:
            self.db.begin_write(connection)
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, created_at)
                   VALUES (?, ?, ?)""",
                (123, "Min", self.now.isoformat()),
            )
            service = self.service
            service._insert_free_intent(
                connection,
                telegram_id=123,
                kind="daily",
                campaign_code=None,
                window_start=None,
                winner_number=None,
                server_id="primary",
                outline_key_id=service._deterministic_slot_id("daily", "123", "first"),
                key_name="123-FREE300MB-24hr-202608270307",
                quota_bytes=300_000_000,
                duration_days=1,
                claim_started_at=self.now.isoformat(),
            )

        self.assertEqual(self.service.process_provisioning_intents(self.now), 1)
        self.assertEqual(len(self.outline.created), 1)
        with self.db.connect() as connection:
            intent = connection.execute(
                "SELECT status FROM free_provisioning_intents WHERE telegram_id = 123"
            ).fetchone()
            key = connection.execute("SELECT status FROM keys WHERE telegram_id = 123").fetchone()
            user = connection.execute(
                "SELECT last_claim_at FROM users WHERE telegram_id = 123"
            ).fetchone()
        self.assertEqual(intent["status"], "done")
        self.assertEqual(key["status"], "active")
        self.assertEqual(user["last_claim_at"], self.now.isoformat())

    def test_claim_key_name_prefers_sanitized_telegram_username(self):
        self.service.claim(123, "Min", self.now, username="@min_user")
        self.assertEqual(self.outline.created[0][0], "min_user-FREE300MB-24hr-202608270307")

    def test_trial_key_name_uses_tier_duration_and_start_time(self):
        self.service.claim_trial(123, "Min", self.now, username="min_user")
        self.assertEqual(self.outline.created[0][0], "min_user-FREE3GB-30day-202608270307")

    def test_free_missing_key_repair_updates_intent_and_preserves_remaining_quota(self):
        commerce = CommerceService(
            CommerceDatabase(self.db.path),
            self.outline,
            Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        commerce.initialize()
        commerce.register_outline_servers({"primary": "Primary"})
        commerce.refresh_server_inventory(self.now)
        claimed = self.service.claim(123, "Min", self.now, username="min_user")
        self.assertTrue(claimed.access_url)
        commerce.refresh_server_inventory(self.now + timedelta(minutes=1))
        self.outline.deleted.append("1")
        self.outline.transfer = {"1": 1_000_000}
        commerce.refresh_server_inventory(self.now + timedelta(minutes=2))
        result = commerce.refresh_server_inventory(self.now + timedelta(minutes=3))[0]
        self.assertEqual(result["repair_jobs_queued"], 1)
        self.assertEqual(
            commerce.process_managed_key_repairs(self.now + timedelta(minutes=3)),
            1,
        )
        with self.db.connect() as connection:
            key = connection.execute(
                "SELECT outline_key_id, data_limit_bytes, status FROM keys WHERE telegram_id = 123"
            ).fetchone()
            intent = connection.execute(
                "SELECT outline_key_id, status FROM free_provisioning_intents WHERE telegram_id = 123"
            ).fetchone()
        self.assertEqual(tuple(key), ("2", 299_000_000, "active"))
        self.assertEqual(tuple(intent), ("2", "done"))

    def test_second_claim_inside_24_hours_is_rejected(self):
        self.service.claim(123, "Min", self.now)
        result = self.service.claim(123, "Min", self.now + timedelta(hours=23, minutes=59))
        self.assertIsNone(result.access_url)
        self.assertEqual(result.next_claim_at, self.now + timedelta(hours=24))
        self.assertEqual(len(self.outline.created), 1)

    def test_claim_at_24_hour_boundary_is_allowed(self):
        self.service.claim(123, "Min", self.now)
        result = self.service.claim(123, "Min", self.now + timedelta(hours=24))
        self.assertEqual(result.access_url, "ss://secret")
        self.assertEqual(len(self.outline.created), 2)

    def test_failed_outline_create_does_not_consume_claim(self):
        self.outline.fail_create = True
        with self.assertRaises(OutlineError):
            self.service.claim(123, "Min", self.now)
        self.outline.fail_create = False
        result = self.service.claim(123, "Min", self.now)
        self.assertEqual(result.access_url, "ss://secret")

    def test_ambiguous_deterministic_create_is_recovered_without_duplicate(self):
        outline = AmbiguousDeterministicOutline()
        service = ClaimService(self.db, outline)

        result = service.claim(123, "Min", self.now)

        self.assertTrue(result.access_url.startswith("ss://aurix-daily-"))
        self.assertEqual(len(outline.created), 1)
        self.assertEqual(len(outline.deterministic_ids), 1)
        self.assertEqual(outline.deleted, [])
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT outline_key_id FROM keys WHERE telegram_id = ?", (123,)
            ).fetchone()
        self.assertEqual(row["outline_key_id"], outline.deterministic_ids[0])

    def test_legacy_post_fallback_recovers_unique_named_key(self):
        outline = FakeOutline()
        key = {
            "id": "server-selected",
            "name": "123-FREE300MB-24hr-202608270307",
            "accessUrl": "ss://legacy",
        }
        outline.created.append((key["name"], 300_000_000, key))
        service = ClaimService(self.db, outline)

        recovered = service._remote_key_for_intent(
            {
                "server_id": "primary",
                "outline_key_id": "deterministic-id",
                "key_name": key["name"],
            }
        )

        self.assertEqual(recovered, key)

    def test_legacy_post_fallback_refuses_ambiguous_named_keys(self):
        outline = FakeOutline()
        key_name = "123-FREE300MB-24hr-202608270307"
        for index in (1, 2):
            outline.created.append(
                (
                    key_name,
                    300_000_000,
                    {
                        "id": f"server-selected-{index}",
                        "name": key_name,
                        "accessUrl": f"ss://legacy-{index}",
                    },
                )
            )
        service = ClaimService(self.db, outline)

        with self.assertRaisesRegex(OutlineError, "Multiple Outline keys"):
            service._remote_key_for_intent(
                {
                    "server_id": "primary",
                    "outline_key_id": "deterministic-id",
                    "key_name": key_name,
                }
            )

    def test_legacy_post_retry_finalizes_unique_named_key_without_second_create(self):
        key_name = "123-FREE300MB-24hr-202608270307"
        key = {"id": "server-selected", "name": key_name, "accessUrl": "ss://legacy"}
        self.outline.created.append((key_name, 300_000_000, key))
        with self.db.connect() as connection:
            self.db.begin_write(connection)
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (123, "Min", self.now.isoformat()),
            )
            intent = self.service._insert_free_intent(
                connection,
                telegram_id=123,
                kind="daily",
                campaign_code=None,
                window_start=None,
                winner_number=None,
                server_id="primary",
                outline_key_id="deterministic-id",
                key_name=key_name,
                quota_bytes=300_000_000,
                duration_days=1,
                claim_started_at=self.now.isoformat(),
            )
            connection.execute(
                "UPDATE free_provisioning_intents SET attempts = 1 WHERE id = ?",
                (intent["id"],),
            )

        self.assertEqual(self.service.process_provisioning_intents(self.now), 1)
        self.assertEqual(len(self.outline.created), 1)
        with self.db.connect() as connection:
            stored_intent = connection.execute(
                "SELECT status, outline_key_id FROM free_provisioning_intents WHERE id = ?",
                (intent["id"],),
            ).fetchone()
            stored_key = connection.execute(
                "SELECT outline_key_id FROM keys WHERE telegram_id = ?", (123,)
            ).fetchone()
        self.assertEqual(stored_intent["status"], "done")
        self.assertEqual(stored_intent["outline_key_id"], "server-selected")
        self.assertEqual(stored_key["outline_key_id"], "server-selected")

    def test_trial_and_promo_use_distinct_deterministic_slots(self):
        outline = AmbiguousDeterministicOutline()
        service = ClaimService(self.db, outline)

        trial = service.claim_trial(123, "Min", self.now)
        promo = service.claim_giveaway(456, "Other", self.now)

        self.assertTrue(trial.access_url.startswith("ss://aurix-monthly-"))
        self.assertTrue(promo.access_url.startswith("ss://aurix-promo-"))
        self.assertEqual(len(outline.created), 2)
        self.assertEqual(len(set(outline.deterministic_ids)), 2)

    def test_expiry_revokes_key_once(self):
        self.service.claim(123, "Min", self.now)
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24)), 1)
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=25)), 0)
        self.assertEqual(self.outline.deleted, ["1"])

        event = self.service.termination_summary()[0]
        self.assertEqual(event["reason"], "expiry")
        self.assertEqual(event["remote_state"], "deleted_verified")
        self.assertIsNotNone(event["deletion_verified_at"])

    def test_quota_revokes_before_expiry_and_records_observed_usage(self):
        self.service.claim(123, "Min", self.now)
        self.outline.transfer = {"1": 300_000_000}

        self.assertEqual(self.service.enforce_quota(self.now + timedelta(hours=1)), 1)

        event = self.service.termination_summary()[0]
        self.assertEqual(event["reason"], "quota")
        self.assertEqual(event["used_bytes"], 300_000_000)
        self.assertEqual(event["remote_state"], "deleted_verified")

    def test_quota_warning_notifications_are_thresholded_and_deduplicated(self):
        self.service.claim(123, "Min", self.now)
        self.outline.transfer = {"1": 240_000_000}
        self.assertEqual(self.service.enforce_quota(self.now + timedelta(hours=1)), 0)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT kind, dedupe_key, text FROM notifications ORDER BY created_at"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "quota_warning")
        self.assertIn(":v1:", rows[0]["dedupe_key"])
        self.assertIn("20.0%", rows[0]["text"])

        # Repeating the same observation does not send another message.
        self.assertEqual(self.service.enforce_quota(self.now + timedelta(hours=2)), 0)
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1
            )

        # A deeper crossing advances to the next threshold exactly once.
        self.outline.transfer = {"1": 276_000_000}
        self.assertEqual(self.service.enforce_quota(self.now + timedelta(hours=3)), 0)
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2
            )

    def test_failed_delete_is_retried_without_losing_enforcement_record(self):
        self.service.claim(123, "Min", self.now)
        original_delete = self.outline.delete_key
        self.outline.delete_key = lambda _key_id: (_ for _ in ()).throw(OutlineError("down"))
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24)), 0)
        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "retrying")
        self.assertEqual(event["delete_attempts"], 1)

        self.outline.delete_key = original_delete
        self.assertEqual(self.service.revoke_expired(self.now + timedelta(hours=24, minutes=1)), 1)
        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "deleted_verified")
        self.assertEqual(event["delete_attempts"], 2)

    def test_repeated_delete_failures_escalate_for_operator_attention(self):
        self.service.claim(123, "Min", self.now)
        self.outline.delete_key = lambda _key_id: (_ for _ in ()).throw(OutlineError("down"))

        for minute in range(10):
            self.service.revoke_expired(self.now + timedelta(hours=24, minutes=minute))

        event = self.service.termination_summary()[0]
        self.assertEqual(event["remote_state"], "escalated")
        self.assertEqual(event["delete_attempts"], 10)

    def test_user_usage_reports_only_owned_free_keys(self):
        self.service.claim(123, "Min", self.now)
        self.service.claim(456, "Other", self.now)
        self.outline.transfer = {"1": 75_000_000, "2": 200_000_000}
        usage = self.service.user_usage(123, self.outline.transfer)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["tier"], "Daily Free 300 MB")
        self.assertEqual(usage[0]["used_bytes"], 75_000_000)
        self.assertEqual(usage[0]["remaining_bytes"], 225_000_000)
        self.assertTrue(usage[0]["usage_observed"])

    def test_database_enforces_one_claim_timestamp_per_user(self):
        with open_sqlite_connection(self.db.path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(keys)")}
        self.assertIn("expires_at", columns)
        self.assertIn("status", columns)

    def test_concurrent_claims_only_one_succeeds(self):
        self.outline.create_delay = 0.01
        results = []

        def claim():
            results.append(self.service.claim(456, "Concurrent", self.now))

        t1 = threading.Thread(target=claim)
        t2 = threading.Thread(target=claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r.access_url is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(self.outline.created), 1)

    def test_giveaway_issues_exactly_five_unique_100_gb_winners(self):
        results = [
            self.service.claim_giveaway(user_id, f"User {user_id}", self.now)
            for user_id in range(1, 7)
        ]

        self.assertEqual([item.outcome for item in results], ["won"] * 5 + ["full"])
        self.assertEqual([item.winner_number for item in results[:5]], [1, 2, 3, 4, 5])
        self.assertTrue(all(item.remaining_slots == 0 for item in results[4:]))
        self.assertEqual(len(self.outline.created), 5)
        self.assertTrue(all(item[1] == 100_000_000_000 for item in self.outline.created))
        self.assertEqual(self.outline.created[0][0], "1-PROMO-100GBFREE-30day-202608270307")

    def test_concurrent_giveaway_claims_cannot_oversubscribe_five_slots(self):
        self.outline.create_delay = 0.01
        results = []
        threads = [
            threading.Thread(
                target=lambda user_id=user_id: results.append(
                    self.service.claim_giveaway(user_id, f"User {user_id}", self.now)
                )
            )
            for user_id in range(1, 9)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [item for item in results if item.outcome == "won"]
        self.assertEqual(len(winners), 5)
        self.assertEqual(sorted(item.winner_number for item in winners), [1, 2, 3, 4, 5])
        self.assertEqual(len(self.outline.created), 5)

    def test_giveaway_retry_is_idempotent_while_normal_plans_are_temporarily_paused(self):
        won = self.service.claim_giveaway(123, "Min", self.now, username="min")
        retried = self.service.claim_giveaway(123, "Min", self.now + timedelta(minutes=1))

        self.assertEqual(won.outcome, "won")
        self.assertEqual(retried.outcome, "already_won")
        self.assertEqual(retried.winner_number, 1)
        self.assertEqual(len(self.outline.created), 1)
        self.assertEqual(self.service.claim(123, "Min", self.now).denied_reason, "active_promo")
        self.assertEqual(
            self.service.claim_trial(123, "Min", self.now).denied_reason,
            "active_promo",
        )

    def test_failed_giveaway_provisioning_does_not_consume_slot(self):
        self.outline.fail_create = True
        with self.assertRaises(OutlineError):
            self.service.claim_giveaway(123, "Min", self.now)
        self.assertEqual(self.service.giveaway_status(123)["remaining_slots"], 5)

        self.outline.fail_create = False
        result = self.service.claim_giveaway(123, "Min", self.now)
        self.assertEqual(result.winner_number, 1)

    def test_giveaway_usage_is_labeled_separately_from_paid_plan(self):
        self.service.claim_giveaway(123, "Min", self.now)
        usage = self.service.user_usage(123, {"1": 25_000_000_000})
        self.assertEqual(usage[0]["tier"], "100 GB Promo · 100GBFREE")
        self.assertEqual(usage[0]["remaining_bytes"], 75_000_000_000)
        self.assertTrue(usage[0]["decimal_quota"])

    def test_custom_hourly_promo_resets_capacity_but_each_account_claims_once(self):
        campaign = self.service.configure_giveaway(
            code="HOUR12",
            quota_bytes=12_500_000_000,
            duration_days=7,
            winner_limit=2,
            frequency="hourly",
            starts_at=self.now - timedelta(hours=1),
            ends_at=self.now + timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(campaign["quota_bytes"], 12_500_000_000)
        self.assertEqual(campaign["campaign_state"], "active")

        first = self.service.claim_giveaway(1, "One", self.now, code="hour12")
        second = self.service.claim_giveaway(2, "Two", self.now, code="HOUR12")
        full = self.service.claim_giveaway(3, "Three", self.now, code="HOUR12")
        next_window = self.service.claim_giveaway(
            3, "Three", self.now + timedelta(hours=1), code="HOUR12"
        )
        repeated = self.service.claim_giveaway(
            1, "One", self.now + timedelta(hours=1), code="HOUR12"
        )

        self.assertEqual([first.outcome, second.outcome, full.outcome], ["won", "won", "full"])
        self.assertEqual(next_window.outcome, "won")
        self.assertEqual(repeated.outcome, "already_won")
        status = self.service.giveaway_status(4, "HOUR12", self.now + timedelta(hours=1))
        self.assertEqual(status["window_claimed_count"], 1)
        self.assertEqual(status["remaining_slots"], 1)

    def test_normal_free_plans_restore_when_gift_or_campaign_ends(self):
        self.service.claim_giveaway(123, "Min", self.now)
        self.assertEqual(
            self.service.claim(123, "Min", self.now).denied_reason,
            "active_promo",
        )

        self.service.set_giveaway_active("100GBFREE", False, now=self.now)
        daily = self.service.claim(123, "Min", self.now)
        monthly = self.service.claim_trial(123, "Min", self.now)
        self.assertIsNotNone(daily.access_url)
        self.assertIsNotNone(monthly.access_url)

        self.service.set_giveaway_active("100GBFREE", True, now=self.now)
        other = self.service.claim_giveaway(456, "Other", self.now)
        self.assertEqual(other.outcome, "won")
        after_expiry = self.service.claim(456, "Other", self.now + timedelta(days=31))
        self.assertIsNotNone(after_expiry.access_url)

    def test_quota_exhaustion_restores_regular_free_plans(self):
        gift = self.service.claim_giveaway(123, "Min", self.now)
        self.assertEqual(gift.quota_bytes, 100_000_000_000)

        self.service.enforce_quota(
            self.now,
            {"bytesTransferredByUserId": {"1": 100_000_000_000}},
        )

        self.assertIsNotNone(self.service.claim(123, "Min", self.now).access_url)

    def test_startup_reconciles_existing_remote_promo_to_exact_decimal_quota(self):
        self.service.claim_giveaway(123, "Min", self.now)
        self.outline.data_limits.clear()

        reconciled = self.service.reconcile_giveaway_limits()

        self.assertEqual(reconciled, 1)
        self.assertEqual(self.outline.data_limits, [("1", 100_000_000_000)])

    def test_outline_key_identity_is_unique_within_each_server(self):
        with open_sqlite_connection(self.db.path) as conn:
            indexes = conn.execute("PRAGMA index_list(keys)").fetchall()
            unique = next(index for index in indexes if index[1] == "free_keys_server_external")
            columns = [
                row[2]
                for row in conn.execute("PRAGMA index_info(free_keys_server_external)").fetchall()
            ]
        self.assertEqual(unique[2], 1)
        self.assertEqual(columns, ["server_id", "outline_key_id"])

    def test_free_keys_are_capacity_routed_and_remote_ids_are_server_scoped(self):
        pool = FakeOutlinePool()
        commerce = CommerceService(CommerceDatabase(self.db.path), pool, Fernet.generate_key())
        commerce.initialize()
        commerce.register_outline_servers({"sg-a": "Singapore A", "sg-b": "Singapore B"})
        commerce.refresh_server_inventory(self.now)
        commerce.configure_tier_allocation("sg-a", "FREE300MB", 1, 999)
        commerce.configure_tier_allocation("sg-b", "FREE300MB", 1, 999)
        service = ClaimService(self.db, pool)

        service.claim(101, "One", self.now)
        service.claim(202, "Two", self.now)

        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT server_id, outline_key_id FROM keys ORDER BY telegram_id"
            ).fetchall()
        self.assertEqual(
            [(row["server_id"], row["outline_key_id"]) for row in rows],
            [("sg-a", "1"), ("sg-b", "1")],
        )

    def test_partial_fleet_metrics_never_treat_missing_server_as_zero(self):
        pool = FakeOutlinePool()
        commerce = CommerceService(CommerceDatabase(self.db.path), pool, Fernet.generate_key())
        commerce.initialize()
        commerce.register_outline_servers()
        commerce.refresh_server_inventory(self.now)
        commerce.configure_tier_allocation("sg-a", "FREE300MB", 1, 999)
        commerce.configure_tier_allocation("sg-b", "FREE300MB", 1, 999)
        service = ClaimService(self.db, pool)
        service.claim(101, "One", self.now)
        service.claim(202, "Two", self.now)

        revoked = service.enforce_quota(
            self.now + timedelta(hours=1),
            metrics={"byServer": {"sg-b": {"1": 300_000_000}}, "errors": {"sg-a": "down"}},
        )

        self.assertEqual(revoked, 1)
        self.assertEqual(pool.clients["sg-a"].deleted, [])
        self.assertEqual(pool.clients["sg-b"].deleted, ["1"])

    def test_outline_get_key_uses_encoded_key_id(self):
        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, **_kwargs: calls.append((method, path)) or {
            "id": "a/b"
        }
        self.assertEqual(client.get_key("a/b")["id"], "a/b")
        self.assertEqual(calls, [("GET", "/access-keys/a%2Fb")])

    def test_outline_get_key_treats_404_as_absent(self):
        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, body=None, accepted_statuses=(
            200,
            201,
            204,
        ): calls.append((method, path, accepted_statuses))
        self.assertIsNone(client.get_key("missing"))
        self.assertEqual(calls[0][2], (200, 404))

    def test_outline_sets_hostname_for_access_keys(self):
        client = OutlineClient.__new__(OutlineClient)
        calls = []
        client._request = lambda method, path, body=None, accepted_statuses=(200, 201, 204): calls.append(
            (method, path, body, accepted_statuses)
        )
        client.set_hostname_for_access_keys("sg-a.vpn.example.com")
        self.assertEqual(
            calls,
            [("PUT", "/server/hostname-for-access-keys", {"hostname": "sg-a.vpn.example.com"}, (204,))],
        )


class FakeHTTPResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.body = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self.body


class FakePeerSocket:
    def __init__(self, certificate):
        self.certificate = certificate

    def getpeercert(self, binary_form=False):
        return self.certificate if binary_form else {}


class FakeHTTPSConnection:
    peer_certificate = b""
    instances = []

    def __init__(self, host, port, context, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method = None
        self.path = None
        self.body = None
        self.create_body = None
        FakeHTTPSConnection.instances.append(self)

    def connect(self):
        self.sock = FakePeerSocket(self.peer_certificate)

    def request(self, method, path, body=None, headers=None):
        self.method = method
        self.path = path
        self.body = body

    def getresponse(self):
        if self.method == "GET" and self.path == "/secret/server":
            return FakeHTTPResponse(200, {"version": "tls-test"})
        if self.method == "GET" and self.path == "/secret/access-keys":
            return FakeHTTPResponse(200, {"accessKeys": []})
        if self.method == "POST" and self.path == "/secret/access-keys":
            self.create_body = json.loads(self.body)
            return FakeHTTPResponse(
                201,
                {"id": "1", "name": self.create_body["name"], "accessUrl": "ss://test-only"},
            )
        if self.method == "PUT":
            return FakeHTTPResponse(204)
        if self.method == "DELETE":
            return FakeHTTPResponse(404)
        return FakeHTTPResponse(404)

    def close(self):
        return None


class OutlineClientTlsTest(unittest.TestCase):
    def setUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aurix-test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
            .sign(key, hashes.SHA256())
        )
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        FakeHTTPSConnection.peer_certificate = cert_der
        FakeHTTPSConnection.instances = []
        self.http_patch = patch("outline_adapter.http.client.HTTPSConnection", FakeHTTPSConnection)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        fingerprint = hashlib.sha256(cert_der).hexdigest()
        self.client = OutlineClient("https://outline.test:1234/secret", fingerprint)

    def test_pinned_outline_transport_and_404_delete(self):
        self.assertEqual(self.client.server_info()["version"], "tls-test")
        key = self.client.create_key("tls-check", 123)
        self.assertEqual(key["id"], "1")
        self.client.set_data_limit("a/b", 123)
        self.client.delete_key("already-gone")
        connection = FakeHTTPSConnection.instances[1]
        self.assertEqual(connection.create_body["limit"]["bytes"], 123)
        self.assertEqual(
            FakeHTTPSConnection.instances[2].path, "/secret/access-keys/a%2Fb/data-limit"
        )

    def test_wrong_certificate_pin_fails_closed_before_request(self):
        from app import OutlineClient

        client = OutlineClient("https://outline.test:1234/secret", "0" * 64)
        with self.assertRaises(OutlineError):
            client.server_info()
        self.assertIsNone(FakeHTTPSConnection.instances[-1].path)

    def test_outline_transport_uses_configured_bounded_timeout(self):
        client = OutlineClient(
            "https://outline.test:1234/secret",
            self.client.fingerprint,
            timeout_seconds=3,
        )

        client.server_info()

        self.assertEqual(FakeHTTPSConnection.instances[-1].timeout, 3.0)
        with self.assertRaises(ValueError):
            OutlineClient("https://outline.test/secret", "0" * 64, timeout_seconds=0.5)

    def test_transport_failure_opens_endpoint_circuit(self):
        client = OutlineClient(
            "https://outline.test:1234/secret",
            self.client.fingerprint,
            circuit_breaker_seconds=60,
        )
        with patch.object(FakeHTTPSConnection, "connect", side_effect=OSError("offline")):
            with self.assertRaises(OutlineError):
                client.server_info()
        connection_count = len(FakeHTTPSConnection.instances)

        with self.assertRaisesRegex(OutlineError, "cooling down"):
            client.server_info()

        self.assertEqual(len(FakeHTTPSConnection.instances), connection_count)

    def test_http_outage_opens_endpoint_circuit(self):
        client = OutlineClient(
            "https://outline.test:1234/secret",
            self.client.fingerprint,
            circuit_breaker_seconds=60,
        )
        with patch.object(
            FakeHTTPSConnection,
            "getresponse",
            return_value=FakeHTTPResponse(503, {"error": "overloaded"}),
        ):
            with self.assertRaisesRegex(OutlineError, "HTTP 503"):
                client.server_info()
        connection_count = len(FakeHTTPSConnection.instances)

        with self.assertRaisesRegex(OutlineError, "cooling down"):
            client.server_info()

        self.assertEqual(len(FakeHTTPSConnection.instances), connection_count)


class RecordingTelegramBot(TelegramBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []
        self.markups = []
        self.media = []

    def send(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)

    def send_photo(self, chat_id, file_id, caption="", reply_markup=None):
        self.media.append(("photo", chat_id, file_id, caption, reply_markup))

    def send_document(self, chat_id, file_id, caption="", reply_markup=None):
        self.media.append(("document", chat_id, file_id, caption, reply_markup))

    def send_receipt_bytes(
        self, chat_id, data, mime_type, caption, reply_markup, *, as_document=False
    ):
        self.media.append(
            (
                "stored_document" if as_document else "stored_photo",
                chat_id,
                data,
                caption,
                reply_markup,
            )
        )

    def send_local_photo(self, chat_id, path, caption="", reply_markup=None):
        self.media.append(("local_photo", chat_id, str(path), caption, reply_markup))

    def edit_local_photo(self, chat_id, message_id, path, caption="", reply_markup=None):
        self.media.append(
            ("edit_local_photo", chat_id, message_id, str(path), caption, reply_markup)
        )


class _TelegramPoolResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.data = json.dumps(payload or {"ok": True, "result": True}).encode()


class _RecordingTelegramPool:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return _TelegramPoolResponse()


class NonBlockingMaintenanceBot(TelegramBot):
    def __init__(self):
        super().__init__("test-token", object(), maintenance_interval_seconds=60)
        self.poll_started = threading.Event()
        self.maintenance_started = threading.Event()
        self.release_maintenance = threading.Event()

    def request(self, method, payload):
        self.poll_started.set()
        self.maintenance_started.wait(1)
        self.running = False
        return []

    def _run_maintenance(self):
        self.maintenance_started.set()
        self.release_maintenance.wait(2)


class _AlwaysNewUpdateDatabase:
    def mark_update_seen(self, _update_id):
        return True


class _PollingService:
    database = _AlwaysNewUpdateDatabase()


class HandlerFailureIsolationBot(TelegramBot):
    def __init__(self):
        super().__init__("test-token", _PollingService())
        self.poll_calls = 0
        self.handled = []
        self.backoffs = []
        self._maintenance_stop.wait = lambda timeout=None: self.backoffs.append(timeout) or False

    def request(self, method, payload):
        self.poll_calls += 1
        if self.poll_calls == 1:
            return [
                {"update_id": 1, "message": {"text": "fail"}},
                {"update_id": 2, "message": {"text": "continue"}},
            ]
        self.running = False
        return []

    def handle(self, message):
        if message["text"] == "fail":
            raise RuntimeError("simulated Telegram send failure")
        self.handled.append(message["text"])

    def _maintenance_loop(self):
        return None


class MaintenanceOutline:
    def __init__(self):
        self.calls = 0
        self.snapshot = {"bytesTransferredByUserId": {"key-1": 123}}

    def transfer_metrics(self):
        self.calls += 1
        return self.snapshot


class MaintenanceClaimService:
    def __init__(self, outline):
        self.outline = outline
        self.metrics = None
        self.expiry_calls = 0

    def enforce_quota(self, metrics=None):
        self.metrics = metrics
        return 0

    def revoke_expired(self):
        self.expiry_calls += 1
        return 0


class MaintenanceCommerceService:
    def __init__(self):
        self.metrics = None
        self.process_calls = 0
        self.snapshot_calls = 0

    def enforce_quotas(self, metrics=None):
        self.metrics = metrics
        return 0

    def expire_and_process(self):
        self.process_calls += 1
        return 0

    def capacity_snapshot(self, *, refresh_inventory=True):
        self.snapshot_calls += 1
        self.snapshot_refresh_inventory = refresh_inventory
        return {}


class FailingQuotaClaimService(MaintenanceClaimService):
    def enforce_quota(self, metrics=None):
        self.metrics = metrics
        raise RuntimeError("quota provider unavailable")


class TelegramBotCommerceTest(unittest.TestCase):
    def setUp(self):
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
        self.bot = RecordingTelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def message(telegram_id, text, chat_id=None):
        return {
            "chat": {"id": chat_id or telegram_id, "type": "private"},
            "from": {"id": telegram_id, "first_name": "Min"},
            "text": text,
        }

    def test_help_is_a_complete_outline_setup_flow_with_official_downloads(self):
        self.bot.handle(self.message(123, "/help"))

        text = self.bot.sent[-1][1]
        self.assertIn("🧭 Connect with Outline · quick setup", text)
        self.assertIn("An Outline key starts with ss://", text)
        self.assertIn("paste the complete key", text)
        self.assertIn("Check My IP", text)
        self.assertIn("Keep the ss:// key private", text)
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        by_label = {button["text"]: button for button in buttons}
        self.assertEqual(
            by_label["📱 iPhone / iPad"]["url"],
            "https://apps.apple.com/app/outline-app/id1356177741",
        )
        self.assertEqual(
            by_label["🤖 Android"]["url"],
            "https://play.google.com/store/apps/details?id=org.outline.android.client",
        )
        self.assertIn("🍎 macOS", by_label)
        self.assertIn("🪟 Windows", by_label)
        self.assertIn("🐧 Linux Guide", by_label)
        self.assertIn("📦 Android APK", by_label)
        self.assertEqual(by_label["🔐 Get / Copy My Key"]["callback_data"], "n:myvpn")
        self.assertEqual(by_label["🏠 Main Menu"]["callback_data"], "n:start")
        self.assertEqual(self.outline.created, [])

    def test_start_only_opens_menu_and_never_provisions_a_key(self):
        self.bot.handle(self.message(123, "/start"))

        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("🎉 AuriX VPN မှ ကြိုဆိုပါတယ်!", self.bot.sent[0][1])
        self.assertIn("100 GB Outline VPN • 30 days • Free", self.bot.sent[0][1])
        self.assertIn("🔥 5/5 gifts available for the whole campaign", self.bot.sent[0][1])
        self.assertIn("100GBFREE", self.bot.sent[0][1])
        self.assertIn("No payment or receipt", self.bot.sent[0][1])
        self.assertIn("not SIM/mobile data", self.bot.sent[0][1])
        self.assertIn("return automatically", self.bot.sent[0][1])
        promo_buttons = self.bot.markups[-1]["inline_keyboard"][0]
        self.assertFalse(any("copy_text" in button for button in promo_buttons))
        self.assertEqual(
            next(button for button in promo_buttons if button["text"].startswith("🎁 Redeem"))[
                "callback_data"
            ],
            "g:c:100GBFREE",
        )
        self.assertEqual(self.outline.created, [])
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM keys WHERE telegram_id = ?", (123,)
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT last_claim_at FROM users WHERE telegram_id = ?", (123,)
                ).fetchone()["last_claim_at"]
            )

    def test_custom_topup_amount_resumes_after_bot_restart(self):
        self.bot._expect_customer_input(123, "topup_amount")
        restarted = RecordingTelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
        )

        restarted.handle(self.message(123, "7500"))

        with self.db.connect() as connection:
            order = connection.execute(
                "SELECT plan_code, amount_minor, status FROM orders WHERE telegram_id = 123"
            ).fetchone()
        self.assertEqual(tuple(order), ("wallet_topup", 7500, "awaiting_payment"))
        self.assertTrue(restarted.media or restarted.sent)
        self.assertIsNone(
            self.commerce.database.load_interaction_state(123, "customer_input")
        )

    def test_owner_selects_and_persists_control_group_via_chat_shared(self):
        access = StaffAccessControl(self.db, 999)
        access.bootstrap(owner_id=999)
        bot = RecordingTelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
            staff_access=access,
        )
        bot._refresh_staff_scopes = lambda: None
        bot._control_group_staff = lambda group_id=None: (
            {"id": 999, "display_name": "Owner"},
            [],
        )
        bot.request = lambda method, payload: (
            {"id": -100123, "title": "AuriX Group"} if method == "getChat" else 2
        )

        bot._send_control_group_picker(999)
        request_chat = bot.markups[-1]["keyboard"][0][0]["request_chat"]
        self.assertEqual(request_chat["request_id"], bot.CONTROL_GROUP_REQUEST_ID)
        self.assertTrue(request_chat["bot_is_member"])

        bot.handle(
            {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999, "first_name": "Owner"},
                "chat_shared": {
                    "request_id": bot.CONTROL_GROUP_REQUEST_ID,
                    "chat_id": -100123,
                    "title": "AuriX Group",
                },
            }
        )

        self.assertEqual(bot.control_group_id, -100123)
        self.assertEqual(access.control_group()["control_group_id"], -100123)
        self.assertIn("control group connected", bot.sent[-1][1])
        self.assertIn("Human owner: 1 verified — you", bot.sent[-1][1])
        self.assertIn("Additional human administrators: 0", bot.sent[-1][1])
        self.assertIn("Bot accounts imported as staff: 0", bot.sent[-1][1])
        self.assertEqual(bot.markups[-1], {"remove_keyboard": True})

    def test_admin_home_message_and_navigation_are_stable_contracts(self):
        self.bot.handle(self.message(999, "/admin"))

        self.assertEqual(
            self.bot.sent[-1][1],
            "AuriX Admin\n\n"
            "Daily flow: Pending Orders → open receipt → verify the transaction "
            "against your receiving account → Approve.\n"
            "Use Failed Jobs to retry a reviewed Outline failure, open an order "
            "to inspect its wallet ledger, and run Consistency before taking "
            "payment decisions.\n\n"
            "Queue: 0 receipt(s) pending · 0 upload(s) pending · "
            "0 upload(s) failed · 0 failed job(s) · "
            "0 key repair(s) pending · 0 key repair(s) manual · 0 stale review(s) · "
            "0 dead notification(s)",
        )
        expected_markup = self.bot.markups[-1]
        labels = {button["text"] for row in expected_markup["inline_keyboard"] for button in row}
        self.assertIn("🧪 Receipt System", labels)
        self.assertIn("🎁 Promotions", labels)
        self.assertTrue(
            all(
                len(button["callback_data"].encode()) <= 64
                for row in expected_markup["inline_keyboard"]
                for button in row
            )
        )

    def test_owner_control_center_exposes_every_admin_area_and_staff_management(self):
        labels = {
            button["text"]
            for row in self.bot._owner_keyboard()["inline_keyboard"]
            for button in row
        }
        self.assertTrue(
            {
                "📊 Admin Dashboard",
                "👥 Staff & Access",
                "📥 Pending Orders",
                "🧾 Receipt Review",
                "🧪 Receipt System",
                "🎁 Promotions",
                "📈 Capacity",
                "🔎 Consistency",
                "🔁 Failed Jobs",
                "🚨 Enforcement",
                "🏢 Control Group",
                "🔄 Group Sync",
            }.issubset(labels)
        )

    def test_staff_panel_refresh_reuses_same_message(self):
        access = StaffAccessControl(self.db, 999)
        access.bootstrap(owner_id=999)
        bot_service = ClaimService(self.db, self.outline)
        bot_service.track_user(123, "Member", username="member")
        access.add_admin(123, 999)
        bot = RecordingTelegramBot(
            "test-token",
            bot_service,
            self.commerce,
            {999},
            {123},
            staff_access=access,
        )
        bot.handle(self.message(999, "/staff"))
        self.assertIn("Staff & Access", bot.sent[-1][1])
        refresh = next(
            button["callback_data"]
            for row in bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["text"] == "🔄 Refresh"
        )
        requests = []
        bot.request = lambda method, payload: requests.append((method, payload)) or True
        bot.handle_callback(
            {
                "id": "staff-refresh",
                "from": {"id": 999, "first_name": "Owner"},
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "message_id": 42,
                    "text": bot.sent[-1][1],
                },
                "data": refresh,
            }
        )
        self.assertTrue(any(method == "editMessageText" for method, _ in requests))
        self.assertFalse(any(method == "sendMessage" for method, _ in requests))

    def test_admin_home_navigation_reuses_same_message(self):
        self.bot.handle(self.message(999, "/admin"))
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "admin-home",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "message_id": 43,
                    "text": self.bot.sent[-1][1],
                },
                "data": "a:n:admin",
            }
        )
        self.assertTrue(any(method == "editMessageText" for method, _ in requests))
        self.assertFalse(any(method == "sendMessage" for method, _ in requests))

    def test_customer_can_create_and_submit_a_paid_order(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order = self.commerce.list_pending_orders()[0]
        self.assertIn(order["id"][:8], self.bot.media[-1][3])

        self.bot.handle(self.message(123, f"/paid {order['id']} transfer-123"))
        self.assertEqual(self.commerce.list_pending_orders()[0]["status"], "payment_submitted")
        self.assertTrue(any("Payment recorded" in text for _, text in self.bot.sent))

    def test_uploaded_photo_notifies_admin_without_persisting_image_in_telegram(self):
        StaffAccessControl(self.db, 999).bootstrap(owner_id=999)
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        self.commerce.choose_payment_method(123, order_id, "kbzpay")
        self.bot._download_telegram_file = lambda _file_id: (b"photo-receipt", "image/jpeg")
        media_count = len(self.bot.media)

        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "first_name": "Min"},
                "caption": f"/paid {order_id}",
                "photo": [
                    {"file_id": "small-photo", "file_unique_id": "small"},
                    {"file_id": "large-photo", "file_unique_id": "large"},
                ],
            }
        )

        receipt = self.commerce.list_pending_receipts()[0]
        self.assertEqual(receipt["provider"], "kbzpay")
        self.assertEqual(len(self.bot.media), media_count)
        self.bot._send_pending_notifications()
        self.assertIn(f"<b>Evidence:</b> {receipt['id'][:10]}", self.bot.sent[-1][1])
        markup = self.bot.markups[-1]
        labels = {button["text"] for row in markup["inline_keyboard"] for button in row}
        self.assertIn("🧾 Open Receipt", labels)
        self.assertIn("⚙ Alerts", labels)

    def test_image_document_keeps_its_media_type_for_admin_review(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        self.bot._download_telegram_file = lambda _file_id: (b"document-receipt", "image/png")
        media_count = len(self.bot.media)

        self.bot.handle(
            {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "first_name": "Min"},
                "caption": f"/paid {order_id}",
                "document": {
                    "file_id": "receipt-document",
                    "file_unique_id": "document-unique",
                    "mime_type": "image/png",
                },
            }
        )

        receipt = self.commerce.get_receipt(self.commerce.list_pending_receipts()[0]["id"])
        self.assertEqual(receipt["telegram_media_type"], "document")
        self.assertEqual(len(self.bot.media), media_count)

        self.bot.handle(self.message(999, f"/receipt {receipt['id']}"))
        self.assertEqual(self.bot.media[-1][:3], ("document", 999, "receipt-document"))

    def test_receipt_review_retries_legacy_file_id_as_document(self):
        calls = []
        self.bot.send_photo = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("wrong file identifier")
        )
        self.bot.send_document = (
            lambda chat_id, file_id, caption="", reply_markup=None: calls.append(
                (chat_id, file_id, caption, reply_markup)
            )
        )
        receipt = {
            "id": "evidence-1",
            "order_id": "order-1",
            "telegram_id": 123,
            "telegram_file_id": "legacy-document-id",
            "amount_minor": 3000,
            "currency": "MMK",
            "extraction": {},
            "telegram_media_type": "photo",
        }

        self.bot._send_receipt_review(999, receipt)

        self.assertEqual(calls[0][0:2], (999, "legacy-document-id"))

    def test_receipt_review_prefers_private_storage_bytes(self):
        self.commerce.receipt_storage = FakeReceiptStorage()
        receipt = {
            "id": "evidence-storage",
            "order_id": "order-storage",
            "telegram_id": 123,
            "telegram_file_id": "legacy-file-id",
            "storage_path": "orders/order-storage/evidence-storage.jpg",
            "storage_status": "stored",
            "amount_minor": 3000,
            "currency": "MMK",
            "extraction": {},
            "telegram_media_type": "photo",
        }
        self.bot._send_receipt_review(999, receipt)
        self.assertEqual(self.bot.media[-1][0:3], ("stored_photo", 999, b"stored-receipt"))

    def test_receipt_review_recovers_rejected_legacy_id_via_get_file(self):
        self.bot.send_photo = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("wrong file identifier")
        )
        self.bot.send_document = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("wrong file identifier")
        )
        self.bot._download_telegram_file = lambda _file_id: (b"legacy-bytes", "image/jpeg")
        receipt = {
            "id": "evidence-legacy",
            "order_id": "order-legacy",
            "telegram_id": 123,
            "telegram_file_id": "legacy-id",
            "amount_minor": 3000,
            "currency": "MMK",
            "extraction": {},
            "telegram_media_type": "photo",
        }

        self.bot._send_receipt_review(999, receipt)

        self.assertEqual(self.bot.media[-1][0:3], ("stored_photo", 999, b"legacy-bytes"))

    def test_receipt_verify_button_uses_extracted_candidate_without_typed_ids(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        self.commerce.choose_payment_method(123, order_id, "kbzpay")
        result = self.commerce.submit_receipt(
            123,
            order_id,
            "kbzpay",
            "receipt-file",
            "receipt-unique",
            b"receipt-candidate",
            "image/jpeg",
            {"transaction_id": "TX-7788", "amount_minor": 3000},
        )
        evidence_id = result["evidence_id"]
        receipt = self.commerce.get_receipt(evidence_id)
        self.bot._send_receipt_review(999, receipt)
        self.assertIn("⚠️ Risk flags:", self.bot.media[-1][3])
        labels = {
            button["text"] for row in self.bot.media[-1][4]["inline_keyboard"] for button in row
        }
        self.assertIn("✅ Verify Payment", labels)
        self.bot.request = lambda _method, _payload: True

        self.bot.handle_callback(
            {
                "id": "verify-receipt",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:v:{evidence_id}",
            }
        )

        self.assertIn("TX-7788", self.bot.sent[-1][1])
        self.assertIn("3,000 MMK", self.bot.sent[-1][1])
        confirm_labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("✅ I Checked · Verify", confirm_labels)

    def test_repeated_buy_returns_existing_order_and_myorders_tracks_it(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        self.bot.handle(self.message(123, "💎 Upgrade 50GB"))
        self.assertIn("Existing open order", self.bot.sent[-1][1])
        self.assertIn(order_id, self.bot.sent[-1][1])
        self.bot.handle(self.message(123, "🧾 My Orders"))
        self.assertIn("Your recent orders", self.bot.sent[-1][1])
        self.assertIn(order_id, self.bot.sent[-1][1])
        self.bot.handle(self.message(123, f"/order {order_id}"))
        self.assertIn("AuriX Order", self.bot.sent[-1][1])
        self.assertIn("Payment: not submitted", self.bot.sent[-1][1])

    def test_uncaptioned_receipt_routing_refuses_ambiguous_multiple_orders(self):
        first = self.commerce.create_order(123, "Min", "basic_50gb")
        second_id = "manually-created-open-order"
        with self.commerce.database.connect() as connection:
            connection.execute(
                """INSERT INTO orders
                   (id, telegram_id, plan_code, amount_minor, currency, plan_name,
                    quota_bytes_snapshot, duration_days_snapshot, status, created_at)
                   VALUES (?, 123, 'standard_100gb', 6000, 'MMK', '100 GB',
                           100000000000, 30, 'awaiting_payment', ?)""",
                (second_id, datetime.now(UTC).isoformat()),
            )

        self.assertIsNone(self.bot._pending_order_id(123))
        self.bot._expect_receipt_order(123, second_id)
        self.assertEqual(self.bot._pending_order_id(123), second_id)
        self.assertNotEqual(first.order_id, second_id)

    def test_wallet_topup_presets_and_manual_amount_use_local_payment_qr(self):
        self.bot.handle(self.message(123, "/wallet"))
        self.bot.request = lambda _method, _payload: True
        self.assertEqual(
            self.bot.markups[-1]["inline_keyboard"][0][0]["callback_data"],
            "t:a:menu",
        )

        self.bot.handle_callback(
            {
                "id": "topup-menu",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "t:a:menu",
            }
        )
        amount_buttons = {
            button["callback_data"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("t:a:3000", amount_buttons)
        self.assertIn("t:a:custom", amount_buttons)

        self.bot.handle_callback(
            {
                "id": "topup-custom",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "t:a:custom",
            }
        )
        self.bot.handle(self.message(123, "7,500"))

        order = self.commerce.list_pending_orders()[0]
        self.assertEqual(order["plan_code"], "wallet_topup")
        self.assertEqual(order["amount_minor"], 7500)
        self.assertEqual(self.bot.media[-1][0], "local_photo")
        self.assertIn("7,500 MMK", self.bot.media[-1][3])
        labels = {
            button["text"] for row in self.bot.media[-1][-1]["inline_keyboard"] for button in row
        }
        self.assertNotIn("💰 Pay Wallet", labels)

    def test_order_category_navigation_edits_the_pressed_message(self):
        self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(123, "/myorders"))
        sent_count = len(self.bot.sent)
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True

        self.bot.handle_callback(
            {
                "id": "orders-all",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 71,
                    "text": self.bot.sent[-1][1],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": "c:o:all:0",
            }
        )

        self.assertEqual(len(self.bot.sent), sent_count)
        edits = [payload for method, payload in calls if method == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], 71)
        self.assertIn("All · 1 order(s)", edits[0]["text"])

    def test_new_order_uses_compact_payment_method_chooser_in_required_order(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]

        self.assertEqual(self.bot.media[-1][0], "local_photo")
        self.assertTrue(self.bot.media[-1][2].endswith("assets/payment_qr/kbzpay.png"))
        self.assertIn("Payment QR 1/5 · KBZPay", self.bot.media[-1][3])
        self.assertIn("✅ Order created", self.bot.media[-1][3])
        markup = self.bot.media[-1][-1]
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        method_buttons = [
            button["text"]
            for button in buttons
            if button.get("callback_data", "").startswith("m:s:")
        ]
        self.assertEqual(
            method_buttons,
            [
                "✓ 📷 1 · KBZPay",
                "📷 2 · WavePay",
                "📷 3 · AYA Pay",
                "📷 4 · UABPay",
                "📷 5 · CB Pay",
            ],
        )

        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "payment-method-1",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 50,
                    "photo": [{"file_id": "existing-kbz-card"}],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": f"m:s:wavepay:{order_id}",
            }
        )

        self.assertEqual(calls[0][0], "answerCallbackQuery")
        self.assertEqual(self.bot.media[-1][0], "edit_local_photo")
        self.assertTrue(self.bot.media[-1][3].endswith("assets/payment_qr/wavepay.png"))
        detail = self.commerce.order_detail(order_id, 123)
        self.assertEqual(detail["payment_method"], "wavepay")
        qr_markup = self.bot.media[-1][-1]
        qr_labels = [button["text"] for row in qr_markup["inline_keyboard"] for button in row]
        self.assertIn("✓ 📷 2 · WavePay", qr_labels)
        self.assertIn("✅ I’ve Paid · Send Receipt", qr_labels)

        self.bot.handle_callback(
            {
                "id": "payment-method-2",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 51,
                    "photo": [{"file_id": "existing-card"}],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": f"m:s:ayapay:{order_id}",
            }
        )
        self.assertEqual(self.bot.media[-1][0], "edit_local_photo")
        self.assertEqual(self.bot.media[-1][2], 51)
        self.assertTrue(self.bot.media[-1][3].endswith("assets/payment_qr/ayapay.png"))
        self.assertEqual(self.commerce.order_detail(order_id, 123)["payment_method"], "ayapay")

    def test_different_plan_button_requires_explicit_replacement(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        self.bot.handle(self.message(123, "/buy standard_100gb"))
        self.assertIn("open order", self.bot.sent[-1][1])
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("Replace Open Order", labels)

    def test_inline_order_buttons_route_without_copying_ids(self):
        self.bot.handle(self.message(123, "/buy basic_50gb"))
        order_id = self.commerce.list_pending_orders()[0]["id"]
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "callback-1",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": f"o:v:{order_id}",
            }
        )
        self.assertEqual(calls[0][0], "answerCallbackQuery")
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("🖼 Open KBZPay QR", labels)
        self.assertIn("🔁 Change QR", labels)
        self.assertIn("💰 Pay Wallet", labels)
        self.assertIn("🔄 Refresh", labels)

    def test_order_list_and_order_refresh_edit_the_interacting_message(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(123, "/myorders"))
        sent_count = len(self.bot.sent)
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True

        self.bot.handle_callback(
            {
                "id": "orders-refresh",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 61,
                    "text": self.bot.sent[-1][1],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": "n:myorders",
            }
        )
        self.bot.handle_callback(
            {
                "id": "order-refresh",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 61,
                    "text": "order detail",
                    "chat": {"id": 123, "type": "private"},
                },
                "data": f"o:v:{order.order_id}",
            }
        )

        self.assertEqual(len(self.bot.sent), sent_count)
        edits = [payload for method, payload in calls if method == "editMessageText"]
        self.assertEqual([payload["message_id"] for payload in edits], [61, 61])
        self.assertIn("Your recent orders", edits[0]["text"])
        self.assertIn("AuriX Order", edits[1]["text"])

    def test_polling_remains_available_while_housekeeping_is_blocked(self):
        bot = NonBlockingMaintenanceBot()
        thread = threading.Thread(target=bot.run)
        thread.start()
        try:
            self.assertTrue(bot.maintenance_started.wait(1))
            self.assertTrue(bot.poll_started.wait(1))
        finally:
            bot.release_maintenance.set()
            bot.stop()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_one_failed_update_does_not_backoff_or_block_the_next_update(self):
        bot = HandlerFailureIsolationBot()

        bot.run()

        self.assertEqual(bot.handled, ["continue"])
        self.assertEqual(bot.poll_calls, 2)
        self.assertNotIn(5, bot.backoffs)

    def test_telegram_requests_reuse_the_bounded_connection_pool(self):
        pool = _RecordingTelegramPool()
        self.bot._http = pool

        self.assertTrue(self.bot.request("sendMessage", {"chat_id": 123, "text": "one"}))
        self.assertTrue(self.bot.request("sendMessage", {"chat_id": 123, "text": "two"}))

        self.assertEqual(len(pool.calls), 2)
        for method, _url, kwargs in pool.calls:
            self.assertEqual(method, "POST")
            self.assertFalse(kwargs["retries"])
            self.assertEqual(kwargs["timeout"].connect_timeout, 5.0)
            self.assertEqual(kwargs["timeout"].read_timeout, 30.0)

    def test_local_qr_photos_use_multipart_upload_and_in_place_media_edit(self):
        pool = _RecordingTelegramPool()
        bot = TelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
        )
        bot._http = pool
        path = bot.PAYMENT_QR_DIR / "kbzpay.png"
        markup = {"inline_keyboard": [[{"text": "Order", "callback_data": "o:v:test"}]]}

        bot.send_local_photo(123, path, "payment card", markup)
        bot.edit_local_photo(123, 77, path, "changed card", markup)

        self.assertEqual(len(pool.calls), 2)
        self.assertTrue(pool.calls[0][1].endswith("/sendPhoto"))
        self.assertTrue(pool.calls[1][1].endswith("/editMessageMedia"))
        for _method, _url, kwargs in pool.calls:
            self.assertTrue(kwargs["headers"]["Content-Type"].startswith("multipart/form-data;"))
            self.assertIn(b'name="photo"', kwargs["body"])
            self.assertIn(b'filename="kbzpay.png"', kwargs["body"])
        self.assertIn(b"attach://photo", pool.calls[1][2]["body"])

    def test_telegram_error_description_is_bounded_and_payload_free(self):
        self.bot._http = _RecordingTelegramPool(
            [
                _TelegramPoolResponse(
                    400, {"ok": False, "description": "Bad Request: query is too old"}
                )
            ]
        )

        with self.assertRaisesRegex(TelegramAPIError, "query is too old"):
            self.bot.request("answerCallbackQuery", {"callback_query_id": "secret-id"})

    def test_telegram_http_200_api_error_keeps_bounded_description(self):
        self.bot._http = _RecordingTelegramPool(
            [
                _TelegramPoolResponse(
                    200, {"ok": False, "description": "Bad Request: command scope unavailable"}
                )
            ]
        )

        with self.assertRaisesRegex(TelegramAPIError, "command scope unavailable"):
            self.bot.request("setMyCommands", {"commands": [], "scope": {"type": "default"}})

    def test_technical_identifiers_are_masked_with_recognizable_edges(self):
        self.assertEqual(
            self.bot._mask_technical_value("157-245-63-95.sslip.io"),
            "157-****p.io",
        )
        self.assertEqual(self.bot._mask_technical_value("short"), "****")

    def test_maintenance_reuses_one_outline_metrics_snapshot(self):
        outline = MaintenanceOutline()
        claim = MaintenanceClaimService(outline)
        commerce = MaintenanceCommerceService()
        bot = TelegramBot("test-token", claim, commerce)
        bot._send_termination_notices = lambda: None
        bot._send_pending_notifications = lambda: None

        bot._run_maintenance()

        self.assertEqual(outline.calls, 1)
        self.assertIs(claim.metrics, outline.snapshot)
        self.assertIs(commerce.metrics, outline.snapshot)
        self.assertEqual(claim.expiry_calls, 1)
        self.assertEqual(commerce.process_calls, 1)
        self.assertEqual(commerce.snapshot_calls, 1)
        self.assertFalse(commerce.snapshot_refresh_inventory)

    def test_maintenance_expiry_runs_when_quota_stage_fails(self):
        outline = MaintenanceOutline()
        claim = FailingQuotaClaimService(outline)
        bot = TelegramBot("test-token", claim)
        bot._send_termination_notices = lambda: None

        bot._run_maintenance()

        self.assertEqual(claim.expiry_calls, 1)
        self.assertEqual(bot._maintenance_last_status["status"], "error")
        self.assertIn("free_quota", bot._maintenance_last_status["last_error"])

    def test_termination_notice_explains_existing_session_limit(self):
        class NoticeService:
            def __init__(self):
                self.marked = []

            def pending_termination_notices(self, audience):
                if audience != "user" or self.marked:
                    return []
                return [
                    {
                        "id": 1,
                        "telegram_id": 123,
                        "reason": "quota",
                        "remote_state": "deleted_verified",
                        "used_bytes": 300_000_000,
                        "quota_bytes": 300_000_000,
                        "expires_at": "2026-09-04T00:00:00+00:00",
                    }
                ]

            def mark_termination_notice(self, event_id, audience, state):
                self.marked.append((event_id, audience, state))

        bot = RecordingTelegramBot("test-token", object())
        bot.service = NoticeService()
        bot._send_termination_notices()

        self.assertIn("New connections are blocked", bot.sent[-1][1])
        self.assertIn("already-established session may linger", bot.sent[-1][1])
        self.assertIn("no documented per-key force-disconnect", bot.sent[-1][1])

    def test_maintenance_heartbeat_is_persisted(self):
        bot = self.bot
        bot._send_termination_notices = lambda: None
        bot._send_pending_notifications = lambda: None

        bot._run_maintenance()

        heartbeat = self.db.get_maintenance_heartbeat()
        self.assertIsNotNone(heartbeat)
        self.assertIsNotNone(heartbeat["last_started_at"])
        self.assertIsNotNone(heartbeat["last_completed_at"])
        self.assertIsNotNone(heartbeat["last_success_at"])
        self.assertEqual(heartbeat["last_stage"], "completed")

    def test_admin_panel_refresh_reuses_same_message(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, "/orders"))
        self.assertIn(order.order_id[:8], self.bot.sent[-1][1])
        sent_count = len(self.bot.sent)
        refresh = next(
            button["callback_data"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["text"] == "🔄 Refresh"
        )
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "panel-refresh",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}, "message_id": 77},
                "data": refresh,
            }
        )
        self.assertEqual(len(self.bot.sent), sent_count)
        self.assertTrue(any(method == "editMessageText" for method, _payload in requests))

    def test_staff_can_control_their_own_operational_alerts(self):
        access = StaffAccessControl(self.db, 999)
        access.bootstrap(owner_id=999)
        self.bot.staff_access = access
        self.bot.admin_operations.staff_access = access

        self.bot.handle(self.message(999, "/notifications"))
        self.assertIn("✅ New orders: On", self.bot.sent[-1][1])
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "toggle-order-alert",
                "from": {"id": 999, "first_name": "Owner"},
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "message_id": 91,
                    "text": self.bot.sent[-1][1],
                },
                "data": "a:u:order_created",
            }
        )

        self.assertFalse(access.notification_preferences(999)["order_created"])
        edit = next(payload for method, payload in requests if method == "editMessageText")
        self.assertEqual(edit["message_id"], 91)
        self.assertIn("🔕 New orders: Off", edit["text"])

    def test_customer_usage_alerts_are_separate_and_edit_in_place(self):
        self.bot.handle(self.message(123, "/alerts"))
        self.assertIn("Your VPN Usage Alerts", self.bot.sent[-1][1])
        self.assertIn("operational alerts", self.bot.sent[-1][1].lower())
        edits = []
        self.bot.edit_message = lambda chat_id, message_id, text, markup=None: edits.append(
            (chat_id, message_id, text, markup)
        )
        self.bot.request = lambda _method, _payload: True

        self.bot.handle_callback(
            {
                "id": "quota-alert-mode",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 77,
                    "chat": {"id": 123, "type": "private"},
                },
                "data": "q:m:mb",
            }
        )

        preferences = self.bot.service.quota_alert_preferences(123)
        self.assertEqual(preferences["mode"], "mb")
        self.assertEqual(edits[0][1], 77)
        self.assertIn("MB remaining", edits[0][2])

    def test_order_alert_fanout_respects_staff_preference(self):
        access = StaffAccessControl(self.db, 999)
        access.bootstrap(owner_id=999)

        first = self.commerce.create_order(123, "Min", "basic_50gb")
        alerts = [
            item
            for item in self.commerce.pending_notifications(limit=100)
            if item["kind"] == "staff_order_created"
        ]
        self.assertEqual(len(alerts), 1)

        self.commerce.reject_order(first.order_id, 999)
        rejection_alerts = [
            item
            for item in self.commerce.pending_notifications(limit=100)
            if item["kind"] == "staff_rejected"
        ]
        self.assertEqual(len(rejection_alerts), 1)
        self.assertIn(first.order_id[:8], rejection_alerts[0]["text"])
        self.assertIn(first.order_id[:8], alerts[0]["text"])

        access.set_notification_preference(999, "order_created", False)
        self.commerce.create_order(456, "Other", "basic_50gb")
        alerts = [
            item
            for item in self.commerce.pending_notifications(limit=100)
            if item["kind"] == "staff_order_created"
        ]
        self.assertEqual(len(alerts), 1)

    def test_receipt_system_refresh_edits_the_interacting_message(self):
        self.bot.handle(self.message(999, "/receiptsystem"))
        sent_count = len(self.bot.sent)
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True

        self.bot.handle_callback(
            {
                "id": "receipt-system-refresh",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {
                    "chat": {"id": 999, "type": "private"},
                    "message_id": 76,
                    "text": self.bot.sent[-1][1],
                },
                "data": "a:n:receiptsystem",
            }
        )

        self.assertEqual(len(self.bot.sent), sent_count)
        edit = next(payload for method, payload in requests if method == "editMessageText")
        self.assertEqual(edit["message_id"], 76)
        self.assertIn("Receipt Verification", edit["text"])

    def test_admin_first_and_last_page_buttons_edit_the_same_message(self):
        for telegram_id in range(200, 212):
            self.commerce.create_order(telegram_id, f"User {telegram_id}", "basic_50gb")
        self.bot.handle(self.message(999, "/orders"))
        last = next(
            button["callback_data"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["text"] == "Last ⏭"
        )
        sent_count = len(self.bot.sent)
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True

        self.bot.handle_callback(
            {
                "id": "admin-last",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}, "message_id": 78},
                "data": last,
            }
        )

        self.assertEqual(len(self.bot.sent), sent_count)
        edit = next(payload for method, payload in requests if method == "editMessageText")
        self.assertEqual(edit["message_id"], 78)
        self.assertIn("Page 3/3", edit["text"])
        self.assertTrue(
            any(
                button["text"] == "⏮ First"
                for row in edit["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )

    def test_maintenance_delivers_free_quota_warning_through_telegram(self):
        now = datetime(2026, 8, 27, 3, 7, tzinfo=UTC)
        self.bot.service.claim(123, "Min", now)
        self.outline.transfer = {"1": 240_000_000}

        self.bot._run_maintenance()

        warning_texts = [text for _chat_id, text in self.bot.sent if "VPN usage alert" in text]
        self.assertEqual(len(warning_texts), 1)
        self.assertIn("20.0%", warning_texts[0])
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM notifications WHERE kind = 'quota_warning'"
                ).fetchone()[0],
                "sent",
            )

    def test_admin_reject_button_requires_confirmation(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        calls = []
        self.bot.request = lambda method, payload: calls.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "callback-admin",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:x:{order.order_id}",
            }
        )
        self.assertIn("Order:", self.bot.sent[-1][1])
        self.assertIn("close the order", self.bot.sent[-1][1])
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "awaiting_payment",
        )
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("Confirm Reject", labels)

    def test_admin_commands_are_allowlisted_and_malformed_updates_are_ignored(self):
        self.bot.handle(self.message(123, "/orders"))
        self.assertEqual(
            self.bot.sent[-1][1],
            "Use the menu to choose an AuriX action.",
        )

        self.bot.handle(self.message(999, "/orders"))
        self.assertEqual(self.bot.sent[-1][1], "No pending orders.")
        self.bot.handle({"chat": None, "from": None, "text": "/orders"})
        self.assertEqual(self.bot.sent[-1][1], "No pending orders.")

    def test_all_admin_commands_and_callbacks_are_generic_for_customers(self):
        calls = []

        def bomb(name):
            def fail(*_args, **_kwargs):
                calls.append(name)
                raise AssertionError(f"privileged method called: {name}")

            return fail

        for name in (
            "list_pending_orders",
            "list_pending_receipts",
            "get_receipt",
            "verify_receipt",
            "reject_receipt",
            "capacity_snapshot",
            "consistency_report",
            "failed_jobs",
            "retry_failed_job",
            "refund_order",
            "approve_order",
            "reject_order",
        ):
            setattr(self.commerce, name, bomb(name))
        self.bot.service.termination_summary = bomb("termination_summary")
        commands = (
            "/admin",
            "/orders",
            "/receipts",
            "/capacity",
            "/reconcile",
            "/enforcement",
            "/failed",
            "/retry order-id",
            "/ledger 123",
            "/refund order-id",
            "/receipt evidence-id",
            "/verify evidence-id tx-id 3000",
            "/rejectreceipt evidence-id",
            "/approve order-id",
            "/reject order-id",
        )
        for command in commands:
            self.bot.handle(self.message(123, command))
            self.assertEqual(self.bot.sent[-1][1], self.bot.UNKNOWN_ACTION_TEXT)
            labels = {button["text"] for row in self.bot.markups[-1]["keyboard"] for button in row}
            self.assertNotIn("🛠 Admin Panel", labels)

        self.bot.request = lambda _method, _payload: True
        for data in (
            "a:malformed",
            "a:n:orders",
            "a:o:order-id",
            "a:k:expired-token",
            "a:p:order-id",
            "a:l:123",
            "a:f:order-id",
            "a:a:order-id",
            "a:x:order-id",
            "a:q:evidence-id",
        ):
            self.bot.handle_callback(
                {
                    "id": "callback-customer",
                    "from": {"id": 123, "first_name": "Min"},
                    "message": {"chat": {"id": 123, "type": "private"}},
                    "data": data,
                }
            )
            self.assertEqual(self.bot.sent[-1][1], self.bot.UNKNOWN_ACTION_TEXT)
        self.assertEqual(calls, [])

    def test_admin_button_labels_are_separate_and_customer_menu_is_side_effect_free(self):
        self.bot.handle(self.message(123, "📥 Pending Orders"))
        self.assertEqual(self.bot.sent[-1][1], self.bot.UNKNOWN_ACTION_TEXT)

        self.bot.handle(self.message(999, "🔁 Failed Jobs"))
        self.assertEqual(self.bot.sent[-1][1], "No terminal worker failures.")
        self.assertEqual(self.bot.markups[-1].keys(), {"inline_keyboard"})

        self.bot.handle(self.message(999, "💰 Wallet Ledger"))
        self.assertIn("Usage: /ledger <telegram-id>", self.bot.sent[-1][1])

        self.bot.handle(self.message(999, "🏠 Customer Menu"))
        self.assertIn("AuriX VPN", self.bot.sent[-1][1])
        self.assertEqual(self.outline.created, [])

    def test_admin_typed_mutation_requires_one_time_confirmation(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/reject {order.order_id}"))
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "awaiting_payment",
        )
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        confirm = next(button for button in buttons if button["text"] == "Confirm Reject")
        self.bot.request = lambda _method, _payload: True
        callback = {
            "id": "callback-confirm",
            "from": {"id": 999, "first_name": "Admin"},
            "message": {"chat": {"id": 999, "type": "private"}},
            "data": confirm["callback_data"],
        }
        self.bot.handle_callback(callback)
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "rejected",
        )
        self.bot.handle_callback(callback)
        self.assertIn("expired or was already used", self.bot.sent[-1][1])

    def test_every_typed_admin_mutation_queues_confirmation(self):
        for command in (
            "/approve order-id",
            "/reject order-id",
            "/retry order-id",
            "/refund order-id",
            "/verify evidence-id tx-id 3000",
            "/rejectreceipt evidence-id",
        ):
            self.bot.handle(self.message(999, command))
            self.assertIn("expires in 5 minutes", self.bot.sent[-1][1])
            self.assertTrue(
                any(
                    button["callback_data"].startswith("a:k:")
                    for row in self.bot.markups[-1]["inline_keyboard"]
                    for button in row
                )
            )

    def test_confirmation_is_durable_single_use_and_state_bound(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/reject {order.order_id}"))
        confirm = next(
            button
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["callback_data"].startswith("a:k:")
        )
        token = confirm["callback_data"].split(":", 2)[2]
        with self.db.connect() as connection:
            challenge = connection.execute(
                "SELECT status, state_fingerprint FROM admin_action_challenges"
            ).fetchone()
        self.assertEqual(challenge["status"], "pending")
        self.assertTrue(challenge["state_fingerprint"])

        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "callback-confirm-durable",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:k:{token}",
            }
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM admin_action_challenges").fetchone()[0],
                "consumed",
            )
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "rejected",
        )

        # A repeated Telegram callback cannot repeat the mutation.
        self.bot.handle_callback(
            {
                "id": "callback-confirm-replay",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:k:{token}",
            }
        )
        self.assertIn("expired or was already used", self.bot.sent[-1][1])

        # A state change between preview and confirmation invalidates the token.
        second = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/reject {second.order_id}"))
        stale_token = next(
            button["callback_data"].split(":", 2)[2]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["callback_data"].startswith("a:k:")
        )
        self.commerce.cancel_order(123, second.order_id)
        self.bot.handle_callback(
            {
                "id": "callback-confirm-stale",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:k:{stale_token}",
            }
        )
        self.assertIn("expired or was already used", self.bot.sent[-1][1])
        self.assertEqual(
            self.commerce.order_detail(second.order_id, 999, is_admin=True)["status"],
            "cancelled",
        )

    def test_confirmation_cancel_marks_durable_challenge_unusable(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/reject {order.order_id}"))
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        cancel = next(button for button in buttons if button["text"] == "Cancel")
        token = cancel["callback_data"].split(":", 2)[2]
        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "callback-cancel",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": f"a:d:{token}",
            }
        )
        with self.db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM admin_action_challenges").fetchone()[0],
                "cancelled",
            )
        self.assertEqual(
            self.commerce.order_detail(order.order_id, 999, is_admin=True)["status"],
            "awaiting_payment",
        )

    def test_admin_operations_boundary_rejects_non_admin_before_service_call(self):
        calls = []
        self.commerce.list_pending_orders = lambda: calls.append("called")
        with self.assertRaises(PermissionError):
            self.bot.admin_operations.call(123, "list_pending_orders")
        self.assertEqual(calls, [])

    def test_private_chat_identity_mismatch_is_ignored(self):
        before = len(self.bot.sent)
        self.bot.handle(
            {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 123, "first_name": "Min"},
                "text": "/orders",
            }
        )
        self.assertEqual(len(self.bot.sent), before)

    def test_admin_inline_navigation_rechecks_role_and_uses_admin_namespace(self):
        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "callback-admin-nav",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": "a:n:failed",
            }
        )
        self.assertEqual(self.bot.sent[-1][1], "No terminal worker failures.")

        self.bot.handle_callback(
            {
                "id": "callback-customer-legacy",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "n:adminorders",
            }
        )
        self.assertEqual(self.bot.sent[-1][1], self.bot.UNKNOWN_ACTION_TEXT)

    def test_command_scope_cleanup_removes_removed_admins(self):
        self.bot.service.database.record_command_scope(998)
        self.bot.command_scope_cleanup_ids = {998}
        calls = []
        expected = {}

        def request(method, payload):
            calls.append((method, payload))
            scope_key = json.dumps(payload.get("scope", {}), sort_keys=True)
            if method == "setMyCommands":
                expected[scope_key] = payload["commands"]
                return True
            if method == "deleteMyCommands":
                expected.pop(scope_key, None)
                return True
            if method == "getMyCommands":
                return expected.get(scope_key, [])
            return True

        self.bot.request = request
        self.bot.configure_commands()
        self.assertIn(
            ("deleteMyCommands", {"scope": {"type": "chat", "chat_id": 998}}),
            calls,
        )
        self.assertNotIn(998, self.bot.service.database.list_command_scope_ids())

    def test_command_scope_cleanup_keeps_state_when_telegram_retains_commands(self):
        self.bot.service.database.record_command_scope(998)
        self.bot.command_scope_cleanup_ids = {998}
        expected = {}

        def request(method, payload):
            scope_key = json.dumps(payload.get("scope", {}), sort_keys=True)
            if method == "setMyCommands":
                expected[scope_key] = payload["commands"]
                return True
            if method == "deleteMyCommands":
                return True
            if method == "getMyCommands":
                if payload.get("scope", {}).get("chat_id") == 998:
                    return [{"command": "orders", "description": "stale"}]
                return expected.get(scope_key, [])
            return True

        self.bot.request = request
        with self.assertRaises(RuntimeError):
            self.bot.configure_commands()
        self.assertIn(998, self.bot.service.database.list_command_scope_ids())

    def test_admin_recovery_and_ledger_buttons_are_available(self):
        self.bot.handle(self.message(999, "/failed"))
        self.assertEqual(self.bot.sent[-1][1], "No terminal worker failures.")
        self.bot.handle(self.message(999, "/ledger 123"))
        self.assertIn("Wallet ledger · tg:123", self.bot.sent[-1][1])

        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(999, f"/order {order.order_id}"))
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("💰 View Ledger", labels)

    def test_capacity_panel_explains_assisted_scale_posture(self):
        text = self.bot._capacity_text(
            {
                "active_keys": 0,
                "pending_jobs": 0,
                "servers": [],
                "scale_advice": {
                    "status": "prepare",
                    "utilization_percent": 80.0,
                    "message": "Prepare and verify the next Outline node now.",
                },
            }
        )

        self.assertIn("Scale posture: Prepare · 80% allocated", text)
        self.assertIn("no automatic purchase or server deletion", text)

    def test_capacity_panel_surfaces_untracked_remote_keys(self):
        text = self.bot._capacity_text(
            {
                "active_keys": 0,
                "pending_jobs": 0,
                "servers": [
                    {
                        "server_id": "sg-a",
                        "label": "Singapore A",
                        "enabled": 1,
                        "health_status": "healthy",
                        "remote_key_count": 4,
                        "remote_orphan_key_count": 2,
                        "max_keys": 10,
                        "reserved_keys": 2,
                        "remaining_key_slots": 4,
                        "committed_traffic_bytes": 0,
                    }
                ],
                "scale_advice": {
                    "status": "stable",
                    "utilization_percent": 40.0,
                    "message": "Current declared fleet headroom is sufficient.",
                },
            }
        )

        self.assertIn("Untracked remote keys: 2", text)
        self.assertIn("audit before strict allocation", text)

    def test_admin_remote_inventory_panel_is_read_only_and_refreshes_in_place(self):
        self.commerce.register_outline_servers({"default": "Singapore"})
        observed = datetime(2026, 8, 27, 3, 7, tzinfo=UTC).isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO outline_remote_keys
                   (server_id, outline_key_id, remote_name, managed, status,
                    first_seen_at, last_seen_at, last_usage_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", "legacy-9", "legacy operator key", 0, "present", observed, observed, 1024),
            )
        self.bot._show_remote_inventory(999, 999, "default")
        self.assertIn("legacy-9", self.bot.sent[-1][1])
        self.assertIn("untracked", self.bot.sent[-1][1])
        self.assertNotIn("ss://", self.bot.sent[-1][1])
        labels = {
            button["text"]
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
        }
        self.assertIn("🔄 Refresh", labels)
        self.assertIn("⬅ Server policy", labels)

        edited = []
        self.bot.edit_message = lambda chat_id, message_id, text, markup: edited.append(
            (chat_id, message_id, text, markup)
        )
        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "inventory-refresh",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}, "message_id": 77},
                "data": "a:I:default:present:0",
            }
        )
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0][1], 77)

    def test_owner_can_review_remote_orphan_in_place_without_disclosing_access_url(self):
        self.commerce.register_outline_servers({"default": "Singapore"})
        observed = datetime(2026, 8, 27, 3, 7, tzinfo=UTC).isoformat()
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO outline_remote_keys
                   (server_id, outline_key_id, remote_name, managed, status,
                    first_seen_at, last_seen_at, last_usage_bytes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", "legacy-10", "legacy operator key", 0, "present", observed, observed, 1024),
            )
        access = StaffAccessControl(self.db, 999)
        access.bootstrap(owner_id=999)
        owner_bot = RecordingTelegramBot(
            "test-token",
            ClaimService(self.db, self.outline),
            self.commerce,
            {999},
            {123},
            staff_access=access,
        )
        owner_bot._show_remote_inventory(999, 999, "default")
        review_button = next(
            button
            for row in owner_bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["text"].startswith("✅ Mark reviewed")
        )
        self.assertNotIn("ss://", owner_bot.sent[-1][1])
        owner_bot.edit_message = lambda chat_id, message_id, text, markup: owner_bot.sent.append(
            (chat_id, text)
        )
        owner_bot.request = lambda _method, _payload: True
        owner_bot.handle_callback(
            {
                "id": "orphan-review",
                "from": {"id": 999, "first_name": "Owner"},
                "message": {
                    "message_id": 81,
                    "text": owner_bot.sent[-1][1],
                    "chat": {"id": 999, "type": "private"},
                },
                "data": review_button["callback_data"],
            }
        )
        with self.db.connect() as connection:
            review = connection.execute(
                """SELECT review_state FROM outline_remote_key_reviews
                   WHERE server_id = 'default' AND outline_key_id = 'legacy-10'"""
            ).fetchone()
            count = connection.execute(
                "SELECT remote_orphan_key_count FROM outline_servers WHERE server_id = 'default'"
            ).fetchone()[0]
        self.assertEqual(review["review_state"], "accepted_external")
        self.assertEqual(count, 0)

    def test_customer_buttons_and_admin_panel_are_separated(self):
        self.bot.handle(self.message(123, "/help"))
        help_text = self.bot.sent[-1][1]
        self.assertNotIn("/approve", help_text)
        self.assertEqual(self.bot.markups[-1].keys(), {"inline_keyboard"})
        help_labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertNotIn("🛠 Admin Panel", help_labels)

        self.bot.handle(self.message(123, "/whoami"))
        customer_labels = {
            button["text"] for row in self.bot.markups[-1]["keyboard"] for button in row
        }
        self.assertIn("🎁 Daily 300MB", customer_labels)
        self.assertIn("💎 Plans & Upgrade", customer_labels)
        self.assertNotIn("📊 Status", customer_labels)
        self.assertNotIn("📶 Usage", customer_labels)
        self.assertNotIn("🛠 Admin Panel", customer_labels)

        self.bot.handle(self.message(999, "/admin"))
        self.assertEqual(self.bot.markups[-1].keys(), {"inline_keyboard"})
        admin_labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("📥 Pending Orders", admin_labels)
        self.assertIn("🧾 Receipt Review", admin_labels)

    def test_button_actions_and_whoami(self):
        self.bot.handle(self.message(123, "📊 Status"))
        self.assertIn("No VPN key yet", self.bot.sent[-1][1])
        self.bot.handle(self.message(123, "/whoami"))
        self.assertIn("Your Telegram ID: 123", self.bot.sent[-1][1])
        self.assertNotIn("Admin access", self.bot.sent[-1][1])

    def test_giveaway_keyword_updates_status_hides_acquisition_and_blocks_orders(self):
        self.bot.handle(self.message(123, "100gbfree"))
        self.assertIn("Promo gift #1: 100GBFREE", self.bot.sent[-1][1])
        self.assertIn("100 GB / 30-day Outline key", self.bot.sent[-1][1])
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        copy_button = next(button for button in buttons if button["text"] == "📋 Copy Outline Key")
        self.assertEqual(copy_button["copy_text"], {"text": "ss://secret"})

        self.bot.handle(self.message(123, "/status"))
        self.assertIn("100 GB Promo · 100GBFREE", self.bot.sent[-1][1])
        self.assertIn("Promo gift #1", self.bot.sent[-1][1])
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertNotIn("💎 Plans & Upgrade", labels)

        self.bot.handle(self.message(123, "/buy basic_50gb"))
        self.assertIn("promo VPN gift is currently active", self.bot.sent[-1][1])
        self.assertEqual(self.commerce.list_user_orders(123), [])

    def test_promo_redeem_button_claims_the_registered_campaign(self):
        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "promo-query",
                "from": {"id": 123, "first_name": "Min"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "g:c:100GBFREE",
            }
        )

        self.assertIn("Promo gift #1: 100GBFREE", self.bot.sent[-1][1])
        self.assertEqual(len(self.outline.created), 1)

    def test_plans_exposes_native_copy_for_active_registered_promo(self):
        self.bot.handle(self.message(123, "/plans"))

        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertTrue(any(button["text"].startswith("🎁 Redeem") for button in buttons))
        self.assertFalse(any("copy_text" in button for button in buttons))

    def test_admin_can_configure_and_stop_a_custom_promo_with_confirmation(self):
        command = "/setpromo QUICK10 10 7 3 daily 2026-08-01T00:00Z 2026-09-30T00:00Z"
        self.bot.handle(self.message(999, command))
        confirm = next(
            button
            for row in self.bot.markups[-1]["inline_keyboard"]
            for button in row
            if button["text"] == "🎁 Confirm Promo"
        )
        self.bot.request = lambda _method, _payload: True
        self.bot.handle_callback(
            {
                "id": "confirm-promo",
                "from": {"id": 999, "first_name": "Admin"},
                "message": {"chat": {"id": 999, "type": "private"}},
                "data": confirm["callback_data"],
            }
        )

        configured = self.bot.service.giveaway_status(123, "QUICK10")
        self.assertEqual(configured["quota_bytes"], 10_000_000_000)
        self.assertEqual(configured["duration_days"], 7)
        self.assertEqual(configured["winner_limit"], 3)
        self.assertEqual(configured["frequency"], "daily")

        self.bot.handle(self.message(999, "/promo"))
        self.assertIn("Code: QUICK10", self.bot.sent[-1][1])
        self.assertIn("Gift: 10 GB / 7 days", self.bot.sent[-1][1])
        labels = {
            button["text"] for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        }
        self.assertIn("⏸ Stop Promo", labels)
        self.assertIn("📋 Copy Setup Example", labels)

    def test_customer_actions_return_after_promo_season_is_stopped(self):
        self.bot.handle(self.message(123, "100GBFREE"))
        self.bot.handle(self.message(123, "/whoami"))
        locked_labels = {
            button["text"] for row in self.bot.markups[-1]["keyboard"] for button in row
        }
        self.assertNotIn("🎁 Daily 300MB", locked_labels)
        self.assertNotIn("💎 Plans & Upgrade", locked_labels)

        self.bot.service.set_giveaway_active("100GBFREE", False)
        self.bot.handle(self.message(123, "/whoami"))
        restored_labels = {
            button["text"] for row in self.bot.markups[-1]["keyboard"] for button in row
        }
        self.assertIn("🎁 Daily 300MB", restored_labels)
        self.assertIn("🚀 Monthly 3GB", restored_labels)
        self.assertIn("💎 Plans & Upgrade", restored_labels)

        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.assertTrue(order.created)

    def test_open_paid_order_makes_user_ineligible_for_giveaway(self):
        self.commerce.create_order(123, "Min", "basic_50gb")
        self.bot.handle(self.message(123, "100GBFREE"))
        self.assertIn("not eligible", self.bot.sent[-1][1])
        self.assertEqual(self.bot.service.giveaway_status(123)["remaining_slots"], 5)

    def test_usage_button_shows_free_key_stats_and_refresh_action(self):
        self.bot.handle(self.message(123, "/claim"))
        self.outline.transfer = {"1": 150_000_000}
        self.bot.handle(self.message(123, "📶 Usage"))
        text = self.bot.sent[-1][1]
        self.assertIn("Daily Free 300 MB", text)
        self.assertIn("Used 150.00 MB", text)
        self.assertIn("Remaining 150.00 MB / 300.00 MB", text)
        self.assertIn("50.0%", text)
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertIn("🔄 Refresh", {button["text"] for button in buttons})
        copy_button = next(button for button in buttons if button["text"].startswith("📋 #1"))
        self.assertEqual(copy_button["copy_text"], {"text": "ss://secret"})

    def test_new_free_key_delivery_has_native_one_tap_copy(self):
        self.bot.handle(self.message(123, "/claim"))

        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertEqual(
            next(button for button in buttons if button["text"] == "📋 Copy Outline Key")[
                "copy_text"
            ],
            {"text": "ss://secret"},
        )
        self.assertTrue(any(button.get("callback_data") == "n:myvpn" for button in buttons))

    def test_myvpn_status_and_usage_aliases_converge_on_one_dashboard(self):
        self.bot.handle(self.message(123, "/claim"))
        dashboards = []
        for command in ("/myvpn", "/status", "/usage"):
            self.bot.handle(self.message(123, command))
            dashboards.append(self.bot.sent[-1][1])

        self.assertEqual(dashboards[0], dashboards[1])
        self.assertEqual(dashboards[1], dashboards[2])
        self.assertIn("Keys • status • usage • next action", dashboards[0])

    def test_customer_refresh_buttons_edit_the_interacting_message(self):
        self.bot.handle(self.message(123, "/claim"))
        self.bot.handle(self.message(123, "/myvpn"))
        sent_count = len(self.bot.sent)
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True

        self.bot.handle_callback(
            {
                "id": "myvpn-refresh",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 81,
                    "text": self.bot.sent[-1][1],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": "n:myvpn",
            }
        )

        self.assertEqual(len(self.bot.sent), sent_count)
        edit = next(payload for method, payload in requests if method == "editMessageText")
        self.assertEqual(edit["message_id"], 81)
        self.assertIn("Keys • status • usage • next action", edit["text"])

    def test_customer_outline_state_skips_unrelated_unreachable_servers(self):
        pool = FakeOutlinePool()
        commerce = CommerceService(
            CommerceDatabase(self.db.path), pool, Fernet.generate_key(), allow_legacy_text_approval=True
        )
        commerce.initialize()
        commerce.register_outline_servers({"sg-a": "Singapore A", "sg-b": "Singapore B"})
        now = datetime.now(UTC).isoformat()
        with self.db.connect() as connection:
            self.db.begin_write(connection)
            connection.execute(
                "INSERT INTO users (telegram_id, first_name, created_at) VALUES (?, ?, ?)",
                (123, "Min", now),
            )
            connection.execute(
                """INSERT INTO keys
                   (telegram_id, outline_key_id, key_type, created_at, expires_at,
                    data_limit_bytes, status, server_id)
                   VALUES (?, ?, 'daily_free', ?, ?, ?, 'active', ?)""",
                (123, "1", now, now, 300_000_000, "sg-a"),
            )

        pool.clients["sg-a"].transfer = {"1": 42}
        for method_name in ("transfer_metrics", "list_keys"):
            setattr(
                pool.clients["sg-b"],
                method_name,
                lambda: self.fail("unrelated server was probed"),
            )
        bot = RecordingTelegramBot(
            "test-token", ClaimService(self.db, pool), commerce, {999}, {123}
        )

        self.assertEqual(bot._customer_server_ids(123), ("sg-a",))
        metrics, _access = bot._collect_outline_state(
            include_access=True, server_ids=bot._customer_server_ids(123)
        )
        self.assertEqual(metrics, {"byServer": {"sg-a": {"1": 42}}})
        with self.assertRaisesRegex(ValueError, "all Outline endpoints are unavailable"):
            bot._collect_outline_state(include_access=True, server_ids=("retired-node",))

    def test_myvpn_surfaces_open_order_as_the_next_action(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")

        self.bot.handle(self.message(123, "/myvpn"))

        self.assertIn(f"Open order {order.order_id[:8]}", self.bot.sent[-1][1])
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertTrue(
            any(button.get("callback_data") == f"o:v:{order.order_id}" for button in buttons)
        )

    def test_paid_key_is_retrievable_and_copyable_from_myvpn(self):
        order = self.commerce.create_order(123, "Min", "basic_50gb")
        self.commerce.submit_payment(123, order.order_id, "manual", "paid-dashboard")
        self.commerce.approve_order(order.order_id, 999)
        self.assertEqual(self.commerce.process_jobs(), 1)

        self.bot.handle(self.message(123, "/myvpn"))

        self.assertIn("50 GB", self.bot.sent[-1][1])
        self.assertNotIn("ss://secret", self.bot.sent[-1][1])
        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertFalse(
            any(button.get("copy_text") == {"text": "ss://secret"} for button in buttons)
        )
        self.assertTrue(any(button["text"].startswith("🔑 Paid Keys") for button in buttons))
        self.assertTrue(any(button["text"].startswith("🔑 #1") for button in buttons))
        self.assertFalse(any(button.get("callback_data") == "n:plans" for button in buttons))
        self.assertFalse(any(button.get("callback_data") == "n:claim" for button in buttons))

    def test_many_paid_keys_use_paginated_browser_and_focused_copy_view(self):
        for index in range(12):
            order = self.commerce.create_order(123, "Min", "basic_50gb")
            self.commerce.submit_payment(123, order.order_id, "manual", f"multi-key-{index}")
            self.commerce.approve_order(order.order_id, 999)
            self.assertEqual(self.commerce.process_jobs(), 1)

        self.bot.handle(self.message(123, "/myvpn"))
        dashboard_buttons = [
            button for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        ]
        self.assertTrue(
            any(
                button["text"] == "🔑 Paid Keys · 12 active / 12 total"
                for button in dashboard_buttons
            )
        )
        # The summary avoids seven repetitive paid-key copy rows.
        self.assertEqual(
            sum(1 for button in dashboard_buttons if button.get("copy_text")),
            0,
        )

        self.bot._send_paid_key_list(123, 123)
        self.assertIn("12 active · 12 total · Page 1/3", self.bot.sent[-1][1])
        page_one = self.bot.markups[-1]["inline_keyboard"]
        key_rows = [row for row in page_one if row[0].get("callback_data", "").startswith("k:v:")]
        self.assertEqual(len(key_rows), 5)
        self.assertTrue(
            any(button.get("callback_data") == "k:l:active:1" for row in page_one for button in row)
        )
        self.assertTrue(
            any(button.get("callback_data") == "k:l:active:2" for row in page_one for button in row)
        )

        sent_count = len(self.bot.sent)
        requests = []
        self.bot.request = lambda method, payload: requests.append((method, payload)) or True
        self.bot.handle_callback(
            {
                "id": "paid-keys-last",
                "from": {"id": 123, "first_name": "Min"},
                "message": {
                    "message_id": 82,
                    "text": self.bot.sent[-1][1],
                    "chat": {"id": 123, "type": "private"},
                },
                "data": "k:l:active:2",
            }
        )
        self.assertEqual(len(self.bot.sent), sent_count)
        last_page = next(payload for method, payload in requests if method == "editMessageText")
        self.assertEqual(last_page["message_id"], 82)
        self.assertIn("Page 3/3", last_page["text"])
        self.assertTrue(
            any(
                button.get("callback_data") == "k:l:active:0"
                for row in last_page["reply_markup"]["inline_keyboard"]
                for button in row
            )
        )

        subscription_id = key_rows[0][0]["callback_data"].split(":", 2)[2]
        self.bot._send_paid_key_detail(123, 123, subscription_id)
        self.assertIn("Key reference:", self.bot.sent[-1][1])
        detail_buttons = [
            button for row in self.bot.markups[-1]["inline_keyboard"] for button in row
        ]
        self.assertEqual(
            next(button for button in detail_buttons if button["text"] == "📋 Copy Outline Key")[
                "copy_text"
            ],
            {"text": "ss://secret"},
        )
        self.assertTrue(
            any(button["text"].startswith("➕ Buy Another") for button in detail_buttons)
        )

    def test_copy_button_falls_back_safely_above_telegram_limit(self):
        markup = self.bot._key_delivery_keyboard("s" * 257)
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertFalse(any("copy_text" in button for button in buttons))
        self.assertEqual(buttons[-1]["callback_data"], "n:myvpn")

    def test_plan_buttons_follow_live_catalog_instead_of_hardcoded_prices(self):
        with self.commerce.database.connect() as connection:
            connection.execute(
                "UPDATE plans SET name = '55 GB Launch', price_minor = 3500 "
                "WHERE code = 'basic_50gb'"
            )

        self.bot.handle(self.message(123, "/plans"))

        buttons = [button for row in self.bot.markups[-1]["inline_keyboard"] for button in row]
        self.assertTrue(any(button["text"] == "💎 55 GB Launch · 3,500 MMK" for button in buttons))

    def test_command_scopes_hide_admin_commands_from_customers(self):
        calls = []
        expected = {}

        def request(method, payload):
            calls.append((method, payload))
            scope_key = json.dumps(payload["scope"], sort_keys=True)
            if method == "setMyCommands":
                expected[scope_key] = payload["commands"]
                return True
            if method == "getMyCommands":
                return expected[scope_key]
            return True

        self.bot.request = request
        self.bot.configure_commands()
        sets = [payload for method, payload in calls if method == "setMyCommands"]
        default = sets[0]
        admin = sets[1]
        self.assertEqual(default["scope"], {"type": "default"})
        self.assertNotIn("approve", {item["command"] for item in default["commands"]})
        self.assertEqual(admin["scope"], {"type": "chat", "chat_id": 999})
        self.assertEqual(
            {item["command"] for item in admin["commands"]},
            {
                "start",
                "plans",
                "claim",
                "trial",
                "myvpn",
                "wallet",
                "topup",
                "myorders",
                "alerts",
                "whoami",
                "help",
                "admin",
                "migrations",
                "notifications",
            },
        )

    def test_concurrent_command_menu_retry_is_serialized(self):
        active = 0
        maximum_active = 0
        expected = {}
        state_lock = threading.Lock()

        def request(method, payload):
            nonlocal active, maximum_active
            scope_key = json.dumps(payload["scope"], sort_keys=True)
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.005)
                if method == "setMyCommands":
                    expected[scope_key] = payload["commands"]
                    return True
                if method == "getMyCommands":
                    return expected.get(scope_key, [])
                return True
            finally:
                with state_lock:
                    active -= 1

        self.bot.request = request
        first = threading.Thread(target=self.bot.configure_commands)
        second = threading.Thread(target=self.bot.configure_commands)
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(maximum_active, 1)

    def test_free_staging_claim_is_fail_closed_for_non_test_accounts(self):
        self.bot.handle(self.message(456, "/claim"))
        self.assertIn("limited to the configured test accounts", self.bot.sent[-1][1])
        self.assertEqual(self.outline.created, [])


if __name__ == "__main__":
    unittest.main()
