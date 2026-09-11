import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aurix_ai.conversations import AIConversationStore, ConversationStoreError


class AIConversationStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AIConversationStore(Path(self.tempdir.name) / "conversations.db")
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_owner_scoped_conversation_and_full_attempt_projection(self):
        conversation = self.store.create_conversation(101, title="First chat")
        attempt, created = self.store.create_turn(
            101,
            conversation["id"],
            source="Hello",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[],
            client_submission_id="client-1",
            request_id="req-1",
        )
        self.assertTrue(created)
        self.assertEqual(attempt["status"], "running")

        completed = self.store.complete_attempt(
            101,
            attempt["id"],
            output_text="Hi there",
            usage={"total_tokens": 3},
            upstream_request_id="upstream-1",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["output_text"], "Hi there")

        detail = self.store.get_conversation(101, conversation["id"])
        self.assertEqual(detail["turns"][0]["submitted_source"], "Hello")
        self.assertEqual(detail["turns"][0]["attempts"][0]["output_text"], "Hi there")
        self.assertEqual(self.store.context_messages(101, conversation["id"]), [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ])
        with self.assertRaisesRegex(ConversationStoreError, "not found"):
            self.store.get_conversation(202, conversation["id"])

    def test_client_submission_is_idempotent(self):
        conversation = self.store.create_conversation(101)
        first, created = self.store.create_turn(
            101,
            conversation["id"],
            source="One",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[],
            client_submission_id="same-client-id",
        )
        duplicate, duplicate_created = self.store.create_turn(
            101,
            conversation["id"],
            source="Changed browser text must not resend",
            mode="english",
            direction=None,
            model_id="other-model",
            context=[],
            client_submission_id="same-client-id",
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(len(self.store.get_conversation(101, conversation["id"])["turns"]), 1)

    def test_concurrent_duplicate_submissions_allocate_one_turn(self):
        conversation = self.store.create_conversation(101)

        def submit(_index):
            return self.store.create_turn(
                101,
                conversation["id"],
                source="Concurrent",
                mode="english",
                direction=None,
                model_id="gemini-test",
                context=[],
                client_submission_id="concurrent-client-id",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(8)))
        self.assertEqual(sum(created for _attempt, created in results), 1)
        self.assertEqual(len({attempt["id"] for attempt, _created in results}), 1)
        self.assertEqual(len(self.store.get_conversation(101, conversation["id"])["turns"]), 1)

    def test_cancel_is_terminal_and_late_completion_cannot_overwrite(self):
        conversation = self.store.create_conversation(101)
        attempt, _ = self.store.create_turn(
            101,
            conversation["id"],
            source="Cancel me",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[],
        )
        cancelled = self.store.cancel_attempt(101, attempt["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        late = self.store.complete_attempt(101, attempt["id"], output_text="late")
        self.assertEqual(late["status"], "cancelled")
        self.assertIsNone(late["output_text"])

    def test_restart_marks_running_attempt_interrupted(self):
        conversation = self.store.create_conversation(101)
        attempt, _ = self.store.create_turn(
            101,
            conversation["id"],
            source="In flight",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[],
        )
        restarted = AIConversationStore(Path(self.tempdir.name) / "conversations.db")
        restarted.initialize()
        self.assertEqual(restarted.attempt(101, attempt["id"])["status"], "interrupted")

    def test_delete_hides_transcript_and_cancels_running_attempt(self):
        conversation = self.store.create_conversation(101)
        attempt, _ = self.store.create_turn(
            101,
            conversation["id"],
            source="Delete me",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[],
        )
        self.assertTrue(self.store.delete_conversation(101, conversation["id"]))
        with self.assertRaisesRegex(ConversationStoreError, "not found"):
            self.store.get_conversation(101, conversation["id"])
        with self.assertRaisesRegex(ConversationStoreError, "not found"):
            self.store.attempt(101, attempt["id"])


if __name__ == "__main__":
    unittest.main()
