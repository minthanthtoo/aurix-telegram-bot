import http.client
import json
import queue
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from aurix_ai.api_keys import APIKeyStore, reconcile_usage_events
from aurix_ai.router import AIChatResult, AIRouterHTTPError
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

    def request_json(self, path, payload, **kwargs):
        if path == "/videos":
            self.last_video = (path, payload, kwargs)
            return {
                "id": "video_123",
                "object": "video",
                "model": payload["model"],
                "status": "queued",
            }
        if path.startswith("/videos/"):
            self.last_video_metadata = (path, payload, kwargs)
            return {
                "id": path.rsplit("/", 1)[-1],
                "object": "video",
                "model": "fake-provider-model",
                "status": "completed",
            }
        self.last_image = (path, payload, kwargs)
        return {
            "created": 1700000000,
            "data": [{"b64_json": "ZmFrZS1pbWFnZQ=="}],
            "model": payload["model"],
        }

    def request_raw(self, path, data, **kwargs):
        self.last_video_content = (path, data, kwargs)
        return {"body": b"MP4-video", "content_type": "video/mp4", "model": "fake-provider-model", "usage": None}

    def list_models(self, category=None):
        if category == "image":
            return [{"id": "gemini-3.7-flash-high", "object": "model", "owned_by": "test"}]
        return [{"id": "gemini-3.7-flash-high", "object": "model", "owned_by": "test"}]


class _SSEResponse:
    def __init__(self):
        self.closed = False
        self.finished = False
        self._lines = iter(
            [
                b'data: {"id":"upstream-stream","model":"provider-model","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n',
                b"\n",
                b'data: {"id":"upstream-stream","model":"provider-model","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n',
                b"\n",
                b'data: {"id":"upstream-stream","model":"provider-model","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n',
                b"\n",
                b"data: [DONE]\n",
                b"\n",
            ]
        )
        self._index = 0

    def readline(self):
        if self._index == 2:
            # Leave the first event observable before the provider finishes.
            time.sleep(0.25)
        try:
            line = next(self._lines)
        except StopIteration:
            self.finished = True
            return b""
        self._index += 1
        return line

    def close(self):
        self.closed = True


class _IncompleteSSEResponse:
    """A deterministic provider stream that EOFs before any terminal marker."""

    def __init__(self):
        self._lines = iter(
            [
                b'data: {"id":"partial","model":"test-model","choices":[{"index":0,"delta":{"content":"partial answer"},"finish_reason":null}]}\n',
                b"\n",
            ]
        )
        self.closed = False

    def readline(self):
        return next(self._lines, b"")

    def close(self):
        self.closed = True


class _GatedSSEResponse:
    """A provider stream held open until the HTTP client disconnects."""

    def __init__(self):
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self._readline_calls = 0
        self._active_readlines = 0
        self.waiting_after_first_event = threading.Event()
        self.closed = threading.Event()

    def feed_event(self, payload):
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._lines.put(b"data: " + encoded + b"\n")
        self._lines.put(b"\n")

    def readline(self):
        with self._lock:
            self._readline_calls += 1
            call_number = self._readline_calls
            self._active_readlines += 1
        if call_number == 3:
            self.waiting_after_first_event.set()
        try:
            line = self._lines.get(timeout=5)
            return b"" if line is None else line
        finally:
            with self._lock:
                self._active_readlines -= 1

    @property
    def active_readlines(self):
        with self._lock:
            return self._active_readlines

    def close(self):
        self.closed.set()
        self._lines.put(None)


class _DisconnectingWriter:
    """Deterministically surface the next write failure after a real client RST."""

    def __init__(self, writer, disconnected):
        self._writer = writer
        self._disconnected = disconnected
        self._broken = False

    def write(self, data):
        if self._disconnected.is_set() and not self._broken:
            self._broken = True
            raise BrokenPipeError("downstream test client disconnected")
        if self._broken:
            return len(data)
        return self._writer.write(data)

    def flush(self):
        if self._broken:
            return None
        return self._writer.flush()

    def close(self):
        return self._writer.close()

    def __getattr__(self, name):
        return getattr(self._writer, name)


class _RawResponse:
    def __init__(self, chunks=(b"RI", b"FF-audio"), content_type="audio/wav"):
        self.headers = {"Content-Type": content_type}
        self._chunks = iter(chunks)
        self.closed = False

    def read(self, _size=-1):
        try:
            return next(self._chunks)
        except StopIteration:
            return b""

    def close(self):
        self.closed = True


class _FeatureRouter:
    model = "ag/gemini-3.7-flash-high"

    def __init__(self):
        self.response_content = None
        self.last_chat = None

    def openai_chat_stream(self, payload, **kwargs):
        self.last_stream = (payload, kwargs)
        self.stream_response = _SSEResponse()
        return self.stream_response

    def openai_chat(self, payload, **kwargs):
        self.last_chat = (payload, kwargs)
        if self.response_content is not None:
            return {
                "id": "upstream-chat-text-1",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.response_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            }
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
        if path.startswith("/images/generations"):
            self.last_image = (path, payload, kwargs)
            return {
                "created": 1700000000,
                "data": [{"url": "https://images.example/generated.png"}],
                "model": payload["model"],
            }
        if path == "/videos":
            self.last_video = (path, payload, kwargs)
            return {
                "id": "video_123",
                "object": "video",
                "model": payload["model"],
                "status": "queued",
            }
        if path.startswith("/videos/"):
            self.last_video_metadata = (path, payload, kwargs)
            return {
                "id": path.rsplit("/", 1)[-1],
                "object": "video",
                "model": "video-model",
                "status": "completed",
            }
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
        if path.startswith("/images/generations"):
            self.last_image_binary = (path, data, kwargs)
            return {"body": b"PNG-image", "content_type": "image/png", "model": "image-model", "usage": None}
        if path.startswith("/videos/"):
            self.last_video_content = (path, data, kwargs)
            return {"body": b"MP4-video", "content_type": "video/mp4", "model": "video-model", "usage": None}
        self.last_audio = (path, data, kwargs)
        return {"body": b"RIFF-audio", "content_type": "audio/wav", "usage": None}

    def request_raw_stream(self, path, data, **kwargs):
        self.last_audio_stream = (path, data, kwargs)
        self.audio_stream_response = _RawResponse()
        return self.audio_stream_response

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
        self.assertEqual([row[0] for row in versions], [1, 2, 3, 4, 5, 6, 7])

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

    def test_persistent_session_connection_closes_after_context_exit(self):
        store = AISessionStore(3600, self.path)
        connection = store._connect()
        with connection:
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

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
            allowed_modes=[
                "translate",
                "embeddings",
                "audio",
                "image_generation",
                "video_generation",
            ],
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

    def request(self, body, token=None, path="/v1/chat", cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if cookie is not None:
            headers["Cookie"] = f"aurix_ai_session={cookie}"
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

    def raw_get(self, path, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        content_type = response.getheader("Content-Type")
        connection.close()
        return response.status, content_type, body

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

    def user_json_request(self, method, path, body, *, cookie):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"aurix_ai_session={cookie}",
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

        status, analytics = self.admin_request(
            "/api/admin/usage?format=analytics", token="admin-secret"
        )
        self.assertEqual(status, 200)
        self.assertEqual(analytics["summary"]["requests"], 1)
        self.assertEqual(analytics["summary"]["successful_requests"], 1)
        self.assertEqual(analytics["summary"]["failed_requests"], 0)
        self.assertEqual(analytics["summary"]["input_tokens"], 4)
        self.assertEqual(analytics["summary"]["output_tokens"], 5)
        self.assertEqual(analytics["summary"]["total_tokens"], 9)
        self.assertEqual(analytics["summary"]["usage_reported_requests"], 1)
        self.assertIsNotNone(analytics["summary"]["last_used_at"])
        self.assertEqual(len(analytics["accounts"]), 1)
        self.assertEqual(analytics["accounts"][0]["successful_requests"], 1)
        self.assertEqual(analytics["keys"][0]["requests"], 1)
        self.assertEqual(analytics["models"][0]["model_id"], "gemini-3.7-flash-high")
        self.assertEqual(analytics["endpoints"][0]["requests"], 1)
        self.assertEqual(sum(item["requests"] for item in analytics["daily"]), 1)
        account_id = analytics["accounts"][0]["account_id"]
        key_id = analytics["keys"][0]["key_id"]
        status, filtered = self.admin_request(
            f"/api/admin/usage?format=analytics&account_id={account_id}"
            f"&key_id={key_id}&status=completed",
            token="admin-secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(filtered["summary"]["requests"], 1)
        self.assertEqual(filtered["accounts"][0]["account_id"], account_id)

        status, export = self.admin_request(
            "/api/admin/usage?format=9router", token="admin-secret"
        )
        self.assertEqual(status, 200)
        self.assertEqual(export["format"], "9router.usageHistory.v1")
        self.assertEqual(len(export["events"]), 1)
        self.assertEqual(export["events"][0]["promptTokens"], 4)
        self.assertEqual(export["events"][0]["completionTokens"], 5)
        self.assertIsNone(export["events"][0]["apiKey"])

    def test_openai_route_maps_upstream_retryable_and_server_errors(self):
        cases = (
            (429, 429, "9Router rate limit reached"),
            (500, 502, "9Router is temporarily unavailable"),
            (502, 502, "9Router is temporarily unavailable"),
            (503, 503, "9Router is temporarily unavailable"),
            (504, 504, "9Router is temporarily unavailable"),
        )
        for upstream_status, public_status, public_error in cases:
            with self.subTest(upstream_status=upstream_status):
                class _FailingRouter:
                    model = "ag/gemini-3.7-flash-high"

                    def openai_chat(self, *_args, **_kwargs):
                        raise AIRouterHTTPError(upstream_status, retry_after=9)

                self.application.router = _FailingRouter()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=3
                )
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(
                        {
                            "messages": [{"role": "user", "content": "Hello"}],
                            "aurix_mode": "translate",
                        }
                    ).encode(),
                    {
                        "Authorization": f"Bearer {self.key}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                retry_after = response.getheader("Retry-After")
                connection.close()

                self.assertEqual(response.status, public_status)
                self.assertEqual(retry_after, "9")
                self.assertEqual(payload["error"], public_error)
                self.assertTrue(payload["request_id"].startswith("req_"))

    def test_any_authenticated_telegram_user_can_open_admin_console(self):
        session_token = self.application.sessions.issue(
            VerifiedTelegramUser(987654321, "Regular", "User", "regular_user", "en")
        )
        status, report = self.admin_request("/api/admin/usage", cookie=session_token)
        self.assertEqual(status, 200)
        self.assertIn("accounts", report)

    def test_any_authenticated_telegram_user_can_create_partner_key(self):
        user = VerifiedTelegramUser(987654321, "Regular", "User", "regular_user", "en")
        session_token = self.application.sessions.issue(user)
        status, payload = self.user_json_request(
            "POST",
            "/api/admin/accounts",
            {"name": "Regular user's integration", "key_label": "personal"},
            cookie=session_token,
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["key"]["token"].startswith("ak_live_"))
        self.assertEqual(payload["account"]["owner_type"], "telegram_admin")
        self.assertEqual(payload["account"]["owner_id"], str(user.telegram_id))
        account_id = payload["account"]["id"]
        status, updated = self.user_json_request(
            "PATCH",
            f"/api/admin/accounts/{account_id}",
            {
                "allowed_modes": ["translate", "audio"],
                "allowed_models": ["gemini-3.7-flash-high"],
                "requests_per_minute": 12,
            },
            cookie=session_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["account"]["allowed_modes"], ["audio", "translate"])
        self.assertEqual(updated["account"]["requests_per_minute"], 12)
        status, report = self.admin_request("/api/admin/accounts", cookie=session_token)
        self.assertEqual(status, 200)
        self.assertEqual(len(report["accounts"]), 1)
        self.assertEqual(report["accounts"][0]["owner_id"], str(user.telegram_id))
        status, analytics = self.admin_request(
            "/api/admin/usage?format=analytics", cookie=session_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(analytics["accounts"]), 1)
        self.assertEqual(analytics["accounts"][0]["account_id"], payload["account"]["id"])
        status, access = self.admin_request("/api/admin/access", cookie=session_token)
        self.assertEqual(status, 200)
        self.assertEqual(access["role"], "account_owner")
        self.assertEqual(access["scope"], f"owner:{user.telegram_id}")

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

    def test_openai_compatible_image_generation_route(self):
        status, payload = self.request(
            {
                "model": "gemini-3.7-flash-high",
                "prompt": "A bright red flower on a dark background",
                "response_format": "b64_json",
                "user_id": "image-user-1",
            },
            token=self.key,
            path="/v1/images/generations",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "gemini-3.7-flash-high")
        self.assertEqual(payload["data"][0]["b64_json"], "ZmFrZS1pbWFnZQ==")
        self.assertEqual(self.application.router.last_image[0], "/images/generations")

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        image_event = next(
            item for item in report["requests"] if item["endpoint"] == "/images/generations"
        )
        self.assertEqual(image_event["mode"], "image_generation")
        self.assertEqual(image_event["user_id"], "image-user-1")

    def test_openai_compatible_video_generation_routes(self):
        status, payload = self.request(
            {
                "model": "gemini-3.7-flash-high",
                "prompt": "A cinematic product reveal",
                "seconds": "4",
                "size": "1280x720",
                "user_id": "video-user-1",
            },
            token=self.key,
            path="/v1/videos",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], "video_123")
        self.assertEqual(self.application.router.last_video[0], "/videos")
        self.assertEqual(self.application.router.last_video[1]["model"], "ag/gemini-3.7-flash-high")

        status, metadata = self.admin_request("/v1/videos/video_123", token=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(self.application.router.last_video_metadata[2]["method"], "GET")

        status, content_type, body = self.raw_get("/v1/videos/video_123/content", token=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "video/mp4")
        self.assertEqual(body, b"MP4-video")
        self.assertEqual(self.application.router.last_video_content[2]["method"], "GET")

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        video_event = next(item for item in report["requests"] if item["endpoint"] == "/videos")
        self.assertEqual(video_event["mode"], "video_generation")
        self.assertEqual(video_event["user_id"], "video-user-1")

    def test_first_party_image_model_catalog_requires_session(self):
        status, _ = self.admin_request("/api/image-models")
        self.assertEqual(status, 401)
        session = self.application.sessions.issue(
            VerifiedTelegramUser(123456789, "Image", "User", "image_user", "en")
        )
        status, payload = self.admin_request("/api/image-models", cookie=session)
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["models"][0]["capabilities"], ["image_generation"])

        status, payload = self.request(
            {
                "model": "gemini-3.7-flash-high",
                "prompt": "A small red flower",
                "response_format": "b64_json",
            },
            path="/api/images/generations",
            cookie=session,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["b64_json"], "ZmFrZS1pbWFnZQ==")

    def test_image_generation_honors_separate_mode_scope(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.store.update_account(account_id, allowed_modes=["translate"])
        status, payload = self.request(
            {"model": "gemini-3.7-flash-high", "prompt": "A red flower"},
            token=self.key,
            path="/v1/images/generations",
        )
        self.assertEqual(status, 403)
        self.assertIn("mode", payload["error"])

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
        self.assertEqual(payload["aurix"]["requested_model"], "gemini-3.7-flash-high")
        self.assertEqual(payload["aurix"]["provider_model"], "fake-provider-model")

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        event = report["requests"][0]
        self.assertEqual(event["user_id"], "standard-user-123")
        self.assertIsNone(event["conversation_id"])

    def test_machine_readable_integration_profile_is_authenticated(self):
        status, _content_type, body = self.raw_get("/api/v1/integration")
        self.assertEqual(status, 401)
        self.assertIn("API key", body.decode("utf-8"))

        status, content_type, body = self.raw_get(
            "/api/v1/integration", token=self.key
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        profile = json.loads(body)
        self.assertEqual(profile["object"], "aurix.integration_profile")
        self.assertEqual(profile["effective_policy"]["requests_per_minute"], 20)

    def test_openai_compatible_streaming_forwards_first_event_immediately(self):
        self.application.router = _FeatureRouter()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps(
                {
                    "model": "gemini-3.7-flash-high",
                    "aurix_mode": "translate",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
            ).encode(),
            {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        first_line = response.readline().decode("utf-8")
        self.assertIn('"content":"Hello"', first_line)
        self.assertFalse(self.application.router.stream_response.finished)
        remainder = response.read().decode("utf-8")
        connection.close()
        self.assertIn('"content":" world"', remainder)
        self.assertIn('"total_tokens":6', remainder)
        self.assertIn("data: [DONE]", remainder)

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(report["requests"][0]["status"], "completed")
        self.assertEqual(report["requests"][0]["total_tokens"], 6)
        status, analytics = self.admin_request(
            "/api/admin/usage?format=analytics", token="admin-secret"
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(analytics["summary"]["avg_first_event_ms"])
        self.assertIsNotNone(analytics["summary"]["avg_duration_ms"])
        self.assertGreaterEqual(
            analytics["summary"]["avg_duration_ms"],
            analytics["summary"]["avg_first_event_ms"],
        )

    def test_partner_stream_eof_without_finish_or_done_is_incomplete(self):
        upstream = _IncompleteSSEResponse()

        class _EOFRouter(_FeatureRouter):
            def openai_chat_stream(self, payload, **kwargs):
                self.last_stream = (payload, kwargs)
                self.stream_response = upstream
                return upstream

        router = _EOFRouter()
        self.application.router = router
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps(
                {
                    "model": "gemini-3.7-flash-high",
                    "aurix_mode": "translate",
                    "messages": [{"role": "user", "content": "Incomplete stream"}],
                    "stream": True,
                }
            ).encode(),
            {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertIn('"content":"partial answer"', body)
        self.assertNotIn("data: [DONE]", body)
        self.assertTrue(upstream.closed)
        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(report["requests"]), 1)
        usage_event = report["requests"][0]
        self.assertEqual(usage_event["status"], "failed")
        self.assertEqual(usage_event["http_status"], 502)
        self.assertEqual(usage_event["endpoint"], "/chat/completions")
        self.assertNotIn("prompt", usage_event)
        self.assertNotIn("messages", usage_event)
        self.assertNotIn("request_body", usage_event)

    def test_partner_responses_stream_eof_records_upstream_failure_status(self):
        upstream = _IncompleteSSEResponse()

        class _EOFRouter(_FeatureRouter):
            def openai_chat_stream(self, payload, **kwargs):
                self.last_stream = (payload, kwargs)
                self.stream_response = upstream
                return upstream

        self.application.router = _EOFRouter()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps(
                {
                    "model": "gemini-3.7-flash-high",
                    "aurix_mode": "translate",
                    "input": "Incomplete response stream",
                    "stream": True,
                }
            ).encode(),
            {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertIn("response.output_text.delta", body)
        self.assertIn('"delta":"partial answer"', body)
        self.assertNotIn("response.completed", body)
        self.assertNotIn("data: [DONE]", body)
        self.assertTrue(upstream.closed)
        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(report["requests"]), 1)
        usage_event = report["requests"][0]
        self.assertEqual(usage_event["status"], "failed")
        self.assertEqual(usage_event["http_status"], 502)
        self.assertEqual(usage_event["endpoint"], "/responses")
        self.assertNotIn("prompt", usage_event)
        self.assertNotIn("messages", usage_event)
        self.assertNotIn("request_body", usage_event)

    def test_partner_stream_disconnect_closes_upstream_and_records_499(self):
        disconnected = threading.Event()
        upstream = _GatedSSEResponse()
        upstream.feed_event(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "first chunk"}, "finish_reason": None}
                ]
            }
        )

        class _DisconnectRouter(_FeatureRouter):
            def openai_chat_stream(self, payload, **kwargs):
                self.last_stream = (payload, kwargs)
                self.stream_response = upstream
                return upstream

        router = _DisconnectRouter()
        self.application.router = router
        handler_base = make_handler(self.application)

        class _DisconnectHandler(handler_base):
            def setup(self):
                super().setup()
                self.wfile = _DisconnectingWriter(self.wfile, disconnected)

        class _TrackedHTTPServer(ThreadingHTTPServer):
            daemon_threads = True

            def __init__(self, address, handler):
                super().__init__(address, handler)
                self.request_finished = threading.Event()

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.request_finished.set()

        server = _TrackedHTTPServer(("127.0.0.1", 0), _DisconnectHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.connect()
            client_socket = connection.sock
            self.assertIsNotNone(client_socket)
            client_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            connection.request(
                "POST",
                "/v1/chat/completions",
                json.dumps(
                    {
                        "model": "gemini-3.7-flash-high",
                        "aurix_mode": "translate",
                        "messages": [{"role": "user", "content": "Stream then disconnect"}],
                        "stream": True,
                    }
                ).encode(),
                {
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            first_event = response.readline().decode("utf-8")
            self.assertIn('"content":"first chunk"', first_event)
            self.assertTrue(upstream.waiting_after_first_event.wait(2))

            connection.close()
            disconnected.set()
            started = time.monotonic()
            upstream.feed_event(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": "after disconnect"}, "finish_reason": None}
                    ]
                }
            )

            self.assertTrue(upstream.closed.wait(2), "upstream response was not closed promptly")
            self.assertTrue(server.request_finished.wait(2), "HTTP handler remained active")
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(upstream.active_readlines, 0)
            self.assertEqual(router.last_stream[0]["stream"], True)
        finally:
            disconnected.set()
            connection.close()
            if not upstream.closed.is_set():
                upstream.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertFalse(server_thread.is_alive(), "HTTP listener thread leaked")
        self.assertTrue(server.request_finished.is_set(), "request worker leaked")
        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(report["requests"]), 1)
        usage_event = report["requests"][0]
        self.assertEqual(usage_event["status"], "failed")
        self.assertEqual(usage_event["http_status"], 499)
        self.assertEqual(usage_event["endpoint"], "/chat/completions")
        self.assertNotIn("prompt", usage_event)
        self.assertNotIn("messages", usage_event)
        self.assertNotIn("request_body", usage_event)

    def test_openai_responses_non_streaming_returns_response_shape_and_usage(self):
        self.application.router = _FeatureRouter()
        self.application.router.response_content = "A concise answer."
        status, payload = self.request(
            {
                "model": "gemini-3.7-flash-high",
                "aurix_mode": "translate",
                "instructions": "Be concise.",
                "input": "Hello",
                "user": "responses-user-1",
                "metadata": {"conversation_id": "responses-chat-1"},
            },
            token=self.key,
            path="/v1/responses",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["id"].startswith("resp_"))
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output_text"], "A concise answer.")
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertEqual(payload["usage"]["input_tokens"], 8)
        self.assertEqual(payload["usage"]["output_tokens"], 3)

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        event = report["requests"][0]
        self.assertEqual(event["endpoint"], "/responses")
        self.assertEqual(event["user_id"], "responses-user-1")
        self.assertEqual(event["conversation_id"], "responses-chat-1")

    def test_openai_responses_stream_emits_live_responses_events(self):
        self.application.router = _FeatureRouter()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            "/v1/responses",
            json.dumps(
                {
                    "model": "gemini-3.7-flash-high",
                    "aurix_mode": "translate",
                    "input": "Hello",
                    "stream": True,
                }
            ).encode(),
            {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        first_line = response.readline().decode("utf-8")
        self.assertEqual(first_line.strip(), "event: response.created")
        body = (first_line + response.read().decode("utf-8"))
        connection.close()
        self.assertIn("response.output_text.delta", body)
        self.assertIn('"delta":"Hello"', body)
        self.assertIn("response.output_text.done", body)
        self.assertIn("response.completed", body)
        self.assertIn("data: [DONE]", body)

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(report["requests"][0]["endpoint"], "/responses")
        self.assertEqual(report["requests"][0]["status"], "completed")

    def test_openai_audio_speech_stream_forwards_bytes_immediately(self):
        self.application.router = _FeatureRouter()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            "/v1/audio/speech",
            json.dumps(
                {
                    "model": "gemini-3.7-flash-high",
                    "input": "Hello",
                    "voice": "alloy",
                    "stream": True,
                }
            ).encode(),
            {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "audio/wav")
        self.assertEqual(response.read(), b"RIFF-audio")
        connection.close()
        self.assertEqual(self.application.router.last_audio_stream[0], "/audio/speech")

        status, report = self.admin_request("/api/admin/usage", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(report["requests"][0]["endpoint"], "/v1/audio/speech")
        self.assertEqual(report["requests"][0]["status"], "completed")

    def test_external_key_info_returns_effective_non_secret_policy(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "GET",
            "/api/v1/key-info",
            headers={"Authorization": f"Bearer {self.key}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["object"], "aurix.key_info")
        self.assertEqual(payload["key"]["status"], "active")
        self.assertNotIn("token", payload["key"])
        self.assertIn("audio", payload["policy"]["allowed_modes"])

    def test_admin_access_endpoint_reports_operator_token_scope(self):
        status, payload = self.admin_request("/api/admin/access", token="admin-secret")
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "operator_token")
        self.assertEqual(payload["scope"], "all_accounts")

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

    def test_account_policy_cannot_exceed_operator_ceiling(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.application.operator_allowed_modes = frozenset({"translate"})
        status, payload = self.admin_json_request(
            "PATCH",
            f"/api/admin/accounts/{account_id}",
            {"allowed_modes": ["translate", "audio"]},
        )
        self.assertEqual(status, 403)
        self.assertIn("operator policy", payload["error"])

    def test_http_model_discovery_uses_fast_curated_chat_catalog_by_default(self):
        def unexpected_live_discovery(_category=None):
            raise AssertionError("default partner catalog must not call live discovery")

        self.application.router.list_models = unexpected_live_discovery
        status, content_type, body = self.raw_get("/v1/models", token=self.key)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertLess(len(body), 20_000)
        payload = json.loads(body)
        self.assertEqual(payload["aurix"]["catalog"], "curated")
        self.assertEqual(payload["aurix"]["category"], "chat")
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["id"], "ag/gemini-3.7-flash-high")
        self.assertEqual(
            payload["data"][0]["capabilities"],
            ["chat", "responses", "streaming"],
        )

    def test_http_model_discovery_can_request_a_live_category_explicitly(self):
        status, _content_type, body = self.raw_get(
            "/v1/models?category=image", token=self.key
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["aurix"]["catalog"], "live")


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

    def test_responses_input_and_function_tools_translate_to_canonical_chat(self):
        payload = self.application.external_responses(
            {
                "model": "vision-tool-model",
                "instructions": "Use the tool when needed.",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Inspect this."},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/image.png",
                            },
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "description": "Look something up.",
                        "parameters": {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "function", "name": "lookup"},
            },
            f"Bearer {self.key}",
        )
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["type"], "function_call")
        forwarded, _kwargs = self.router.last_chat
        self.assertEqual(forwarded["messages"][0]["role"], "system")
        self.assertEqual(forwarded["messages"][1]["content"][0]["type"], "text")
        self.assertEqual(forwarded["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(forwarded["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(
            forwarded["tool_choice"],
            {"type": "function", "function": {"name": "lookup"}},
        )

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

    def test_image_generation_supports_json_and_binary_responses(self):
        result = self.application.external_image_generation(
            {
                "model": "image-model",
                "prompt": "A golden bird over a mountain lake",
                "size": "1024x1024",
                "response_format": "url",
                "user_id": "site-user-1",
                "conversation_id": "image-chat-1",
            },
            f"Bearer {self.key}",
        )
        self.assertEqual(result["data"][0]["url"], "https://images.example/generated.png")
        self.assertEqual(self.router.last_image[0], "/images/generations")
        self.assertEqual(self.router.last_image[1]["model"], "image-model")
        self.assertEqual(self.router.last_image[1]["size"], "1024x1024")

        binary = self.application.external_image_generation(
            {"model": "image-model", "prompt": "A square blue flower"},
            f"Bearer {self.key}",
            binary=True,
        )
        self.assertEqual(binary["body"], b"PNG-image")
        self.assertEqual(self.router.last_image_binary[0], "/images/generations?response_format=binary")

        summary = self.store.usage_summary(
            account_id=self.store.list_accounts()[0]["id"],
            start_at="1970-01-01T00:00:00+00:00",
            end_at="2100-01-01T00:00:00+00:00",
        )
        self.assertEqual(summary[0]["requests"], 2)
        self.assertEqual(summary[0]["total_tokens"], 0)

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
        payload = json.loads(normalized.split(b"data: ", 1)[1])
        self.assertEqual(payload["aurix"]["requested_model"], "public-model")
        self.assertEqual(payload["aurix"]["provider_model"], "provider-model")

    def test_model_discovery_includes_image_capability(self):
        payload = self.application.external_models(f"Bearer {self.key}")
        image_models = [item for item in payload["data"] if item["capabilities"] == ["image_generation"]]
        self.assertEqual(len(image_models), 1)
        self.assertEqual(image_models[0]["id"], "image/test")

        video_models = [item for item in payload["data"] if item["capabilities"] == ["video_generation"]]
        self.assertEqual(len(video_models), 1)
        self.assertEqual(video_models[0]["id"], "video/test")

    def test_model_discovery_exposes_hualogu_metadata_and_accepts_route_alias_policy(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.store.update_account(
            account_id,
            allowed_models=["gemini-3.7-flash-high"],
        )
        self.router.list_models = lambda category=None: [
            {
                "id": "ag/gemini-3.7-flash-high",
                "object": "model",
                "owned_by": "ag",
            }
        ]

        catalog = self.application.external_models(f"Bearer {self.key}")
        self.assertEqual(len(catalog["data"]), 1)
        model = catalog["data"][0]
        self.assertEqual(model["id"], "ag/gemini-3.7-flash-high")
        self.assertEqual(model["display_name"], "Gemini 3.7 Flash High")
        self.assertEqual(model["aurix"]["canonical_model_id"], "gemini-3.7-flash-high")
        self.assertEqual(
            model["aurix"]["language_quality"]["lisu"],
            "tested-experimental",
        )

        response = self.application.external_chat_completions(
            {
                "model": "ag/gemini-3.7-flash-high",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            f"Bearer {self.key}",
        )
        self.assertEqual(response["model"], "gemini-3.7-flash-high")

    def test_model_discovery_cache_is_bounded_and_profile_is_key_scoped(self):
        calls = []
        original = self.router.list_models

        def counted(category=None):
            calls.append(category)
            return original(category)

        self.router.list_models = counted
        self.application.image_models_payload()
        self.application.image_models_payload()
        self.assertEqual(calls, ["image"])

        profile = self.application.external_integration_profile(f"Bearer {self.key}")
        self.assertEqual(profile["schema"], "aurix.external.v1")
        self.assertEqual(profile["model_discovery"]["cache_ttl_seconds"], 60)
        self.assertEqual(profile["endpoints"]["chat_completions"]["terminal"], "data: [DONE]")
        self.assertNotIn("token", json.dumps(profile).lower())

    def test_model_discovery_hides_image_models_without_image_scope(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.store.update_account(account_id, allowed_modes=["translate"])
        payload = self.application.external_models(f"Bearer {self.key}")
        self.assertFalse(
            any(item["capabilities"] == ["image_generation"] for item in payload["data"])
        )
        self.assertFalse(
            any(item["capabilities"] == ["video_generation"] for item in payload["data"])
        )

    def test_model_discovery_and_requests_honor_embedding_audio_scopes(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.store.update_account(account_id, allowed_modes=["translate"])
        payload = self.application.external_models(f"Bearer {self.key}")
        self.assertFalse(any(item["capabilities"] == ["embeddings"] for item in payload["data"]))
        self.assertFalse(any(item["capabilities"] == ["audio_input"] for item in payload["data"]))
        with self.assertRaises(PermissionError):
            self.application.external_embeddings(
                {"model": "embedding-model", "input": "hello"},
                f"Bearer {self.key}",
            )
        with self.assertRaises(PermissionError):
            self.application.external_audio(
                "/v1/audio/speech",
                b"{}",
                "application/json",
                f"Bearer {self.key}",
                model="tts-model",
            )

    def test_video_generation_supports_submit_metadata_and_content(self):
        result = self.application.external_video_generation(
            {
                "model": "video-model",
                "prompt": "A cinematic product reveal",
                "seconds": "4",
                "size": "1280x720",
                "user_id": "site-user-1",
                "conversation_id": "video-chat-1",
            },
            f"Bearer {self.key}",
        )
        self.assertEqual(result["id"], "video_123")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.router.last_video[0], "/videos")
        self.assertEqual(self.router.last_video[1]["model"], "video-model")
        self.assertEqual(self.router.last_video[1]["seconds"], "4")
        self.assertEqual(self.router.last_video[2]["user_id"], "site-user-1")

        metadata = self.application.external_video_metadata("video_123", f"Bearer {self.key}")
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(self.router.last_video_metadata[0], "/videos/video_123")
        self.assertEqual(self.router.last_video_metadata[2]["method"], "GET")

        content = self.application.external_video_content("video_123", f"Bearer {self.key}")
        self.assertEqual(content["body"], b"MP4-video")
        self.assertEqual(self.router.last_video_content[0], "/videos/video_123/content")
        self.assertEqual(self.router.last_video_content[2]["method"], "GET")

        summary = self.store.usage_summary(
            account_id=self.store.list_accounts()[0]["id"],
            start_at="1970-01-01T00:00:00+00:00",
            end_at="2100-01-01T00:00:00+00:00",
        )
        self.assertEqual(summary[0]["requests"], 1)

    def test_video_generation_honors_separate_mode_scope(self):
        account_id = self.store.list_accounts()[0]["id"]
        self.store.update_account(account_id, allowed_modes=["image_generation"])
        with self.assertRaises(PermissionError):
            self.application.external_video_generation(
                {"model": "video-model", "prompt": "A cinematic product reveal"},
                f"Bearer {self.key}",
            )


if __name__ == "__main__":
    unittest.main()
