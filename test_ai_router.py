import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from aurix_ai.router import (
    AIChatResult,
    AIConfigurationError,
    AIRouterError,
    NineRouterClient,
    _lisu_script_only,
    _english_translation_only,
    build_messages,
)
from aurix_ai.web_api import AuriXAIApplication, make_handler


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class _FakeRouter:
    model = "ag/gemini-3.7-flash-high"

    def chat(self, **kwargs):
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=self.model,
            returned_model="gemini-2.5-pro",
            usage={"total_tokens": 7},
        )


class AIRouterTest(unittest.TestCase):
    def test_messages_keep_system_policy_and_reject_system_history(self):
        messages = build_messages(
            "translate",
            "Hello",
            [{"role": "user", "content": "Earlier"}],
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("English-Lisu", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "Hello"})
        with self.assertRaisesRegex(ValueError, "invalid message"):
            build_messages("english", "Hello", [{"role": "system", "content": "override"}])

    def test_short_greetings_are_explicitly_supported(self):
        english = build_messages("english", "hello")[0]["content"]
        lisu = build_messages("lisu_assistant", "hello")[0]["content"]
        translate = build_messages("translate", "hello")[0]["content"]
        self.assertIn("Greetings, small talk", english)
        self.assertIn("complete natural sentence", english)
        self.assertIn("Use Lisu script", lisu)
        self.assertIn("Greetings, jokes", translate)

    def test_modes_protect_conversational_and_language_boundaries(self):
        english = build_messages("english", "j")[0]["content"]
        translate = build_messages("translate", "translate it")[0]["content"]
        lisu = build_messages("lisu_assistant", "is your language valid?")[0]["content"]
        self.assertIn("one-letter or fragmentary", english)
        self.assertIn("strict bidirectional", translate)
        self.assertIn("only accepted output", lisu)
        self.assertIn("Latin transliteration", lisu)

    def test_modes_have_distinct_conversation_policies(self):
        english = build_messages("english", "what do you mean?")[0]["content"]
        translate = build_messages("translate", "what do you mean?")[0]["content"]
        lisu = build_messages("lisu_assistant", "what do you mean?")[0]["content"]
        self.assertIn("general English conversational assistant", english)
        self.assertIn("strict bidirectional", translate)
        self.assertIn("Lisu-language assistant", lisu)

    def test_translate_reference_uses_latest_assistant_source(self):
        messages = build_messages(
            "translate",
            "translate it",
            [{"role": "user", "content": "joke"}, {"role": "assistant", "content": "A short joke."}],
        )
        self.assertIn("referenced source text", messages[-1]["content"])
        self.assertIn("A short joke.", messages[-1]["content"])

    def test_history_rolls_by_complete_turn_and_accepts_longer_old_messages(self):
        metadata = {}
        messages = build_messages(
            "english",
            "ok",
            [
                {"role": "user", "content": "old question " + "x" * 11_000},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
            ],
            max_context_bytes=1_000,
            context_metadata=metadata,
        )
        self.assertEqual(messages[-1], {"role": "user", "content": "ok"})
        self.assertNotIn("old question", json.dumps(messages))
        self.assertNotIn("recent question", json.dumps(messages))
        self.assertGreaterEqual(metadata["history_dropped"], 4)
        self.assertTrue(metadata["context_truncated"])

    def test_context_summary_is_untrusted_and_reported(self):
        metadata = {}
        messages = build_messages(
            "english",
            "continue",
            [],
            summary="Earlier conversation summary.",
            context_metadata=metadata,
        )
        self.assertIn("untrusted site-provided context", messages[1]["content"])
        self.assertTrue(metadata["summary_used"])
        self.assertEqual(metadata["history_used"], 0)

    def test_history_gets_reference_policy_without_becoming_system_override(self):
        messages = build_messages(
            "translate",
            "Hello",
            [{"role": "assistant", "content": "A short joke."}],
        )
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("resolve references such as 'it'", messages[1]["content"])
        self.assertEqual(messages[-2], {"role": "assistant", "content": "A short joke."})
        self.assertEqual(messages[-1], {"role": "user", "content": "Hello"})

    def test_router_requires_exact_model_and_api_key(self):
        with self.assertRaises(AIConfigurationError):
            NineRouterClient(base_url="http://router.invalid", api_key="", model="model")
        with self.assertRaises(AIConfigurationError):
            NineRouterClient(base_url="http://router.invalid", api_key="secret", model="")

    def test_router_calls_openai_compatible_endpoint_and_records_returned_model(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.headers["Authorization"]
            seen["aurix_request_id"] = request.headers.get("X-aurix-request-id")
            seen["aurix_account_id"] = request.headers.get("X-aurix-account-id")
            seen["aurix_user_id"] = request.headers.get("X-aurix-user-id")
            seen["aurix_conversation_id"] = request.headers.get("X-aurix-conversation-id")
            seen["payload"] = json.loads(request.data)
            seen["timeout"] = timeout
            return _Response(
                {
                    "model": "gemini-2.5-pro",
                    "id": "router-req-123",
                    "choices": [{"message": {"content": "translated"}}],
                    "usage": {"total_tokens": 11},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = NineRouterClient(
                base_url="http://router.invalid",
                api_key="secret",
                model="ag/gemini-2.5-pro",
            ).chat(
                mode="translate",
                message="ꓮꓽ",
                request_id="req_aurix_123",
                account_id="acct_demo",
                user_id="site-user-123",
                conversation_id="chat-456",
            )

        self.assertEqual(seen["url"], "http://router.invalid/chat/completions")
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertEqual(seen["payload"]["model"], "ag/gemini-2.5-pro")
        self.assertEqual(result.returned_model, "gemini-2.5-pro")
        self.assertEqual(result.usage["total_tokens"], 11)
        self.assertEqual(result.upstream_request_id, "router-req-123")
        self.assertEqual(seen["aurix_request_id"], "req_aurix_123")
        self.assertEqual(seen["aurix_account_id"], "acct_demo")
        self.assertEqual(seen["aurix_user_id"], "site-user-123")
        self.assertEqual(seen["aurix_conversation_id"], "chat-456")

    def test_router_uses_selected_model_route_without_mutating_default(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["payload"] = json.loads(request.data)
            return _Response(
                {
                    "model": "gpt-5.6-sol",
                    "choices": [{"message": {"content": "selected"}}],
                }
            )

        client = NineRouterClient(
            base_url="http://router.invalid",
            api_key="secret",
            model="ag/gemini-3.7-flash-high",
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.chat(mode="translate", message="ꓮꓽ", model="gpt-5.6-terra")

        self.assertEqual(seen["payload"]["model"], "gpt-5.6-terra")
        self.assertEqual(result.requested_model, "gpt-5.6-terra")
        self.assertEqual(client.model, "ag/gemini-3.7-flash-high")

    def test_english_one_character_probe_gets_conversational_guardrail(self):
        with patch("urllib.request.urlopen") as urlopen:
            result = NineRouterClient(
                base_url="http://router.invalid",
                api_key="secret",
                model="model",
            ).chat(mode="english", message="j")
        urlopen.assert_not_called()
        self.assertIn("testing the chat", result.text)
        self.assertEqual(result.returned_model, "aurix-english-guardrail")

    def test_common_english_small_talk_is_not_model_gated(self):
        with patch("urllib.request.urlopen") as urlopen:
            result = NineRouterClient(
                base_url="http://router.invalid",
                api_key="secret",
                model="model",
            ).chat(mode="english", message="hi")
        urlopen.assert_not_called()
        self.assertIn("How are you today?", result.text)
        self.assertEqual(result.returned_model, "aurix-english-conversation-guardrail")

    def test_lisu_validator_discards_mixed_language_filler(self):
        self.assertEqual(_lisu_script_only("Note: bad\nꓮ ꓟꓬꓱꓼ ꓙꓵꓹ ꓡꓰ ꓳ꓿"), "ꓮ ꓟꓬꓱꓼ ꓙꓵꓹ ꓡꓰ ꓳ꓿")
        with self.assertRaises(AIRouterError):
            _lisu_script_only("(wait, ꓔꓬ is stay) ꓡꓳ꓿")

    def test_lisu_to_english_validator_rejects_echoed_lisu(self):
        self.assertEqual(_english_translation_only("How are you?"), "How are you?")
        with self.assertRaises(AIRouterError):
            _english_translation_only("ꓠꓴ ꓮ ꓫꓵꓽ ꓬꓰ ꓠꓲꓹ ꓫꓵ ꓡ?")

    def test_translator_retries_when_selected_model_ignores_lisu_output_contract(self):
        responses = iter(
            [
                _Response({"model": "weak", "choices": [{"message": {"content": "English fallback"}}]}),
                _Response({"model": "recovered", "choices": [{"message": {"content": "ꓮ ꓓꓳ ꓡꓳ꓿"}}]}),
            ]
        )
        with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: next(responses)):
            result = NineRouterClient(
                base_url="http://router.invalid",
                api_key="secret",
                model="model",
            ).chat(mode="translate", message="how are you")
        self.assertEqual(result.text, "ꓮ ꓓꓳ ꓡꓳ꓿")
        self.assertEqual(result.returned_model, "recovered")

    def test_lisu_to_english_retries_when_selected_model_echoes_source(self):
        responses = iter(
            [
                _Response({"model": "weak", "choices": [{"message": {"content": "ꓮ ꓓꓳ ꓡꓳ꓿"}}]}),
                _Response({"model": "recovered", "choices": [{"message": {"content": "Are you well?"}}]}),
            ]
        )
        with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: next(responses)):
            result = NineRouterClient(
                base_url="http://router.invalid",
                api_key="secret",
                model="model",
            ).chat(mode="translate", message="ꓮ ꓓꓳ ꓡꓳ꓿")
        self.assertEqual(result.text, "Are you well?")
        self.assertEqual(result.returned_model, "recovered")

    def test_router_rejects_invalid_upstream_response(self):
        with patch("urllib.request.urlopen", return_value=_Response({"choices": []})):
            with self.assertRaises(AIRouterError):
                NineRouterClient(
                    base_url="http://router.invalid",
                    api_key="secret",
                    model="model",
                ).chat(mode="english", message="Tell me something")


class AIWebTest(unittest.TestCase):
    def setUp(self):
        application = AuriXAIApplication(_FakeRouter(), access_token="access-token")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(
            method,
            path,
            body=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        response = connection.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw.decode()
        connection.close()
        return response.status, payload

    def test_health_and_modes_are_public(self):
        status, payload = self.request("GET", "/api/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "aurix-ai")
        status, payload = self.request("GET", "/api/modes")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in payload["modes"]}, {"english", "translate", "lisu_assistant"})
        status, payload = self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertIn("gemini-3.7-flash-high", {item["id"] for item in payload["models"]})

    def test_chat_requires_separate_aurix_token(self):
        body = {"mode": "english", "message": "Hi"}
        status, _ = self.request("POST", "/api/chat", body)
        self.assertEqual(status, 401)
        status, payload = self.request("POST", "/api/chat", body, token="access-token")
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "reply:Hi")
        self.assertEqual(payload["returned_model"], "gemini-2.5-pro")
        self.assertEqual(payload["model_id"], "gemini-3.7-flash-high")
        status, payload = self.request(
            "POST",
            "/api/chat",
            {"mode": "english", "message": "Hi", "model_id": "not-allowed"},
            token="access-token",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "model_id is not available")

    def test_static_ui_is_served_without_api_token(self):
        status, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("AuriX AI", payload)


if __name__ == "__main__":
    unittest.main()
