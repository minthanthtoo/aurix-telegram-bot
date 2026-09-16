"""Safe dynamic Shadowsocks profiles backed by AuriX entitlement state.

Outline ``ssconf`` documents describe one tunnel.  The service therefore
models a multi-server *pool* in AuriX, but returns one currently eligible
Shadowsocks route on each refresh.  This keeps the document compatible with
the official Outline clients while allowing a future client that supports
multi-server subscriptions to use the same durable pool.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_METHOD_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class SsconfError(RuntimeError):
    """A dynamic Shadowsocks profile cannot be safely issued or rendered."""


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _parse_port(parsed: Any) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise SsconfError("Shadowsocks port is invalid") from exc
    if port is None or not 1 <= int(port) <= 65_535:
        raise SsconfError("Shadowsocks port is invalid")
    return int(port)


def _decode_credentials(user_info: str) -> tuple[str, str]:
    value = unquote(str(user_info or "")).strip()
    if not value:
        raise SsconfError("Shadowsocks credentials are missing")
    if ":" in value:
        method, password = value.split(":", 1)
    else:
        try:
            padded = value + "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise SsconfError("Shadowsocks credentials are invalid") from exc
        if ":" not in decoded:
            raise SsconfError("Shadowsocks credentials are invalid")
        method, password = decoded.split(":", 1)
    method = str(method).strip()
    password = str(password).strip()
    if not _METHOD_PATTERN.fullmatch(method) or not password or len(password) > 4096:
        raise SsconfError("Shadowsocks credentials are invalid")
    return method, password


def parse_shadowsocks_uri(access_url: str) -> dict[str, Any]:
    """Parse a static ``ss://`` URI into an Outline dynamic-key document."""
    value = str(access_url or "").replace("\r", "").replace("\n", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "ss" or not parsed.hostname:
        raise SsconfError("access URL is not a Shadowsocks URI")
    if parsed.query and any(
        part.split("=", 1)[0].strip().lower() not in {"outline"}
        for part in parsed.query.split("&")
        if part
    ):
        raise SsconfError("Shadowsocks access URL has unsupported query fields")
    try:
        host = str(parsed.hostname)
    except ValueError as exc:
        raise SsconfError("Shadowsocks host is invalid") from exc
    if any(char.isspace() or char in "/?#" for char in host) or len(host) > 253:
        raise SsconfError("Shadowsocks host is invalid")
    user_info, separator, _host_port = parsed.netloc.rpartition("@")
    if not separator:
        raise SsconfError("Shadowsocks credentials are missing")
    method, password = _decode_credentials(user_info.rsplit("//", 1)[-1].split("://", 1)[-1])
    return {
        "server": host,
        "server_port": _parse_port(parsed),
        "method": method,
        "password": password,
        "remarks": unquote(parsed.fragment) if parsed.fragment else "",
    }


def _public_profile_parts(public_base_url: str) -> tuple[str, str]:
    parsed = urlsplit(str(public_base_url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SsconfError("ssconf public base URL must be HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SsconfError("ssconf public base URL must not contain URL decorations")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SsconfError("ssconf public base URL port is invalid") from exc
    host = str(parsed.hostname)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port in (None, 443) else f"{host}:{port}"
    prefix = parsed.path.rstrip("/")
    return netloc, prefix


def render_ssconf_uri(public_base_url: str, token: str, *, label: str = "AuriX VPN") -> str:
    """Render the client-facing dynamic access key without persisting plaintext."""
    token = str(token or "").strip()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise SsconfError("ssconf token is invalid")
    netloc, prefix = _public_profile_parts(public_base_url)
    path = f"{prefix}/api/ssconf/{quote(token, safe='')}"
    suffix = f"#{quote(str(label or 'AuriX VPN')[:128], safe='')}"
    return f"ssconf://{netloc}{path}{suffix}"


def select_outline_route(candidates: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Choose one route for an Outline-compatible single-tunnel document."""
    usable = [
        item
        for item in candidates
        if isinstance(item, Mapping)
        and str(item.get("access_url") or "").strip()
        and item.get("healthy") is not False
    ]
    if not usable:
        return None

    def sort_key(item: Mapping[str, Any]) -> tuple[int, int, str, str]:
        try:
            generation = int(item.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        return (
            0 if item.get("healthy") is True else 1,
            -generation,
            str(item.get("endpoint_id") or ""),
            str(item.get("generation_id") or ""),
        )

    return sorted(usable, key=sort_key)[0]


def render_outline_ssconf_document(
    access_url: str, *, remarks: str = "AuriX VPN"
) -> dict[str, Any]:
    """Return the single-object JSON shape consumed by Outline clients."""
    document = parse_shadowsocks_uri(access_url)
    document["remarks"] = str(remarks or document.get("remarks") or "AuriX VPN")[:128]
    return document


class SsconfProfileService:
    """Issue and serve bearer-scoped dynamic profiles for active accounts."""

    def __init__(
        self,
        database: Any,
        *,
        identity: Any,
        secret_encryptor: Callable[[str], str],
        secret_decryptor: Callable[[str], str | None],
        public_base_url: str = "",
        route_health: Callable[[str], bool | None] | None = None,
    ):
        self.database = database
        self.identity = identity
        self.secret_encryptor = secret_encryptor
        self.secret_decryptor = secret_decryptor
        self.public_base_url = str(public_base_url or "").strip()
        self.route_health = route_health
        if self.public_base_url:
            _public_profile_parts(self.public_base_url)

    @property
    def configured(self) -> bool:
        return bool(self.public_base_url)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _require_configured(self) -> None:
        if not self.configured:
            raise SsconfError("dynamic Shadowsocks profiles are not configured")

    def _account_status(self, account_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM accounts WHERE account_id = ?", (str(account_id),)
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def _candidate_routes(self, account_id: str) -> list[dict[str, Any]]:
        routes = self.identity.routes_for_account(str(account_id))
        candidates: list[dict[str, Any]] = []
        for route in routes or []:
            if not isinstance(route, Mapping):
                continue
            if str(route.get("protocol") or "").strip().lower() != "outline":
                continue
            route_id = str(route.get("route_id") or route.get("generation_id") or "").strip()
            if not route_id:
                continue
            try:
                secret_record = self.identity.route_secret_record(str(account_id), route_id)
                if not isinstance(secret_record, Mapping):
                    continue
                access_url = self.secret_decryptor(str(secret_record.get("secret_ciphertext") or ""))
                if not access_url:
                    continue
                parse_shadowsocks_uri(str(access_url))
            except Exception:
                # A malformed/corrupt route must not poison the whole dynamic
                # profile or expose provider exceptions to an unauthenticated
                # client.
                continue
            candidate = {**dict(route), "access_url": str(access_url)}
            if callable(self.route_health):
                try:
                    health = self.route_health(str(route.get("endpoint_id") or ""))
                    if isinstance(health, bool):
                        candidate["healthy"] = health
                except Exception:
                    pass
            candidates.append(candidate)
        return candidates

    def issue_for_telegram(self, telegram_id: int, *, label: str = "AuriX VPN") -> dict[str, Any]:
        account_id = self.identity.ensure_account(int(telegram_id))
        return self.issue_for_account(account_id, label=label)

    def issue_for_account(self, account_id: str, *, label: str = "AuriX VPN") -> dict[str, Any]:
        self._require_configured()
        account_id = str(account_id or "").strip()
        if not account_id or len(account_id) > 128:
            raise SsconfError("account is invalid")
        now = self._now()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            account = connection.execute(
                "SELECT status FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if account is None or str(account["status"]) != "active":
                raise SsconfError("account is not active")
            row = connection.execute(
                "SELECT token_ciphertext, status FROM dynamic_shadowsocks_profiles WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            token = ""
            if row is not None and str(row["status"]) == "active":
                try:
                    candidate = self.secret_decryptor(str(row["token_ciphertext"] or ""))
                    if candidate and _TOKEN_PATTERN.fullmatch(candidate):
                        token = candidate
                except Exception:
                    token = ""
            if not token:
                token = secrets.token_urlsafe(32)
                values = (
                    _token_hash(token),
                    self.secret_encryptor(token),
                    "active",
                    now,
                )
                if row is None:
                    connection.execute(
                        """INSERT INTO dynamic_shadowsocks_profiles
                           (profile_id, account_id, token_hash, token_ciphertext,
                            status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (f"ssconf-{uuid.uuid4().hex}", account_id, *values[:3], now, now),
                    )
                else:
                    connection.execute(
                        """UPDATE dynamic_shadowsocks_profiles
                              SET token_hash = ?, token_ciphertext = ?, status = ?,
                                  updated_at = ?
                            WHERE account_id = ?""",
                        (*values, account_id),
                    )
        candidates = self._candidate_routes(account_id)
        profile_url = render_ssconf_uri(self.public_base_url, token, label=label)
        return {
            "protocol": "ssconf",
            "profile_url": profile_url,
            "access_url": profile_url,
            "selection_mode": "single-active-route",
            "compatible_route_count": len(candidates),
            "multi_server_pool": len(candidates) > 1,
        }

    def document(self, token: str) -> dict[str, Any] | None:
        """Serve one current route; return ``None`` for an unknown bearer."""
        token = str(token or "").strip()
        if not _TOKEN_PATTERN.fullmatch(token):
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT account_id FROM dynamic_shadowsocks_profiles
                    WHERE token_hash = ? AND status = 'active'""",
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        account_id = str(row["account_id"])
        if self._account_status(account_id) != "active":
            return {"error": {"message": "AuriX VPN access is not currently active"}}
        selected = select_outline_route(self._candidate_routes(account_id))
        if selected is None:
            return {"error": {"message": "AuriX VPN has no compatible route available"}}
        try:
            document = render_outline_ssconf_document(
                str(selected["access_url"]),
                remarks=str(selected.get("region") or selected.get("endpoint_id") or "AuriX VPN"),
            )
        except SsconfError:
            return {"error": {"message": "AuriX VPN route configuration is unavailable"}}
        try:
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                connection.execute(
                    """UPDATE dynamic_shadowsocks_profiles
                          SET served_count = served_count + 1, last_served_at = ?,
                              updated_at = ?
                        WHERE token_hash = ? AND status = 'active'""",
                    (self._now(), self._now(), _token_hash(token)),
                )
        except Exception:
            # Serving a valid profile must not fail because a telemetry counter
            # could not be updated.
            pass
        return document


__all__ = [
    "SsconfError",
    "SsconfProfileService",
    "parse_shadowsocks_uri",
    "render_outline_ssconf_document",
    "render_ssconf_uri",
    "select_outline_route",
]
