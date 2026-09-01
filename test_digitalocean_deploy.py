import unittest

from deploy.digitalocean_autodeploy import ci_conclusion, github_slug


class DigitalOceanDeployTest(unittest.TestCase):
    def test_github_slug_accepts_only_normal_https_repository(self):
        self.assertEqual(
            github_slug("https://github.com/minthanthtoo/aurix-telegram-bot.git"),
            "minthanthtoo/aurix-telegram-bot",
        )
        self.assertIsNone(github_slug("ssh://github.com/minthanthtoo/aurix.git"))
        self.assertIsNone(github_slug("https://example.com/minthanthtoo/aurix.git"))

    def test_ci_gate_requires_named_completed_success(self):
        self.assertEqual(ci_conclusion({"check_runs": []}), "pending")
        self.assertEqual(
            ci_conclusion(
                {"check_runs": [{"name": "safety-net", "status": "in_progress"}]}
            ),
            "pending",
        )
        self.assertEqual(
            ci_conclusion(
                {
                    "check_runs": [
                        {
                            "name": "safety-net",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ),
            "failed",
        )
        self.assertEqual(
            ci_conclusion(
                {
                    "check_runs": [
                        {
                            "name": "safety-net",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ),
            "success",
        )


if __name__ == "__main__":
    unittest.main()
