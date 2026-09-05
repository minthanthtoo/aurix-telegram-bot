import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from commerce import CommerceDatabase, CommerceError, CommerceService, JOB_RETRY_DELAY
from commerce_order_repository import OrderRepository
from commerce_payment_repository import PaymentRepository
from commerce_wallet_approval_repository import WalletApprovalRepository
from persistence import open_sqlite_connection


UTC = timezone.utc


class RecordingOutline:
    """Small remote fake that exposes the control-plane/effect timeline."""

    default_server_id = None

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.keys = {}
        self.created = []
        self.deleted = []
        self.committed_states = []
        self.fail_create = False
        self.fail_delete = False

    def list_keys(self):
        return {"accessKeys": list(self.keys.values())}

    def create_key(self, name, limit_bytes):
        if self.fail_create:
            raise RuntimeError("remote create unavailable")
        with open_sqlite_connection(self.database_path) as connection:
            self.committed_states.append(
                connection.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE status = 'pending'"
                ).fetchone()[0]
            )
        key_id = str(len(self.keys) + 1)
        key = {"id": key_id, "name": name, "accessUrl": f"ss://contract-{key_id}"}
        self.keys[key_id] = key
        self.created.append(key_id)
        return dict(key)

    def get_key(self, key_id):
        key = self.keys.get(str(key_id))
        return dict(key) if key is not None else None

    def delete_key(self, key_id):
        if self.fail_delete:
            raise RuntimeError("remote delete unavailable")
        self.deleted.append(str(key_id))
        self.keys.pop(str(key_id), None)

    def set_data_limit(self, key_id, limit_bytes):
        return None

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": {}}

    def server_info(self):
        return {"version": "contract-outline"}


class RecordingReceiptStorage:
    configured = True
    bucket = "contract-receipts"

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.uploaded = []
        self.deleted = []
        self.fail_upload = False
        self.state_during_upload = None

    def upload(self, path, data, mime_type):
        with open_sqlite_connection(self.database_path) as connection:
            row = connection.execute(
                """SELECT e.storage_status, o.status
                   FROM payment_evidence e JOIN orders o ON o.id = e.order_id
                   WHERE e.storage_path = ?""",
                (path,),
            ).fetchone()
        self.state_during_upload = tuple(row) if row is not None else None
        if self.fail_upload:
            raise RuntimeError("object storage unavailable")
        self.uploaded.append((path, data, mime_type))
        return f"supabase://{self.bucket}/{path}"

    def delete(self, path):
        self.deleted.append(path)

    def signed_url(self, path, expires_in=300):
        return None

    def download(self, path):
        return None


class ReductionContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "contract.db"
        self.database = CommerceDatabase(self.path)
        self.outline = RecordingOutline(self.path)
        self.service = CommerceService(
            self.database,
            self.outline,
            Fernet.generate_key(),
            allow_legacy_text_approval=True,
        )
        self.service.initialize()
        self.now = datetime(2026, 9, 5, 3, 7, tzinfo=UTC)

    def tearDown(self):
        self.tmp.cleanup()

    def _paid_order(self, telegram_id=123):
        order = self.service.create_order(telegram_id, "Contract", "basic_50gb", self.now)
        self.service.submit_payment(
            telegram_id, order.order_id, "manual", f"ref-{order.order_id}", self.now
        )
        return order

    def test_order_repository_preserves_lookup_and_ownership_contracts(self):
        order = self.service.create_order(123, "Repository", "basic_50gb", self.now)

        self.assertIsInstance(self.service.orders, OrderRepository)
        self.assertIsInstance(self.service.payments, PaymentRepository)
        self.assertIsInstance(self.service.wallet_approvals, WalletApprovalRepository)
        with self.database.connect() as connection:
            self.assertEqual(
                self.service.orders.get(connection, order.order_id)["id"],
                order.order_id,
            )
            self.assertEqual(
                self.service.orders.get_owned(connection, order.order_id, 123)["telegram_id"],
                123,
            )
            self.assertIsNone(self.service.orders.get_owned(connection, order.order_id, 999))
            self.assertEqual(
                self.service.orders.find_open_for_user(connection, 123)["id"],
                order.order_id,
            )
            context = self.service.orders.get_payment_context(connection, order.order_id)
            self.assertEqual(tuple(context), (123, None))

    def test_approval_commits_durable_provisioning_intent_before_remote_effect(self):
        order = self._paid_order()

        approval = self.service.approve_order(order.order_id, 999, self.now)

        self.assertEqual(approval.status, "approved")
        self.assertEqual(self.outline.created, [])
        with self.database.connect() as connection:
            state = connection.execute(
                """SELECT o.status, s.status, j.status
                   FROM orders o JOIN subscriptions s ON s.order_id = o.id
                   JOIN provisioning_jobs j ON j.subscription_id = s.id
                   WHERE o.id = ?""",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("approved", "pending", "pending"))

        self.assertEqual(self.service.process_jobs(self.now), 1)
        self.assertEqual(self.outline.created, ["1"])
        self.assertEqual(self.outline.committed_states, [1])
        self.assertEqual(self.service.process_jobs(self.now), 0)

    def test_failed_provision_retries_without_duplicate_remote_effect(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.outline.fail_create = True

        self.assertEqual(self.service.process_jobs(self.now), 1)
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT status, attempts FROM provisioning_jobs"
            ).fetchone()
        self.assertEqual(tuple(job), ("pending", 1))
        self.assertEqual(self.outline.created, [])

        self.outline.fail_create = False
        self.assertEqual(self.service.process_jobs(self.now + JOB_RETRY_DELAY), 1)
        self.assertEqual(self.outline.created, ["1"])
        self.assertEqual(self.service.process_jobs(self.now + JOB_RETRY_DELAY), 0)

    def test_receipt_upload_observes_committed_pending_evidence_and_retries(self):
        storage = RecordingReceiptStorage(self.path)
        service = CommerceService(
            self.database,
            self.outline,
            Fernet.generate_key(),
            receipt_storage=storage,
            receipt_storage_required=True,
        )
        order = service.create_order(456, "Receipt", "basic_50gb", self.now)
        image = b"contract-receipt-image"

        result = service.submit_receipt(
            456,
            order.order_id,
            "manual",
            "file-contract",
            "unique-contract",
            image,
            "image/jpeg",
            extraction={"transaction_id": "TX-CONTRACT"},
            now=self.now,
        )

        self.assertEqual(result["storage_status"], "stored")
        self.assertEqual(storage.state_during_upload, ("pending", "awaiting_payment"))
        with self.database.connect() as connection:
            state = connection.execute(
                """SELECT o.status, e.storage_status
                   FROM orders o JOIN payment_evidence e ON e.order_id = o.id
                   WHERE o.id = ?""",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("payment_submitted", "stored"))

        failed_order = service.create_order(789, "Storage Failure", "basic_50gb", self.now)
        storage.fail_upload = True
        with self.assertRaisesRegex(CommerceError, "Receipt image could not be saved"):
            service.submit_receipt(
                789,
                failed_order.order_id,
                "manual",
                "file-contract-failure",
                "unique-contract-failure",
                b"failed-receipt",
                "image/jpeg",
                now=self.now + timedelta(minutes=1),
            )
        with self.database.connect() as connection:
            failed_state = connection.execute(
                """SELECT o.status, e.storage_status
                   FROM orders o JOIN payment_evidence e ON e.order_id = o.id
                   WHERE o.id = ?""",
                (failed_order.order_id,),
            ).fetchone()
        self.assertEqual(tuple(failed_state), ("awaiting_payment", "failed"))
        self.assertEqual(storage.deleted, [])

        storage.fail_upload = False
        retried = service.submit_receipt(
            789,
            failed_order.order_id,
            "manual",
            "file-contract-failure",
            "unique-contract-failure",
            b"failed-receipt",
            "image/jpeg",
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(retried["storage_status"], "stored")
        self.assertEqual(len(storage.uploaded), 2)

    def test_failed_remote_revoke_does_not_commit_local_revocation(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        self.outline.fail_delete = True

        self.assertEqual(
            self.service.expire_and_process(self.now + timedelta(days=31)),
            1,
        )
        with self.database.connect() as connection:
            state = connection.execute(
                """SELECT s.status, k.status, j.status
                   FROM subscriptions s JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   JOIN provisioning_jobs j ON j.subscription_id = s.id
                   WHERE s.order_id = ? AND j.operation = 'revoke'""",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("expired", "active", "pending"))
        self.assertEqual(self.outline.deleted, [])

    def test_quota_observation_queues_one_idempotent_revoke(self):
        order = self._paid_order()
        self.service.approve_order(order.order_id, 999, self.now)
        self.service.process_jobs(self.now)
        metrics = {"bytesTransferredByUserId": {"1": 50_000_000_000}}

        self.assertEqual(self.service.enforce_quotas(self.now, metrics), 1)
        self.assertEqual(self.service.enforce_quotas(self.now, metrics), 0)
        with self.database.connect() as connection:
            state = connection.execute(
                """SELECT s.status, k.status, j.status, COUNT(q.id)
                   FROM subscriptions s JOIN paid_vpn_keys k ON k.subscription_id = s.id
                   JOIN provisioning_jobs j ON j.subscription_id = s.id
                   LEFT JOIN quota_events q ON q.subscription_id = s.id
                   WHERE s.order_id = ? AND j.operation = 'revoke'
                   GROUP BY s.status, k.status, j.status""",
                (order.order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), ("revoked", "active", "pending", 1))


if __name__ == "__main__":
    unittest.main()
