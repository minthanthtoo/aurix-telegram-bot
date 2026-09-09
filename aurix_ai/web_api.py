"""Branded AuriX AI web service backed by the existing 9Router."""

from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default_policy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .router import (
    MODEL_CATALOG,
    AIChatResult,
    AIConfigurationError,
    AIRouterError,
    NineRouterClient,
    MAX_MESSAGE_CHARS,
    model_id_for_route,
    normalize_mode,
    _optional_context_text,
    resolve_model_id,
)
from .api_keys import APIKeyStore, normalize_token_usage
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
MAX_IMAGE_URL_CHARS = 16 * 1024
SESSION_COOKIE_NAME = "aurix_ai_session"


class ExternalAPIUnavailableError(RuntimeError):
    """The external API was requested before its persistent key store was configured."""


class ExternalAPIAccessDeniedError(PermissionError):
    """The external account key is valid but lacks the requested scope."""


class ExternalAPIRateLimitError(RuntimeError):
    """The external account has exceeded its configured request rate."""


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
    """Process-local opaque sessions; the browser never receives bot data."""

    def __init__(self, max_age_seconds: int) -> None:
        self.max_age_seconds = max(300, min(int(max_age_seconds), 2_592_000))
        self._lock = threading.Lock()
        self._sessions: dict[str, _AISession] = {}

    def issue(self, user: VerifiedTelegramUser) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked(time.time())
            self._sessions[token] = _AISession(user, time.time() + self.max_age_seconds)
        return token

    def get(self, token: str | None) -> VerifiedTelegramUser | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session.expires_at <= now:
                self._sessions.pop(token, None)
                return None
            return session.user

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

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
        session_max_age_seconds: int = 86_400,
        legacy_token_enabled: bool | None = None,
        api_keys: APIKeyStore | None = None,
        admin_token: str = "",
        admin_telegram_ids: set[int] | None = None,
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
        self.sessions = AISessionStore(session_max_age_seconds)
        self.api_keys = api_keys
        self.admin_token = admin_token.strip()
        self.admin_telegram_ids = frozenset(admin_telegram_ids or set())
        # Direct construction stays compatible with the old test/client API;
        # environment-based production startup passes an explicit false.
        self.legacy_token_enabled = bool(access_token) if legacy_token_enabled is None else legacy_token_enabled
        self.rate_limiter = AIRateLimiter(requests_per_minute)

    @staticmethod
    def user_payload(user: VerifiedTelegramUser) -> dict[str, Any]:
        return {
            "telegram_id": user.telegram_id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username,
            "language_code": user.language_code,
        }

    def authenticate(self, cookie_header: str | None, authorization: str | None) -> VerifiedTelegramUser | None:
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
        raise PermissionError("Telegram login required")

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
                    "capabilities": ["chat", "streaming", "tools"],
                }
                for model_id, item in MODEL_CATALOG.items()
            ]
        }

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
        )
        return _chat_payload(result, model_id=model_id)

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
        if not principal.allows_model(model_id):
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
        except AIRouterError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=mode,
                model_id=model_id,
                status="failed",
                http_status=502,
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
        except AIRouterError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=502,
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
        try:
            response = self.router.openai_chat_stream(
                payload,
                request_id=request_id,
                account_id=principal.account_id,
                user_id=request["user_id"],
                conversation_id=request["conversation_id"],
            )
        except AIRouterError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode=request["mode"],
                model_id=model_id,
                status="failed",
                http_status=502,
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
        )

    def record_stream_result(
        self,
        stream: _ExternalStream,
        *,
        usage: dict[str, Any] | None,
        provider_model: str | None,
        router_request_id: str | None,
        completed: bool,
    ) -> None:
        self.api_keys.record_usage(
            request_id=stream.request_id,
            principal=stream.principal,
            mode=stream.mode,
            model_id=stream.model_id,
            status="completed" if completed else "failed",
            http_status=200 if completed else 499,
            provider_model=provider_model,
            usage=usage,
            router_request_id=router_request_id,
            provider="9router",
            endpoint="/chat/completions",
            user_id=stream.user_id,
            conversation_id=stream.conversation_id,
        )

    def _authenticate_external_request(self, authorization: str | None) -> Any:
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        principal = self.api_keys.authenticate(_bearer_token(authorization))
        if principal is None:
            raise PermissionError("AuriX API key required")
        if not self.rate_limiter.allow(
            f"api-account:{principal.account_id}", limit=principal.requests_per_minute
        ):
            raise ExternalAPIRateLimitError("API request rate limit reached")
        return principal

    @staticmethod
    def _authorize_external_request(principal: Any, *, model_id: str, mode: str) -> None:
        if mode in {"english", "translate", "lisu_assistant"} and not principal.allows_mode(mode):
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
        except AIRouterError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="embeddings",
                model_id=model,
                status="failed",
                http_status=502,
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
        try:
            result = self.router.request_raw(
                path,
                body,
                content_type=content_type,
                accept="application/json, text/plain, text/event-stream, audio/*, */*",
                max_response_bytes=MAX_AUDIO_RESPONSE_BYTES,
                request_id=request_id,
                account_id=principal.account_id,
            )
        except AIRouterError:
            self.api_keys.record_usage(
                request_id=request_id,
                principal=principal,
                mode="audio",
                model_id=model,
                status="failed",
                http_status=502,
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

    def external_models(self, authorization: str | None) -> dict[str, Any]:
        principal = self._authenticate_external_request(authorization)
        output: list[dict[str, Any]] = []
        categories = ((None, {"chat", "streaming", "tools"}), ("embedding", {"embeddings"}), ("stt", {"audio_input"}), ("tts", {"audio_output"}))
        for category, capabilities in categories:
            try:
                models = self.router.list_models(category)
            except AIRouterError:
                continue
            for item in models:
                model_id = item["id"]
                if not principal.allows_model(model_id):
                    continue
                output.append({**item, "capabilities": sorted(capabilities)})
        return {"object": "list", "data": output}

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
    ) -> None:
        token = _bearer_token(authorization)
        if token is not None and self.admin_token and hmac.compare_digest(token, self.admin_token):
            return
        cookie = SimpleCookie()
        if cookie_header:
            cookie.load(cookie_header)
        session = cookie.get(SESSION_COOKIE_NAME)
        user = self.sessions.get(session.value if session else None)
        if user is not None and user.telegram_id in self.admin_telegram_ids:
            return
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
    ) -> dict[str, Any]:
        if self.api_keys is None:
            raise ExternalAPIUnavailableError("External API is not configured")
        accounts = self.api_keys.list_accounts()
        summaries = self.api_keys.usage_summary(
            account_id=account_id, start_at=start_at, end_at=end_at
        )
        events = self.api_keys.usage_events(
            account_id=account_id, start_at=start_at, end_at=end_at, limit=limit
        )
        summary_by_id = {item["account_id"]: item for item in summaries}
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
        return {
            "period": {"start_at": start_at, "end_at": end_at},
            "accounts": accounts if account_id is None else [
                account for account in accounts if account["id"] == account_id
            ],
            "requests": events,
        }

    def admin_9router_usage_export(
        self,
        *,
        account_id: str | None,
        start_at: str,
        end_at: str,
        limit: int,
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
            ),
        }


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


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


def _resolve_standard_model(value: Any, *, default_route: str) -> tuple[str, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        route = default_route
        return route, model_id_for_route(route) or route
    requested = _feature_model(value, name="model")
    catalog_item = MODEL_CATALOG.get(requested)
    if catalog_item is not None:
        return catalog_item["route"], requested
    route_model_id = model_id_for_route(requested)
    return requested, route_model_id or requested


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
    if len(mode) > 40:
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
    return {
        "id": f"chatcmpl_{completion_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_model,
        "choices": choices,
        "usage": usage,
    }


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


def _normalize_sse_event(
    event: bytes,
    *,
    public_id: str,
    model_id: str,
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

        def _read_audio_multipart(self) -> tuple[bytes, str, str]:
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
            return body, content_type, model

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

        def _stream_chat(self, stream: _ExternalStream) -> None:
            """Normalize upstream SSE metadata while forwarding each event promptly."""

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", stream.request_id)
            self.end_headers()
            usage: dict[str, Any] | None = None
            provider_model: str | None = None
            router_request_id: str | None = None
            completed = False
            stream_completed = False
            event_lines: list[bytes] = []

            def emit_event(event: bytes) -> None:
                nonlocal usage, provider_model, router_request_id, completed
                normalized, event_usage, event_model, event_id, done = _normalize_sse_event(
                    event,
                    public_id=stream.public_completion_id,
                    model_id=stream.model_id,
                )
                if event_usage is not None:
                    usage = event_usage
                if event_model:
                    provider_model = event_model
                if event_id:
                    router_request_id = event_id
                if normalized:
                    self.wfile.write(normalized + b"\n\n")
                    self.wfile.flush()
                completed = completed or done

            try:
                while True:
                    line = stream.response.readline()
                    if not line:
                        break
                    if line in {b"\n", b"\r\n"}:
                        if event_lines:
                            event = b"\n".join(event_lines)
                            emit_event(event)
                            event_lines = []
                            if completed or b"data: [DONE]" in event:
                                break
                    else:
                        event_lines.append(line.rstrip(b"\r\n"))
                if event_lines:
                    emit_event(b"\n".join(event_lines))
                stream_completed = completed
            except (BrokenPipeError, ConnectionResetError, OSError):
                stream_completed = False
            finally:
                try:
                    stream.response.close()
                finally:
                    application.record_stream_result(
                        stream,
                        usage=usage,
                        provider_model=provider_model,
                        router_request_id=router_request_id,
                        completed=stream_completed,
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
            if path == "/api/auth/config" and method == "GET":
                self._write(200, application.auth_config(), no_store=False)
                return
            if path == "/api/session" and method == "GET":
                user = application.authenticate(
                    self.headers.get("Cookie"), self.headers.get("Authorization")
                )
                self._write(
                    200,
                    {"authenticated": True, "user": application.user_payload(user)}
                    if user is not None
                    else {"authenticated": True, "user": None},
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
            if path in {"/api/admin/accounts", "/api/admin/usage"} and method == "GET":
                application.authenticate_admin(
                    self.headers.get("Authorization"), self.headers.get("Cookie")
                )
                start_at, end_at, account_id, limit = _usage_period(query)
                requested_format = parse_qs(query, keep_blank_values=False).get(
                    "format", ["summary"]
                )[0].lower()
                if path == "/api/admin/usage" and requested_format == "9router":
                    self._write(
                        200,
                        application.admin_9router_usage_export(
                            account_id=account_id,
                            start_at=start_at,
                            end_at=end_at,
                            limit=limit,
                        ),
                    )
                    return
                report = application.admin_usage_report(
                    account_id=account_id,
                    start_at=start_at,
                    end_at=end_at,
                    limit=limit,
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
                self._write(
                    200,
                    application.external_models(self.headers.get("Authorization")),
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
                body, content_type, model = self._read_audio_multipart()
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
            relative = "index.html" if path in {"/", "/app", "/app/"} else path.lstrip("/")
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
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
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
            except AIRouterError as exc:
                self._error(502, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:
                print(f"ai web request error: {type(exc).__name__}", file=sys.stderr)
                self._error(500, "The AuriX AI service is temporarily unavailable")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("POST")

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch("HEAD")

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
    try:
        admin_telegram_ids = {
            int(value.strip()) for value in raw_admin_ids.split(",") if value.strip()
        }
    except ValueError as exc:
        raise AIConfigurationError("ADMIN_TELEGRAM_IDS must contain numeric IDs") from exc
    if any(value <= 0 for value in admin_telegram_ids):
        raise AIConfigurationError("ADMIN_TELEGRAM_IDS must contain positive IDs")
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
        session_max_age_seconds = int(os.environ.get("AURIX_AI_SESSION_MAX_AGE_SECONDS", "86400"))
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
    api_database_url = os.environ.get("AURIX_AI_DATABASE_URL", "").strip()
    api_keys = None
    if api_database_url:
        api_keys = APIKeyStore(database_url=api_database_url)
        api_keys.initialize()
    elif api_keys_path:
        api_keys = APIKeyStore(api_keys_path)
        api_keys.initialize()
    return AuriXAIApplication(
        router,
        access_token=access_token,
        allow_anonymous=allow_anonymous,
        requests_per_minute=requests_per_minute,
        telegram_bot_token=telegram_bot_token,
        telegram_bot_username=telegram_bot_username,
        session_max_age_seconds=session_max_age_seconds,
        legacy_token_enabled=legacy_token_enabled,
        api_keys=api_keys,
        admin_token=admin_token,
        admin_telegram_ids=admin_telegram_ids,
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
        if application.api_keys is not None:
            application.api_keys.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
