import tempfile
import unittest
from pathlib import Path

from aurix_ai.api_keys import APIKeyStore


class AIAdminControlsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = APIKeyStore(Path(self.tempdir.name) / "api-keys.db")
        self.store.initialize()
        self.account = self.store.create_account("Audited site")
        self.issued = self.store.issue_key(
            self.account["id"],
            label="production",
            audit_context={
                "action": "key.issue",
                "actor_type": "telegram_user",
                "actor_id": "101",
                "request_id": "req-issue",
            },
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_audit_rows_are_non_secret_and_correlated(self):
        self.assertTrue(
            self.store.revoke_key(
                self.issued.key_id,
                audit_context={
                    "action": "key.revoke",
                    "actor_type": "telegram_user",
                    "actor_id": "101",
                    "request_id": "req-revoke",
                },
            )
        )
        events = self.store.audit_events(target_type="key", target_id=self.issued.key_id)
        self.assertEqual([event["action"] for event in events], ["key.revoke", "key.issue"])
        self.assertEqual(events[0]["request_id"], "req-revoke")
        self.assertEqual(events[1]["request_id"], "req-issue")
        self.assertNotIn(self.issued.token, self.store.path.read_bytes().decode("latin1", "ignore"))
        for event in events:
            self.assertNotIn("token", json_text := str(event.get("metadata")))

    def test_usage_filters_do_not_change_unfiltered_summary_and_page(self):
        principal = self.store.authenticate(self.issued.token)
        self.assertIsNotNone(principal)
        self.store.record_usage(
            request_id="request-a",
            principal=principal,
            mode="english",
            model_id="model-a",
            status="completed",
            http_status=200,
            usage={"total_tokens": 10},
            endpoint="/v1/chat/completions",
        )
        self.store.record_usage(
            request_id="request-b",
            principal=principal,
            mode="translate",
            model_id="model-b",
            status="failed",
            http_status=502,
            usage=None,
            endpoint="/v1/embeddings",
        )
        all_summary = self.store.usage_summary()
        filtered_summary = self.store.usage_summary(model_id="model-a")
        self.assertEqual(all_summary[0]["requests"], 2)
        self.assertEqual(filtered_summary[0]["requests"], 1)
        page = self.store.usage_event_page(limit=1)
        self.assertTrue(page["has_more"])
        next_page = self.store.usage_event_page(limit=1, offset=page["next_offset"])
        self.assertEqual({page["items"][0]["request_id"], next_page["items"][0]["request_id"]}, {"request-a", "request-b"})
        self.assertEqual(
            self.store.usage_events(endpoint="/v1/embeddings")[0]["request_id"],
            "request-b",
        )


if __name__ == "__main__":
    unittest.main()
