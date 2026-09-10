import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commerce import CommerceDatabase
from identity import IdentityService


class SessionRevocationTest(unittest.TestCase):
    def test_auth_revoke_does_not_release_lease_without_session_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = CommerceDatabase(Path(directory) / "sessions.db")
            database.initialize()
            now = datetime(2026, 9, 10, tzinfo=timezone.utc)
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO users (telegram_id, first_name, created_at) VALUES (1, 'A', ?)",
                    (now.isoformat(),),
                )
                connection.execute(
                    """INSERT INTO orders (id, telegram_id, plan_code, amount_minor, currency, status, created_at)
                       VALUES ('order-1', 1, 'basic_50gb', 1, 'MMK', 'approved', ?)""",
                    (now.isoformat(),),
                )
                connection.execute(
                    """INSERT INTO subscriptions
                       (id, order_id, telegram_id, plan_code, starts_at, expires_at, quota_bytes, duration_days, status)
                       VALUES ('sub-1', 'order-1', 1, 'basic_50gb', ?, ?, 1000, 30, 'active')""",
                    (now.isoformat(), (now + timedelta(days=30)).isoformat()),
                )
            identity = IdentityService(database)
            entitlement = identity.ensure_subscription_entitlement(1, "sub-1", quota_bytes=1000)
            generation = identity.create_generation(
                entitlement, "sg-a", external_id="remote-a", usage_baseline_provenance="new"
            )
            lease = identity.grant_lease(
                entitlement, "sg-a", lease_bytes=100, generation_id=generation, now=now
            )
            self.assertTrue(
                identity.mark_remote_revoked(
                    generation, verified=True, sessions_terminated=False, now=now
                )
            )
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM quota_leases WHERE lease_id = ?", (lease,)
                    ).fetchone()[0],
                    "active",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM credential_generations WHERE generation_id = ?",
                        (generation,),
                    ).fetchone()[0],
                    "retiring",
                )
            self.assertTrue(identity.mark_sessions_terminated(generation, now=now))
            with database.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM quota_leases WHERE lease_id = ?", (lease,)
                    ).fetchone()[0],
                    "released",
                )


if __name__ == "__main__":
    unittest.main()
