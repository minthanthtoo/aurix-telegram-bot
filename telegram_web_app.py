"""Telegram Mini App authentication helpers.

Telegram's ``initDataUnsafe`` is for UI hints only. Every AuriX web API
request must validate the signed ``initData`` string with the bot token before
using its Telegram user identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qsl


class TelegramWebAppAuthError(ValueError):
    """The Telegram Mini App payload is missing, invalid, or expired."""


@dataclass(frozen=True)
class VerifiedTelegramUser:
    """The small identity subset needed by the AuriX customer portal."""

    telegram_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None

    @classmethod
    def from_user_payload(cls, user: Mapping[str, object]) -> "VerifiedTelegramUser":
        try:
            telegram_id = int(user["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramWebAppAuthError("Telegram user identity is invalid") from exc
        if telegram_id <= 0:
            raise TelegramWebAppAuthError("Telegram user identity is invalid")
        first_name = str(user.get("first_name") or "Telegram user").strip()[:128]
        return cls(
            telegram_id=telegram_id,
            first_name=first_name or "Telegram user",
            last_name=str(user.get("last_name") or "").strip()[:128] or None,
            username=str(user.get("username") or "").strip().lstrip("@")[:64] or None,
            language_code=str(user.get("language_code") or "").strip()[:32] or None,
        )


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: float | None = None,
    max_age_seconds: int = 86_400,
    future_skew_seconds: int = 60,
) -> VerifiedTelegramUser:
    """Verify Telegram Web App ``initData`` and return its signed user.

    The token is never included in an exception. Duplicate fields, invalid
    timestamps, missing hash, and stale sessions are rejected fail-closed.
    """
    if not isinstance(init_data, str) or not init_data.strip():
        raise TelegramWebAppAuthError("Telegram session is required")
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise TelegramWebAppAuthError("Telegram Web App authentication is unavailable")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramWebAppAuthError("Telegram session is malformed") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in values:
            raise TelegramWebAppAuthError("Telegram session contains duplicate fields")
        values[key] = value
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64 or any(char not in "0123456789abcdef" for char in received_hash.lower()):
        raise TelegramWebAppAuthError("Telegram session signature is invalid")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise TelegramWebAppAuthError("Telegram session signature is invalid")
    try:
        auth_date = int(values.get("auth_date", ""))
    except ValueError as exc:
        raise TelegramWebAppAuthError("Telegram session timestamp is invalid") from exc
    if auth_date <= 0:
        raise TelegramWebAppAuthError("Telegram session timestamp is invalid")
    current = int(time.time() if now is None else now)
    if auth_date > current + max(0, int(future_skew_seconds)):
        raise TelegramWebAppAuthError("Telegram session timestamp is invalid")
    if max_age_seconds <= 0 or current - auth_date > int(max_age_seconds):
        raise TelegramWebAppAuthError("Telegram session has expired")
    try:
        user = json.loads(values.get("user", ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramWebAppAuthError("Telegram user identity is invalid") from exc
    if not isinstance(user, dict):
        raise TelegramWebAppAuthError("Telegram user identity is invalid")
    return VerifiedTelegramUser.from_user_payload(user)


def verify_login_widget(
    data: Mapping[str, object],
    bot_token: str,
    *,
    now: float | None = None,
    max_age_seconds: int = 86_400,
    future_skew_seconds: int = 60,
) -> VerifiedTelegramUser:
    """Verify the Telegram Login Widget callback payload.

    The widget signs a sorted ``key=value`` string with a SHA-256 digest of
    the bot token as the HMAC key.  This is a separate protocol from Mini App
    ``initData`` but produces the same verified identity type.
    """
    if not isinstance(data, Mapping) or not data:
        raise TelegramWebAppAuthError("Telegram login is required")
    if not isinstance(bot_token, str) or not bot_token.strip():
        raise TelegramWebAppAuthError("Telegram login is unavailable")

    values: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key or key == "hash":
            continue
        if isinstance(value, (dict, list, tuple, set)):
            raise TelegramWebAppAuthError("Telegram login payload is invalid")
        values[key] = str(value)
    received_hash = str(data.get("hash") or "")
    if len(received_hash) != 64 or any(char not in "0123456789abcdef" for char in received_hash.lower()):
        raise TelegramWebAppAuthError("Telegram login signature is invalid")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise TelegramWebAppAuthError("Telegram login signature is invalid")
    try:
        auth_date = int(values.get("auth_date", ""))
    except ValueError as exc:
        raise TelegramWebAppAuthError("Telegram login timestamp is invalid") from exc
    if auth_date <= 0:
        raise TelegramWebAppAuthError("Telegram login timestamp is invalid")
    current = int(time.time() if now is None else now)
    if auth_date > current + max(0, int(future_skew_seconds)):
        raise TelegramWebAppAuthError("Telegram login timestamp is invalid")
    if max_age_seconds <= 0 or current - auth_date > int(max_age_seconds):
        raise TelegramWebAppAuthError("Telegram login has expired")
    return VerifiedTelegramUser.from_user_payload(values)
