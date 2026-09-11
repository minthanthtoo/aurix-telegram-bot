import http.client
import json
import tempfile
import threading
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

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=kwargs["model"],
            returned_model="fake-provider-model",
            usage={"total_tokens": 3},
            upstream_request_id=f"upstream-{len(self.calls)}",
        )


class AIConversationHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        store = AIConversationStore(Path(self.tempdir.name) / "conversations.db")
        store.initialize()
        self.router = _ConversationRouter()
        self.application = AuriXAIApplication(
            self.router,
            access_token="legacy-test",
            conversation_store=store,
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
        self.assertEqual(status, 200)
        self.assertEqual(first["attempt"]["status"], "completed")
        self.assertEqual(first["text"], "reply:Hello")
        self.assertEqual(len(self.router.calls), 1)
        status, duplicate = self.request(
            "POST", f"/api/conversations/{conversation_id}/turns", body, self.cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["attempt"]["id"], first["attempt"]["id"])
        self.assertEqual(len(self.router.calls), 1)
        status, detail = self.request(
            "GET", f"/api/conversations/{conversation_id}", cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["turns"][0]["attempts"][0]["output_text"], "reply:Hello")

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
