"""Persistent account and API-key storage for the AuriX external AI API.

This layer intentionally sits in front of 9Router.  9Router keeps one private
provider credential; consuming websites receive separate AuriX credentials.
The database stores only a hash of each bearer token, so the plaintext key is
available only once, when it is issued by the operator CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from migrations import Migration, apply_migrations
from persistence import open_sqlite_connection


API_KEY_PREFIX = "ak_live_"
ALL_SCOPE = "*"
UTC = timezone.utc


class APIKeyStoreError(ValueError):
    """Raised when an account or API-key operation is invalid."""


@dataclass(frozen=True)
class APIKeyPrincipal:
    """The non-secret identity and policy attached to an authenticated key."""

    key_id: str
    account_id: str
    account_name: str
    allowed_modes: frozenset[str]
    allowed_models: frozenset[str]
    requests_per_minute: int

    def allows_mode(self, mode: str) -> bool:
        return ALL_SCOPE in self.allowed_modes or mode in self.allowed_modes

    def allows_model(self, model_id: str) -> bool:
        return ALL_SCOPE in self.allowed_models or model_id in self.allowed_models


@dataclass(frozen=True)
class IssuedAPIKey:
    account_id: str
    key_id: str
    label: str
    token: str
    token_prefix: str
    expires_at: str | None


class _PostgresConnection:
    """Small placeholder adapter matching the repository SQLite API."""

    def __init__(self, pool: Any):
        self._pool = pool
        self._context: Any | None = None
        self._connection: Any | None = None

    def __enter__(self) -> "_PostgresConnection":
        self._context = self._pool.connection()
        self._connection = self._context.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            if self._context is None:
                return False
            return self._context.__exit__(exc_type, exc_value, traceback)
        finally:
            self._context = None
            self._connection = None

    def execute(self, query: str, params: Any = None) -> Any:
        if self._connection is None:
            raise RuntimeError("PostgreSQL connection is not active")
        query = query.replace("?", "%s")
        if params is None:
            return self._connection.execute(query)
        return self._connection.execute(query, params)


def _create_postgres_pool(database_url: str) -> Any:
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise APIKeyStoreError(
            "PostgreSQL AI storage requires psycopg and psycopg-pool"
        ) from exc
    pool = ConnectionPool(
        conninfo=database_url,
        kwargs={
            "row_factory": dict_row,
            "prepare_threshold": None,
            "connect_timeout": 10,
        },
        min_size=1,
        max_size=4,
        timeout=10,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=10)
    except Exception:
        pool.close()
        raise
    return pool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_token_usage(usage: dict[str, Any] | None) -> dict[str, int | None]:
    """Normalize common OpenAI/Google usage field names for account metering."""

    if not isinstance(usage, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_tokens": None,
        }

    def integer(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, bool):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                return parsed
        return None

    input_tokens = integer("prompt_tokens", "input_tokens", "prompt_token_count", "inputTokenCount")
    output_tokens = integer(
        "completion_tokens", "output_tokens", "candidates_token_count", "outputTokenCount"
    )
    total_tokens = integer("total_tokens", "total_token_count", "totalTokenCount")
    cached_tokens = integer(
        "cached_tokens",
        "cache_read_input_tokens",
        "cached_content_token_count",
        "cachedContentTokenCount",
    )
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def reconcile_usage_events(
    aurix_events: Iterable[dict[str, Any]],
    router_events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare AuriX export events with a read-only 9Router export.

    The function is intentionally independent of either database. It accepts
    the public export shapes and reports aggregate differences without copying
    records into 9Router.
    """

    def integer(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    def event_values(event: dict[str, Any], *, router: bool) -> tuple[str, dict[str, int]]:
        model = str(
            event.get("model")
            or event.get("provider_model")
            or event.get("model_id")
            or "unknown"
        )
        tokens = event.get("tokens") if isinstance(event.get("tokens"), dict) else {}
        input_tokens = integer(
            event.get("promptTokens") if router else event.get("input_tokens")
        )
        output_tokens = integer(
            event.get("completionTokens") if router else event.get("output_tokens")
        )
        total_tokens = integer(event.get("totalTokens") if router else event.get("total_tokens"))
        if not input_tokens:
            input_tokens = integer(tokens.get("prompt_tokens") or tokens.get("input_tokens"))
        if not output_tokens:
            output_tokens = integer(tokens.get("completion_tokens") or tokens.get("output_tokens"))
        if not total_tokens:
            total_tokens = integer(tokens.get("total_tokens")) or input_tokens + output_tokens
        return model, {
            "requests": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def aggregate(events: Iterable[dict[str, Any]], *, router: bool) -> dict[str, Any]:
        totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        by_model: dict[str, dict[str, int]] = {}
        for event in events:
            model, values = event_values(event, router=router)
            for key in totals:
                totals[key] += values[key]
            current = by_model.setdefault(model, {key: 0 for key in totals})
            for key in totals:
                current[key] += values[key]
        return {"totals": totals, "by_model": by_model}

    aurix_list = list(aurix_events)
    router_list = list(router_events)
    aurix_aggregate = aggregate(aurix_list, router=False)
    router_aggregate = aggregate(router_list, router=True)
    aurix_ids = {
        str(event.get("request_id"))
        for event in aurix_list
        if event.get("request_id")
    }
    router_ids = {
        str((event.get("meta") or {}).get("aurixRequestId"))
        for event in router_list
        if isinstance(event.get("meta"), dict) and event.get("meta", {}).get("aurixRequestId")
    }
    return {
        "aurix": aurix_aggregate,
        "router": router_aggregate,
        "matched_request_ids": len(aurix_ids & router_ids),
        "unmatched_aurix_requests": len(aurix_ids - router_ids),
        "unmatched_router_requests": len(router_ids - aurix_ids),
        "difference": {
            key: router_aggregate["totals"][key] - aurix_aggregate["totals"][key]
            for key in ("requests", "input_tokens", "output_tokens", "total_tokens")
        },
    }


def _scope(values: Iterable[str] | None, *, name: str) -> tuple[str, ...]:
    if values is None:
        return (ALL_SCOPE,)
    if isinstance(values, str):
        values = (values,)
    result = tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
    if not result:
        raise APIKeyStoreError(f"{name} must not be empty")
    if ALL_SCOPE in result and len(result) > 1:
        raise APIKeyStoreError(f"{name} cannot combine '*' with explicit values")
    return result


API_KEY_MIGRATIONS = (
    Migration(
        1,
        "api_key_accounts_and_usage",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS api_accounts (
                   id TEXT PRIMARY KEY,
                   name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   allowed_modes_json TEXT NOT NULL,
                   allowed_models_json TEXT NOT NULL,
                   requests_per_minute INTEGER NOT NULL
                       CHECK (requests_per_minute BETWEEN 1 AND 600),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS api_keys (
                   id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES api_accounts(id),
                   label TEXT NOT NULL,
                   token_prefix TEXT NOT NULL,
                   token_hash TEXT NOT NULL UNIQUE,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   last_used_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS api_keys_lookup ON api_keys(token_hash, status)",
            """CREATE TABLE IF NOT EXISTS api_usage (
                   request_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES api_accounts(id),
                   key_id TEXT NOT NULL REFERENCES api_keys(id),
                   mode TEXT NOT NULL,
                   model_id TEXT,
                   status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
                   http_status INTEGER NOT NULL,
                   provider_model TEXT,
                   usage_json TEXT,
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS api_usage_account_time ON api_usage(account_id, created_at)",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS api_accounts (
                   id TEXT PRIMARY KEY,
                   name TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   allowed_modes_json TEXT NOT NULL,
                   allowed_models_json TEXT NOT NULL,
                   requests_per_minute INTEGER NOT NULL
                       CHECK (requests_per_minute BETWEEN 1 AND 600),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS api_keys (
                   id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES api_accounts(id),
                   label TEXT NOT NULL,
                   token_prefix TEXT NOT NULL,
                   token_hash TEXT NOT NULL UNIQUE,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'revoked')),
                   created_at TEXT NOT NULL,
                   revoked_at TEXT,
                   last_used_at TEXT
               )""",
            "CREATE INDEX IF NOT EXISTS api_keys_lookup ON api_keys(token_hash, status)",
            """CREATE TABLE IF NOT EXISTS api_usage (
                   request_id TEXT PRIMARY KEY,
                   account_id TEXT NOT NULL REFERENCES api_accounts(id),
                   key_id TEXT NOT NULL REFERENCES api_keys(id),
                   mode TEXT NOT NULL,
                   model_id TEXT,
                   status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
                   http_status INTEGER NOT NULL,
                   provider_model TEXT,
                   usage_json TEXT,
                   created_at TEXT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS api_usage_account_time ON api_usage(account_id, created_at)",
        ),
    ),
    Migration(
        2,
        "api_key_expiration",
        sqlite_statements=("ALTER TABLE api_keys ADD COLUMN expires_at TEXT",),
        postgres_statements=("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TEXT",),
    ),
    Migration(
        3,
        "api_usage_token_counters",
        sqlite_statements=(
            "ALTER TABLE api_usage ADD COLUMN input_tokens INTEGER",
            "ALTER TABLE api_usage ADD COLUMN output_tokens INTEGER",
            "ALTER TABLE api_usage ADD COLUMN total_tokens INTEGER",
        ),
        postgres_statements=(
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS input_tokens BIGINT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS output_tokens BIGINT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS total_tokens BIGINT",
        ),
    ),
    Migration(
        4,
        "router_compatible_usage_metadata",
        sqlite_statements=(
            "ALTER TABLE api_usage ADD COLUMN cached_tokens INTEGER",
            "ALTER TABLE api_usage ADD COLUMN router_request_id TEXT",
            "ALTER TABLE api_usage ADD COLUMN provider TEXT",
            "ALTER TABLE api_usage ADD COLUMN endpoint TEXT",
            "ALTER TABLE api_usage ADD COLUMN cost REAL",
            "ALTER TABLE api_accounts ADD COLUMN owner_type TEXT NOT NULL DEFAULT 'external_site'",
            "ALTER TABLE api_accounts ADD COLUMN owner_id TEXT",
        ),
        postgres_statements=(
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS cached_tokens BIGINT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS router_request_id TEXT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS provider TEXT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS endpoint TEXT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION",
            "ALTER TABLE api_accounts ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'external_site'",
            "ALTER TABLE api_accounts ADD COLUMN IF NOT EXISTS owner_id TEXT",
        ),
    ),
    Migration(
        5,
        "site_conversation_attribution",
        sqlite_statements=(
            "ALTER TABLE api_usage ADD COLUMN user_id TEXT",
            "ALTER TABLE api_usage ADD COLUMN conversation_id TEXT",
            "CREATE INDEX IF NOT EXISTS api_usage_account_user_time ON api_usage(account_id, user_id, created_at)",
        ),
        postgres_statements=(
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS user_id TEXT",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS conversation_id TEXT",
            "CREATE INDEX IF NOT EXISTS api_usage_account_user_time ON api_usage(account_id, user_id, created_at)",
        ),
    ),
)


class APIKeyStore:
    """AuriX account ledger using SQLite or the existing AuriX PostgreSQL DB."""

    def __init__(self, path: Path | str | None = None, *, database_url: str | None = None):
        if bool(path) == bool(database_url):
            raise ValueError("provide exactly one AI database path or database URL")
        self.path = Path(path) if path is not None else None
        self.database_url = database_url.strip() if database_url else None
        self._pool: Any | None = None
        self._pool_lock = threading.Lock()
        self._closed = False

    @property
    def uses_postgres(self) -> bool:
        return self.database_url is not None

    def initialize(self) -> None:
        if self.uses_postgres:
            with self._pool_lock:
                if self._pool is None:
                    self._pool = _create_postgres_pool(self.database_url or "")
            with self.connect() as connection:
                apply_migrations(
                    connection,
                    component="ai_api_keys",
                    dialect="postgres",
                    migrations=API_KEY_MIGRATIONS,
                )
            return
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            apply_migrations(
                connection,
                component="ai_api_keys",
                dialect="sqlite",
                migrations=API_KEY_MIGRATIONS,
            )
        # The token hash is the sensitive value in this database.  Keep the
        # database itself private when the directory permissions permit it.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection | _PostgresConnection:
        if self.uses_postgres:
            with self._pool_lock:
                if self._closed:
                    raise APIKeyStoreError("PostgreSQL AI storage is closed")
                if self._pool is None:
                    self._pool = _create_postgres_pool(self.database_url or "")
                pool = self._pool
            return _PostgresConnection(pool)
        assert self.path is not None
        return open_sqlite_connection(self.path, busy_timeout_ms=30_000)

    def close(self) -> None:
        with self._pool_lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def create_account(
        self,
        name: str,
        *,
        account_id: str | None = None,
        allowed_modes: Iterable[str] | None = None,
        allowed_models: Iterable[str] | None = None,
        requests_per_minute: int = 60,
        owner_type: str = "external_site",
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = str(name).strip()
        if not clean_name or len(clean_name) > 160:
            raise APIKeyStoreError("account name must be 1-160 characters")
        try:
            rpm = int(requests_per_minute)
        except (TypeError, ValueError) as exc:
            raise APIKeyStoreError("requests_per_minute must be an integer") from exc
        if not 1 <= rpm <= 600:
            raise APIKeyStoreError("requests_per_minute must be between 1 and 600")
        clean_owner_type = str(owner_type).strip() or "external_site"
        if len(clean_owner_type) > 80:
            raise APIKeyStoreError("owner_type is too long")
        clean_owner_id = str(owner_id).strip() if owner_id is not None else None
        if clean_owner_id and len(clean_owner_id) > 160:
            raise APIKeyStoreError("owner_id is too long")
        resolved_id = str(account_id or f"acct_{secrets.token_hex(10)}").strip()
        if not resolved_id or len(resolved_id) > 80:
            raise APIKeyStoreError("account_id is invalid")
        modes = _scope(allowed_modes, name="allowed_modes")
        models = _scope(allowed_models, name="allowed_models")
        now = _now()
        with self.connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO api_accounts
                       (id, name, status, allowed_modes_json, allowed_models_json,
                        requests_per_minute, owner_type, owner_id, created_at, updated_at)
                       VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        resolved_id,
                        clean_name,
                        json.dumps(modes),
                        json.dumps(models),
                        rpm,
                        clean_owner_type,
                        clean_owner_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise APIKeyStoreError("account_id already exists") from exc
        return {
            "id": resolved_id,
            "name": clean_name,
            "status": "active",
            "allowed_modes": list(modes),
            "allowed_models": list(models),
            "requests_per_minute": rpm,
            "owner_type": clean_owner_type,
            "owner_id": clean_owner_id,
            "created_at": now,
        }

    def issue_key(
        self,
        account_id: str,
        *,
        label: str = "default",
        expires_at: str | None = None,
    ) -> IssuedAPIKey:
        clean_account_id = str(account_id).strip()
        clean_label = str(label).strip()
        if not clean_account_id:
            raise APIKeyStoreError("account_id is required")
        if not clean_label or len(clean_label) > 160:
            raise APIKeyStoreError("key label must be 1-160 characters")
        if expires_at is not None:
            try:
                parsed_expiry = datetime.fromisoformat(expires_at)
            except ValueError as exc:
                raise APIKeyStoreError("expires_at must be an ISO-8601 timestamp") from exc
            if parsed_expiry.tzinfo is None:
                raise APIKeyStoreError("expires_at must include a timezone")
            expires_at = parsed_expiry.astimezone(UTC).isoformat()
        key_id = f"key_{secrets.token_hex(10)}"
        token = f"{API_KEY_PREFIX}{key_id}_{secrets.token_urlsafe(32)}"
        token_prefix = token[:18]
        now = _now()
        with self.connect() as connection:
            account = connection.execute(
                "SELECT id FROM api_accounts WHERE id = ? AND status = 'active'",
                (clean_account_id,),
            ).fetchone()
            if account is None:
                raise APIKeyStoreError("active account not found")
            connection.execute(
                """INSERT INTO api_keys
                   (id, account_id, label, token_prefix, token_hash, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    clean_account_id,
                    clean_label,
                    token_prefix,
                    _token_hash(token),
                    now,
                    expires_at,
                ),
            )
        return IssuedAPIKey(clean_account_id, key_id, clean_label, token, token_prefix, expires_at)

    def authenticate(self, token: str | None) -> APIKeyPrincipal | None:
        if not token or not token.startswith(API_KEY_PREFIX):
            return None
        token_hash = _token_hash(token)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT k.id AS key_id, k.account_id, a.name AS account_name,
                          k.token_hash, a.allowed_modes_json, a.allowed_models_json,
                          a.requests_per_minute
                   FROM api_keys AS k
                   JOIN api_accounts AS a ON a.id = k.account_id
                   WHERE k.token_hash = ? AND k.status = 'active' AND a.status = 'active'
                     AND (k.expires_at IS NULL OR k.expires_at > ?)""",
                (token_hash, _now()),
            ).fetchone()
            if row is None or row["token_hash"] != token_hash:
                return None
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (_now(), row["key_id"]),
            )
        return APIKeyPrincipal(
            key_id=str(row["key_id"]),
            account_id=str(row["account_id"]),
            account_name=str(row["account_name"]),
            allowed_modes=frozenset(json.loads(row["allowed_modes_json"])),
            allowed_models=frozenset(json.loads(row["allowed_models_json"])),
            requests_per_minute=int(row["requests_per_minute"]),
        )

    def update_account(
        self,
        account_id: str,
        *,
        name: str | None = None,
        allowed_modes: Iterable[str] | None = None,
        allowed_models: Iterable[str] | None = None,
        requests_per_minute: int | None = None,
        owner_type: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        clean_account_id = str(account_id).strip()
        if not clean_account_id:
            raise APIKeyStoreError("account_id is required")
        if (
            name is None
            and allowed_modes is None
            and allowed_models is None
            and requests_per_minute is None
            and owner_type is None
            and owner_id is None
        ):
            raise APIKeyStoreError("at least one account field must be provided")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_accounts WHERE id = ? AND status = 'active'",
                (clean_account_id,),
            ).fetchone()
            if row is None:
                raise APIKeyStoreError("active account not found")
            clean_name = str(name).strip() if name is not None else str(row["name"])
            if not clean_name or len(clean_name) > 160:
                raise APIKeyStoreError("account name must be 1-160 characters")
            try:
                rpm = int(requests_per_minute) if requests_per_minute is not None else int(row["requests_per_minute"])
            except (TypeError, ValueError) as exc:
                raise APIKeyStoreError("requests_per_minute must be an integer") from exc
            if not 1 <= rpm <= 600:
                raise APIKeyStoreError("requests_per_minute must be between 1 and 600")
            modes = _scope(
                allowed_modes
                if allowed_modes is not None
                else json.loads(row["allowed_modes_json"]),
                name="allowed_modes",
            )
            models = _scope(
                allowed_models
                if allowed_models is not None
                else json.loads(row["allowed_models_json"]),
                name="allowed_models",
            )
            clean_owner_type = (
                str(owner_type).strip()
                if owner_type is not None
                else str(row["owner_type"] or "external_site")
            )
            clean_owner_id = (
                str(owner_id).strip()
                if owner_id is not None and str(owner_id).strip()
                else row["owner_id"]
            )
            if not clean_owner_type or len(clean_owner_type) > 80:
                raise APIKeyStoreError("owner_type is invalid")
            if clean_owner_id and len(str(clean_owner_id)) > 160:
                raise APIKeyStoreError("owner_id is too long")
            connection.execute(
                """UPDATE api_accounts
                   SET name = ?, allowed_modes_json = ?, allowed_models_json = ?,
                       requests_per_minute = ?, owner_type = ?, owner_id = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    clean_name,
                    json.dumps(modes),
                    json.dumps(models),
                    rpm,
                    clean_owner_type,
                    clean_owner_id,
                    _now(),
                    clean_account_id,
                ),
            )
        return next(account for account in self.list_accounts() if account["id"] == clean_account_id)

    def revoke_key(self, key_id: str) -> bool:
        now = _now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE api_keys
                   SET status = 'revoked', revoked_at = ?
                   WHERE id = ? AND status = 'active'""",
                (now, str(key_id).strip()),
            )
        return result.rowcount == 1

    def revoke_account(self, account_id: str) -> bool:
        now = _now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE api_accounts
                   SET status = 'revoked', updated_at = ?
                   WHERE id = ? AND status = 'active'""",
                (now, str(account_id).strip()),
            )
            connection.execute(
                """UPDATE api_keys
                   SET status = 'revoked', revoked_at = COALESCE(revoked_at, ?)
                   WHERE account_id = ? AND status = 'active'""",
                (now, str(account_id).strip()),
            )
        return result.rowcount == 1

    def record_usage(
        self,
        *,
        request_id: str,
        principal: APIKeyPrincipal,
        mode: str,
        model_id: str | None,
        status: str,
        http_status: int,
        provider_model: str | None = None,
        usage: dict[str, Any] | None = None,
        router_request_id: str | None = None,
        provider: str | None = "9router",
        endpoint: str | None = "/chat/completions",
        cost: float | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise APIKeyStoreError("usage status is invalid")
        token_usage = normalize_token_usage(usage)
        insert = (
            """INSERT INTO api_usage
                   (request_id, account_id, key_id, mode, model_id, status,
                   http_status, provider_model, usage_json, input_tokens,
                   output_tokens, total_tokens, cached_tokens, router_request_id,
                    provider, endpoint, cost, user_id, conversation_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO NOTHING"""
            if self.uses_postgres
            else """INSERT OR IGNORE INTO api_usage
                   (request_id, account_id, key_id, mode, model_id, status,
                   http_status, provider_model, usage_json, input_tokens,
                   output_tokens, total_tokens, cached_tokens, router_request_id,
                    provider, endpoint, cost, user_id, conversation_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        )
        with self.connect() as connection:
            connection.execute(
                insert,
                (
                    str(request_id),
                    principal.account_id,
                    principal.key_id,
                    mode,
                    model_id,
                    status,
                    int(http_status),
                    provider_model,
                    json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                    token_usage["input_tokens"],
                    token_usage["output_tokens"],
                    token_usage["total_tokens"],
                    token_usage["cached_tokens"],
                    router_request_id,
                    provider,
                    endpoint,
                    cost,
                    user_id,
                    conversation_id,
                    _now(),
                ),
            )

    def usage_summary(
        self,
        *,
        account_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return account-level request and token totals for a time window."""

        filters = []
        join_filters = ["u.account_id = a.id"]
        join_params: list[Any] = []
        where_params: list[Any] = []
        if account_id:
            filters.append("a.id = ?")
            where_params.append(str(account_id).strip())
        if start_at:
            join_filters.append("u.created_at >= ?")
            join_params.append(start_at)
        if end_at:
            join_filters.append("u.created_at < ?")
            join_params.append(end_at)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        join = " AND ".join(join_filters)
        params = join_params + where_params
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT a.id, a.name, a.status,
                          COUNT(u.request_id) AS requests,
                          COALESCE(SUM(CASE WHEN u.status = 'completed' THEN 1 ELSE 0 END), 0)
                              AS successful_requests,
                          COALESCE(SUM(CASE WHEN u.status = 'failed' THEN 1 ELSE 0 END), 0)
                              AS failed_requests,
                          COALESCE(SUM(CASE WHEN u.input_tokens IS NOT NULL
                                              OR u.output_tokens IS NOT NULL
                                              OR u.total_tokens IS NOT NULL THEN 1 ELSE 0 END), 0)
                              AS usage_reported_requests,
                          COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                          COALESCE(SUM(u.cost), 0) AS cost
                   FROM api_accounts AS a
                   LEFT JOIN api_usage AS u ON {join}
                   {where}
                   GROUP BY a.id, a.name, a.status
                   ORDER BY a.created_at, a.id""",
                params,
            ).fetchall()
        return [
            {
                "account_id": str(row["id"]),
                "account_name": str(row["name"]),
                "account_status": str(row["status"]),
                "requests": int(row["requests"]),
                "successful_requests": int(row["successful_requests"]),
                "failed_requests": int(row["failed_requests"]),
                "usage_reported_requests": int(row["usage_reported_requests"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "cached_tokens": int(row["cached_tokens"]),
                "cost": float(row["cost"]),
            }
            for row in rows
        ]

    def usage_events(
        self,
        *,
        account_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return prompt-free request records for support/admin inspection."""

        bounded_limit = max(1, min(int(limit), 1_000))
        filters = []
        params: list[Any] = []
        if account_id:
            filters.append("u.account_id = ?")
            params.append(str(account_id).strip())
        if start_at:
            filters.append("u.created_at >= ?")
            params.append(start_at)
        if end_at:
            filters.append("u.created_at < ?")
            params.append(end_at)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(bounded_limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT u.request_id, u.account_id, a.name AS account_name,
                          a.owner_type, a.owner_id,
                          u.key_id, u.mode, u.model_id, u.status, u.http_status,
                          u.provider_model, u.input_tokens, u.output_tokens,
                          u.total_tokens, u.cached_tokens, u.provider, u.endpoint,
                          u.cost, u.router_request_id, u.user_id, u.conversation_id,
                          u.usage_json, u.created_at
                   FROM api_usage AS u
                   JOIN api_accounts AS a ON a.id = u.account_id
                   {where}
                   ORDER BY u.created_at DESC, u.request_id DESC
                   LIMIT ?""",
                params,
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            raw_usage = item.pop("usage_json", None)
            try:
                item["usage"] = json.loads(raw_usage) if raw_usage else None
            except (TypeError, ValueError, json.JSONDecodeError):
                item["usage"] = None
            events.append(item)
        return events

    def usage_9router_events(
        self,
        *,
        account_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Export prompt-free events using 9Router's usageHistory shape."""

        events = self.usage_events(
            account_id=account_id,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        return [
            {
                "timestamp": event["created_at"],
                "provider": event.get("provider") or "9router",
                "model": event.get("provider_model") or event.get("model_id"),
                "connectionId": None,
                "apiKey": None,
                "endpoint": event.get("endpoint") or "/chat/completions",
                "promptTokens": event.get("input_tokens") or 0,
                "completionTokens": event.get("output_tokens") or 0,
                "cost": event.get("cost") or 0,
                "status": event.get("status") or "unknown",
                "tokens": event.get("usage") or {},
                "meta": {
                    "aurixRequestId": event["request_id"],
                    "aurixAccountId": event["account_id"],
                    "ownerType": event.get("owner_type"),
                    "ownerId": event.get("owner_id"),
                    "aurixKeyId": event["key_id"],
                    "mode": event.get("mode"),
                    "cachedTokens": event.get("cached_tokens") or 0,
                    "routerRequestId": event.get("router_request_id"),
                    "httpStatus": event.get("http_status"),
                    "userId": event.get("user_id"),
                    "conversationId": event.get("conversation_id"),
                },
            }
            for event in events
        ]

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, name, status, allowed_modes_json, allowed_models_json,
                          requests_per_minute, owner_type, owner_id, created_at, updated_at
                   FROM api_accounts ORDER BY created_at, id"""
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "status": str(row["status"]),
                "allowed_modes": json.loads(row["allowed_modes_json"]),
                "allowed_models": json.loads(row["allowed_models_json"]),
                "requests_per_minute": int(row["requests_per_minute"]),
                "owner_type": str(row["owner_type"] or "external_site"),
                "owner_id": row["owner_id"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def list_keys(self, account_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT k.id, k.account_id, a.name AS account_name, k.label,
                          k.token_prefix, k.status, k.created_at, k.expires_at,
                          k.revoked_at, k.last_used_at
                   FROM api_keys AS k JOIN api_accounts AS a ON a.id = k.account_id"""
        params: tuple[Any, ...] = ()
        if account_id:
            query += " WHERE k.account_id = ?"
            params = (str(account_id).strip(),)
        query += " ORDER BY k.created_at, k.id"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
