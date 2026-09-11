import threading
import time
import unittest

from aurix_ai.conversation_jobs import ConversationJobManager


class ConversationJobManagerTest(unittest.TestCase):
    def test_enforces_conversation_and_owner_limits_and_releases_slots(self):
        manager = ConversationJobManager(max_workers=2, max_jobs_per_owner=1)
        started = threading.Event()
        release = threading.Event()

        def blocked(stop_event):
            started.set()
            release.wait(2)
            self.assertFalse(stop_event.is_set())

        try:
            self.assertTrue(
                manager.submit(
                    "attempt-1",
                    blocked,
                    conversation_id="conversation-1",
                    owner_id=101,
                )
            )
            self.assertTrue(started.wait(1))
            self.assertFalse(
                manager.submit(
                    "attempt-2",
                    lambda _stop: None,
                    conversation_id="conversation-1",
                    owner_id=202,
                )
            )
            self.assertFalse(
                manager.submit(
                    "attempt-3",
                    lambda _stop: None,
                    conversation_id="conversation-2",
                    owner_id=101,
                )
            )
            release.set()
            for _ in range(50):
                if not manager.is_running("attempt-1"):
                    break
                time.sleep(0.01)
            self.assertFalse(manager.is_running("attempt-1"))
            self.assertTrue(
                manager.submit(
                    "attempt-4",
                    lambda _stop: None,
                    conversation_id="conversation-2",
                    owner_id=101,
                )
            )
        finally:
            release.set()
            manager.close()


if __name__ == "__main__":
    unittest.main()
