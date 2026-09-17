"""Branded AuriX AI web service backed by the existing 9Router."""

from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as email_default_policy
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from .router import (
    MODEL_CATALOG,
    AIChatResult,
    AIConfigurationError,
    AIRouterError,
    AIRouterHTTPError,
    AIRouterTimeoutError,
    build_messages,
    NineRouterClient,
    MAX_MESSAGE_CHARS,
    MAX_CONTEXT_SUMMARY_CHARS,
    model_id_for_route,
    model_profile,
    normalize_mode,
    normalize_translation_direction,
    _optional_context_text,
    _text,
    resolve_model_id,
)
from .api_keys import APIKeyStore, APIKeyStoreError, normalize_token_usage
from .capabilities import build_capability_report
from .conversations import (
    AIConversationStore,
    ConversationNotFoundError,
    ConversationStoreError,
)
from .conversation_jobs import ConversationJobManager
from persistence import open_sqlite_connection
from telegram_web_app import (
    TelegramWebAppAuthError,
    VerifiedTelegramUser,
    verify_init_data,
    verify_login_widget,
)


# The package lives below the repository/container root while the static UI
# remains a top-level web asset shared by the deployment image.
STATIC_ROOT = Path(__file__).resolve().parents[1] / "web" / "ai-app"
MAX_JSON_BYTES = 128 * 1024
MAX_HISTORY_ITEMS = 100
MAX_STANDARD_MESSAGES = MAX_HISTORY_ITEMS + 1
MAX_STANDARD_TOOLS = 64
MAX_STANDARD_TOOL_BYTES = 64 * 1024
MAX_AUDIO_REQUEST_BYTES = 30 * 1024 * 1024
MAX_AUDIO_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_URL_CHARS = 16 * 1024
SESSION_COOKIE_NAME = "aurix_ai_session"
DEFAULT_PARTNER_MODES = frozenset(
    {
        "english",
        "translate",
        "lisu_assistant",
        "embeddings",
        "audio",
        "image_generation",
        "video_generation",
    }
)


class ExternalAPIUnavailableError(RuntimeError):
    """The external API was requested before its persistent key store was configured."""


class ExternalAPIAccessDeniedError(PermissionError):
    """The external account key is valid but lacks the requested scope."""


class ExternalAPIRateLimitError(RuntimeError):
    """The external account has exceeded its configured request rate."""


class _DownstreamStreamDisconnected(Exception):
    """A client stopped accepting bytes after an SSE response was opened."""


@dataclass
class _ExternalStream:
    response: Any
    request_id: str
    principal: Any
    model_id: str
    user_id: str | None
    conversation_id: str | None
    public_completion_id: str
    mode: str
    endpoint: str = "/chat/completions"
    protocol: str = "chat"
    started_at: float = 0.0


@dataclass
class _RawExternalStream:
    response: Any
    request_id: str
    principal: Any
    model_id: str
    endpoint: str
    mode: str = "audio"
    started_at: float = 0.0
    max_bytes: int = MAX_AUDIO_RESPONSE_BYTES


@dataclass
class _DurableStream:
    """One first-party browser stream and its durable attempt."""

    user: VerifiedTelegramUser
    conversation_id: str
    attempt: dict[str, Any]
    response: Any | None


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


class AIRateLimiter:
    """Small process-local fixed-window limiter for the single-worker MVP."""

    def __init__(self, requests_per_minute: int = 20) -> None:
        self.limit = max(1, min(int(requests_per_minute), 600))
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, identity: str, *, limit: int | None = None) -> bool:
        now = int(time.time())
        window = now // 60
        configured_limit = self.limit if limit is None else max(1, min(int(limit), 600))
        with self._lock:
            current_window, count = self._windows.get(identity, (window, 0))
            if current_window != window:
                current_window, count = window, 0
            if count >= configured_limit:
                self._windows[identity] = (current_window, count)
                return False
            self._windows[identity] = (current_window, count + 1)
            if len(self._windows) > 10_000:
                self._windows = {
                    key: value for key, value in self._windows.items() if value[0] >= window
                }
            return True


@dataclass(frozen=True)
class _AISession:
    user: VerifiedTelegramUser
    expires_at: float


class AISessionStore:
    """Opaque Telegram sessions with optional durable storage.

    Production uses a small SQLite table on the mounted AuriX data volume so
    container restarts do not sign every browser out. Only a SHA-256 token
    digest is persisted; the browser keeps the opaque token in an HttpOnly
    cookie. Sessions are rolling while the account is active.
    """

    def __init__(self, max_age_seconds: int, database_path: str | Path | None = None) -> None:
        self.max_age_seconds = max(300, min(int(max_age_seconds), 7_776_000))
        self.database_path = Path(database_path) if database_path else None
        self._lock = threading.Lock()
        self._sessions: dict[str, _AISession] = {}
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_web_sessions (
                        token_hash TEXT PRIMARY KEY,
                        telegram_id INTEGER NOT NULL,
                        first_name TEXT NOT NULL,
                        last_name TEXT,
                        username TEXT,
                        language_code TEXT,
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ai_web_sessions_expiry "
                    "ON ai_web_sessions (expires_at)"
                )

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        if self.database_path is None:
            raise RuntimeError("persistent session storage is not configured")
        return open_sqlite_connection(self.database_path, timeout_seconds=10)

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> VerifiedTelegramUser:
        return VerifiedTelegramUser(
            telegram_id=int(row["telegram_id"]),
            first_name=row["first_name"],
            last_name=row["last_name"],
            username=row["username"],
            language_code=row["language_code"],
        )

    def issue(self, user: VerifiedTelegramUser) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            expires_at = now + self.max_age_seconds
            if self.database_path is None:
                self._purge_locked(now)
                self._sessions[token] = _AISession(user, expires_at)
            else:
                with self._connect() as connection:
                    connection.execute("DELETE FROM ai_web_sessions WHERE expires_at <= ?", (now,))
                    connection.execute(
                        """
                        INSERT INTO ai_web_sessions
                        (token_hash, telegram_id, first_name, last_name, username,
                         language_code, expires_at, created_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._digest(token), user.telegram_id, user.first_name,
                            user.last_name, user.username, user.language_code,
                            expires_at, now, now,
                        ),
                    )
        return token

    def get(self, token: str | None) -> VerifiedTelegramUser | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            if self.database_path is None:
                session = self._sessions.get(token)
                if session is None or session.expires_at <= now:
                    self._sessions.pop(token, None)
                    return None
                return session.user
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM ai_web_sessions WHERE token_hash = ?",
                    (self._digest(token),),
                ).fetchone()
                if row is None or float(row["expires_at"]) <= now:
                    if row is not None:
                        connection.execute(
                            "DELETE FROM ai_web_sessions WHERE token_hash = ?",
                            (self._digest(token),),
                        )
                    return None
                refreshed_expiry = now + self.max_age_seconds
                connection.execute(
                    "UPDATE ai_web_sessions SET expires_at = ?, last_seen_at = ? "
                    "WHERE token_hash = ?",
                    (refreshed_expiry, now, self._digest(token)),
                )
                return self._user_from_row(row)

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                if self.database_path is None:
                    self._sessions.pop(token, None)
                else:
                    with self._connect() as connection:
                        connection.execute(
                            "DELETE FROM ai_web_sessions WHERE token_hash = ?",
                            (self._digest(token),),
                        )

    def _purge_locked(self, now: float) -> None:
        if len(self._sessions) > 10_000:
            self._sessions = {
                token: session
                for token, session in self._sessions.items()
                if session.expires_at > now
            }


class AuriXAIApplication:
    def __init__(
        self,
        router: NineRouterClient,
        *,
        access_token: str,
        allow_anonymous: bool = False,
        requests_per_minute: int = 20,
        telegram_bot_token: str = "",
        telegram_bot_username: str = "",
        session_max_age_seconds: int = 2_592_000,
        session_database_path: str | Path | None = None,
        legacy_token_enabled: bool | None = None,
        api_keys: APIKeyStore | None = None,
        conversation_store: AIConversationStore | None = None,
        admin_token: str = "",
        admin_telegram_ids: set[int] | None = None,
        operator_telegram_ids: set[int] | None = None,
        operator_allowed_modes: set[str] | None = None,
        operator_allowed_models: set[str] | None = None,
        operator_max_requests_per_minute: int = 600,
        model_catalog_ttl_seconds: int = 60,
    ) -> None:
        if not allow_anonymous and not access_token and not telegram_bot_token:
            raise AIConfigurationError("Telegram authentication or AURIX_AI_ACCESS_TOKEN is required")
        if telegram_bot_token and not telegram_bot_username:
            raise AIConfigurationError("AURIX_TELEGRAM_BOT_USERNAME is required")
        self.router = router
        self.access_token = access_token
        self.allow_anonymous = allow_anonymous
        self.telegram_bot_token = telegram_bot_token
        self.telegram_bot_username = telegram_bot_username
        self.sessions = AISessionStore(session_max_age_seconds, session_database_path)
        self.api_keys = api_keys
        self.conversations = conversation_store
        self.conversation_jobs = ConversationJobManager() if conversation_store is not None else None
        self._durable_stream_lock = threading.Lock()
        self._durable_streams: dict[str, Any] = {}
        self.admin_token = admin_token.strip()
        self.admin_telegram_ids = frozenset(admin_telegram_ids or set())
        self.operator_telegram_ids = frozenset(operator_telegram_ids or set())
        self.operator_allowed_modes = frozenset(operator_allowed_modes or DEFAULT_PARTNER_MODES)
        self.operator_allowed_models = frozenset(operator_allowed_models or {"*"})
        self.operator_max_requests_per_minute = _admin_requests_per_minute(
            operator_max_requests_per_minute
        )
        if not isinstance(model_catalog_ttl_seconds, int) or not 0 <= model_catalog_ttl_seconds <= 3_600:
            raise AIConfigurationError(
                "model_catalog_ttl_seconds must be an integer between 0 and 3600"
            )
        self.model_catalog_ttl_seconds = model_catalog_ttl_seconds
        self._model_catalog_cache_lock = threading.Lock()
        self._model_catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # Direct construction stays compatible with the old test/client API;
        # environment-based production startup passes an explicit false.
        self.legacy_token_enabled = bool(access_token) if legacy_token_enabled is None else legacy_token_enabled
        self.rate_limiter = AIRateLimiter(requests_per_minute)

    def close(self) -> None:
        with self._durable_stream_lock:
            active_streams = list(self._durable_streams.values())
            self._durable_streams.clear()
        for response in active_streams:
            try:
                response.close()
            except Exception:
                pass
        if self.conversation_jobs is not None:
            self.conversation_jobs.close()

    def _register_durable_stream(self, attempt_id: str, response: Any) -> None:
        with self._durable_stream_lock:
            self._durable_streams[str(attempt_id)] = response

    def _unregister_durable_stream(self, attempt_id: str) -> None:
        with self._durable_stream_lock:
            self._durable_streams.pop(str(attempt_id), None)

    def cancel_durable_stream(self, attempt_id: str) -> None:
        with self._durable_stream_lock:
            response = self._durable_streams.get(str(attempt_id))
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    @staticmethod
    def user_payload(user: VerifiedTelegramUser) -> dict[str, Any]:
        return {
            "telegram_id": user.telegram_id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username,
            "language_code": user.language_code,
        }

    def authenticate(
        self,
        cookie_header: str | None,
        authorization: str | None,
        *,
        required: bool = True,
    ) -> VerifiedTelegramUser | None:
        if self.allow_anonymous:
            return None
        cookie = SimpleCookie()
        if cookie_header:
            cookie.load(cookie_header)
        user = self.sessions.get(cookie.get(SESSION_COOKIE_NAME).value if cookie.get(SESSION_COOKIE_NAME) else None)
        if user is not None:
            return user
        if self.legacy_token_enabled:
            expected = f"Bearer {self.access_token}"
            if authorization and self.access_token and hmac.compare_digest(authorization, expected):
                return None
        if required:
            raise PermissionError("Telegram login required")
        return None

    def login_widget(self, payload: dict[str, Any]) -> tuple[str, VerifiedTelegramUser]:
        user = verify_login_widget(
            payload,
            self.telegram_bot_token,
            max_age_seconds=self.sessions.max_age_seconds,
        )
        return self.sessions.issue(user), user

    def login_miniapp(self, init_data: str) -> tuple[str, VerifiedTelegramUser]:
        user = verify_init_data(
            init_data,
            self.telegram_bot_token,
            max_age_seconds=self.sessions.max_age_seconds,
        )
        return self.sessions.issue(user), user

    def auth_config(self) -> dict[str, Any]:
        return {
            "telegram_login_enabled": bool(self.telegram_bot_token and self.telegram_bot_username),
            "bot_username": self.telegram_bot_username or None,
        }

    @staticmethod
    def modes_payload() -> dict[str, Any]:
        return {
            "modes": [
                {
                    "id": "english",
                    "label": "English assistant",
                    "description": "General answers in clear English.",
                },
                {
                    "id": "translate",
                    "label": "English ↔ Lisu translator",
                    "description": "Faithful translation with uncertainty notes when needed.",
                },
                {
                    "id": "lisu_assistant",
                    "label": "Lisu assistant (experimental)",
                    "description": "Lisu-script conversation; native-speaker review recommended.",
                },
            ]
        }

    def models_payload(self) -> dict[str, Any]:
        return {
            "models": [
                {
                    "id": model_id,
                    "label": item["label"],
                    "description": item["description"],
                    "default": item["route"] == self.router.model,
                    "capabilities": ["chat", "responses", "streaming"],
                }
                for model_id, item in MODEL_CATALOG.items()
            ]
        }

    def image_models_payload(self) -> dict[str, Any]:
        """Return the image-generation catalog exposed by the configured 9Router."""

        try:
            models = self._discovered_models("image")
        except AIRouterError:
            return {"models": [], "available": False}
        profiles = []
        for model in models:
            profile = model_profile(
                model["id"],
                capabilities=("image_generation",),
                owned_by=str(model.get("owned_by") or "9router"),
            )
            profiles.append({**profile, "label": profile["display_name"]})
        return {"models": profiles, "available": bool(profiles)}

    def _discovered_models(self, category: str | None = None) -> list[dict[str, Any]]:
        """Return a short-lived raw catalog cache without caching key policy."""

        cache_key = category or "*"
        now = time.monotonic()
        with self._model_catalog_cache_lock:
            cached = self._model_catalog_cache.get(cache_key)
            if (
                cached is not None
                and self.model_catalog_ttl_seconds > 0
                and now - cached[0] < self.model_catalog_ttl_seconds
            ):
                return [dict(item) for item in cached[1]]
        models = self.router.list_models(category)
        safe_models = [
            dict(item)
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        with self._model_catalog_cache_lock:
            self._model_catalog_cache[cache_key] = (now, safe_models)
        return [dict(item) for item in safe_models]

    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode", "english")
        message = body.get("message")
        history = body.get("history")
        model_route, model_id = resolve_model_id(body.get("model_id"), default_route=self.router.model)
        result = self.router.chat(
            mode=mode,
            message=message,
            history=history if history is not None else [],
            model=model_route,
            summary=body.get("context_summary"),
            direction=body.get("direction"),
        )
        return _chat_payload(result, model_id=model_id)

    def image_generation(
        self,
        user: VerifiedTelegramUser | None,
        body: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate image data for the authenticated first-party web console."""

        request = _normalize_image_generation_request(body)
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        result = self.router.request_json(
            "/images/generations",
            request["payload"],
            request_id=request_id,
            user_id=(f"telegram:{user.telegram_id}" if user is not None else None),
        )
        return _standard_image_payload(result, model=request["model_id"])

    @staticmethod
    def _public_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in attempt.items()
            if key != "owner_telegram_id"
        }

    @classmethod
    def _public_conversation(cls, conversation: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value
            for key, value in conversation.items()
            if key != "owner_telegram_id"
        }
        turns = []
        for turn in conversation.get("turns", []):
            clean_turn = {
                key: value
                for key, value in turn.items()
                if key != "owner_telegram_id"
            }
            clean_turn["attempts"] = [
                cls._public_attempt(attempt)
                for attempt in turn.get("attempts", [])
            ]
            turns.append(clean_turn)
        if "turns" in conversation:
            result["turns"] = turns
        return result

    def durable_conversation_create(
        self, user: VerifiedTelegramUser, body: dict[str, Any]
    ) -> dict[str, Any]:
        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        mode = normalize_mode(body.get("mode", "english"))
        direction = normalize_translation_direction(body.get("direction"))
        title = _text(body.get("title", "New conversation"), name="title", maximum=120)
        return self._public_conversation(
            self.conversations.create_conversation(
                user.telegram_id,
                mode=mode,
                direction=direction,
                title=title,
            )
        )

    def durable_conversation_list(
        self, user: VerifiedTelegramUser, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        return [
            self._public_conversation(item)
            for item in self.conversations.list_conversations(user.telegram_id, limit=limit)
        ]

    def durable_conversation_detail(
        self, user: VerifiedTelegramUser, conversation_id: str
    ) -> dict[str, Any]:
        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        return self._public_conversation(
            self.conversations.get_conversation(user.telegram_id, conversation_id)
        )

    def durable_conversation_delete(
        self, user: VerifiedTelegramUser, conversation_id: str
    ) -> bool:
        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        return self.conversations.delete_conversation(user.telegram_id, conversation_id)

    def durable_chat(
        self,
        user: VerifiedTelegramUser,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one submitted turn and execute at most one upstream call.

        A duplicate ``client_submission_id`` returns the existing attempt. A
        process restart leaves a running attempt as ``interrupted`` and this
        endpoint will not resend it implicitly; callers must explicitly retry.
        """

        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        mode = normalize_mode(body.get("mode", "english"))
        message = _text(body.get("message"), name="message", maximum=MAX_MESSAGE_CHARS)
        direction = normalize_translation_direction(body.get("direction"))
        model_route, model_id = resolve_model_id(
            body.get("model_id"), default_route=self.router.model
        )
        client_submission_id = body.get("client_submission_id")
        if client_submission_id is not None and not isinstance(client_submission_id, str):
            raise ValueError("client_submission_id must be text")
        context = self.conversations.context_messages(user.telegram_id, conversation_id)
        attempt, created = self.conversations.create_turn(
            user.telegram_id,
            conversation_id,
            source=message,
            mode=mode,
            direction=direction,
            model_id=model_id,
            context=context,
            client_submission_id=client_submission_id,
        )
        if not created or attempt["status"] != "running":
            return {
                "conversation_id": conversation_id,
                "turn_id": attempt["turn_id"],
                "attempt": self._public_attempt(attempt),
                "mode_result": attempt["status"],
            }
        try:
            result = self.router.chat(
                mode=mode,
                message=message,
                history=context,
                model=model_route,
                request_id=attempt["request_id"],
                user_id=str(user.telegram_id),
                conversation_id=conversation_id,
                direction=direction,
            )
        except AIRouterError:
            failed = self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="upstream_error"
            )
            return {
                "conversation_id": conversation_id,
                "turn_id": attempt["turn_id"],
                "attempt": self._public_attempt(failed),
                "mode_result": "failed",
            }
        except Exception:
            failed = self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="internal_error"
            )
            return {
                "conversation_id": conversation_id,
                "turn_id": attempt["turn_id"],
                "attempt": self._public_attempt(failed),
                "mode_result": "failed",
            }
        completed = self.conversations.complete_attempt(
            user.telegram_id,
            attempt["id"],
            output_text=result.text,
            usage=result.usage,
            upstream_request_id=result.upstream_request_id,
        )
        payload = _chat_payload(result, model_id=model_id)
        payload.update(
            {
                "conversation_id": conversation_id,
                "turn_id": attempt["turn_id"],
                "attempt": self._public_attempt(completed),
            }
        )
        return payload

    def _durable_stream_payload(
        self,
        *,
        mode: str,
        message: str,
        history: list[dict[str, str]],
        model_route: str,
        direction: str | None,
    ) -> dict[str, Any]:
        direction_instruction = (
            "Translation direction is English to Lisu. Translate the source into Lisu; do not "
            "answer it. Return only the translation."
            if direction == "en_to_lisu"
            else "Translation direction is Lisu to English. Translate the source into English; "
            "do not answer it. Return only the translation."
            if direction == "lisu_to_en"
            else None
        )
        messages = build_messages(
            mode,
            message,
            history,
            instructions=direction_instruction,
        )
        return {
            "model": model_route,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.2,
        }

    @staticmethod
    def _stream_payloads(response: Any):
        event_lines: list[bytes] = []
        while True:
            line = response.readline()
            if not line:
                break
            if line in {b"\n", b"\r\n"}:
                if event_lines:
                    event = b"\n".join(event_lines)
                    event_lines = []
                    data_lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:")]
                    if data_lines:
                        data = b"\n".join(data_lines).strip()
                        if data == b"[DONE]":
                            yield None
                        else:
                            try:
                                payload = json.loads(data)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                continue
                            if isinstance(payload, dict):
                                yield payload
            else:
                event_lines.append(line.rstrip(b"\r\n"))
        if event_lines:
            data_lines = [line[5:].lstrip() for line in b"\n".join(event_lines).splitlines() if line.startswith(b"data:")]
            if data_lines:
                data = b"\n".join(data_lines).strip()
                if data == b"[DONE]":
                    yield None
                else:
                    try:
                        payload = json.loads(data)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict):
                        yield payload

    def _run_durable_attempt(
        self,
        user: VerifiedTelegramUser,
        attempt: dict[str, Any],
        *,
        mode: str,
        message: str,
        history: list[dict[str, str]],
        model_route: str,
        direction: str | None,
        stop_event: Any,
    ) -> None:
        if self.conversations is None:
            return
        attempt_id = str(attempt["id"])
        response = None
        try:
            current = self.conversations.attempt(user.telegram_id, attempt_id)
            if current["status"] != "running" or stop_event.is_set():
                return
            stream_method = getattr(self.router, "openai_chat_stream", None)
            if callable(stream_method):
                response = stream_method(
                    self._durable_stream_payload(
                        mode=mode,
                        message=message,
                        history=history,
                        model_route=model_route,
                        direction=direction,
                    ),
                    request_id=attempt["request_id"],
                    user_id=str(user.telegram_id),
                    conversation_id=attempt["conversation_id"],
                )
                output = ""
                usage = None
                upstream_request_id = None
                for payload in self._stream_payloads(response):
                    if stop_event.is_set():
                        return
                    if payload is None:
                        break
                    if payload.get("id"):
                        upstream_request_id = str(payload["id"])
                    if isinstance(payload.get("usage"), dict):
                        usage = payload["usage"]
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    fragment = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(fragment, str) and fragment:
                        output += fragment
                        self.conversations.update_attempt_output(
                            user.telegram_id, attempt_id, output_text=output
                        )
                if not output.strip():
                    raise AIRouterError("9Router returned an empty stream")
                self.conversations.complete_attempt(
                    user.telegram_id,
                    attempt_id,
                    output_text=output,
                    usage=usage,
                    upstream_request_id=upstream_request_id,
                )
                return
            result = self.router.chat(
                mode=mode,
                message=message,
                history=history,
                model=model_route,
                request_id=attempt["request_id"],
                user_id=str(user.telegram_id),
                conversation_id=attempt["conversation_id"],
                direction=direction,
            )
            if not stop_event.is_set():
                self.conversations.complete_attempt(
                    user.telegram_id,
                    attempt_id,
                    output_text=result.text,
                    usage=result.usage,
                    upstream_request_id=result.upstream_request_id,
                )
        except AIRouterError:
            self.conversations.fail_attempt(user.telegram_id, attempt_id, error_code="upstream_error")
        except ConversationNotFoundError:
            return
        except Exception:
            self.conversations.fail_attempt(user.telegram_id, attempt_id, error_code="internal_error")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def durable_chat_submit(
        self,
        user: VerifiedTelegramUser,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Create an attempt and return before inference completes."""
        if self.conversations is None or self.conversation_jobs is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        mode = normalize_mode(body.get("mode", "english"))
        message = _text(body.get("message"), name="message", maximum=MAX_MESSAGE_CHARS)
        direction = normalize_translation_direction(body.get("direction"))
        model_route, model_id = resolve_model_id(
            body.get("model_id"), default_route=self.router.model
        )
        client_submission_id = body.get("client_submission_id")
        if client_submission_id is not None and not isinstance(client_submission_id, str):
            raise ValueError("client_submission_id must be text")
        context = self.conversations.context_messages(user.telegram_id, conversation_id)
        attempt, created = self.conversations.create_turn(
            user.telegram_id,
            conversation_id,
            source=message,
            mode=mode,
            direction=direction,
            model_id=model_id,
            context=context,
            client_submission_id=client_submission_id,
        )
        if created:
            try:
                submitted = self.conversation_jobs.submit(
                    attempt["id"],
                    lambda stop_event: self._run_durable_attempt(
                        user,
                        attempt,
                        mode=mode,
                        message=message,
                        history=context,
                        model_route=model_route,
                        direction=direction,
                        stop_event=stop_event,
                    ),
                    conversation_id=attempt["conversation_id"],
                    owner_id=user.telegram_id,
                )
                if not submitted:
                    attempt = self.conversations.fail_attempt(
                        user.telegram_id, attempt["id"], error_code="concurrency_limit"
                    )
            except Exception:
                failed = self.conversations.fail_attempt(
                    user.telegram_id, attempt["id"], error_code="worker_unavailable"
                )
                attempt = failed
        return {
            "conversation_id": conversation_id,
            "turn_id": attempt["turn_id"],
            "attempt": self._public_attempt(attempt),
            "mode_result": attempt["status"],
        }

    def durable_chat_stream(
        self,
        user: VerifiedTelegramUser,
        conversation_id: str,
        body: dict[str, Any],
    ) -> _DurableStream:
        """Create a durable turn and open the provider stream before the first byte.

        The existing ``/turns`` endpoint remains an asynchronous, reconnectable
        job API. This companion path is for the first-party composer: it keeps
        the same durable turn/attempt records but lets the HTTP handler forward
        provider deltas immediately instead of waiting for a database snapshot.
        """

        if self.conversations is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        mode = normalize_mode(body.get("mode", "english"))
        message = _text(body.get("message"), name="message", maximum=MAX_MESSAGE_CHARS)
        direction = normalize_translation_direction(body.get("direction"))
        model_route, model_id = resolve_model_id(
            body.get("model_id"), default_route=self.router.model
        )
        client_submission_id = body.get("client_submission_id")
        if client_submission_id is not None and not isinstance(client_submission_id, str):
            raise ValueError("client_submission_id must be text")
        context = self.conversations.context_messages(user.telegram_id, conversation_id)
        attempt, created = self.conversations.create_turn(
            user.telegram_id,
            conversation_id,
            source=message,
            mode=mode,
            direction=direction,
            model_id=model_id,
            context=context,
            client_submission_id=client_submission_id,
        )
        if not created or attempt["status"] != "running":
            return _DurableStream(
                user=user,
                conversation_id=conversation_id,
                attempt=attempt,
                response=None,
            )

        stream_method = getattr(self.router, "openai_chat_stream", None)
        if not callable(stream_method):
            self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="streaming_unavailable"
            )
            raise ValueError("live streaming is not supported by the configured router")
        try:
            response = stream_method(
                self._durable_stream_payload(
                    mode=mode,
                    message=message,
                    history=context,
                    model_route=model_route,
                    direction=direction,
                ),
                request_id=attempt["request_id"],
                user_id=str(user.telegram_id),
                conversation_id=conversation_id,
            )
        except AIRouterError:
            self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="upstream_error"
            )
            raise
        except Exception:
            self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="internal_error"
            )
            raise
        if response is None or not callable(getattr(response, "readline", None)):
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="invalid_upstream_stream"
            )
            raise AIRouterError("9Router returned an invalid stream")
        self._register_durable_stream(attempt["id"], response)
        return _DurableStream(
            user=user,
            conversation_id=conversation_id,
            attempt=attempt,
            response=response,
        )

    def durable_attempt_retry(
        self,
        user: VerifiedTelegramUser,
        conversation_id: str,
        attempt_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry a failed durable attempt without creating another user turn."""
        if self.conversations is None or self.conversation_jobs is None:
            raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
        previous = self.conversations.attempt(user.telegram_id, attempt_id)
        if previous["conversation_id"] != conversation_id:
            raise ConversationNotFoundError("attempt not found")
        requested_model = body.get("model_id")
        if requested_model is not None and not isinstance(requested_model, str):
            raise ValueError("model_id must be text")
        if requested_model:
            model_route, model_id = resolve_model_id(
                requested_model, default_route=self.router.model
            )
        else:
            model_route, model_id = resolve_model_id(
                previous["model_id"], default_route=self.router.model
            )
        attempt, context = self.conversations.retry_attempt(
            user.telegram_id,
            attempt_id,
            model_id=model_id,
        )
        try:
            submitted = self.conversation_jobs.submit(
                attempt["id"],
                lambda stop_event: self._run_durable_attempt(
                    user,
                    attempt,
                    mode=previous["mode"],
                    message=previous["submitted_source"],
                    history=context,
                    model_route=model_route,
                    direction=previous["direction"],
                    stop_event=stop_event,
                ),
                conversation_id=attempt["conversation_id"],
                owner_id=user.telegram_id,
            )
            if not submitted:
                attempt = self.conversations.fail_attempt(
                    user.telegram_id, attempt["id"], error_code="concurrency_limit"
                )
        except Exception:
            attempt = self.conversations.fail_attempt(
                user.telegram_id, attempt["id"], error_code="worker_unavailable"
            )
        return {
            "conversation_id": conversation_id,
            "turn_id": attempt["turn_id"],
            "attempt": self._public_attempt(attempt),
            "mode_result": attempt["status"],
        }

    def external_chat(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> dict[str, Any]:
        """Authenticate and execute a request from a consuming website.

        The upstream 9Router credential is never accepted here.  A caller must
        present an AuriX-issued key, and the account policy is checked before
        the request is sent upstream.
        """

        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        token = _bearer_token(authorization)
        principal = self.api_keys.authenticate(token)
        if principal is None:
            raise PermissionError("AuriX API key required")

        mode = normalize_mode(body.get("mode", "english"))
        _model_route, model_id = resolve_model_id(
            body.get("model_id"), default_route=self.router.model
        )
        if not principal.allows_mode(mode):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this mode")
        if not _principal_allows_model(principal, model_id):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this model")
        user_id, conversation_id = _request_context(body)
        if not self.rate_limiter.allow(
            f"api-account:{principal.account_id}", limit=principal.requests_per_minute
        ):
            raise ExternalAPIRateLimitError("API request rate limit reached")

        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        try:
            result = self._chat_for_external(
                body,
                request_id=request_id,
                account_id=principal.account_id,
                user_id=user_id,
                conversation_id=conversation_id,
                instructions=instructions,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                top_p=top_p,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=mode,
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        except ValueError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=mode,
                model_id=model_id,
                status="failed",
                http_status=400,
                provider="9router",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        except Exception:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=mode,
                model_id=model_id,
                status="failed",
                http_status=500,
                provider="9router",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode=mode,
            model_id=result["model_id"],
            status="completed",
            http_status=200,
            provider_model=result.get("returned_model"),
            usage=result.get("usage"),
            router_request_id=result.get("upstream_request_id"),
            provider="9router",
            endpoint="/chat/completions",
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return {"request_id": request_id, **result}

    def external_chat_completions(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Serve a standard OpenAI chat request without losing multimodal parts."""

        request = _normalize_standard_chat_request(body)
        if not hasattr(self.router, "openai_chat"):
            if request["stream"]:
                raise ValueError("streaming is not supported by the configured router")
            if request["upstream_payload"].get("tools"):
                raise ValueError("tools are not supported by the configured router")
            fallback_messages = [
                item
                for item in request["upstream_payload"]["messages"]
                if item["role"] in {"user", "assistant"}
            ]
            fallback_last = next(
                (item for item in reversed(fallback_messages) if item["role"] == "user"),
                None,
            )
            if fallback_last is None or not isinstance(fallback_last.get("content"), str):
                raise ValueError("legacy router requires a final text user message")
            result = self.external_chat(
                {
                    "mode": request["mode"],
                    "model_id": request["model"],
                    "message": fallback_last["content"],
                    "history": fallback_messages[: fallback_messages.index(fallback_last)],
                    "context_summary": None,
                    "user_id": request["user_id"],
                    "conversation_id": request["conversation_id"],
                },
                authorization,
                request_id=request_id,
            )
            return _standard_completion_payload(result, request_id=request_id)
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        model_route, model_id = _resolve_standard_model(
            request["model"], default_route=self.router.model
        )
        self._authorize_external_request(principal, model_id=model_id, mode=request["mode"])
        user_id = request["user_id"]
        conversation_id = request["conversation_id"]
        try:
            result = self.router.openai_chat(
                request["upstream_payload"] | {"model": model_route, "stream": False},
                request_id=request_id,
                account_id=principal.account_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/chat/completions",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode=request["mode"],
            model_id=model_id,
            status="completed",
            http_status=200,
            provider_model=result.get("model") if isinstance(result, dict) else None,
            usage=result.get("usage") if isinstance(result, dict) else None,
            router_request_id=result.get("id") if isinstance(result, dict) else None,
            provider="9router",
            endpoint="/chat/completions",
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return _standard_completion_payload(
            result,
            model_id=model_id,
            request_id=request_id,
        )

    def external_responses(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Serve the portable OpenAI Responses request over the chat transport.

        The Responses surface is deliberately an adapter: AuriX keeps one
        authorization, routing, usage, and provider transport path rather than
        maintaining a second model implementation.
        """

        request = _normalize_responses_request(body)
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        model_route, model_id = _resolve_standard_model(
            request["model"], default_route=self.router.model
        )
        self._authorize_external_request(principal, model_id=model_id, mode=request["mode"])
        try:
            result = self.router.openai_chat(
                request["upstream_payload"] | {"model": model_route, "stream": False},
                request_id=request_id,
                account_id=principal.account_id,
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/responses",
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
            raise
        try:
            payload = _standard_response_payload(
                result,
                request=request,
                model_id=model_id,
                request_id=request_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/responses",
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode=request["mode"],
            model_id=model_id,
            status="completed",
            http_status=200,
            provider_model=result.get("model") if isinstance(result, dict) else None,
            usage=result.get("usage") if isinstance(result, dict) else None,
            router_request_id=result.get("id") if isinstance(result, dict) else None,
            provider="9router",
            endpoint="/responses",
            user_id=request["user_id"],
            conversation_id=request["conversation_id"],
        )
        return payload

    def openai_chat_stream(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> _ExternalStream:
        """Authenticate and open a bounded OpenAI-compatible SSE stream."""

        request = _normalize_standard_chat_request(body)
        if not request["stream"]:
            raise ValueError("stream must be true for the streaming route")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        model_route, model_id = _resolve_standard_model(
            request["model"], default_route=self.router.model
        )
        self._authorize_external_request(principal, model_id=model_id, mode=request["mode"])
        payload = dict(request["upstream_payload"])
        payload.update(
            {
                "model": model_route,
                "stream": True,
                "stream_options": request["stream_options"],
            }
        )
        started_at = time.monotonic()
        try:
            response = self.router.openai_chat_stream(
                payload,
                request_id=request_id,
                account_id=principal.account_id,
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/chat/completions",
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
            raise
        completion_id = request_id[4:] if request_id.startswith("req_") else request_id
        return _ExternalStream(
            response=response,
            request_id=request_id,
            principal=principal,
            model_id=model_id,
            user_id=request["user_id"],
            conversation_id=request["conversation_id"],
            public_completion_id=f"chatcmpl_{completion_id}",
            mode=request["mode"],
            started_at=started_at,
        )

    def openai_response_stream(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> _ExternalStream:
        """Open a Responses-compatible stream over the existing SSE transport."""

        request = _normalize_responses_request(body)
        if not request["stream"]:
            raise ValueError("stream must be true for the streaming route")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        model_route, model_id = _resolve_standard_model(
            request["model"], default_route=self.router.model
        )
        self._authorize_external_request(principal, model_id=model_id, mode=request["mode"])
        payload = dict(request["upstream_payload"])
        payload.update({"model": model_route, "stream": True, "stream_options": {"include_usage": True}})
        started_at = time.monotonic()
        try:
            response = self.router.openai_chat_stream(
                payload,
                request_id=request_id,
                account_id=principal.account_id,
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/responses",
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
            raise
        response_id = _responses_id(request_id)
        return _ExternalStream(
            response=response,
            request_id=request_id,
            principal=principal,
            model_id=model_id,
            user_id=request["user_id"],
            conversation_id=request["conversation_id"],
            public_completion_id=response_id,
            mode=request["mode"],
            endpoint="/responses",
            protocol="responses",
            started_at=started_at,
        )

    def record_stream_result(
        self,
        stream: _ExternalStream,
        *,
        usage: dict[str, Any] | None,
        provider_model: str | None,
        router_request_id: str | None,
        completed: bool,
        failure_http_status: int = 499,
        first_event_ms: float | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Record stream outcome; failure status describes why delivery failed.

        The public SSE HTTP status is already committed as 200 by the time the
        provider stream is consumed.  The usage event retains the mapped
        upstream failure (normally 502) or 499 for a downstream disconnect.
        """
        self.api_keys.record_usage(
            request_id=stream.request_id,
            principal=stream.principal,
            mode=stream.mode,
            model_id=stream.model_id,
            status="completed" if completed else "failed",
            http_status=200 if completed else failure_http_status,
            provider_model=provider_model,
            usage=usage,
            router_request_id=router_request_id,
            provider="9router",
            endpoint=stream.endpoint,
            user_id=stream.user_id,
            conversation_id=stream.conversation_id,
            first_event_ms=first_event_ms,
            duration_ms=duration_ms,
        )

    def _authenticate_external_request(
        self,
        authorization: str | None,
        *,
        count_request: bool = True,
    ) -> Any:
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        principal = self.api_keys.authenticate(_bearer_token(authorization))
        if principal is None:
            raise PermissionError("AuriX API key required")
        if count_request and not self.rate_limiter.allow(
            f"api-account:{principal.account_id}", limit=principal.requests_per_minute
        ):
            raise ExternalAPIRateLimitError("API request rate limit reached")
        return principal

    @staticmethod
    def _authorize_external_request(principal: Any, *, model_id: str, mode: str) -> None:
        if mode in {
            "english",
            "translate",
            "lisu_assistant",
            "image_generation",
            "video_generation",
            "embeddings",
            "audio",
        } and not principal.allows_mode(mode):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this mode")
        if not principal.allows_model(model_id):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this model")

    def external_embeddings(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(body.get("input"), (str, list)):
            raise ValueError("input must be text or an array of text")
        inputs = [body["input"]] if isinstance(body["input"], str) else body["input"]
        if not inputs or len(inputs) > 128 or any(not isinstance(item, str) or not item.strip() for item in inputs):
            raise ValueError("input must contain 1-128 non-empty text values")
        if any(len(item) > 32_000 for item in inputs):
            raise ValueError("embedding input is too long")
        model = _feature_model(body.get("model"), name="model")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        self._authorize_external_request(principal, model_id=model, mode="embeddings")
        payload = dict(body)
        payload["model"] = model
        try:
            result = self.router.request_json(
                "/embeddings",
                payload,
                request_id=request_id,
                account_id=principal.account_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="embeddings",
                model_id=model,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/embeddings",
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode="embeddings",
            model_id=model,
            status="completed",
            http_status=200,
            provider_model=result.get("model"),
            usage=result.get("usage"),
            router_request_id=result.get("id"),
            provider="9router",
            endpoint="/embeddings",
        )
        return _standard_embeddings_payload(result, model=model)

    def external_audio(
        self,
        path: str,
        body: bytes,
        content_type: str,
        authorization: str | None,
        *,
        model: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        model = _feature_model(model, name="model")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        self._authorize_external_request(principal, model_id=model, mode="audio")
        upstream_path = path[3:] if path.startswith("/v1/") else path
        try:
            result = self.router.request_raw(
                upstream_path,
                body,
                content_type=content_type,
                accept="application/json, text/plain, text/event-stream, audio/*, */*",
                max_response_bytes=MAX_AUDIO_RESPONSE_BYTES,
                request_id=request_id,
                account_id=principal.account_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="audio",
                model_id=model,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint=path,
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode="audio",
            model_id=model,
            status="completed",
            http_status=200,
            provider_model=result.get("model"),
            usage=result.get("usage"),
            provider="9router",
            endpoint=path,
        )
        return result

    def external_audio_stream(
        self,
        path: str,
        body: bytes,
        content_type: str,
        authorization: str | None,
        *,
        model: str,
        request_id: str | None = None,
    ) -> _RawExternalStream:
        """Open a streaming audio or transcription response.

        The upstream must expose a readable response object.  AuriX forwards
        bytes as they arrive and records timing when the handler completes.
        """

        model = _feature_model(model, name="model")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        self._authorize_external_request(principal, model_id=model, mode="audio")
        upstream_path = path[3:] if path.startswith("/v1/") else path
        stream_method = getattr(self.router, "request_raw_stream", None)
        if not callable(stream_method):
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="audio",
                model_id=model,
                status="failed",
                http_status=501,
                provider="9router",
                endpoint=path,
            )
            raise AIRouterError("streaming is not supported by the configured router")
        started_at = time.monotonic()
        try:
            response = stream_method(
                upstream_path,
                body,
                content_type=content_type,
                accept="text/event-stream, audio/*, application/json, */*",
                request_id=request_id,
                account_id=principal.account_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="audio",
                model_id=model,
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint=path,
            )
            raise
        return _RawExternalStream(
            response=response,
            request_id=request_id,
            principal=principal,
            model_id=model,
            endpoint=path,
            started_at=started_at,
            max_bytes=MAX_AUDIO_RESPONSE_BYTES,
        )

    def record_raw_stream_result(
        self,
        stream: _RawExternalStream,
        *,
        completed: bool,
        first_event_ms: float | None,
        duration_ms: float,
    ) -> None:
        self.api_keys.record_usage(
            request_id=stream.request_id,
            principal=stream.principal,
            mode=stream.mode,
            model_id=stream.model_id,
            status="completed" if completed else "failed",
            http_status=200 if completed else 499,
            provider="9router",
            endpoint=stream.endpoint,
            first_event_ms=first_event_ms,
            duration_ms=duration_ms,
        )

    def external_image_generation(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
        binary: bool = False,
    ) -> dict[str, Any]:
        """Serve an OpenAI-compatible image-generation request through 9Router."""

        request = _normalize_image_generation_request(body)
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        self._authorize_external_request(
            principal,
            model_id=request["model_id"],
            mode="image_generation",
        )
        user_id, conversation_id = _request_context(body)
        upstream_path = "/images/generations?response_format=binary" if binary else "/images/generations"
        try:
            if binary:
                result = self.router.request_raw(
                    upstream_path,
                    _json_bytes(request["payload"]),
                    content_type="application/json",
                    accept="image/*, application/octet-stream, */*",
                    max_response_bytes=MAX_IMAGE_RESPONSE_BYTES,
                    request_id=request_id,
                    account_id=principal.account_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            else:
                result = self.router.request_json(
                    upstream_path,
                    request["payload"],
                    request_id=request_id,
                    account_id=principal.account_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="image_generation",
                model_id=request["model_id"],
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/images/generations",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        try:
            normalized = (
                result
                if binary
                else _standard_image_payload(result, model=request["model_id"])
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="image_generation",
                model_id=request["model_id"],
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/images/generations",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode="image_generation",
            model_id=request["model_id"],
            status="completed",
            http_status=200,
            provider_model=result.get("model") if isinstance(result, dict) else None,
            usage=result.get("usage") if isinstance(result, dict) else None,
            router_request_id=result.get("id") if isinstance(result, dict) else None,
            provider="9router",
            endpoint="/images/generations",
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return normalized

    def external_video_generation(
        self,
        body: dict[str, Any],
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Serve an OpenAI-compatible video-generation submit request."""

        request = _normalize_video_generation_request(body)
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        principal = self._authenticate_external_request(authorization)
        self._authorize_external_request(
            principal,
            model_id=request["model_id"],
            mode="video_generation",
        )
        user_id, conversation_id = _request_context(body)
        try:
            result = self.router.request_json(
                "/videos",
                request["payload"],
                request_id=request_id,
                account_id=principal.account_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        except AIRouterError as exc:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="video_generation",
                model_id=request["model_id"],
                status="failed",
                http_status=_router_error_status(exc),
                provider="9router",
                endpoint="/videos",
                user_id=user_id,
                conversation_id=conversation_id,
            )
            raise
        self.api_keys.record_usage(
            request_id=request_id,
            principal=principal,
            mode="video_generation",
            model_id=request["model_id"],
            status="completed",
            http_status=200,
            provider_model=result.get("model") if isinstance(result, dict) else None,
            usage=result.get("usage") if isinstance(result, dict) else None,
            router_request_id=result.get("id") if isinstance(result, dict) else None,
            provider="9router",
            endpoint="/videos",
            user_id=user_id,
            conversation_id=conversation_id,
        )
        return result

    def external_video_metadata(
        self,
        video_id: str,
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        principal = self._authenticate_external_request(authorization)
        if not principal.allows_mode("video_generation"):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this mode")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        return self.router.request_json(
            f"/videos/{_feature_path_segment(video_id, name='video_id')}",
            {},
            request_id=request_id,
            account_id=principal.account_id,
            method="GET",
        )

    def external_video_content(
        self,
        video_id: str,
        authorization: str | None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        principal = self._authenticate_external_request(authorization)
        if not principal.allows_mode("video_generation"):
            raise ExternalAPIAccessDeniedError("API key is not enabled for this mode")
        request_id = request_id or f"req_{secrets.token_urlsafe(12)}"
        return self.router.request_raw(
            f"/videos/{_feature_path_segment(video_id, name='video_id')}/content",
            b"",
            content_type="application/octet-stream",
            accept="video/mp4, application/octet-stream, */*",
            max_response_bytes=MAX_VIDEO_RESPONSE_BYTES,
            request_id=request_id,
            account_id=principal.account_id,
            method="GET",
        )

    def external_models(
        self,
        authorization: str | None,
        *,
        category: str | None = None,
        live: bool = True,
    ) -> dict[str, Any]:
        """Return a policy-filtered model catalog for an external site.

        Direct callers retain the historical live-discovery behavior. The HTTP
        endpoint uses the bounded curated chat view by default so a partner's
        first model-picker request never waits for every 9Router media catalog.
        Media and full live discovery remain available explicitly through
        ``?category=...`` or ``?view=live``.
        """

        principal = self._authenticate_external_request(authorization, count_request=False)
        normalized_category = (str(category).strip().lower() if category else None) or None
        if normalized_category in {"all", "*"}:
            normalized_category = None
            live = True

        if not live and normalized_category in {None, "chat", "responses"}:
            return self._curated_external_chat_models(principal)

        output_by_id: dict[str, dict[str, Any]] = {}
        categories = (
            (None, {"chat", "responses", "streaming"}),
            ("embedding", {"embeddings"}),
            ("stt", {"audio_input"}),
            ("tts", {"audio_output"}),
            ("image", {"image_generation"}),
            ("video", {"video_generation"}),
        )
        if normalized_category not in {None, "chat", "responses"}:
            category_aliases = {
                "embeddings": "embedding",
                "audio_input": "stt",
                "audio_output": "tts",
                "image_generation": "image",
                "video_generation": "video",
            }
            selected = category_aliases.get(normalized_category, normalized_category)
            categories = tuple(item for item in categories if item[0] == selected)
            if not categories:
                raise ValueError("category must be chat, embedding, stt, tts, image, or video")
        for category, capabilities in categories:
            category_mode = {
                "embedding": "embeddings",
                "stt": "audio",
                "tts": "audio",
                "image": "image_generation",
                "video": "video_generation",
            }.get(category)
            if category is None and not any(
                principal.allows_mode(mode)
                for mode in ("english", "translate", "lisu_assistant")
            ):
                continue
            if category_mode and not principal.allows_mode(category_mode):
                continue
            try:
                models = self._discovered_models(category)
            except AIRouterError:
                continue
            for item in models:
                model_id = item["id"]
                if not _principal_allows_model(principal, model_id):
                    continue
                profile = model_profile(
                    model_id,
                    capabilities=capabilities,
                    owned_by=str(item.get("owned_by") or "9router"),
                )
                existing = output_by_id.get(model_id)
                if existing is None:
                    output_by_id[model_id] = profile
                else:
                    existing["capabilities"] = sorted(
                        set(existing.get("capabilities", []))
                        | set(profile.get("capabilities", []))
                    )
        return {
            "object": "list",
            "data": list(output_by_id.values()),
            "aurix": {
                "catalog": "live",
                "category": normalized_category or "all",
                "cache_ttl_seconds": self.model_catalog_ttl_seconds,
                "curated_path": "/v1/models",
                "live_path": "/v1/models?view=live",
            },
        }

    def _curated_external_chat_models(self, principal: Any) -> dict[str, Any]:
        """Return the small platform-owned chat catalog without upstream I/O."""

        data: list[dict[str, Any]] = []
        if any(
            principal.allows_mode(mode)
            for mode in ("english", "translate", "lisu_assistant")
        ):
            for item in MODEL_CATALOG.values():
                route = str(item["route"])
                if not _principal_allows_model(principal, route):
                    continue
                profile = model_profile(
                    route,
                    capabilities=("chat", "responses", "streaming"),
                    owned_by="9router",
                )
                profile["aurix"]["catalog_source"] = "curated"
                data.append(profile)

        return {
            "object": "list",
            "data": data,
            "aurix": {
                "catalog": "curated",
                "category": "chat",
                "cache_ttl_seconds": self.model_catalog_ttl_seconds,
                "live_path": "/v1/models?view=live",
                "category_path": "/v1/models?category=image",
            },
        }

    def external_integration_profile(self, authorization: str | None) -> dict[str, Any]:
        """Return a safe machine-readable contract for a consuming backend."""

        principal = self._authenticate_external_request(authorization, count_request=False)
        return {
            "object": "aurix.integration_profile",
            "schema": "aurix.external.v1",
            "authentication": {
                "type": "http_bearer",
                "header": "Authorization",
                "key_info_path": "/api/v1/key-info",
            },
            "model_discovery": {
                "path": "/v1/models",
                "cache_ttl_seconds": self.model_catalog_ttl_seconds,
                "policy_filtered": True,
                "default_catalog": "curated_chat",
                "live_path": "/v1/models?view=live",
                "category_path_template": "/v1/models?category={category}",
                "categories": ["chat", "embedding", "stt", "tts", "image", "video"],
            },
            "effective_policy": {
                "allowed_modes": sorted(str(mode) for mode in principal.allowed_modes),
                "allowed_models": sorted(str(model) for model in principal.allowed_models),
                "requests_per_minute": principal.requests_per_minute,
            },
            "endpoints": {
                "chat_completions": {
                    "path": "/v1/chat/completions",
                    "stream": True,
                    "content_type": "text/event-stream",
                    "terminal": "data: [DONE]",
                },
                "responses": {
                    "path": "/v1/responses",
                    "stream": True,
                    "content_type": "text/event-stream",
                    "terminal_event": "response.completed",
                },
                "built_in_chat": {"path": "/v1/chat", "stream": False},
                "embeddings": {"path": "/v1/embeddings", "stream": False},
                "image_generation": {"path": "/v1/images/generations", "stream": False},
                "audio": {
                    "paths": [
                        "/v1/audio/transcriptions",
                        "/v1/audio/translations",
                        "/v1/audio/speech",
                    ],
                    "stream": True,
                },
            },
            "limits": {
                "max_json_bytes": MAX_JSON_BYTES,
                "max_messages": MAX_STANDARD_MESSAGES,
                "max_tools": MAX_STANDARD_TOOLS,
                "max_tool_bytes": MAX_STANDARD_TOOL_BYTES,
                "max_audio_request_bytes": MAX_AUDIO_REQUEST_BYTES,
            },
            "attribution": {
                "fields": ["user", "user_id", "conversation_id", "metadata.user_id"],
                "authentication_note": "Attribution fields do not authenticate users.",
            },
            "attachments": {
                "chat_image_parts": "gateway-accepted; provider-model support must be verified",
                "pdf_docx": "consumer must extract or transform content before sending",
            },
        }

    def external_key_info(self, authorization: str | None) -> dict[str, Any]:
        """Expose the authenticated key's effective, non-secret policy."""

        principal = self._authenticate_external_request(authorization, count_request=False)
        info = self.api_keys.key_info(principal.key_id)
        if info is None:
            raise PermissionError("AuriX API key is no longer available")
        return {
            "object": "aurix.key_info",
            "account": {
                "id": info["account_id"],
                "name": info["account_name"],
                "owner_type": info["owner_type"],
                "owner_id": info["owner_id"],
            },
            "key": {
                "id": info["key_id"],
                "label": info["label"],
                "token_prefix": info["token_prefix"],
                "status": info["status"],
                "created_at": info["created_at"],
                "expires_at": info["expires_at"],
                "last_used_at": info["last_used_at"],
            },
            "policy": {
                "allowed_modes": info["allowed_modes"],
                "allowed_models": info["allowed_models"],
                "requests_per_minute": info["requests_per_minute"],
                "policy_revision": info.get("updated_at"),
            },
        }

    def _chat_for_external(
        self,
        body: dict[str, Any],
        *,
        request_id: str,
        account_id: str,
        user_id: str | None = None,
        conversation_id: str | None = None,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> dict[str, Any]:
        mode = body.get("mode", "english")
        message = body.get("message")
        history = body.get("history")
        model_route, model_id = resolve_model_id(
            body.get("model_id"), default_route=self.router.model
        )
        result = self.router.chat(
            mode=mode,
            message=message,
            history=history if history is not None else [],
            model=model_route,
            request_id=request_id,
            account_id=account_id,
            user_id=user_id,
            conversation_id=conversation_id,
            summary=body.get("context_summary"),
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        return _chat_payload(result, model_id=model_id)

    def authenticate_admin(
        self,
        authorization: str | None,
        cookie_header: str | None = None,
    ) -> VerifiedTelegramUser | None:
        token = _bearer_token(authorization)
        if token is not None and self.admin_token and hmac.compare_digest(token, self.admin_token):
            return None
        cookie = SimpleCookie()
        if cookie_header:
            cookie.load(cookie_header)
        session = cookie.get(SESSION_COOKIE_NAME)
        user = self.sessions.get(session.value if session else None)
        # The browser console is available to every verified Telegram user.
        # Keep the bearer admin token for non-browser automation; external API
        # keys remain separately scoped by APIKeyStore.
        if user is not None:
            return user
        if not self.admin_token and not self.admin_telegram_ids:
            raise ExternalAPIUnavailableError("Admin API is not configured")
        raise PermissionError("Admin authentication required")

    def admin_usage_report(
        self,
        *,
        account_id: str | None,
        start_at: str,
        end_at: str,
        limit: int,
        offset: int = 0,
        key_id: str | None = None,
        model_id: str | None = None,
        endpoint: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        accounts = self.api_keys.list_accounts(owner_id=owner_id)
        summaries = self.api_keys.usage_summary(
            account_id=account_id,
            start_at=start_at,
            end_at=end_at,
            key_id=key_id,
            model_id=model_id,
            endpoint=endpoint,
            status=status,
            user_id=user_id,
            owner_id=owner_id,
        )
        event_page = self.api_keys.usage_event_page(
            account_id=account_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
            offset=offset,
            key_id=key_id,
            model_id=model_id,
            endpoint=endpoint,
            status=status,
            user_id=user_id,
            owner_id=owner_id,
        )
        summary_by_id = {item["account_id"]: item for item in summaries}
        keys_by_account: dict[str, list[dict[str, Any]]] = {}
        for key in self.api_keys.list_keys(account_id):
            keys_by_account.setdefault(str(key["account_id"]), []).append(
                {
                    "id": str(key["id"]),
                    "label": str(key["label"]),
                    "token_prefix": str(key["token_prefix"]),
                    "status": str(key["status"]),
                    "created_at": str(key["created_at"]),
                    "expires_at": key.get("expires_at"),
                    "revoked_at": key.get("revoked_at"),
                    "last_used_at": key.get("last_used_at"),
                }
            )
        for account in accounts:
            account.update(summary_by_id.get(account["id"], {
                "requests": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "usage_reported_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
            }))
            account["keys"] = keys_by_account.get(str(account["id"]), [])
        return {
            "period": {"start_at": start_at, "end_at": end_at},
            "accounts": accounts if account_id is None else [
                account for account in accounts if account["id"] == account_id
            ],
            "requests": event_page["items"],
            "pagination": {
                key: event_page[key]
                for key in ("offset", "limit", "has_more", "next_offset")
            },
            "filters": {
                key: value
                for key, value in {
                    "account_id": account_id,
                    "key_id": key_id,
                    "model_id": model_id,
                    "endpoint": endpoint,
                    "status": status,
                    "user_id": user_id,
                }.items()
                if value
            },
        }

    def admin_usage_analytics(
        self,
        *,
        account_id: str | None,
        start_at: str,
        end_at: str,
        key_id: str | None = None,
        model_id: str | None = None,
        endpoint: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Return privacy-safe aggregate usage data for the admin dashboard."""

        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        return {
            "period": {"start_at": start_at, "end_at": end_at},
            **self.api_keys.usage_breakdown(
                account_id=account_id,
                start_at=start_at,
                end_at=end_at,
                key_id=key_id,
                model_id=model_id,
                endpoint=endpoint,
                status=status,
                user_id=user_id,
                owner_id=owner_id,
            ),
            "filters": {
                key: value
                for key, value in {
                    "account_id": account_id,
                    "key_id": key_id,
                    "model_id": model_id,
                    "endpoint": endpoint,
                    "status": status,
                    "user_id": user_id,
                }.items()
                if value
            },
        }

    @staticmethod
    def _admin_audit_context(
        actor: VerifiedTelegramUser | None,
        *,
        action: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "actor_type": "telegram_user" if actor is not None else "operator_token",
            "actor_id": str(actor.telegram_id) if actor is not None else None,
            "request_id": request_id,
        }

    def admin_owner_scope(self, actor: VerifiedTelegramUser | None) -> str | None:
        """Return the customer scope; platform/operator sessions see all."""

        if (
            actor is None
            or actor.telegram_id in self.admin_telegram_ids
            or actor.telegram_id in self.operator_telegram_ids
        ):
            return None
        return str(actor.telegram_id)

    def admin_access_context(self, actor: VerifiedTelegramUser | None) -> dict[str, Any]:
        """Describe the authenticated console role without exposing credentials."""

        if actor is None:
            role = "operator_token"
            scope = "all_accounts"
        elif actor.telegram_id in self.admin_telegram_ids:
            role = "platform_owner"
            scope = "all_accounts"
        elif actor.telegram_id in self.operator_telegram_ids:
            role = "operator"
            scope = "all_accounts"
        else:
            role = "account_owner"
            scope = f"owner:{actor.telegram_id}"
        return {
            "object": "aurix.admin_access",
            "role": role,
            "scope": scope,
            "can_manage_keys": True,
            "can_view_usage": True,
            "can_view_capabilities": True,
        }

    def _assert_customer_account_access(
        self,
        actor: VerifiedTelegramUser | None,
        account_id: str,
    ) -> None:
        owner_id = self.admin_owner_scope(actor)
        if owner_id is None or self.api_keys is None:
            return
        account = next(
            (item for item in self.api_keys.list_accounts(owner_id=owner_id) if item["id"] == account_id),
            None,
        )
        if account is None:
            raise PermissionError("account is outside the authenticated user's scope")

    def _effective_account_policy(
        self,
        body: dict[str, Any],
        *,
        current: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[str], int]:
        current_modes = current.get("allowed_modes") if current else None
        current_models = current.get("allowed_models") if current else None
        modes = _policy_scope_values(
            body.get("allowed_modes"),
            name="allowed_modes",
            ceiling=self.operator_allowed_modes,
            default=current_modes or self.operator_allowed_modes,
        )
        models = _policy_scope_values(
            body.get("allowed_models"),
            name="allowed_models",
            ceiling=self.operator_allowed_models,
            default=current_models or self.operator_allowed_models,
        )
        requested_rpm = body.get(
            "requests_per_minute",
            current.get("requests_per_minute", 60) if current else 60,
        )
        rpm = _admin_requests_per_minute(requested_rpm)
        if rpm > self.operator_max_requests_per_minute:
            raise ExternalAPIAccessDeniedError(
                "requests_per_minute exceeds the operator policy"
            )
        return modes, models, rpm

    def admin_update_account(
        self,
        account_id: str,
        body: dict[str, Any],
        authorization: str | None,
        cookie_header: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        actor = self.authenticate_admin(authorization, cookie_header)
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        clean_account_id = _text(account_id, name="account_id", maximum=80)
        self._assert_customer_account_access(actor, clean_account_id)
        accounts = self.api_keys.list_accounts()
        current = next(
            (item for item in accounts if item["id"] == clean_account_id), None
        )
        if current is None:
            raise APIKeyStoreError("active account not found")
        name = body.get("name")
        if name is not None:
            name = _text(name, name="name", maximum=160)
        modes, models, rpm = self._effective_account_policy(body, current=current)
        account = self.api_keys.update_account(
            clean_account_id,
            name=name,
            allowed_modes=modes,
            allowed_models=models,
            requests_per_minute=rpm,
        )
        self.api_keys.record_audit_event(
            action="account.policy.update",
            actor_type="telegram_user" if actor is not None else "operator_token",
            actor_id=str(actor.telegram_id) if actor is not None else None,
            target_type="account",
            target_id=clean_account_id,
            outcome="success",
            request_id=request_id,
            metadata={
                "allowed_modes": modes,
                "allowed_models": models,
                "requests_per_minute": rpm,
            },
        )
        return {"account": account}

    def admin_create_account(
        self,
        body: dict[str, Any],
        authorization: str | None,
        cookie_header: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an all-capability external account and issue its first key."""

        actor = self.authenticate_admin(authorization, cookie_header)
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        name = _text(body.get("name"), name="name", maximum=160)
        allowed_modes, allowed_models, requests_per_minute = self._effective_account_policy(body)
        label = _text(body.get("key_label", "production"), name="key_label", maximum=160)
        expires_at = _expires_at(body.get("expires_in_days", 90))
        owner_type = "telegram_admin" if actor is not None else "operator"
        owner_id = str(actor.telegram_id) if actor is not None else None
        account = self.api_keys.create_account(
            name,
            allowed_modes=allowed_modes,
            allowed_models=allowed_models,
            requests_per_minute=requests_per_minute,
            owner_type=owner_type,
            owner_id=owner_id,
            audit_context=self._admin_audit_context(
                actor, action="account.create", request_id=request_id
            ),
        )
        try:
            issued = self.api_keys.issue_key(
                account["id"],
                label=label,
                expires_at=expires_at,
                audit_context=self._admin_audit_context(
                    actor, action="key.issue", request_id=request_id
                ),
            )
        except Exception:
            self.api_keys.revoke_account(account["id"])
            self.api_keys.record_audit_event(
                action="key.issue",
                actor_type="telegram_user" if actor is not None else "operator_token",
                actor_id=str(actor.telegram_id) if actor is not None else None,
                target_type="account",
                target_id=account["id"],
                outcome="failure",
                request_id=request_id,
                metadata={"reason": "initial_key_issue_failed"},
            )
            raise
        return {
            "account": account,
            "key": _issued_key_payload(issued),
        }

    def admin_issue_key(
        self,
        body: dict[str, Any],
        authorization: str | None,
        cookie_header: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        actor = self.authenticate_admin(authorization, cookie_header)
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        account_id = _text(body.get("account_id"), name="account_id", maximum=80)
        self._assert_customer_account_access(actor, account_id)
        label = _text(body.get("label", "rotation"), name="label", maximum=160)
        expires_at = _expires_at(body.get("expires_in_days", 90))
        try:
            issued = self.api_keys.issue_key(
                account_id,
                label=label,
                expires_at=expires_at,
                audit_context=self._admin_audit_context(
                    actor, action="key.issue", request_id=request_id
                ),
            )
        except Exception:
            self.api_keys.record_audit_event(
                action="key.issue",
                actor_type="telegram_user" if actor is not None else "operator_token",
                actor_id=str(actor.telegram_id) if actor is not None else None,
                target_type="account",
                target_id=account_id,
                outcome="failure",
                request_id=request_id,
                metadata={"label": label},
            )
            raise
        return {"key": _issued_key_payload(issued)}

    def admin_revoke_key(
        self,
        key_id: str,
        authorization: str | None,
        cookie_header: str | None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        actor = self.authenticate_admin(authorization, cookie_header)
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        clean_key_id = _text(key_id, name="key_id", maximum=80)
        key_info = self.api_keys.key_info(clean_key_id)
        if key_info is None:
            raise APIKeyStoreError("active API key not found")
        self._assert_customer_account_access(actor, str(key_info["account_id"]))
        revoked = self.api_keys.revoke_key(
            clean_key_id,
            audit_context=self._admin_audit_context(
                actor, action="key.revoke", request_id=request_id
            ),
        )
        if not revoked:
            self.api_keys.record_audit_event(
                action="key.revoke",
                actor_type="telegram_user" if actor is not None else "operator_token",
                actor_id=str(actor.telegram_id) if actor is not None else None,
                target_type="key",
                target_id=clean_key_id,
                outcome="failure",
                request_id=request_id,
                metadata={"reason": "active_key_not_found"},
            )
            raise APIKeyStoreError("active API key not found")
        return {"revoked": True, "key_id": clean_key_id}

    def admin_9router_usage_export(
        self,
        *,
        account_id: str | None,
        start_at: str,
        end_at: str,
        limit: int,
        key_id: str | None = None,
        model_id: str | None = None,
        endpoint: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        return {
            "source": "aurix",
            "format": "9router.usageHistory.v1",
            "period": {"start_at": start_at, "end_at": end_at},
            "events": self.api_keys.usage_9router_events(
                account_id=account_id,
                start_at=start_at,
                end_at=end_at,
                limit=limit,
                key_id=key_id,
                model_id=model_id,
                endpoint=endpoint,
                status=status,
                user_id=user_id,
                owner_id=owner_id,
            ),
        }

    def admin_capability_report(self) -> dict[str, Any]:
        """Return a live, credential-free capability and quota report."""

        return build_capability_report(self.router)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _router_error_status(error: AIRouterError) -> int:
    """Return the safe HTTP status to persist for an upstream failure."""

    status = getattr(error, "public_status", 502)
    return status if status in {429, 502, 503, 504} else 502


def _model_scope_candidates(model_id: str) -> tuple[str, ...]:
    """Accept both the stable AuriX model ID and its raw provider route."""

    candidates = [str(model_id)]
    canonical_id = model_id_for_route(str(model_id))
    if canonical_id:
        candidates.append(canonical_id)
    catalog_item = MODEL_CATALOG.get(str(model_id))
    if catalog_item and catalog_item.get("route"):
        candidates.append(str(catalog_item["route"]))
    return tuple(dict.fromkeys(candidates))


def _principal_allows_model(principal: Any, model_id: str) -> bool:
    return any(
        principal.allows_model(candidate)
        for candidate in _model_scope_candidates(model_id)
    )


def _admin_requests_per_minute(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("requests_per_minute must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("requests_per_minute must be an integer") from exc
    if not 1 <= result <= 600:
        raise ValueError("requests_per_minute must be between 1 and 600")
    return result


def _policy_scope_values(
    value: Any,
    *,
    name: str,
    ceiling: frozenset[str],
    default: Iterable[str],
) -> list[str]:
    """Normalize a requested account policy and enforce the operator ceiling."""

    if value is None:
        requested = {str(item).strip() for item in default if str(item).strip()}
    elif isinstance(value, str):
        requested = {item.strip() for item in value.split(",") if item.strip()}
    elif isinstance(value, (list, tuple, set, frozenset)):
        requested = {str(item).strip() for item in value if str(item).strip()}
    else:
        raise ValueError(f"{name} must be a list of strings or comma-separated text")
    if not requested:
        raise ValueError(f"{name} must not be empty")
    if "*" in requested:
        requested = set(ceiling) if "*" not in ceiling else {"*"}
    if "*" not in ceiling and not requested.issubset(ceiling):
        disallowed = ", ".join(sorted(requested - set(ceiling)))
        raise ExternalAPIAccessDeniedError(
            f"{name} exceeds the operator policy: {disallowed}"
        )
    return sorted(requested)


def _expires_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("expires_in_days must be a positive integer or null")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_in_days must be a positive integer or null") from exc
    if not 1 <= days <= 3_650:
        raise ValueError("expires_in_days must be between 1 and 3650")
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _issued_key_payload(issued: Any) -> dict[str, Any]:
    return {
        "account_id": issued.account_id,
        "key_id": issued.key_id,
        "label": issued.label,
        "token": issued.token,
        "token_prefix": issued.token_prefix,
        "expires_at": issued.expires_at,
        "one_time": True,
    }


def _standard_message_text(value: Any, *, index: int) -> str:
    value = _standard_message_content(value, index=index, allow_images=False)
    if isinstance(value, str):
        return value
    return "".join(part["text"] for part in value if part.get("type") == "text").strip()


def _standard_image_url(value: Any, *, index: int, part_index: int) -> dict[str, Any]:
    if isinstance(value, str):
        url = value.strip()
        detail = None
    elif isinstance(value, dict):
        url = value.get("url")
        detail = value.get("detail")
        if detail is not None and detail not in {"auto", "low", "high"}:
            raise ValueError(f"messages[{index}].content[{part_index}].image_url.detail is invalid")
    else:
        raise ValueError(f"messages[{index}].content[{part_index}].image_url is invalid")
    if not isinstance(url, str) or not url or len(url) > MAX_IMAGE_URL_CHARS:
        raise ValueError(f"messages[{index}].content[{part_index}].image_url.url is invalid")
    parsed = urlsplit(url)
    if url.startswith("data:"):
        if not url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,", "data:image/gif;base64,")):
            raise ValueError("only base64 PNG, JPEG, WebP, or GIF data images are supported")
    elif parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("image URLs must use HTTPS or an approved data URL")
    else:
        hostname = parsed.hostname.casefold()
        if hostname in {"localhost", "localhost.localdomain"}:
            raise ValueError("private image hosts are not allowed")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("private image hosts are not allowed")
        if parsed.username or parsed.password:
            raise ValueError("image URLs must not contain credentials")
    result: dict[str, Any] = {"url": url}
    if detail is not None:
        result["detail"] = detail
    return result


def _standard_message_content(
    value: Any,
    *,
    index: int,
    allow_images: bool = True,
) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        result = value
        if not result.strip():
            raise ValueError(f"messages[{index}].content is required text")
        if len(result) > MAX_MESSAGE_CHARS:
            raise ValueError(f"messages[{index}].content is too long")
        return result
    if not isinstance(value, list) or not value:
        raise ValueError(f"messages[{index}].content is required")
    parts: list[dict[str, Any]] = []
    for part_index, part in enumerate(value):
        if not isinstance(part, dict):
            raise ValueError(f"messages[{index}].content[{part_index}] is invalid")
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS:
                raise ValueError(f"messages[{index}].content[{part_index}].text is invalid")
            parts.append({"type": "text", "text": text})
        elif part_type == "image_url" and allow_images:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": _standard_image_url(
                        part.get("image_url"), index=index, part_index=part_index
                    ),
                }
            )
        else:
            raise ValueError(f"messages[{index}].content part type is unsupported")
    if not any(part.get("type") == "text" or part.get("type") == "image_url" for part in parts):
        raise ValueError(f"messages[{index}].content is empty")
    if not any(part.get("type") == "text" for part in parts) and not allow_images:
        raise ValueError(f"messages[{index}].content supports text only")
    return parts


def _standard_optional_number(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _standard_max_tokens(body: dict[str, Any]) -> int | None:
    values = [
        body[name]
        for name in ("max_tokens", "max_completion_tokens")
        if name in body and body[name] is not None
    ]
    if len(values) > 1 and values[0] != values[1]:
        raise ValueError("max_tokens and max_completion_tokens must match")
    if not values:
        return None
    value = values[0]
    if isinstance(value, bool):
        raise ValueError("max_tokens must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_tokens must be a positive integer") from exc
    if result < 1:
        raise ValueError("max_tokens must be a positive integer")
    return result


def _feature_model(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    result = value.strip()
    if len(result) > 200 or any(ord(char) < 32 for char in result):
        raise ValueError(f"{name} is invalid")
    return result


def _feature_path_segment(value: Any, *, name: str) -> str:
    result = _feature_model(value, name=name)
    if any(char in result for char in "/?#\\"):
        raise ValueError(f"{name} is invalid")
    return result


def _resolve_standard_model(value: Any, *, default_route: str) -> tuple[str, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        route = str(default_route or "").strip()
        model_id = model_id_for_route(route)
        if not model_id:
            raise ValueError("configured default model is not available")
        return route, model_id
    requested = _feature_model(value, name="model")
    catalog_item = MODEL_CATALOG.get(requested)
    if catalog_item is not None:
        return catalog_item["route"], requested
    route_model_id = model_id_for_route(requested)
    return requested, route_model_id or requested


def _normalize_image_generation_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the portable image subset and preserve supported 9Router fields."""

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    if len(prompt) > 12_000:
        raise ValueError("prompt is too long")

    requested_model = _feature_model(body.get("model"), name="model")
    model_route, model_id = _resolve_standard_model(
        requested_model,
        default_route=requested_model,
    )
    payload: dict[str, Any] = {"model": model_route, "prompt": prompt}

    count = body.get("n")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
            raise ValueError("n must be an integer from 1 to 4")
        payload["n"] = count

    string_fields = {
        "size": 64,
        "quality": 64,
        "style": 64,
        "response_format": 32,
        "output_format": 32,
        "background": 32,
        "aspect_ratio": 32,
        "image_detail": 32,
        "user": 160,
    }
    for field, maximum in string_fields.items():
        value = body.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{field} is invalid")
        if field == "response_format" and value not in {"url", "b64_json"}:
            raise ValueError("response_format must be url or b64_json")
        payload[field] = value.strip()

    for field in ("image", "negative_prompt"):
        value = body.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_IMAGE_URL_CHARS:
            raise ValueError(f"{field} is invalid")
        payload[field] = value

    images = body.get("images")
    if images is not None:
        if (
            not isinstance(images, list)
            or not 1 <= len(images) <= 4
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > MAX_IMAGE_URL_CHARS
                for item in images
            )
        ):
            raise ValueError("images must contain 1-4 valid image values")
        payload["images"] = images

    metadata = body.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 16_384:
            raise ValueError("metadata is invalid")
        payload["metadata"] = metadata

    return {"payload": payload, "model_id": model_id}


def _normalize_video_generation_request(body: dict[str, Any]) -> dict[str, Any]:
    """Validate a portable OpenAI-style video submit request."""

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    if len(prompt) > 12_000:
        raise ValueError("prompt is too long")

    requested_model = _feature_model(body.get("model"), name="model")
    model_route, model_id = _resolve_standard_model(
        requested_model,
        default_route=requested_model,
    )
    payload: dict[str, Any] = {"model": model_route, "prompt": prompt.strip()}

    seconds = body.get("seconds")
    if seconds is not None:
        seconds_text = str(seconds).strip()
        if seconds_text not in {"4", "8", "12"}:
            raise ValueError("seconds must be 4, 8, or 12")
        payload["seconds"] = seconds_text

    size = body.get("size")
    if size is not None:
        if not isinstance(size, str) or size not in {
            "720x1280",
            "1280x720",
            "1024x1792",
            "1792x1024",
        }:
            raise ValueError("size is invalid")
        payload["size"] = size

    input_reference = body.get("input_reference")
    if input_reference is not None:
        if (
            not isinstance(input_reference, str)
            or not input_reference.strip()
            or len(input_reference) > MAX_IMAGE_URL_CHARS
        ):
            raise ValueError("input_reference is invalid")
        payload["input_reference"] = input_reference

    metadata = body.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 16_384:
            raise ValueError("metadata is invalid")
        payload["metadata"] = metadata

    user = body.get("user")
    if user is not None:
        if not isinstance(user, str) or not user.strip() or len(user) > 160:
            raise ValueError("user is invalid")
        payload["user"] = user.strip()

    return {"payload": payload, "model_id": model_id}


def _standard_tools(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_STANDARD_TOOLS:
        raise ValueError(f"tools must contain at most {MAX_STANDARD_TOOLS} items")
    for index, tool in enumerate(value):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError(f"tools[{index}] must be a function tool")
        function = tool.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError(f"tools[{index}].function.name is required")
        if not 1 <= len(function["name"]) <= 128:
            raise ValueError(f"tools[{index}].function.name is invalid")
        if "parameters" in function and not isinstance(function["parameters"], dict):
            raise ValueError(f"tools[{index}].function.parameters must be an object")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_STANDARD_TOOL_BYTES:
        raise ValueError("tools are too large")
    return value


def _responses_content(value: Any, *, index: int) -> str | list[dict[str, Any]]:
    """Translate the portable Responses content blocks to Chat content blocks."""

    if isinstance(value, str):
        if not value.strip() or len(value) > MAX_MESSAGE_CHARS:
            raise ValueError(f"input[{index}].content is invalid")
        return value
    if not isinstance(value, list) or not value:
        raise ValueError(f"input[{index}].content is required")
    parts: list[dict[str, Any]] = []
    for part_index, part in enumerate(value):
        if not isinstance(part, dict):
            raise ValueError(f"input[{index}].content[{part_index}] is invalid")
        part_type = part.get("type")
        if part_type in {"input_text", "text"}:
            text = part.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                raise ValueError(f"input[{index}].content[{part_index}].text is invalid")
            parts.append({"type": "text", "text": text})
        elif part_type in {"input_image", "image_url"}:
            image_value = part.get("image_url")
            if image_value is None:
                raise ValueError(
                    f"input[{index}].content[{part_index}].image_url is required"
                )
            parts.append(
                {
                    "type": "image_url",
                    "image_url": _standard_image_url(
                        image_value, index=index, part_index=part_index
                    ),
                }
            )
        else:
            raise ValueError(
                f"input[{index}].content[{part_index}].type is unsupported; "
                "use input_text or input_image"
            )
    return parts


def _responses_input_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list) or not value:
        raise ValueError("input must be text or a non-empty list")
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"input[{index}] is invalid")
        item_type = item.get("type", "message")
        if item_type == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant", "tool"}:
                raise ValueError(
                    f"input[{index}].role must be system, developer, user, assistant, or tool"
                )
            message: dict[str, Any] = {
                "role": role,
                "content": _responses_content(item.get("content"), index=index),
            }
            if role == "assistant" and item.get("tool_calls") is not None:
                message["tool_calls"] = item["tool_calls"]
            if role == "tool":
                call_id = item.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError(f"input[{index}].call_id is required for tool output")
                message["tool_call_id"] = call_id
            messages.append(message)
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError(f"input[{index}].call_id is required")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            messages.append(
                {
                    "role": "tool",
                    "content": output or "(empty tool output)",
                    "tool_call_id": call_id,
                }
            )
        elif item_type == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"input[{index}].name is required")
            arguments = item.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            call_id = item.get("call_id") or item.get("id") or f"call_{index}"
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError(f"input[{index}].call_id is invalid")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name.strip(),
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
        else:
            raise ValueError(
                f"input[{index}].type is unsupported; use message or function_call_output"
            )
    return messages


def _responses_tools(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("tools must be a list")
    converted: list[dict[str, Any]] = []
    for index, tool in enumerate(value):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError(
                f"tools[{index}] is unsupported; only type=function is available"
            )
        if isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"tools[{index}].name is required")
        function: dict[str, Any] = {"name": name.strip()}
        for field in ("description", "parameters", "strict"):
            if field in tool:
                function[field] = tool[field]
        converted.append({"type": "function", "function": function})
    return _standard_tools(converted)


def _responses_tool_choice(value: Any) -> Any:
    if value is None or isinstance(value, str):
        if value is not None and value not in {"none", "auto", "required"}:
            raise ValueError("tool_choice is invalid")
        return value
    if not isinstance(value, dict):
        raise ValueError("tool_choice must be text or an object")
    if value.get("type") != "function" or not isinstance(value.get("name"), str):
        raise ValueError("tool_choice function object is invalid")
    return {"type": "function", "function": {"name": value["name"]}}


def _responses_max_output_tokens(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_output_tokens must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_output_tokens must be a positive integer") from exc
    if result < 1:
        raise ValueError("max_output_tokens must be a positive integer")
    return result


def _normalize_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize the 80/20 Responses subset to the existing Chat contract."""

    if body.get("previous_response_id") is not None or body.get("conversation") is not None:
        raise ValueError(
            "previous_response_id and conversation state are not supported; send the full input"
        )
    if body.get("background"):
        raise ValueError("background Responses are not supported")
    if body.get("store") not in (None, False):
        raise ValueError("store=true is not supported; AuriX Responses are stateless")

    instructions = body.get("instructions")
    if instructions is not None:
        instructions = _text(instructions, name="instructions", maximum=MAX_CONTEXT_SUMMARY_CHARS)
    messages = _responses_input_messages(body.get("input"))
    if instructions is not None:
        messages.insert(0, {"role": "system", "content": instructions})

    tools = _responses_tools(body.get("tools"))
    tool_choice = _responses_tool_choice(body.get("tool_choice"))
    max_output_tokens = _responses_max_output_tokens(body.get("max_output_tokens"))
    if body.get("max_tokens") is not None:
        raise ValueError("use max_output_tokens with the Responses endpoint")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be a boolean")
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    metadata = metadata if isinstance(metadata, dict) else {}
    text_config = body.get("text")
    if text_config is not None:
        if not isinstance(text_config, dict):
            raise ValueError("text must be an object")
        output_format = text_config.get("format")
        if output_format is not None and (
            not isinstance(output_format, dict)
            or output_format.get("type", "text") != "text"
        ):
            raise ValueError("structured Responses output is not supported yet")

    standard_body: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": stream,
        "aurix_mode": body.get("aurix_mode", body.get("mode", "english")),
        "user": body.get("user"),
        "metadata": metadata,
        "conversation_id": body.get("conversation_id"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "tools": tools,
        "tool_choice": tool_choice,
        "parallel_tool_calls": body.get("parallel_tool_calls"),
        "max_tokens": max_output_tokens,
    }
    normalized = _normalize_standard_chat_request(standard_body)
    normalized.update(
        {
            "instructions": instructions,
            "metadata": metadata,
            "store": False,
        }
    )
    return normalized


def _normalize_standard_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if len(messages) > MAX_STANDARD_MESSAGES:
        raise ValueError(f"messages must contain at most {MAX_STANDARD_MESSAGES} items")

    clean_messages: list[dict[str, Any]] = []
    conversation: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise ValueError(f"messages[{index}] is invalid")
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(
                f"messages[{index}].role must be system, developer, user, assistant, or tool"
            )
        raw_content = item.get("content")
        if role == "assistant" and raw_content is None and item.get("tool_calls"):
            content: Any = None
        else:
            content = _standard_message_content(
                raw_content,
                index=index,
                allow_images=role in {"user", "system", "developer"},
            )
        clean: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant" and item.get("tool_calls") is not None:
            if not isinstance(item["tool_calls"], list) or len(item["tool_calls"]) > MAX_STANDARD_TOOLS:
                raise ValueError(f"messages[{index}].tool_calls is invalid")
            clean["tool_calls"] = item["tool_calls"]
        if role == "tool":
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                raise ValueError(f"messages[{index}].tool_call_id is required")
            clean["tool_call_id"] = tool_call_id
        clean_messages.append(clean)
        conversation.append(clean)

    if not conversation or conversation[-1]["role"] not in {"user", "tool"}:
        raise ValueError("the last message must have role user or tool")

    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    metadata = metadata if isinstance(metadata, dict) else {}
    user_id = body.get("user")
    if user_id is None:
        user_id = metadata.get("user_id")
    conversation_id = metadata.get("conversation_id")
    if body.get("conversation_id") is not None:
        conversation_id = body.get("conversation_id")

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("stream must be a boolean")
    n = body.get("n")
    if n is not None and n != 1:
        raise ValueError("only n=1 is supported")

    model = body.get("model")
    if model is not None:
        _feature_model(model, name="model")
    mode = body.get("aurix_mode", body.get("mode", "english"))
    if not isinstance(mode, str):
        raise ValueError("aurix_mode must be text")
    mode = mode.strip().lower()
    if mode not in {"english", "translate", "lisu_assistant"}:
        raise ValueError("aurix_mode is invalid")
    if body.get("user") is not None and not isinstance(body.get("user"), str):
        raise ValueError("user must be text")
    tools = _standard_tools(body.get("tools"))
    tool_choice = body.get("tool_choice")
    if tool_choice is not None and not isinstance(tool_choice, (str, dict)):
        raise ValueError("tool_choice must be text or an object")
    if isinstance(tool_choice, str) and tool_choice not in {"none", "auto", "required"}:
        raise ValueError("tool_choice is invalid")
    stream_options = body.get("stream_options")
    if stream_options is not None and not isinstance(stream_options, dict):
        raise ValueError("stream_options must be an object")
    if stream and stream_options is None:
        stream_options = {"include_usage": True}
    elif not stream:
        stream_options = None
    passthrough_fields = (
        "response_format",
        "stop",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "logprobs",
        "top_logprobs",
    )
    upstream_payload: dict[str, Any] = {"messages": clean_messages}
    if tools is not None:
        upstream_payload["tools"] = tools
    if tool_choice is not None:
        upstream_payload["tool_choice"] = tool_choice
    if body.get("parallel_tool_calls") is not None:
        if not isinstance(body["parallel_tool_calls"], bool):
            raise ValueError("parallel_tool_calls must be a boolean")
        upstream_payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    max_tokens = _standard_max_tokens(body)
    if max_tokens is not None:
        upstream_payload["max_tokens"] = max_tokens
    for field in passthrough_fields:
        if field in body and body[field] is not None:
            upstream_payload[field] = body[field]
    temperature = _standard_optional_number(
        body.get("temperature"), name="temperature", minimum=0.0, maximum=2.0
    )
    top_p = _standard_optional_number(
        body.get("top_p"), name="top_p", minimum=0.0, maximum=1.0
    )
    if temperature is not None:
        upstream_payload["temperature"] = temperature
    if top_p is not None:
        upstream_payload["top_p"] = top_p
    return {
        "mode": mode,
        "model": model,
        "message": conversation[-1]["content"] if isinstance(conversation[-1]["content"], str) else "image input",
        "history": conversation[:-1],
        "user_id": user_id,
        "conversation_id": conversation_id,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream,
        "stream_options": stream_options,
        "upstream_payload": upstream_payload,
    }


def _standard_completion_payload(
    result: dict[str, Any],
    *,
    model_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = str(request_id or result.get("request_id") or f"req_{secrets.token_urlsafe(12)}")
    completion_id = request_id[4:] if request_id.startswith("req_") else request_id
    if "text" in result:
        # Compatibility path for the small fake/test router and older callers.
        usage = result.get("token_usage")
        if isinstance(usage, dict):
            usage = {
                "prompt_tokens": usage.get("input_tokens") or 0,
                "completion_tokens": usage.get("output_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
            }
        message = {"role": "assistant", "content": result.get("text", "")}
        choices = [{"index": 0, "message": message, "finish_reason": "stop"}]
        response_model = model_id or result.get("model_id")
    else:
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIRouterError("9Router returned no chat choices")
        normalized_choices = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise AIRouterError("9Router returned an invalid chat choice")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise AIRouterError("9Router returned an invalid chat message")
            normalized_choices.append(
                {
                    "index": choice.get("index", len(normalized_choices)),
                    "message": message,
                    "finish_reason": choice.get("finish_reason"),
                }
            )
        choices = normalized_choices
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        response_model = model_id or result.get("model")
    provider_model = (
        result.get("model")
        or result.get("returned_model")
        if isinstance(result, dict)
        else None
    )
    requested_model = model_id or (
        result.get("model_id") if isinstance(result, dict) else None
    ) or provider_model
    return {
        "id": f"chatcmpl_{completion_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_model,
        "choices": choices,
        "usage": usage,
        "aurix": {
            "request_id": request_id,
            "requested_model": requested_model,
            "provider_model": provider_model,
        },
    }


def _responses_id(request_id: str) -> str:
    suffix = request_id[4:] if request_id.startswith("req_") else request_id
    return f"resp_{suffix}"


def _response_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _response_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = value.get("input_tokens", value.get("prompt_tokens"))
    output_tokens = value.get("output_tokens", value.get("completion_tokens"))
    total_tokens = value.get("total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    usage: dict[str, Any] = {}
    for key, token_value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        if isinstance(token_value, int) and not isinstance(token_value, bool):
            usage[key] = token_value
    cached_tokens = None
    input_details = value.get("input_tokens_details")
    prompt_details = value.get("prompt_tokens_details")
    if isinstance(input_details, dict):
        cached_tokens = input_details.get("cached_tokens")
    elif isinstance(prompt_details, dict):
        cached_tokens = prompt_details.get("cached_tokens")
    if isinstance(cached_tokens, int) and not isinstance(cached_tokens, bool):
        usage["input_tokens_details"] = {"cached_tokens": cached_tokens}
    return usage


def _response_output_items(message: dict[str, Any], *, response_id: str) -> tuple[list[dict[str, Any]], str]:
    text = _response_message_text(message)
    output: list[dict[str, Any]] = []
    message_id = f"msg_{response_id.removeprefix('resp_')}"
    if text:
        output.append(
            {
                "type": "message",
                "id": message_id,
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                continue
            call_id = str(tool_call.get("id") or f"call_{index}")
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "status": "completed",
                    "call_id": call_id,
                    "name": function["name"],
                    "arguments": str(function.get("arguments") or ""),
                }
            )
    if not output:
        raise AIRouterError("9Router returned an empty Responses output")
    return output, text


def _standard_response_payload(
    result: dict[str, Any],
    *,
    request: dict[str, Any],
    model_id: str,
    request_id: str,
) -> dict[str, Any]:
    completion = _standard_completion_payload(
        result,
        model_id=model_id,
        request_id=request_id,
    )
    choice = completion["choices"][0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AIRouterError("9Router returned an invalid Responses message")
    response_id = _responses_id(request_id)
    output, output_text = _response_output_items(message, response_id=response_id)
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": completion["created"],
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "metadata": request.get("metadata", {}),
        "model": completion.get("model") or model_id,
        "output": output,
        "parallel_tool_calls": bool(request.get("upstream_payload", {}).get("parallel_tool_calls", False)),
        "temperature": request.get("temperature"),
        "top_p": request.get("top_p"),
        "usage": _response_usage(completion.get("usage")),
        "aurix": completion.get("aurix"),
    }
    # output_text is a convenience field provided by OpenAI SDKs; keeping it
    # in the wire response is useful for lightweight HTTP clients too.
    payload["output_text"] = output_text
    return payload


def _standard_embeddings_payload(result: dict[str, Any], *, model: str) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, list):
        raise AIRouterError("9Router returned invalid embeddings")
    normalized = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), (list, str)):
            raise AIRouterError("9Router returned invalid embedding data")
        normalized.append(
            {
                "object": "embedding",
                "index": item.get("index", index),
                "embedding": item["embedding"],
            }
        )
    return {
        "object": "list",
        "data": normalized,
        "model": model,
        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else None,
    }


def _standard_image_payload(result: dict[str, Any], *, model: str) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, list) or not data:
        raise AIRouterError("9Router returned invalid image data")
    normalized = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise AIRouterError("9Router returned invalid image data")
        image: dict[str, Any] = {"index": item.get("index", index)}
        if isinstance(item.get("url"), str) and item["url"].strip():
            image["url"] = item["url"]
        elif isinstance(item.get("b64_json"), str) and item["b64_json"]:
            image["b64_json"] = item["b64_json"]
        else:
            raise AIRouterError("9Router returned an image without url or b64_json")
        if isinstance(item.get("revised_prompt"), str):
            image["revised_prompt"] = item["revised_prompt"]
        normalized.append(image)
    return {
        "created": int(result.get("created") or time.time()),
        "data": normalized,
        "model": model,
    }


def _normalize_sse_event(
    event: bytes,
    *,
    public_id: str,
    model_id: str,
    request_id: str | None = None,
) -> tuple[bytes, dict[str, Any] | None, str | None, str | None, bool]:
    lines = event.replace(b"\r\n", b"\n").split(b"\n")
    data_lines = [line[5:].lstrip() for line in lines if line.startswith(b"data:")]
    if not data_lines:
        return event, None, None, None, False
    data = b"\n".join(data_lines).strip()
    if data == b"[DONE]":
        return b"data: [DONE]", None, None, None, True
    try:
        payload = json.loads(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return event, None, None, None, False
    if not isinstance(payload, dict):
        return event, None, None, None, False
    provider_model = str(payload.get("model")) if payload.get("model") else None
    router_request_id = str(payload.get("id")) if payload.get("id") else None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    payload["id"] = public_id
    payload["object"] = "chat.completion.chunk"
    payload["model"] = model_id
    payload["aurix"] = {
        "request_id": request_id,
        "requested_model": model_id,
        "provider_model": provider_model,
    }
    done = any(
        isinstance(choice, dict) and choice.get("finish_reason") is not None
        for choice in payload.get("choices", [])
    )
    return (
        b"data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        usage,
        provider_model,
        router_request_id,
        done,
    )


def _default_usage_period() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return start.isoformat(), now.isoformat()


def _usage_period(query: str) -> tuple[str, str, str | None, int]:
    values = parse_qs(query, keep_blank_values=False)
    default_start, default_end = _default_usage_period()
    start_at = values.get("from", [default_start])[0]
    end_at = values.get("to", [default_end])[0]
    normalized: dict[str, str] = {}
    for value, name in ((start_at, "from"), (end_at, "to")):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{name} must include a timezone")
        normalized[name] = parsed.astimezone(timezone.utc).isoformat()
    start_at = normalized["from"]
    end_at = normalized["to"]
    if start_at >= end_at:
        raise ValueError("from must be before to")
    account_id = values.get("account_id", [None])[0]
    try:
        limit = int(values.get("limit", ["100"])[0])
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    return start_at, end_at, account_id, max(1, min(limit, 1_000))


def _usage_filters(query: str) -> dict[str, Any]:
    values = parse_qs(query, keep_blank_values=False)
    filters: dict[str, Any] = {}
    for name in ("key_id", "model_id", "endpoint", "status", "user_id"):
        value = values.get(name, [None])[0]
        if value:
            clean = str(value).strip()
            if len(clean) > 200:
                raise ValueError(f"{name} is too long")
            filters[name] = clean
        else:
            filters[name] = None
    if filters["status"] is not None and filters["status"] not in {"completed", "failed"}:
        raise ValueError("status must be completed or failed")
    try:
        offset = int(values.get("offset", ["0"])[0])
    except ValueError as exc:
        raise ValueError("offset must be an integer") from exc
    filters["offset"] = max(0, offset)
    return filters


def _request_context(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract site-owned attribution only; these values never authenticate a caller."""

    return (
        _optional_context_text(body.get("user_id"), name="user_id"),
        _optional_context_text(body.get("conversation_id"), name="conversation_id"),
    )


def _chat_payload(result: AIChatResult, *, model_id: str) -> dict[str, Any]:
    model_label = MODEL_CATALOG.get(model_id, {}).get("label", "Configured model")
    return {
        "text": result.text,
        "mode_result": "complete",
        "model_id": model_id,
        "model_label": model_label,
        "requested_model": result.requested_model,
        "returned_model": result.returned_model,
        "upstream_request_id": result.upstream_request_id,
        "usage": result.usage,
        "token_usage": normalize_token_usage(result.usage),
        "context": result.context,
    }


def make_handler(application: AuriXAIApplication, static_root: Path = STATIC_ROOT):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AuriXAI/1.0"

        def _write(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            no_store: bool = True,
            retry_after: int | None = None,
            set_cookie: str | None = None,
            request_id: str | None = None,
        ) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            if request_id is not None:
                self.send_header("X-Request-ID", request_id)
            if retry_after is not None:
                self.send_header("Retry-After", str(retry_after))
            if set_cookie is not None:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
            self.wfile.write(body)

        def _write_text(
            self,
            status: int,
            body: bytes,
            *,
            content_type: str = "text/plain; charset=utf-8",
            no_store: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str, *, retry_after: int | None = None) -> None:
            request_id = getattr(self, "_request_id", None)
            payload: dict[str, Any] = {"error": message}
            if request_id is not None:
                payload["request_id"] = request_id
            self._write(status, payload, retry_after=retry_after, request_id=request_id)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Request body is invalid") from exc
            if length <= 0 or length > MAX_JSON_BYTES:
                raise ValueError("Request body is invalid")
            try:
                value = json.loads(self.rfile.read(length))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Request body is invalid") from exc
            if not isinstance(value, dict):
                raise ValueError("Request body is invalid")
            history = value.get("history")
            if history is not None and (
                not isinstance(history, list) or len(history) > MAX_HISTORY_ITEMS
            ):
                raise ValueError("history is invalid")
            for name in ("user_id", "conversation_id"):
                if name in value:
                    _optional_context_text(value.get(name), name=name)
            return value

        def _read_bounded_body(self, maximum: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Request body is invalid") from exc
            if length <= 0 or length > maximum:
                raise ValueError("Request body is invalid or too large")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Request body is incomplete")
            return body

        def _read_audio_multipart(self) -> tuple[bytes, str, str, bool]:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data"):
                raise ValueError("audio endpoint requires multipart/form-data")
            body = self._read_bounded_body(MAX_AUDIO_REQUEST_BYTES)
            envelope = (
                f"Content-Type: {content_type}\r\n"
                f"MIME-Version: 1.0\r\n\r\n"
            ).encode("utf-8") + body
            message = BytesParser(policy=email_default_policy).parsebytes(envelope)
            if not message.is_multipart():
                raise ValueError("audio multipart body is invalid")
            fields: dict[str, str] = {}
            file_body: bytes | None = None
            for part in message.iter_parts():
                disposition = part.get("Content-Disposition", "")
                name = part.get_param("name", header="content-disposition")
                if not isinstance(name, str):
                    continue
                decoded = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                if name == "file":
                    if filename is None or not decoded:
                        raise ValueError("audio file is required")
                    if len(decoded) > 25 * 1024 * 1024:
                        raise ValueError("audio file is too large")
                    file_body = decoded
                elif filename is None:
                    fields[name] = decoded.decode("utf-8", "strict").strip()
            if file_body is None:
                raise ValueError("audio file is required")
            model = fields.get("model")
            if not model:
                raise ValueError("model is required")
            stream = fields.get("stream", "false").lower() in {"1", "true", "yes", "on"}
            return body, content_type, model, stream

        def _write_raw(
            self,
            status: int,
            result: dict[str, Any],
            *,
            request_id: str | None = None,
        ) -> None:
            body = result.get("body")
            if not isinstance(body, bytes):
                raise ValueError("upstream returned an invalid body")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                str(result.get("content_type") or "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if request_id is not None:
                self.send_header("X-Request-ID", request_id)
            self.end_headers()
            self.wfile.write(body)

        def _stream_conversation_attempt(
            self,
            user: VerifiedTelegramUser,
            conversation_id: str,
            attempt_id: str,
        ) -> None:
            if application.conversations is None:
                raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
            attempt = application.conversations.attempt(user.telegram_id, attempt_id)
            if attempt["conversation_id"] != conversation_id:
                raise ConversationNotFoundError("attempt not found")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            last_output = None
            deadline = time.monotonic() + 900
            try:
                while time.monotonic() < deadline:
                    current = application.conversations.attempt(user.telegram_id, attempt_id)
                    output = current.get("output_text")
                    if output != last_output:
                        last_output = output
                        payload = {
                            "attempt_id": attempt_id,
                            "status": current["status"],
                            "text": output or "",
                        }
                        self.wfile.write(
                            b"event: snapshot\ndata: "
                            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                            + b"\n\n"
                        )
                        self.wfile.flush()
                    if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        payload = application._public_attempt(current)
                        self.wfile.write(
                            b"event: terminal\ndata: "
                            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                            + b"\n\n"
                        )
                        self.wfile.flush()
                        return
                    time.sleep(0.15)
                self.wfile.write(b"event: timeout\ndata: {\"status\":\"timeout\"}\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def _stream_durable_chat(self, stream: _DurableStream) -> None:
            """Forward first-party provider deltas while committing the attempt."""

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", stream.attempt["request_id"])
            self.end_headers()
            self.close_connection = True

            def emit(event: str, payload: dict[str, Any]) -> None:
                self.wfile.write(
                    f"event: {event}\n".encode("ascii")
                    + b"data: "
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                    + b"\n\n"
                )
                self.wfile.flush()

            attempt_id = str(stream.attempt["id"])
            response = stream.response
            output = str(stream.attempt.get("output_text") or "")
            usage: dict[str, Any] | None = None
            upstream_request_id: str | None = None

            try:
                emit(
                    "start",
                    {
                        "conversation_id": stream.conversation_id,
                        "turn_id": stream.attempt["turn_id"],
                        "attempt": application._public_attempt(stream.attempt),
                    },
                )
                if response is None:
                    # A retried request with the same idempotency key may attach
                    # to an already-running attempt. Keep that case compatible
                    # with the reconnectable event API.
                    last_output = output
                    deadline = time.monotonic() + 900
                    while time.monotonic() < deadline:
                        current = application.conversations.attempt(
                            stream.user.telegram_id, attempt_id
                        )
                        current_output = str(current.get("output_text") or "")
                        if current_output != last_output:
                            last_output = current_output
                            emit(
                                "snapshot",
                                {
                                    "text": current_output,
                                    "attempt": application._public_attempt(current),
                                },
                            )
                        if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                            emit("terminal", {"attempt": application._public_attempt(current)})
                            return
                        time.sleep(0.15)
                    emit("timeout", {"status": "timeout"})
                    return

                for payload in application._stream_payloads(response):
                    current = application.conversations.attempt(
                        stream.user.telegram_id, attempt_id
                    )
                    if current["status"] != "running":
                        emit("terminal", {"attempt": application._public_attempt(current)})
                        return
                    if payload is None:
                        break
                    if payload.get("id"):
                        upstream_request_id = str(payload["id"])
                    if isinstance(payload.get("usage"), dict):
                        usage = payload["usage"]
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    fragment = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(fragment, str) and fragment:
                        output += fragment
                        application.conversations.update_attempt_output(
                            stream.user.telegram_id, attempt_id, output_text=output
                        )
                        emit("delta", {"text": fragment})

                if not output.strip():
                    raise AIRouterError("9Router returned an empty stream")
                completed_attempt = application.conversations.complete_attempt(
                    stream.user.telegram_id,
                    attempt_id,
                    output_text=output,
                    usage=usage,
                    upstream_request_id=upstream_request_id,
                )
                emit("terminal", {"attempt": application._public_attempt(completed_attempt)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
                try:
                    application.conversations.cancel_attempt(stream.user.telegram_id, attempt_id)
                except ConversationStoreError:
                    pass
            except AIRouterError:
                failed = application.conversations.fail_attempt(
                    stream.user.telegram_id, attempt_id, error_code="upstream_error"
                )
                emit("terminal", {"attempt": application._public_attempt(failed)})
            except ConversationNotFoundError:
                return
            except Exception:
                failed = application.conversations.fail_attempt(
                    stream.user.telegram_id, attempt_id, error_code="internal_error"
                )
                emit("terminal", {"attempt": application._public_attempt(failed)})
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                application._unregister_durable_stream(attempt_id)

        def _stream_chat(self, stream: _ExternalStream) -> None:
            """Normalize upstream SSE metadata while forwarding each event promptly."""

            if stream.protocol == "responses":
                self._stream_responses(stream)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", stream.request_id)
            self.end_headers()
            usage: dict[str, Any] | None = None
            provider_model: str | None = None
            router_request_id: str | None = None
            completed = False
            stream_completed = False
            done_sent = False
            failure_http_status = _router_error_status(
                AIRouterError("9Router stream ended before completion")
            )
            first_event_ms: float | None = None
            event_lines: list[bytes] = []

            def write_downstream(data: bytes) -> None:
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    raise _DownstreamStreamDisconnected from exc

            def emit_event(event: bytes) -> None:
                nonlocal usage, provider_model, router_request_id, completed, first_event_ms
                normalized, event_usage, event_model, event_id, done = _normalize_sse_event(
                    event,
                    public_id=stream.public_completion_id,
                    model_id=stream.model_id,
                    request_id=stream.request_id,
                )
                if event_usage is not None:
                    usage = event_usage
                if event_model:
                    provider_model = event_model
                if event_id:
                    router_request_id = event_id
                if normalized:
                    if first_event_ms is None:
                        first_event_ms = (time.monotonic() - stream.started_at) * 1000
                    write_downstream(normalized + b"\n\n")
                completed = completed or done

            try:
                while True:
                    try:
                        line = stream.response.readline()
                    except Exception:
                        # The upstream stream opened successfully but failed
                        # before a terminal marker; persist it as a gateway
                        # failure, not as a downstream client cancellation.
                        break
                    if not line:
                        break
                    if line in {b"\n", b"\r\n"}:
                        if event_lines:
                            event = b"\n".join(event_lines)
                            emit_event(event)
                            event_lines = []
                            if b"data: [DONE]" in event:
                                done_sent = True
                                break
                    else:
                        event_lines.append(line.rstrip(b"\r\n"))
                if event_lines:
                    emit_event(b"\n".join(event_lines))
                if completed and not done_sent:
                    write_downstream(b"data: [DONE]\n\n")
                    done_sent = True
                stream_completed = completed
            except _DownstreamStreamDisconnected:
                stream_completed = False
                failure_http_status = 499
            finally:
                self.close_connection = True
                try:
                    stream.response.close()
                finally:
                    application.record_stream_result(
                        stream,
                        usage=usage,
                        provider_model=provider_model,
                        router_request_id=router_request_id,
                        completed=stream_completed,
                        failure_http_status=failure_http_status,
                        first_event_ms=first_event_ms,
                        duration_ms=(
                            (time.monotonic() - stream.started_at) * 1000
                            if stream.started_at
                            else None
                        ),
                    )

        def _stream_raw(self, stream: _RawExternalStream) -> None:
            """Forward audio/media bytes without waiting for the full body."""

            response = stream.response
            headers = getattr(response, "headers", {})
            content_type = headers.get("Content-Type", "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", stream.request_id)
            self.end_headers()
            completed = False
            first_event_ms: float | None = None
            remaining = max(1, int(stream.max_bytes))
            try:
                while True:
                    chunk = response.read(min(64 * 1024, remaining + 1))
                    if not chunk:
                        completed = True
                        break
                    if len(chunk) > remaining:
                        if remaining:
                            self.wfile.write(chunk[:remaining])
                            self.wfile.flush()
                        completed = False
                        break
                    if first_event_ms is None:
                        first_event_ms = (time.monotonic() - stream.started_at) * 1000
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                completed = False
            finally:
                self.close_connection = True
                try:
                    response.close()
                finally:
                    application.record_raw_stream_result(
                        stream,
                        completed=completed,
                        first_event_ms=first_event_ms,
                        duration_ms=(time.monotonic() - stream.started_at) * 1000,
                    )

        def _stream_responses(self, stream: _ExternalStream) -> None:
            """Translate provider Chat SSE chunks to OpenAI Responses events."""

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", stream.request_id)
            self.end_headers()

            response_id = stream.public_completion_id
            message_id = f"msg_{response_id.removeprefix('resp_')}"
            created_at = int(time.time())
            usage: dict[str, Any] | None = None
            provider_model: str | None = None
            router_request_id: str | None = None
            text = ""
            tool_calls: dict[int, dict[str, str]] = {}
            text_item_started = False
            tool_items_started: set[int] = set()
            sequence_number = 0
            completed = False
            failure_http_status = _router_error_status(
                AIRouterError("9Router stream ended before completion")
            )
            first_event_ms: float | None = None
            event_lines: list[bytes] = []

            def write_downstream(data: bytes) -> None:
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    raise _DownstreamStreamDisconnected from exc

            def emit(event_type: str, payload: dict[str, Any]) -> None:
                nonlocal sequence_number
                sequence_number += 1
                payload = {"type": event_type, **payload}
                payload.setdefault("sequence_number", sequence_number)
                write_downstream(
                    f"event: {event_type}\n".encode("ascii")
                    + b"data: "
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\n\n"
                )

            def parse_event(event: bytes) -> tuple[dict[str, Any] | None, bool]:
                lines = event.replace(b"\r\n", b"\n").split(b"\n")
                data_lines = [line[5:].lstrip() for line in lines if line.startswith(b"data:")]
                if not data_lines:
                    return None, False
                data = b"\n".join(data_lines).strip()
                if data == b"[DONE]":
                    return None, True
                try:
                    payload = json.loads(data)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None, False
                return (payload if isinstance(payload, dict) else None), False

            try:
                emit(
                    "response.created",
                    {
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created_at,
                            "status": "in_progress",
                            "model": stream.model_id,
                            "output": [],
                            "error": None,
                            "incomplete_details": None,
                        }
                    },
                )
                while True:
                    line = stream.response.readline()
                    if not line:
                        break
                    if line in {b"\n", b"\r\n"}:
                        if not event_lines:
                            continue
                        payload, done = parse_event(b"\n".join(event_lines))
                        event_lines = []
                        if done:
                            completed = True
                            break
                        if payload is None:
                            continue
                    else:
                        event_lines.append(line.rstrip(b"\r\n"))
                        continue
                    if first_event_ms is None:
                        first_event_ms = (time.monotonic() - stream.started_at) * 1000

                    if payload.get("id") and router_request_id is None:
                        router_request_id = str(payload["id"])
                    if payload.get("model"):
                        provider_model = str(payload["model"])
                    if isinstance(payload.get("usage"), dict):
                        usage = payload["usage"]
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    if not isinstance(delta, dict):
                        delta = {}
                    fragment = delta.get("content")
                    if isinstance(fragment, str) and fragment:
                        if not text_item_started:
                            text_item_started = True
                            emit(
                                "response.output_item.added",
                                {
                                    "output_index": 0,
                                    "item": {
                                        "type": "message",
                                        "id": message_id,
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                },
                            )
                            emit(
                                "response.content_part.added",
                                {
                                    "item_id": message_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": "", "annotations": []},
                                },
                            )
                        text += fragment
                        emit(
                            "response.output_text.delta",
                            {
                                "item_id": message_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": fragment,
                            },
                        )

                    raw_tool_calls = delta.get("tool_calls")
                    if isinstance(raw_tool_calls, list):
                        for raw_call in raw_tool_calls:
                            if not isinstance(raw_call, dict):
                                continue
                            try:
                                call_index = int(raw_call.get("index", 0))
                            except (TypeError, ValueError):
                                call_index = 0
                            call = tool_calls.setdefault(
                                call_index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if raw_call.get("id"):
                                call["id"] = str(raw_call["id"])
                            function = raw_call.get("function")
                            if not isinstance(function, dict):
                                function = {}
                            if function.get("name"):
                                call["name"] = str(function["name"])
                            arguments = function.get("arguments")
                            if isinstance(arguments, str) and arguments:
                                call["arguments"] += arguments
                                if call_index not in tool_items_started:
                                    tool_items_started.add(call_index)
                                    call_id = call["id"] or f"call_{call_index}"
                                    emit(
                                        "response.output_item.added",
                                        {
                                            "output_index": call_index,
                                            "item": {
                                                "type": "function_call",
                                                "id": f"fc_{call_id}",
                                                "status": "in_progress",
                                                "call_id": call_id,
                                                "name": call["name"],
                                                "arguments": "",
                                            },
                                        },
                                    )
                                emit(
                                    "response.function_call_arguments.delta",
                                    {
                                        "item_id": f"fc_{call['id'] or f'call_{call_index}'}",
                                        "output_index": call_index,
                                        "delta": arguments,
                                    },
                                )
                    if any(
                        isinstance(choice, dict) and choice.get("finish_reason") is not None
                        for choice in choices
                    ):
                        completed = True

                if event_lines:
                    payload, done = parse_event(b"\n".join(event_lines))
                    if done:
                        completed = True
                    elif payload is not None:
                        if isinstance(payload.get("usage"), dict):
                            usage = payload["usage"]
                        if payload.get("id") and router_request_id is None:
                            router_request_id = str(payload["id"])

                if not completed:
                    raise AIRouterError("9Router stream ended before completion")
                message: dict[str, Any] = {"role": "assistant", "content": text or None}
                if tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": call["id"] or f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for index, call in sorted(tool_calls.items())
                    ]
                response_payload = _standard_response_payload(
                    {
                        "id": router_request_id,
                        "model": provider_model or stream.model_id,
                        "choices": [{"message": message, "finish_reason": "stop"}],
                        "usage": usage,
                    },
                    request={"instructions": None, "metadata": {}, "upstream_payload": {}},
                    model_id=stream.model_id,
                    request_id=stream.request_id,
                )
                if text:
                    emit(
                        "response.output_text.done",
                        {"item_id": message_id, "output_index": 0, "content_index": 0, "text": text},
                    )
                    emit(
                        "response.output_item.done",
                        {"output_index": 0, "item": response_payload["output"][0]},
                    )
                for index, call in sorted(tool_calls.items()):
                    call_id = call["id"] or f"call_{index}"
                    output_item = next(
                        (
                            item
                            for item in response_payload["output"]
                            if item.get("type") == "function_call"
                            and item.get("call_id") == call_id
                        ),
                        None,
                    )
                    emit(
                        "response.function_call_arguments.done",
                        {
                            "item_id": f"fc_{call_id}",
                            "output_index": index,
                            "arguments": call["arguments"],
                        },
                    )
                    emit(
                        "response.output_item.done",
                        {
                            "output_index": index if not text else index + 1,
                            "item": output_item,
                        },
                    )
                emit("response.completed", {"response": response_payload})
                write_downstream(b"data: [DONE]\n\n")
            except _DownstreamStreamDisconnected:
                failure_http_status = 499
                completed = False
            except AIRouterError as exc:
                failure_http_status = _router_error_status(exc)
                completed = False
            except Exception:
                failure_http_status = _router_error_status(
                    AIRouterError("9Router stream failed")
                )
                completed = False
            finally:
                self.close_connection = True
                try:
                    stream.response.close()
                finally:
                    application.record_stream_result(
                        stream,
                        usage=usage,
                        provider_model=provider_model,
                        router_request_id=router_request_id,
                        completed=completed,
                        failure_http_status=failure_http_status,
                        first_event_ms=first_event_ms,
                        duration_ms=(time.monotonic() - stream.started_at) * 1000,
                    )

        def _identity(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        def _session_cookie(self, token: str, *, max_age: int | None = None) -> str:
            age = application.sessions.max_age_seconds if max_age is None else max_age
            return (
                f"{SESSION_COOKIE_NAME}={token}; Max-Age={age}; Path=/; HttpOnly; Secure; SameSite=Lax"
            )

        def _request_session_token(self) -> str | None:
            cookie = SimpleCookie()
            header = self.headers.get("Cookie")
            if header:
                cookie.load(header)
            morsel = cookie.get(SESSION_COOKIE_NAME)
            return morsel.value if morsel else None

        def _route_api(self, method: str, path: str, query: str = "") -> None:
            if path == "/api/docs/external" and method == "GET":
                guide = static_root / "AURIX_EXTERNAL_API.md"
                if not guide.is_file():
                    guide = Path(__file__).resolve().parents[1] / "docs" / "AURIX_EXTERNAL_API.md"
                if not guide.is_file():
                    self._error(404, "Guide unavailable")
                    return
                self._write_text(200, guide.read_bytes(), no_store=False)
                return
            if path == "/api/healthz" and method == "GET":
                self._write(
                    200,
                    {
                        "ok": True,
                        "service": "aurix-ai",
                        "provider": "9router",
                        "model_configured": bool(application.router.model),
                        "external_api_enabled": application.api_keys is not None,
                    },
                )
                return
            if path == "/api/modes" and method == "GET":
                self._write(200, application.modes_payload(), no_store=False)
                return
            if path == "/api/models" and method == "GET":
                self._write(200, application.models_payload(), no_store=False)
                return
            if path == "/api/image-models" and method == "GET":
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization")
                )
                if user is None and not application.allow_anonymous:
                    raise PermissionError("Telegram login required")
                self._write(200, application.image_models_payload(), no_store=False)
                return
            if path == "/api/auth/config" and method == "GET":
                self._write(200, application.auth_config(), no_store=False)
                return
            if path == "/api/session" and method == "GET":
                session_token = self._request_session_token()
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization"), required=False
                )
                self._write(
                    200,
                    {"authenticated": True, "user": application.user_payload(user)}
                    if user is not None
                    else {"authenticated": True, "user": None},
                    set_cookie=(
                        self._session_cookie(session_token)
                        if user is not None and session_token
                        else None
                    ),
                )
                return
            if path == "/api/auth/telegram" and method == "POST":
                token, user = application.login_widget(self._read_json())
                self._write(
                    200,
                    {"authenticated": True, "user": application.user_payload(user)},
                    set_cookie=self._session_cookie(token),
                )
                return
            if path == "/api/auth/miniapp" and method == "POST":
                body = self._read_json()
                init_data = body.get("init_data")
                if not isinstance(init_data, str) or not init_data:
                    raise TelegramWebAppAuthError("Telegram session is required")
                token, user = application.login_miniapp(init_data)
                self._write(
                    200,
                    {"authenticated": True, "user": application.user_payload(user)},
                    set_cookie=self._session_cookie(token),
                )
                return
            if path == "/api/auth/logout" and method == "POST":
                application.sessions.revoke(self._request_session_token())
                self._write(
                    200,
                    {"authenticated": False},
                    set_cookie=(
                        f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"
                    ),
                )
                return
            if path == "/api/conversations" and method in {"GET", "POST"}:
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization")
                )
                if user is None:
                    raise PermissionError("Telegram login required")
                if method == "GET":
                    try:
                        limit = int(parse_qs(query, keep_blank_values=False).get("limit", ["50"])[0])
                    except ValueError as exc:
                        raise ValueError("limit must be an integer") from exc
                    self._write(
                        200,
                        {"conversations": application.durable_conversation_list(user, limit=limit)},
                    )
                else:
                    self._write(201, application.durable_conversation_create(user, self._read_json()))
                return
            conversation_parts = path.strip("/").split("/")
            if len(conversation_parts) >= 3 and conversation_parts[:2] == ["api", "conversations"]:
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization")
                )
                if user is None:
                    raise PermissionError("Telegram login required")
                conversation_id = conversation_parts[2]
                if len(conversation_parts) == 3 and method == "GET":
                    self._write(200, application.durable_conversation_detail(user, conversation_id))
                    return
                if len(conversation_parts) == 3 and method == "DELETE":
                    if not application.durable_conversation_delete(user, conversation_id):
                        raise ConversationNotFoundError("conversation not found")
                    self._write(200, {"deleted": True, "conversation_id": conversation_id})
                    return
                if (
                    len(conversation_parts) == 5
                    and conversation_parts[3] == "turns"
                    and conversation_parts[4] == "stream"
                    and method == "POST"
                ):
                    identity = f"telegram:{user.telegram_id}"
                    if not application.rate_limiter.allow(identity):
                        self._error(429, "AI request rate limit reached", retry_after=60)
                        return
                    stream = application.durable_chat_stream(
                        user, conversation_id, self._read_json()
                    )
                    self._stream_durable_chat(stream)
                    return
                if len(conversation_parts) == 4 and conversation_parts[3] == "turns" and method == "POST":
                    identity = f"telegram:{user.telegram_id}"
                    if not application.rate_limiter.allow(identity):
                        self._error(429, "AI request rate limit reached", retry_after=60)
                        return
                    payload = application.durable_chat_submit(user, conversation_id, self._read_json())
                    status = 202 if payload.get("attempt", {}).get("status") == "running" else 200
                    self._write(status, payload)
                    return
                if (
                    len(conversation_parts) == 6
                    and conversation_parts[3] == "attempts"
                    and conversation_parts[5] == "events"
                    and method == "GET"
                ):
                    self._stream_conversation_attempt(
                        user, conversation_id, conversation_parts[4]
                    )
                    return
                if (
                    len(conversation_parts) == 5
                    and conversation_parts[3] == "attempts"
                    and method == "GET"
                ):
                    if application.conversations is None:
                        raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
                    attempt = application.conversations.attempt(
                        user.telegram_id, conversation_parts[4]
                    )
                    if attempt["conversation_id"] != conversation_id:
                        raise ConversationNotFoundError("attempt not found")
                    self._write(200, {"attempt": application._public_attempt(attempt)})
                    return
                if (
                    len(conversation_parts) == 6
                    and conversation_parts[3] == "attempts"
                    and conversation_parts[5] == "cancel"
                    and method == "POST"
                ):
                    if application.conversations is None:
                        raise ExternalAPIUnavailableError("Durable AI conversations are not configured")
                    attempt = application.conversations.attempt(
                        user.telegram_id, conversation_parts[4]
                    )
                    if attempt["conversation_id"] != conversation_id:
                        raise ConversationNotFoundError("attempt not found")
                    if application.conversation_jobs is not None:
                        application.conversation_jobs.cancel(conversation_parts[4])
                    cancelled = application.conversations.cancel_attempt(
                        user.telegram_id, conversation_parts[4]
                    )
                    application.cancel_durable_stream(conversation_parts[4])
                    self._write(200, {"attempt": application._public_attempt(cancelled)})
                    return
                if (
                    len(conversation_parts) == 6
                    and conversation_parts[3] == "attempts"
                    and conversation_parts[5] == "retry"
                    and method == "POST"
                ):
                    identity = f"telegram:{user.telegram_id}"
                    if not application.rate_limiter.allow(identity):
                        self._error(429, "AI request rate limit reached", retry_after=60)
                        return
                    payload = application.durable_attempt_retry(
                        user,
                        conversation_id,
                        conversation_parts[4],
                        self._read_json(),
                    )
                    status = 202 if payload.get("attempt", {}).get("status") == "running" else 200
                    self._write(status, payload)
                    return
                self._error(404, "Not found")
                return
            if path == "/api/admin/accounts" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                payload = application.admin_create_account(
                    self._read_json(),
                    self.headers.get("Authorization"),
                    self.headers.get("Cookie"),
                    request_id=self._request_id,
                )
                self._write(201, payload, request_id=self._request_id)
                return
            if path.startswith("/api/admin/accounts/") and method == "PATCH":
                account_id = path[len("/api/admin/accounts/") :].strip("/")
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                payload = application.admin_update_account(
                    account_id,
                    self._read_json(),
                    self.headers.get("Authorization"),
                    self.headers.get("Cookie"),
                    request_id=self._request_id,
                )
                self._write(200, payload, request_id=self._request_id)
                return
            if path == "/api/admin/keys" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                payload = application.admin_issue_key(
                    self._read_json(),
                    self.headers.get("Authorization"),
                    self.headers.get("Cookie"),
                    request_id=self._request_id,
                )
                self._write(201, payload, request_id=self._request_id)
                return
            if path.startswith("/api/admin/keys/") and path.endswith("/revoke") and method == "POST":
                key_id = path[len("/api/admin/keys/") : -len("/revoke")].strip("/")
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                payload = application.admin_revoke_key(
                    key_id,
                    self.headers.get("Authorization"),
                    self.headers.get("Cookie"),
                    request_id=self._request_id,
                )
                self._write(200, payload, request_id=self._request_id)
                return
            if path == "/api/admin/capabilities" and method == "GET":
                application.authenticate_admin(
                    self.headers.get("Authorization"), self.headers.get("Cookie")
                )
                self._write(200, application.admin_capability_report())
                return
            if path == "/api/admin/access" and method == "GET":
                actor = application.authenticate_admin(
                    self.headers.get("Authorization"), self.headers.get("Cookie")
                )
                self._write(200, application.admin_access_context(actor))
                return
            if path in {"/api/admin/accounts", "/api/admin/usage"} and method == "GET":
                admin_actor = application.authenticate_admin(
                    self.headers.get("Authorization"), self.headers.get("Cookie")
                )
                owner_scope = application.admin_owner_scope(admin_actor)
                start_at, end_at, account_id, limit = _usage_period(query)
                filters = _usage_filters(query)
                requested_format = parse_qs(query, keep_blank_values=False).get(
                    "format", ["summary"]
                )[0].lower()
                if path == "/api/admin/usage" and requested_format == "analytics":
                    self._write(
                        200,
                        application.admin_usage_analytics(
                            account_id=account_id,
                            start_at=start_at,
                            end_at=end_at,
                            owner_id=owner_scope,
                            **{key: value for key, value in filters.items() if key != "offset"},
                        ),
                    )
                    return
                if path == "/api/admin/usage" and requested_format == "9router":
                    self._write(
                        200,
                        application.admin_9router_usage_export(
                            account_id=account_id,
                            start_at=start_at,
                            end_at=end_at,
                            limit=limit,
                            owner_id=owner_scope,
                            **{key: value for key, value in filters.items() if key != "offset"},
                        ),
                    )
                    return
                report = application.admin_usage_report(
                    account_id=account_id,
                    start_at=start_at,
                    end_at=end_at,
                    limit=limit,
                    owner_id=owner_scope,
                    **filters,
                )
                if path == "/api/admin/accounts":
                    self._write(
                        200,
                        {"period": report["period"], "accounts": report["accounts"]},
                    )
                else:
                    self._write(200, report)
                return
            if path == "/v1/models" and method == "GET":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                model_query = parse_qs(query, keep_blank_values=False)
                requested_category = model_query.get("category", [None])[0]
                requested_view = model_query.get("view", ["curated"])[0].strip().lower()
                self._write(
                    200,
                    application.external_models(
                        self.headers.get("Authorization"),
                        category=requested_category,
                        live=requested_view in {"live", "full"},
                    ),
                    request_id=self._request_id,
                )
                return
            if path == "/api/v1/integration" and method == "GET":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                self._write(
                    200,
                    application.external_integration_profile(
                        self.headers.get("Authorization")
                    ),
                    request_id=self._request_id,
                )
                return
            if path == "/api/v1/key-info" and method == "GET":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                self._write(
                    200,
                    application.external_key_info(self.headers.get("Authorization")),
                    request_id=self._request_id,
                )
                return
            if path == "/v1/images/generations" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                body = self._read_json()
                response_format = parse_qs(query, keep_blank_values=False).get(
                    "response_format", [None]
                )[0]
                result = application.external_image_generation(
                    body,
                    self.headers.get("Authorization"),
                    request_id=self._request_id,
                    binary=response_format == "binary",
                )
                if response_format == "binary":
                    self._write_raw(200, result, request_id=self._request_id)
                else:
                    self._write(200, result, request_id=self._request_id)
                return
            if path == "/v1/videos" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                self._write(
                    200,
                    application.external_video_generation(
                        self._read_json(),
                        self.headers.get("Authorization"),
                        request_id=self._request_id,
                    ),
                    request_id=self._request_id,
                )
                return
            if path.startswith("/v1/videos/") and method == "GET":
                video_suffix = path[len("/v1/videos/") :].strip("/")
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                if video_suffix.endswith("/content"):
                    video_id = video_suffix[: -len("/content")].strip("/")
                    self._write_raw(
                        200,
                        application.external_video_content(
                            video_id,
                            self.headers.get("Authorization"),
                            request_id=self._request_id,
                        ),
                        request_id=self._request_id,
                    )
                    return
                self._write(
                    200,
                    application.external_video_metadata(
                        video_suffix,
                        self.headers.get("Authorization"),
                        request_id=self._request_id,
                    ),
                    request_id=self._request_id,
                )
                return
            if path == "/v1/embeddings" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                payload = application.external_embeddings(
                    self._read_json(),
                    self.headers.get("Authorization"),
                    request_id=self._request_id,
                )
                self._write(200, payload, request_id=self._request_id)
                return
            if path in {"/v1/audio/transcriptions", "/v1/audio/translations"} and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                body, content_type, model, stream_audio = self._read_audio_multipart()
                if stream_audio:
                    stream = application.external_audio_stream(
                        path,
                        body,
                        content_type,
                        self.headers.get("Authorization"),
                        model=model,
                        request_id=self._request_id,
                    )
                    self._stream_raw(stream)
                    return
                result = application.external_audio(
                    path,
                    body,
                    content_type,
                    self.headers.get("Authorization"),
                    model=model,
                    request_id=self._request_id,
                )
                self._write_raw(200, result, request_id=self._request_id)
                return
            if path == "/v1/audio/speech" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                body = self._read_json()
                model = _feature_model(body.get("model"), name="model")
                if body.get("stream") is True:
                    stream = application.external_audio_stream(
                        path,
                        _json_bytes(body),
                        "application/json",
                        self.headers.get("Authorization"),
                        model=model,
                        request_id=self._request_id,
                    )
                    self._stream_raw(stream)
                    return
                result = application.external_audio(
                    path,
                    _json_bytes(body),
                    "application/json",
                    self.headers.get("Authorization"),
                    model=model,
                    request_id=self._request_id,
                )
                self._write_raw(200, result, request_id=self._request_id)
                return
            if path == "/v1/responses" and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                body = self._read_json()
                normalized = _normalize_responses_request(body)
                if normalized["stream"] and hasattr(application.router, "openai_chat_stream"):
                    stream = application.openai_response_stream(
                        body,
                        self.headers.get("Authorization"),
                        request_id=self._request_id,
                    )
                    self._stream_chat(stream)
                    return
                payload = application.external_responses(
                    body,
                    self.headers.get("Authorization"),
                    request_id=self._request_id,
                )
                self._write(200, payload, request_id=self._request_id)
                return
            if path == "/api/images/generations" and method == "POST":
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization")
                )
                identity = f"telegram:{user.telegram_id}" if user else self._identity()
                if not application.rate_limiter.allow(identity):
                    self._error(429, "AI request rate limit reached", retry_after=60)
                    return
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                self._write(
                    200,
                    application.image_generation(
                        user,
                        self._read_json(),
                        request_id=self._request_id,
                    ),
                    request_id=self._request_id,
                )
                return
            if path in {"/v1/chat", "/v1/chat/completions"} and method == "POST":
                self._request_id = f"req_{secrets.token_urlsafe(12)}"
                body = self._read_json()
                if path == "/v1/chat/completions":
                    normalized = _normalize_standard_chat_request(body)
                    if normalized["stream"] and hasattr(application.router, "openai_chat_stream"):
                        stream = application.openai_chat_stream(
                            body,
                            self.headers.get("Authorization"),
                            request_id=self._request_id,
                        )
                        self._stream_chat(stream)
                        return
                    payload = application.external_chat_completions(
                        body, self.headers.get("Authorization"), request_id=self._request_id
                    )
                else:
                    payload = application.external_chat(
                        body,
                        self.headers.get("Authorization"),
                        request_id=self._request_id,
                    )
                self._write(
                    200,
                    payload,
                    request_id=self._request_id,
                )
                return
            if path != "/api/chat" or method != "POST":
                self._error(404, "Not found")
                return
            user = application.authenticate(
                self.headers.get("Cookie"), self.headers.get("Authorization")
            )
            identity = f"telegram:{user.telegram_id}" if user else self._identity()
            if not application.rate_limiter.allow(identity):
                self._error(429, "AI request rate limit reached", retry_after=60)
                return
            self._write(200, application.chat(self._read_json()))

        def _serve_static(self, path: str, *, head_only: bool = False) -> None:
            if path == "/AURIX_EXTERNAL_API.md":
                guide = static_root / "AURIX_EXTERNAL_API.md"
                if not guide.is_file():
                    guide = Path(__file__).resolve().parents[1] / "docs" / "AURIX_EXTERNAL_API.md"
                if not guide.is_file():
                    self._error(404, "Guide unavailable")
                    return
                body = guide.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="AURIX_EXTERNAL_API.md"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)
                return
            if path in {"/", "/app", "/app/"}:
                relative = "index.html"
            elif path in {"/admin", "/admin/"}:
                relative = "admin.html"
            elif path in {"/docs/ai-api", "/docs/ai-api/"}:
                relative = "api-guide.html"
            else:
                relative = path.lstrip("/")
            target = (static_root / relative).resolve()
            root = static_root.resolve()
            if root not in target.parents and target != root:
                self._error(404, "Not found")
                return
            if not target.is_file():
                self._error(404, "Not found")
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' https://telegram.org 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; "
                "frame-src https://oauth.telegram.org https://telegram.org; base-uri 'none'; "
                "frame-ancestors https://web.telegram.org https://*.telegram.org",
            )
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                if path.startswith("/api/admin/") and method in {"POST", "PATCH"}:
                    # Non-simple header prevents cross-origin HTML form mutations.
                    # No CORS permission is granted for this admin surface.
                    origin = self.headers.get("Origin")
                    host = self.headers.get("Host", "")
                    if (self.headers.get("X-AuriX-Admin") != "1"
                        or (origin is not None and origin not in {f"https://{host}", f"http://{host}"})):
                        self._error(403, "Same-origin admin request required")
                        return
                if (
                    (path == "/api/conversations" or path.startswith("/api/conversations/"))
                    and method in {"POST", "DELETE"}
                ):
                    origin = self.headers.get("Origin")
                    host = self.headers.get("Host", "")
                    if origin is not None and origin not in {f"https://{host}", f"http://{host}"}:
                        self._error(403, "Same-origin conversation request required")
                        return
                if path.startswith("/api/") or path.startswith("/v1/"):
                    self._route_api(method, path, parsed.query)
                    return
                if method in {"GET", "HEAD"}:
                    self._serve_static(path, head_only=method == "HEAD")
                    return
                self._error(405, "Method not allowed")
            except ExternalAPIAccessDeniedError as exc:
                self._error(403, str(exc))
            except ExternalAPIRateLimitError as exc:
                self._error(429, str(exc), retry_after=60)
            except ExternalAPIUnavailableError as exc:
                self._error(503, str(exc))
            except PermissionError as exc:
                self._error(401, str(exc))
            except TelegramWebAppAuthError as exc:
                self._error(401, str(exc))
            except ConversationNotFoundError as exc:
                self._error(404, str(exc))
            except AIRouterTimeoutError as exc:
                self._error(exc.public_status, str(exc))
            except AIRouterHTTPError as exc:
                message = (
                    "9Router rate limit reached"
                    if exc.public_status == 429
                    else "9Router is temporarily unavailable"
                )
                self._error(exc.public_status, message, retry_after=exc.retry_after)
            except AIRouterError as exc:
                self._error(502, str(exc))
            except APIKeyStoreError as exc:
                self._error(400, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:
                print(f"ai web request error: {type(exc).__name__}", file=sys.stderr)
                self._error(500, "The AuriX AI service is temporarily unavailable")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("POST")

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("PATCH")

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("HEAD")

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("DELETE")

        def log_message(self, format: str, *args: Any) -> None:
            # Never write query strings, prompts, or authentication headers to logs.
            sys.stderr.write("aurix ai web request\n")

    return Handler


def create_server(
    application: AuriXAIApplication,
    *,
    port: int,
    static_root: Path = STATIC_ROOT,
) -> ThreadingHTTPServer:
    if not 1 <= int(port) <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    return ThreadingHTTPServer(("0.0.0.0", int(port)), make_handler(application, static_root))


def build_application_from_environment() -> AuriXAIApplication:
    router = NineRouterClient()
    access_token = os.environ.get("AURIX_AI_ACCESS_TOKEN", "").strip()
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_bot_username = os.environ.get("AURIX_TELEGRAM_BOT_USERNAME", "").strip()
    admin_token = os.environ.get("AURIX_AI_ADMIN_TOKEN", "").strip()
    raw_admin_ids = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
    raw_operator_ids = os.environ.get("OPERATOR_TELEGRAM_IDS", "").strip()
    raw_partner_modes = os.environ.get("AURIX_AI_PARTNER_ALLOWED_MODES", "").strip()
    raw_partner_models = os.environ.get("AURIX_AI_PARTNER_ALLOWED_MODELS", "").strip()
    try:
        admin_telegram_ids = {
            int(value.strip()) for value in raw_admin_ids.split(",") if value.strip()
        }
    except ValueError as exc:
        raise AIConfigurationError("ADMIN_TELEGRAM_IDS must contain numeric IDs") from exc
    if any(value <= 0 for value in admin_telegram_ids):
        raise AIConfigurationError("ADMIN_TELEGRAM_IDS must contain positive IDs")
    try:
        operator_telegram_ids = {
            int(value.strip()) for value in raw_operator_ids.split(",") if value.strip()
        }
    except ValueError as exc:
        raise AIConfigurationError("OPERATOR_TELEGRAM_IDS must contain numeric IDs") from exc
    if any(value <= 0 for value in operator_telegram_ids):
        raise AIConfigurationError("OPERATOR_TELEGRAM_IDS must contain positive IDs")
    try:
        operator_allowed_modes = frozenset(
            _policy_scope_values(
                raw_partner_modes or None,
                name="AURIX_AI_PARTNER_ALLOWED_MODES",
                ceiling=frozenset(DEFAULT_PARTNER_MODES),
                default=DEFAULT_PARTNER_MODES,
            )
        )
        operator_allowed_models = frozenset(
            _policy_scope_values(
                raw_partner_models or None,
                name="AURIX_AI_PARTNER_ALLOWED_MODELS",
                ceiling=frozenset({"*"}),
                default={"*"},
            )
        )
    except (ValueError, ExternalAPIAccessDeniedError) as exc:
        raise AIConfigurationError(str(exc)) from exc
    try:
        operator_max_requests_per_minute = int(
            os.environ.get("AURIX_AI_PARTNER_MAX_REQUESTS_PER_MINUTE", "600")
        )
        operator_max_requests_per_minute = _admin_requests_per_minute(
            operator_max_requests_per_minute
        )
    except (TypeError, ValueError) as exc:
        raise AIConfigurationError(
            "AURIX_AI_PARTNER_MAX_REQUESTS_PER_MINUTE must be an integer between 1 and 600"
        ) from exc
    try:
        model_catalog_ttl_seconds = int(
            os.environ.get("AURIX_AI_MODEL_CATALOG_TTL_SECONDS", "60")
        )
    except (TypeError, ValueError) as exc:
        raise AIConfigurationError(
            "AURIX_AI_MODEL_CATALOG_TTL_SECONDS must be an integer between 0 and 3600"
        ) from exc
    if not 0 <= model_catalog_ttl_seconds <= 3_600:
        raise AIConfigurationError(
            "AURIX_AI_MODEL_CATALOG_TTL_SECONDS must be an integer between 0 and 3600"
        )
    allow_anonymous = os.environ.get("AURIX_AI_ALLOW_ANONYMOUS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    try:
        requests_per_minute = int(os.environ.get("AURIX_AI_MAX_REQUESTS_PER_MINUTE", "20"))
    except ValueError as exc:
        raise AIConfigurationError("AURIX_AI_MAX_REQUESTS_PER_MINUTE must be an integer") from exc
    try:
        session_max_age_seconds = int(os.environ.get("AURIX_AI_SESSION_MAX_AGE_SECONDS", "2592000"))
    except ValueError as exc:
        raise AIConfigurationError("AURIX_AI_SESSION_MAX_AGE_SECONDS must be an integer") from exc
    legacy_token_enabled = os.environ.get("AURIX_AI_LEGACY_TOKEN_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow_anonymous and not telegram_bot_token and not legacy_token_enabled:
        raise AIConfigurationError("TELEGRAM_BOT_TOKEN is required when legacy token auth is disabled")
    api_keys_path = os.environ.get("AURIX_AI_API_KEYS_DB_PATH", "").strip()
    session_database_path = os.environ.get(
        "AURIX_AI_SESSION_DB_PATH", "/var/lib/aurix-ai/sessions.db"
    ).strip()
    api_database_url = os.environ.get("AURIX_AI_DATABASE_URL", "").strip()
    api_keys = None
    if api_database_url:
        api_keys = APIKeyStore(database_url=api_database_url)
        api_keys.initialize()
    elif api_keys_path:
        api_keys = APIKeyStore(api_keys_path)
        api_keys.initialize()
    conversation_store = None
    if api_keys is not None:
        conversation_store = AIConversationStore(
            connection_factory=api_keys.connect,
            dialect="postgres" if api_keys.uses_postgres else "sqlite",
        )
        conversation_store.initialize()
    elif session_database_path:
        conversation_store = AIConversationStore(session_database_path)
        conversation_store.initialize()
    return AuriXAIApplication(
        router,
        access_token=access_token,
        allow_anonymous=allow_anonymous,
        requests_per_minute=requests_per_minute,
        telegram_bot_token=telegram_bot_token,
        telegram_bot_username=telegram_bot_username,
        session_max_age_seconds=session_max_age_seconds,
        session_database_path=session_database_path or None,
        legacy_token_enabled=legacy_token_enabled,
        api_keys=api_keys,
        conversation_store=conversation_store,
        admin_token=admin_token,
        admin_telegram_ids=admin_telegram_ids,
        operator_telegram_ids=operator_telegram_ids,
        operator_allowed_modes=set(operator_allowed_modes),
        operator_allowed_models=set(operator_allowed_models),
        operator_max_requests_per_minute=operator_max_requests_per_minute,
        model_catalog_ttl_seconds=model_catalog_ttl_seconds,
    )


def main() -> int:
    try:
        port = int(os.environ.get("PORT", "10000"))
        application = build_application_from_environment()
        server = create_server(application, port=port)
    except (ValueError, AIConfigurationError) as exc:
        print(f"AuriX AI startup failed: {exc}", file=sys.stderr)
        return 2
    print(f"AuriX AI listening on :{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        application.close()
        if application.api_keys is not None:
            application.api_keys.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
