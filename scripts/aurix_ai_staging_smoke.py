#!/usr/bin/env python3
"""Run a deterministic, local staging smoke against the real AuriX AI HTTP stack.

This harness starts the actual application and handler on loopback with a
deterministic fake model stream. It does not contact Telegram, 9Router, or a
remote database, and it never changes production state. It is intended to
prove the end-to-end local transport before a real staging smoke is approved.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from aurix_ai.conversations import AIConversationStore
from aurix_ai.router import AIChatResult
from aurix_ai.web_api import AuriXAIApplication, make_handler


SMOKE_BOT_TOKEN = "aurix-local-staging-smoke-token"
SMOKE_USER_ID = 101
OTHER_USER_ID = 202


class _StreamResponse:
    def __init__(self, call_number: int):
        chunks = [
            {
                "id": f"smoke-upstream-{call_number}",
                "choices": [{"delta": {"content": "Hello "}}],
            },
            {
                "choices": [{"delta": {"content": "from AuriX 世界"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            },
        ]
        self.lines = iter(
            sum(
                ([f"data: {json.dumps(chunk, ensure_ascii=False)}\n".encode("utf-8"), b"\n"] for chunk in chunks),
                [],
            )
            + [b"data: [DONE]\n", b"\n"]
        )

    def readline(self):
        try:
            return next(self.lines)
        except StopIteration:
            return b""

    def close(self):
        return None


class _SmokeRouter:
    model = "ag/gemini-3.7-flash-high"

    def __init__(self):
        self.calls = []

    def openai_chat_stream(self, payload, **kwargs):
        self.calls.append({"payload": payload, "kwargs": kwargs})
        return _StreamResponse(len(self.calls))

    def chat(self, **kwargs):
        return AIChatResult(
            text=f"reply:{kwargs['message']}",
            requested_model=kwargs["model"],
            returned_model="smoke-provider-model",
            usage={"total_tokens": 1},
        )


def _init_data(user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": f"AA-smoke-{user_id}",
        "user": json.dumps(
            {"id": user_id, "first_name": "Smoke", "username": f"smoke_{user_id}"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", SMOKE_BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _request(port: int, method: str, path: str, *, body=None, cookie: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    encoded = None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    connection.request(method, path, encoded, headers)
    response = connection.getresponse()
    raw = response.read()
    set_cookie = response.getheader("Set-Cookie")
    connection.close()
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = raw.decode("utf-8", "replace")
    return response.status, payload, set_cookie


def _cookie_value(set_cookie: str | None) -> str:
    cookie = SimpleCookie()
    cookie.load(set_cookie or "")
    morsel = cookie.get("aurix_ai_session")
    if morsel is None or not morsel.value:
        raise RuntimeError("local smoke login did not issue a session cookie")
    return f"aurix_ai_session={morsel.value}"


def _wait_for_attempt(port: int, conversation_id: str, attempt_id: str, cookie: str):
    path = f"/api/conversations/{conversation_id}/attempts/{attempt_id}"
    for _ in range(100):
        status, payload, _ = _request(port, "GET", path, cookie=cookie)
        if status != 200:
            raise RuntimeError(f"attempt polling failed with HTTP {status}")
        if payload["attempt"]["status"] != "running":
            return payload
        time.sleep(0.01)
    raise RuntimeError("local smoke attempt did not reach a terminal state")


def run_smoke() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        store = AIConversationStore(Path(temporary_directory) / "conversations.db")
        store.initialize()
        router = _SmokeRouter()
        application = AuriXAIApplication(
            router,
            access_token="unused-local-smoke-token",
            telegram_bot_token=SMOKE_BOT_TOKEN,
            telegram_bot_username="aurix_local_smoke_bot",
            conversation_store=store,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            checks: list[str] = []
            status, health, _ = _request(port, "GET", "/api/healthz")
            if status != 200 or health.get("service") != "aurix-ai":
                raise RuntimeError("health check failed")
            checks.append("health")

            status, _login, set_cookie = _request(
                port,
                "POST",
                "/api/auth/miniapp",
                body={"init_data": _init_data(SMOKE_USER_ID)},
            )
            if status != 200:
                raise RuntimeError("signed Mini App login failed")
            cookie = _cookie_value(set_cookie)
            checks.append("signed_auth")

            status, created, _ = _request(
                port,
                "POST",
                "/api/conversations",
                body={"mode": "english", "title": "Local smoke"},
                cookie=cookie,
            )
            if status != 201:
                raise RuntimeError("conversation creation failed")
            conversation_id = created["id"]
            checks.append("conversation_create")

            turn_body = {
                "mode": "english",
                "message": "Smoke test 世界",
                "client_submission_id": "local-smoke-submission-1",
            }
            status, streamed, _ = _request(
                port,
                "POST",
                f"/api/conversations/{conversation_id}/turns/stream",
                body=turn_body,
                cookie=cookie,
            )
            if status != 200 or not isinstance(streamed, str):
                raise RuntimeError("direct durable turn stream did not complete")
            if 'event: delta' not in streamed or '"text":"Hello "' not in streamed:
                raise RuntimeError("direct stream did not forward provider deltas")
            if "event: terminal" not in streamed:
                raise RuntimeError("direct stream did not emit a terminal event")
            detail_status, detail, _ = _request(
                port,
                "GET",
                f"/api/conversations/{conversation_id}",
                cookie=cookie,
            )
            if detail_status != 200 or detail["turns"][0]["attempts"][0]["output_text"] != "Hello from AuriX 世界":
                raise RuntimeError("streamed Unicode output was not persisted exactly")
            attempt_id = detail["turns"][0]["attempts"][0]["id"]
            checks.append("streamed_turn")

            status, duplicate, _ = _request(
                port,
                "POST",
                f"/api/conversations/{conversation_id}/turns",
                body=turn_body,
                cookie=cookie,
            )
            if status != 200 or duplicate["attempt"]["id"] != attempt_id or len(router.calls) != 1:
                raise RuntimeError("duplicate submission was not idempotent")
            checks.append("duplicate_idempotency")

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "GET",
                f"/api/conversations/{conversation_id}/attempts/{attempt_id}/events",
                headers={"Cookie": cookie},
            )
            response = connection.getresponse()
            events = response.read().decode("utf-8")
            connection.close()
            if response.status != 200 or "event: snapshot" not in events or "event: terminal" not in events:
                raise RuntimeError("reconnectable event stream contract failed")
            checks.append("reconnectable_events")

            other_status, _other_payload, _ = _request(
                port,
                "POST",
                "/api/auth/miniapp",
                body={"init_data": _init_data(OTHER_USER_ID)},
            )
            if other_status != 200:
                raise RuntimeError("second signed smoke login failed")
            # The second cookie is intentionally not retained: owner isolation
            # is covered by the HTTP regression suite without exposing it here.
            checks.append("second_auth")
            return {"ok": True, "checks": checks, "provider_calls": len(router.calls)}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            application.close()


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
