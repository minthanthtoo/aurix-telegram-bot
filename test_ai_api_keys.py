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
from aurix_ai.web_api import (
    AISessionStore,
    AuriXAIApplication,
    _normalize_sse_event,
    make_handler,
)
from telegram_web_app import VerifiedTelegramUser


class _FakeRouter:
    model = "ag/gemini-3.7-flash-high"

    def chat(self, **kwargs):
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=kwargs["model"],
            returned_model="fake-provider-model",
            usage={"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        )


class _FeatureRouter:
    model = "ag/gemini-3.7-flash-high"

    def openai_chat(self, payload, **kwargs):
        return {
            "id": "upstream-chat-1",
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        }

    def request_json(self, path, payload, **kwargs):
        self.last_embedding = (path, payload)
        return {
            "object": "list",
            "model": payload["model"],
            "data": [
                {"object": "embedding", "index": index, "embedding": [0.1, 0.2]}
                for index, _ in enumerate(
                    payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
                )
            ],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        }

    def request_raw(self, path, data, **kwargs):
        self.last_audio = (path, data, kwargs)
        return {"body": b"RIFF-audio", "content_type": "audio/wav", "usage": None}

    def list_models(self, category=None):
        return [{"id": f"{category or 'chat'}/test", "object": "model", "owned_by": "test"}]


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
        self.assertEqual([row[0] for row in versions], [1, 2, 3, 4, 5, 6])

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


class AISessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "sessions.db"
        self.user = VerifiedTelegramUser(12345, "Auri", "X", "aurix", "en")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_durable_session_survives_store_reinitialization_and_refreshes(self):
        first = AISessionStore(3600, self.path)
        token = first.issue(self.user)
        self.assertEqual(first.get(token), self.user)

        second = AISessionStore(3600, self.path)
        self.assertEqual(second.get(token), self.user)
        with second._connect() as connection:
            row = connection.execute(
                "SELECT expires_at, last_seen_at FROM ai_web_sessions"
            ).fetchone()
        self.assertGreaterEqual(row["expires_at"], row["last_seen_at"])
        second.revoke(token)
        self.assertIsNone(second.get(token))

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
        self.application = AuriXAIApplication(
            _FakeRouter(),
            access_token="legacy-only-test",
            api_keys=self.store,
            admin_token="admin-secret",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
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

    def admin_request(self, path, token=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if cookie is not None:
            headers["Cookie"] = f"aurix_ai_session={cookie}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def admin_json_request(self, method, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer admin-secret",
            "X-AuriX-Admin": "1",
        }
        connection.request(method, path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_browser_readable_integration_guide_avoids_attachment_download(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/api/docs/external")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
        self.assertIsNone(response.getheader("Content-Disposition"))
        self.assertIn("AuriX AI API integration guide", body)
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/docs/ai-api")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Build an assistant.", html)
        self.assertIn("/api-guide.js", html)
        connection.close()

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

    def test_any_authenticated_telegram_user_can_open_admin_console(self):
        session_token = self.application.sessions.issue(
            VerifiedTelegramUser(987654321, "Regular", "User", "regular_user", "en")
        )
        status, report = self.admin_request("/api/admin/usage", cookie=session_token)
        self.assertEqual(status, 200)
        self.assertIn("accounts", report)

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

    def test_admin_can_create_issue_list_and_revoke_partner_key(self):
        status, created = self.admin_json_request(
            "POST",
            "/api/admin/accounts",
            {
                "name": "Admin-created site",
                "requests_per_minute": 120,
                "key_label": "production",
                "expires_in_days": 90,
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(created["key"]["token"].startswith("ak_live_"))
        account_id = created["account"]["id"]
        key_id = created["key"]["key_id"]

        status, report = self.admin_request("/api/admin/accounts", token="admin-secret")
        self.assertEqual(status, 200)
        account = next(item for item in report["accounts"] if item["id"] == account_id)
        self.assertEqual(account["requests_per_minute"], 120)
        self.assertEqual(account["keys"][0]["id"], key_id)
        self.assertNotIn("token", account["keys"][0])

        status, issued = self.admin_json_request(
            "POST",
            "/api/admin/keys",
            {"account_id": account_id, "label": "staging", "expires_in_days": 30},
        )
        self.assertEqual(status, 201)
        self.assertTrue(issued["key"]["token"].startswith("ak_live_"))

        status, revoked = self.admin_json_request(
            "POST",
            f"/api/admin/keys/{key_id}/revoke",
            {},
        )
        self.assertEqual(status, 200)
        self.assertTrue(revoked["revoked"])
        self.assertIsNone(self.store.authenticate(created["key"]["token"]))

    def test_admin_mutations_reject_cross_origin_and_missing_header(self):
        for extra in ({}, {"X-AuriX-Admin": "1", "Origin": "https://untrusted.example"}):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            connection.request("POST", "/api/admin/accounts", json.dumps({"name": "Blocked"}),
                               {"Authorization": "Bearer admin-secret", "Content-Type": "application/json", **extra})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        self.assertFalse(any(a["name"] == "Blocked" for a in self.store.list_accounts()))

    def test_admin_rejects_fractional_limits(self):
        for fields in ({"requests_per_minute": 1.9}, {"expires_in_days": 1.9}):
            status, _ = self.admin_json_request("POST", "/api/admin/accounts", {"name": "Invalid", **fields})
            self.assertEqual(status, 400)


class ExternalFeatureForwardingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = APIKeyStore(Path(self.tempdir.name) / "api-keys.db")
        self.store.initialize()
        account = self.store.create_account(
            "Feature site",
            allowed_modes=["*"],
            allowed_models=["*"],
        )
        self.key = self.store.issue_key(account["id"], label="feature").token
        self.router = _FeatureRouter()
        self.application = AuriXAIApplication(
            self.router,
            access_token="legacy-only-test",
            api_keys=self.store,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tools_and_image_parts_are_forwarded_and_returned(self):
        payload = self.application.external_chat_completions(
            {
                "model": "vision-tool-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Inspect this."},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/image.png"},
                            },
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "lookup", "parameters": {"type": "object"}},
                    }
                ],
                "tool_choice": "required",
            },
            f"Bearer {self.key}",
        )
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(payload["choices"][0]["message"]["tool_calls"])

    def test_embeddings_and_audio_use_openai_routes(self):
        embedding = self.application.external_embeddings(
            {"model": "embedding-model", "input": ["one", "two"]},
            f"Bearer {self.key}",
        )
        self.assertEqual(len(embedding["data"]), 2)
        self.assertEqual(self.router.last_embedding[0], "/embeddings")

        audio = self.application.external_audio(
            "/v1/audio/speech",
            b"{}",
            "application/json",
            f"Bearer {self.key}",
            model="tts-model",
        )
        self.assertEqual(audio["body"], b"RIFF-audio")
        self.assertEqual(self.router.last_audio[0], "/audio/speech")

    def test_sse_normalizer_rewrites_id_and_preserves_usage(self):
        event = (
            b'data: {"id":"upstream","model":"provider-model",'
            b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}],'
            b'"usage":{"prompt_tokens":2}}'
        )
        normalized, usage, provider_model, upstream_id, done = _normalize_sse_event(
            event,
            public_id="chatcmpl_public",
            model_id="public-model",
        )
        self.assertIn(b'"id":"chatcmpl_public"', normalized)
        self.assertIn(b'"model":"public-model"', normalized)
        self.assertEqual(usage["prompt_tokens"], 2)
        self.assertEqual(provider_model, "provider-model")
        self.assertEqual(upstream_id, "upstream")
        self.assertFalse(done)


if __name__ == "__main__":
    unittest.main()
