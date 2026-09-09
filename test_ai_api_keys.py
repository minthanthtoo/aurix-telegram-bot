import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from aurix_ai.api_keys import APIKeyStore, reconcile_usage_events
from aurix_ai.router import AIChatResult
from aurix_ai.web_api import AuriXAIApplication, make_handler


class _FakeRouter:
    model = "ag/gemini-3.7-flash-high"

    def chat(self, **kwargs):
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=kwargs["model"],
            returned_model="fake-provider-model",
            usage={"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        )


class APIKeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = APIKeyStore(Path(self.tempdir.name) / "api-keys.db")
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_issued_token_is_authenticatable_but_not_listed(self):
        account = self.store.create_account(
            "Translator site",
            allowed_modes=["translate"],
            allowed_models=["gemini-3.7-flash-high"],
            requests_per_minute=7,
        )
        issued = self.store.issue_key(account["id"], label="production")
        principal = self.store.authenticate(issued.token)

        self.assertIsNotNone(principal)
        self.assertEqual(principal.account_id, account["id"])
        self.assertTrue(principal.allows_mode("translate"))
        self.assertFalse(principal.allows_mode("english"))
        self.assertTrue(principal.allows_model("gemini-3.7-flash-high"))
        self.assertNotIn("token", self.store.list_keys()[0])
        self.assertNotIn(issued.token, self.store.path.read_bytes().decode("latin1", "ignore"))

        self.assertTrue(self.store.revoke_key(issued.key_id))
        self.assertIsNone(self.store.authenticate(issued.token))

    def test_schema_migrations_and_expiry_survive_reinitialization(self):
        account = self.store.create_account("Expiring site")
        expired = self.store.issue_key(
            account["id"],
            label="expired",
            expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )
        self.store.initialize()

        self.assertIsNone(self.store.authenticate(expired.token))
        with self.store.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations WHERE component = 'ai_api_keys' ORDER BY version"
            ).fetchall()
        self.assertEqual([row[0] for row in versions], [1, 2, 3, 4, 5])

    def test_account_policy_can_be_updated_without_rotating_key(self):
        account = self.store.create_account("Initial site", allowed_modes=["english"])
        issued = self.store.issue_key(account["id"])
        self.store.update_account(
            account["id"], allowed_modes=["translate"], requests_per_minute=11
        )

        principal = self.store.authenticate(issued.token)
        self.assertEqual(principal.allowed_modes, frozenset({"translate"}))
        self.assertEqual(principal.requests_per_minute, 11)

    def test_operator_cli_lists_and_updates_accounts(self):
        account = self.store.create_account("CLI site")
        script = Path(__file__).resolve().parent / "scripts" / "aurix_ai_keys.py"
        listed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--database",
                str(self.store.path),
                "list-accounts",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(account["id"], listed.stdout)

        updated = subprocess.run(
            [
                sys.executable,
                str(script),
                "--database",
                str(self.store.path),
                "update-account",
                account["id"],
                "--requests-per-minute",
                "15",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn('"requests_per_minute": 15', updated.stdout)

    def test_container_includes_api_store_runtime_dependencies(self):
        dockerfile = (Path(__file__).resolve().parent / "deploy" / "aurix-ai.Dockerfile").read_text()
        self.assertIn("COPY aurix_ai /app/aurix_ai", dockerfile)
        self.assertIn('CMD ["python", "-u", "-m", "aurix_ai"]', dockerfile)
        self.assertIn("migrations.py", dockerfile)
        self.assertIn("persistence.py", dockerfile)

    def test_reconciliation_compares_aurix_and_router_exports(self):
        result = reconcile_usage_events(
            [
                {
                    "request_id": "req_1",
                    "model": "gemini",
                    "input_tokens": 4,
                    "output_tokens": 5,
                    "total_tokens": 9,
                }
            ],
            [
                {
                    "model": "gemini",
                    "promptTokens": 4,
                    "completionTokens": 5,
                    "tokens": {"total_tokens": 9},
                    "meta": {"aurixRequestId": "req_1"},
                }
            ],
        )
        self.assertEqual(result["matched_request_ids"], 1)
        self.assertEqual(result["difference"]["total_tokens"], 0)


class ExternalAPIHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = APIKeyStore(Path(self.tempdir.name) / "api-keys.db")
        self.store.initialize()
        account = self.store.create_account(
            "Translator site",
            allowed_modes=["translate"],
            allowed_models=["gemini-3.7-flash-high"],
            requests_per_minute=20,
        )
        self.key = self.store.issue_key(account["id"], label="production").token
        application = AuriXAIApplication(
            _FakeRouter(),
            access_token="legacy-only-test",
            api_keys=self.store,
            admin_token="admin-secret",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, body, token=None, path="/v1/chat"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("POST", path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def admin_request(self, path, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_external_api_uses_account_key_and_policy(self):
        status, payload = self.request({"mode": "translate", "message": "Hello"})
        self.assertEqual(status, 401)
        self.assertIn("API key", payload["error"])

        status, payload = self.request(
            {"mode": "translate", "message": "Hello"}, token=self.key
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["request_id"].startswith("req_"))
        self.assertEqual(payload["model_id"], "gemini-3.7-flash-high")
        self.assertEqual(payload["token_usage"]["input_tokens"], 4)
        self.assertEqual(payload["token_usage"]["output_tokens"], 5)

        status, payload = self.request(
            {"mode": "english", "message": "Hello"}, token=self.key
        )
        self.assertEqual(status, 403)
        self.assertIn("mode", payload["error"])

        status, _ = self.admin_request("/api/admin/usage")
        self.assertEqual(status, 401)
        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        summary = next(item for item in report["accounts"] if item["id"] == report["accounts"][0]["id"])
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["input_tokens"], 4)
        self.assertEqual(summary["output_tokens"], 5)
        self.assertEqual(summary["total_tokens"], 9)
        self.assertEqual(len(report["requests"]), 1)

        status, export = self.admin_request(
            "/api/admin/usage?format=9router", token="admin-secret"
        )
        self.assertEqual(status, 200)
        self.assertEqual(export["format"], "9router.usageHistory.v1")
        self.assertEqual(len(export["events"]), 1)
        self.assertEqual(export["events"][0]["promptTokens"], 4)
        self.assertEqual(export["events"][0]["completionTokens"], 5)
        self.assertIsNone(export["events"][0]["apiKey"])

    def test_external_api_attributes_usage_to_site_user_and_conversation(self):
        status, payload = self.request(
            {
                "mode": "translate",
                "message": "Hello",
                "user_id": "site-user-123",
                "conversation_id": "chat-456",
            },
            token=self.key,
        )
        self.assertEqual(status, 200)
        self.assertIn("context", payload)
        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        event = report["requests"][0]
        self.assertEqual(event["user_id"], "site-user-123")
        self.assertEqual(event["conversation_id"], "chat-456")

    def test_openai_compatible_chat_completions_without_conversation_id(self):
        status, payload = self.request(
            {
                "model": "ag/gemini-3.7-flash-high",
                "aurix_mode": "translate",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Hello"},
                ],
                "user": "standard-user-123",
            },
            token=self.key,
            path="/v1/chat/completions",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["id"].startswith("chatcmpl_"))
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["model"], "gemini-3.7-flash-high")
        self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(payload["choices"][0]["message"]["content"], "reply:Hello")
        self.assertEqual(payload["usage"]["prompt_tokens"], 4)
        self.assertEqual(payload["usage"]["completion_tokens"], 5)
        self.assertEqual(payload["usage"]["total_tokens"], 9)

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        event = report["requests"][0]
        self.assertEqual(event["user_id"], "standard-user-123")
        self.assertIsNone(event["conversation_id"])

    def test_openai_compatible_streaming_is_explicitly_rejected(self):
        status, payload = self.request(
            {
                "model": "gemini-3.7-flash-high",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            token=self.key,
            path="/v1/chat/completions",
        )
        self.assertEqual(status, 400)
        self.assertIn("streaming is not supported", payload["error"])

    def test_revoke_prevents_future_external_requests(self):
        principal = self.store.authenticate(self.key)
        self.assertIsNotNone(principal)
        self.assertTrue(self.store.revoke_key(principal.key_id))
        status, _ = self.request({"mode": "translate", "message": "Hello"}, token=self.key)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
