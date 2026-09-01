import unittest

from deploy.digitalocean_autodeploy import (
    ci_conclusion,
    github_slug,
    missing_release_configuration,
)


class DigitalOceanDeployTest(unittest.TestCase):
    def test_release_gate_reports_names_without_secret_values(self):
        environment = {
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "secret-value",
            "RECEIPT_LLM_BASE_URL": "https://vision.example/v1",
            "RECEIPT_LLM_MODEL": "vision-model",
            "RECEIPT_LLM_API_KEY": "vision-secret",
            "RECEIPT_STORAGE_REQUIRED": "1",
        }
        self.assertEqual(missing_release_configuration(environment), [])
        environment.pop("SUPABASE_SERVICE_ROLE_KEY")
        environment["RECEIPT_STORAGE_REQUIRED"] = "0"
        self.assertEqual(
            missing_release_configuration(environment),
            ["SUPABASE_SERVICE_ROLE_KEY", "RECEIPT_STORAGE_REQUIRED=1"],
        )

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
