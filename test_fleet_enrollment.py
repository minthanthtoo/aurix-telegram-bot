import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceDatabase
from fleet_enrollment import (
    EnrollmentError,
    create_pending_enrollment,
    expire_pending_enrollments,
    generate_token,
    mark_consumed,
    read_enrollment,
    render_user_data,
    receive_enrollment,
)


UTC = timezone.utc


class FleetEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = CommerceDatabase(Path(self.temporary.name) / "enrollment.db")
        self.database.initialize()
        self.now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO infrastructure_jobs
                   (id, operation, status, attempts, next_attempt_at,
                    request_fingerprint, created_at)
                   VALUES (?, 'provision', 'pending', 0, ?, ?, ?)""",
                ("job-enroll-1", self.now.isoformat(), "fingerprint", self.now.isoformat()),
            )
        self.key = Fernet.generate_key().decode()
        self.token = generate_token()
        self.payload = {
            "job_id": "job-enroll-1",
            "node_id": "auto-node-1",
            "public_ip": "203.0.113.10",
            "access_txt": (
                "apiUrl:https://203.0.113.10:61603/abcdefghijklmnop\n"
                "certSha256:" + "a" * 64
            ),
            "ssh_host_key": "ssh-ed25519 " + "A" * 44,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_token_is_hashed_and_payload_is_encrypted_until_consumed(self):
        created = create_pending_enrollment(
            self.database,
            job_id="job-enroll-1",
            token=self.token,
            now=self.now,
        )
        self.assertEqual(created["job_id"], "job-enroll-1")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT token_hash, payload_ciphertext, status FROM infrastructure_enrollments"
            ).fetchone()
        self.assertNotEqual(row["token_hash"], self.token)
        self.assertIsNone(row["payload_ciphertext"])
        self.assertEqual(row["status"], "pending")

        received = receive_enrollment(
            self.database,
            token=self.token,
            payload=self.payload,
            encryption_key=self.key,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(received, {"status": "accepted", "job_id": "job-enroll-1"})
        read = read_enrollment(
            self.database,
            job_id="job-enroll-1",
            encryption_key=self.key,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(read, self.payload)
        self.assertEqual(mark_consumed(self.database, job_id="job-enroll-1", now=self.now), True)
        self.assertIsNone(
            read_enrollment(
                self.database,
                job_id="job-enroll-1",
                encryption_key=self.key,
                now=self.now + timedelta(minutes=3),
            )
        )
        self.assertEqual(
            receive_enrollment(
                self.database,
                token=self.token,
                payload=self.payload,
                encryption_key=self.key,
                now=self.now + timedelta(minutes=3),
            ),
            {"status": "already_consumed", "job_id": "job-enroll-1"},
        )

    def test_replay_does_not_replace_first_payload(self):
        create_pending_enrollment(self.database, job_id="job-enroll-1", token=self.token, now=self.now)
        receive_enrollment(
            self.database,
            token=self.token,
            payload=self.payload,
            encryption_key=self.key,
            now=self.now,
        )
        changed = dict(self.payload, public_ip="203.0.113.11")
        self.assertEqual(
            receive_enrollment(
                self.database,
                token=self.token,
                payload=changed,
                encryption_key=self.key,
                now=self.now + timedelta(minutes=1),
            ),
            {"status": "already_received", "job_id": "job-enroll-1"},
        )
        self.assertEqual(
            read_enrollment(
                self.database,
                job_id="job-enroll-1",
                encryption_key=self.key,
                now=self.now + timedelta(minutes=1),
            )["public_ip"],
            "203.0.113.10",
        )

    def test_unknown_expired_and_invalid_requests_fail_closed(self):
        with self.assertRaisesRegex(EnrollmentError, "unknown"):
            receive_enrollment(
                self.database,
                token=generate_token(),
                payload=self.payload,
                encryption_key=self.key,
                now=self.now,
            )
        create_pending_enrollment(
            self.database,
            job_id="job-enroll-1",
            token=self.token,
            expires_at=self.now + timedelta(seconds=1),
            now=self.now,
        )
        with self.assertRaisesRegex(EnrollmentError, "expired"):
            receive_enrollment(
                self.database,
                token=self.token,
                payload=self.payload,
                encryption_key=self.key,
                now=self.now + timedelta(seconds=2),
            )
        with self.assertRaisesRegex(EnrollmentError, "public IP"):
            receive_enrollment(
                self.database,
                token=generate_token(),
                payload=dict(self.payload, public_ip="not-an-ip"),
                encryption_key=self.key,
                now=self.now,
            )

    def test_rendered_user_data_contains_only_short_lived_token_and_retries_callback(self):
        rendered = render_user_data(
            bootstrap_script=b"#!/usr/bin/env bash\necho ready\n",
            registration_url="https://control.example/fleet/register",
            token=self.token,
            job_id="job-enroll-1",
            node_id="auto-node-1",
            control_plane_source="203.0.113.7/32",
            api_port=61603,
            keys_port=443,
        )
        self.assertIn("AURIX_ENROLLMENT_TOKEN=", rendered)
        self.assertIn(self.token, rendered)
        self.assertIn("metadata/v1/interfaces/public/0/ipv4/address", rendered)
        self.assertIn("Restart=on-failure", rendered)
        self.assertNotIn(self.key, rendered)
        self.assertNotIn("AURIX_ENROLLMENT_PUBLIC_IP=$public_ip", rendered)
        with self.assertRaisesRegex(EnrollmentError, "HTTPS"):
            render_user_data(
                bootstrap_script=b"echo ready",
                registration_url="http://control.example/register",
                token=self.token,
                job_id="job-enroll-1",
                node_id="auto-node-1",
                control_plane_source="203.0.113.7/32",
                api_port=61603,
                keys_port=443,
            )

    def test_expiry_pass_marks_stale_rows_without_provider_side_effects(self):
        create_pending_enrollment(
            self.database,
            job_id="job-enroll-1",
            token=self.token,
            expires_at=self.now + timedelta(seconds=1),
            now=self.now,
        )
        self.assertEqual(
            expire_pending_enrollments(self.database, now=self.now + timedelta(seconds=2)), 1
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, last_error FROM infrastructure_enrollments WHERE job_id = ?",
                ("job-enroll-1",),
            ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIn("expired", row["last_error"])


if __name__ == "__main__":
    unittest.main()
