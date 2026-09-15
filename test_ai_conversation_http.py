import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from aurix_ai.conversations import AIConversationStore
from aurix_ai.router import AIChatResult
from aurix_ai.web_api import AuriXAIApplication, make_handler
from telegram_web_app import VerifiedTelegramUser


class _ConversationRouter:
    model = "ag/gemini-3.7-flash-high"

    def __init__(self):
        self.calls = []
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.block:
            self.started.set()
            self.release.wait(2)
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=kwargs["model"],
            returned_model="fake-provider-model",
            usage={"total_tokens": 3},
            upstream_request_id=f"upstream-{len(self.calls)}",
        )


class _DirectStreamResponse:
    def __init__(self):
        self.closed = False
        self.finished = False
        self._lines = iter(
            [
                b'data: {"id":"direct-upstream","choices":[{"delta":{"content":"Hel"},"finish_reason":null}]}\n',
                b"\n",
                b'data: {"id":"direct-upstream","choices":[{"delta":{"content":"lo"},"finish_reason":null}]}\n',
                b"\n",
                b'data: {"id":"direct-upstream","choices":[],"usage":{"total_tokens":2}}\n',
                b"\n",
                b"data: [DONE]\n",
                b"\n",
            ]
        )
        self._index = 0

    def readline(self):
        if self._index == 2:
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


class _DirectConversationRouter(_ConversationRouter):
    def __init__(self):
        super().__init__()
        self.payload = None
        self.response = None

    def openai_chat_stream(self, payload, **_kwargs):
        self.payload = payload
        self.response = _DirectStreamResponse()
        return self.response


class AIConversationHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AIConversationStore(Path(self.tempdir.name) / "conversations.db")
        self.store.initialize()
        self.router = _ConversationRouter()
        self.application = AuriXAIApplication(
            self.router,
            access_token="legacy-test",
            conversation_store=self.store,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.user = VerifiedTelegramUser(101, "Auri", "X", "aurix", "en")
        self.other_user = VerifiedTelegramUser(202, "Other", "User", "other", "en")
        self.cookie = self.application.sessions.issue(self.user)
        self.other_cookie = self.application.sessions.issue(self.other_user)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.application.close()
        self.tempdir.cleanup()

    def request(self, method, path, body=None, cookie=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie is not None:
            headers["Cookie"] = f"aurix_ai_session={cookie}"
        if extra_headers:
            headers.update(extra_headers)
        connection.request(
            method,
            path,
            json.dumps(body).encode() if body is not None else None,
            headers,
        )
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def test_authenticated_turn_is_durable_and_duplicate_does_not_resend(self):
        status, conversation = self.request(
            "POST",
            "/api/conversations",
            {"mode": "english", "title": "Durable chat"},
            self.cookie,
        )
        self.assertEqual(status, 201)
        conversation_id = conversation["id"]
        body = {
            "mode": "english",
            "message": "Hello",
            "client_submission_id": "browser-submit-1",
        }
        status, first = self.request(
            "POST", f"/api/conversations/{conversation_id}/turns", body, self.cookie
        )
        self.assertEqual(status, 202)
        self.assertEqual(first["attempt"]["status"], "running")
        attempt_id = first["attempt"]["id"]
        for _ in range(30):
            status, polled = self.request(
                "GET",
                f"/api/conversations/{conversation_id}/attempts/{attempt_id}",
                cookie=self.cookie,
            )
            if polled["attempt"]["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(status, 200)
        self.assertEqual(polled["attempt"]["output_text"], "reply:Hello")
        self.assertEqual(len(self.router.calls), 1)
        status, duplicate = self.request(
            "POST", f"/api/conversations/{conversation_id}/turns", body, self.cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["attempt"]["id"], attempt_id)
        self.assertEqual(len(self.router.calls), 1)
        status, detail = self.request(
            "GET", f"/api/conversations/{conversation_id}", cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["turns"][0]["attempts"][0]["output_text"], "reply:Hello")

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "GET",
            f"/api/conversations/{conversation_id}/attempts/{attempt_id}/events",
            headers={"Cookie": f"aurix_ai_session={self.cookie}"},
        )
        response = connection.getresponse()
        events = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("event: snapshot", events)
        self.assertIn("event: terminal", events)

    def test_first_party_turn_streams_provider_deltas_and_persists_completion(self):
        self.router = _DirectConversationRouter()
        self.application.router = self.router
        status, conversation = self.request(
            "POST",
            "/api/conversations",
            {"mode": "english", "title": "Live chat"},
            self.cookie,
        )
        self.assertEqual(status, 201)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST",
            f"/api/conversations/{conversation['id']}/turns/stream",
            json.dumps(
                {
                    "mode": "english",
                    "message": "Hello",
                    "client_submission_id": "direct-stream-1",
                }
            ).encode(),
            {
                "Content-Type": "application/json",
                "Cookie": f"aurix_ai_session={self.cookie}",
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        prefix = b"".join(response.readline() for _ in range(5)).decode("utf-8")
        self.assertIn("event: start", prefix)
        self.assertIn('"text":"Hel"', prefix)
        self.assertFalse(self.router.response.finished)
        remainder = response.read().decode("utf-8")
        connection.close()
        self.assertIn('"text":"lo"', remainder)
        self.assertIn("event: terminal", remainder)

        detail = self.store.get_conversation(101, conversation["id"])
        attempt = detail["turns"][0]["attempts"][0]
        self.assertEqual(attempt["status"], "completed")
        self.assertEqual(attempt["output_text"], "Hello")
        self.assertEqual(attempt["usage"]["total_tokens"], 2)
        self.assertTrue(self.router.response.closed)
        self.assertTrue(self.router.payload["stream"])

    def test_conversation_and_attempts_are_owner_scoped(self):
        status, conversation = self.request(
            "POST", "/api/conversations", {"title": "Private"}, self.cookie
        )
        self.assertEqual(status, 201)
        conversation_id = conversation["id"]
        status, _ = self.request(
            "GET", f"/api/conversations/{conversation_id}", cookie=self.other_cookie
        )
        self.assertEqual(status, 404)
        status, listing = self.request("GET", "/api/conversations", cookie=self.other_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(listing["conversations"], [])

    def test_cancel_prevents_late_worker_completion(self):
        self.router.block = True
        status, conversation = self.request(
            "POST", "/api/conversations", {"title": "Cancel"}, self.cookie
        )
        self.assertEqual(status, 201)
        conversation_id = conversation["id"]
        status, submitted = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/turns",
            {"message": "Do not finish"},
            self.cookie,
        )
        self.assertEqual(status, 202)
        attempt_id = submitted["attempt"]["id"]
        self.assertTrue(self.router.started.wait(1))
        status, cancelled = self.request(
            "POST",
            f"/api/conversations/{conversation_id}/attempts/{attempt_id}/cancel",
            {},
            self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["attempt"]["status"], "cancelled")
        self.router.release.set()
        for _ in range(30):
            _status, polled = self.request(
                "GET",
                f"/api/conversations/{conversation_id}/attempts/{attempt_id}",
                cookie=self.cookie,
            )
            if polled["attempt"]["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(polled["attempt"]["status"], "cancelled")
        self.assertIsNone(polled["attempt"]["output_text"])

    def test_retry_route_reuses_turn_and_runs_from_server_snapshot(self):
        conversation = self.store.create_conversation(101, title="Retry")
        first, _ = self.store.create_turn(
            101,
            conversation["id"],
            source="Frozen source",
            mode="english",
            direction=None,
            model_id="gemini-test",
            context=[{"role": "user", "content": "Frozen context"}],
        )
        self.store.fail_attempt(101, first["id"], error_code="upstream_error")
        status, retried = self.request(
            "POST",
            f"/api/conversations/{conversation['id']}/attempts/{first['id']}/retry",
            {},
            self.cookie,
        )
        self.assertEqual(status, 202)
        self.assertEqual(retried["attempt"]["turn_id"], first["turn_id"])
        retry_id = retried["attempt"]["id"]
        for _ in range(30):
            _status, polled = self.request(
                "GET",
                f"/api/conversations/{conversation['id']}/attempts/{retry_id}",
                cookie=self.cookie,
            )
            if polled["attempt"]["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(polled["attempt"]["status"], "completed")
        self.assertEqual(polled["attempt"]["output_text"], "reply:Frozen source")
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.router.calls[0]["history"], [{
            "role": "user", "content": "Frozen context"
        }])

    def test_delete_hides_conversation_and_unauthenticated_requests_fail(self):
        status, _ = self.request("GET", "/api/conversations")
        self.assertEqual(status, 401)
        status, conversation = self.request(
            "POST", "/api/conversations", {"title": "Remove"}, self.cookie
        )
        self.assertEqual(status, 201)
        status, deleted = self.request(
            "DELETE", f"/api/conversations/{conversation['id']}", cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        status, _ = self.request(
            "GET", f"/api/conversations/{conversation['id']}", cookie=self.cookie
        )
        self.assertEqual(status, 404)

    def test_conversation_mutations_reject_cross_origin_requests(self):
        status, _ = self.request(
            "POST",
            "/api/conversations",
            {"title": "Blocked"},
            self.cookie,
            {"Origin": "https://untrusted.example"},
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
