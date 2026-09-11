import unittest

from scripts.aurix_ai_staging_smoke import run_smoke


class AuriXAIStagingSmokeTest(unittest.TestCase):
    def test_local_staging_smoke_exercises_real_http_stack(self):
        result = run_smoke()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(
            result["checks"],
            [
                "health",
                "signed_auth",
                "conversation_create",
                "streamed_turn",
                "duplicate_idempotency",
                "reconnectable_events",
                "second_auth",
            ],
        )


if __name__ == "__main__":
    unittest.main()
