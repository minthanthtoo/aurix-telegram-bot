"""PostgreSQL commerce database lifecycle and connection adapters."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError, _now_text
from commerce_schema_bootstrap import initialize_postgres
from commerce_sqlite_database import CommerceDatabase
from observability import latency_log as _latency_log

class _PostgresConnection:
    """Small qmark-parameter adapter shared by the existing service queries."""

    def __init__(self, connection: Any, context: Any | None = None):
        # ``connection`` is a raw psycopg connection for the adapter tests. In
        # production it is replaced on enter by a psycopg_pool checkout.
        self._connection = connection
        self._context = context
        self._entered_at: float | None = None

    def __enter__(self) -> "_PostgresConnection":
        started_at = time.perf_counter()
        if self._context is not None:
            self._connection = self._context.__enter__()
        else:
            self._connection.__enter__()
        self._entered_at = time.perf_counter()
        _latency_log("postgres_checkout", started_at)
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            if self._context is not None:
                return self._context.__exit__(exc_type, exc_value, traceback)
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            if self._entered_at is not None:
                _latency_log("postgres_transaction", self._entered_at)
                self._entered_at = None

    def execute(self, query: str, params: Any = None) -> Any:
        query = query.replace("?", "%s")
        if params is None:
            return self._connection.execute(query)
        return self._connection.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

class PostgresCommerceDatabase:
    """PostgreSQL repository for hosted deployments with a durable database."""

    def __init__(self, url: str):
        self.url = url
        self._pool: Any | None = None
        self._pool_lock = threading.Lock()
        self._closed = False

    def _create_pool(self) -> Any:
        try:
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise CommerceError("PostgreSQL support requires the psycopg package") from exc
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise CommerceError("PostgreSQL support requires the psycopg-pool package") from exc
        # A persistent client-side pool removes DNS/TLS/authentication setup
        # from each repository call. Prepared statements stay disabled so the
        # same code remains safe with either Supabase pooler mode.
        started_at = time.perf_counter()
        pool = ConnectionPool(
            conninfo=self.url,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "connect_timeout": 10,
            },
            min_size=1,
            # The process has two database-using execution paths: Telegram
            # polling/handlers and one maintenance thread.
            max_size=2,
            timeout=10,
            open=False,
        )
        try:
            pool.open(wait=True, timeout=10)
        except Exception:
            pool.close()
            _latency_log("postgres_pool_open", started_at, status="error")
            raise
        _latency_log("postgres_pool_open", started_at, status="ready")
        return pool

    def connect(self) -> _PostgresConnection:
        with self._pool_lock:
            if self._closed:
                raise CommerceError("PostgreSQL connection pool is closed")
            if self._pool is None:
                self._pool = self._create_pool()
            pool = self._pool
        return _PostgresConnection(None, pool.connection())

    def close(self) -> None:
        """Return pooled connections cleanly during process shutdown."""
        with self._pool_lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    @staticmethod
    def begin_write(connection: _PostgresConnection) -> None:
        # psycopg starts a transaction automatically before the first
        # statement when autocommit is disabled.  Sending an explicit BEGIN
        # here would create a nested BEGIN and Supabase logs status 25001
        # ("there is already a transaction in progress") for every write.
        return None

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        try:
            import psycopg
        except ImportError:
            return False
        return isinstance(error, psycopg.IntegrityError)

    @staticmethod
    def _seed_plans(connection: Any) -> None:
        CommerceDatabase._seed_plans(connection)

    def mark_update_seen(self, update_id: int) -> bool:
        """Durably deduplicate Telegram updates across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO telegram_updates (update_id, received_at) VALUES (?, ?)",
                    (int(update_id), _now_text()),
                )
            except Exception as exc:
                if self.is_integrity_error(exc):
                    return False
                raise
        return True

    def list_command_scope_ids(self) -> set[int]:
        """Return chat-specific command scopes previously configured by this bot."""
        with self.connect() as connection:
            rows = connection.execute("SELECT chat_id FROM telegram_command_scopes").fetchall()
        return {int(row["chat_id"]) for row in rows}

    def record_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_command_scopes (chat_id, configured_at)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET configured_at = EXCLUDED.configured_at""",
                (int(chat_id), _now_text()),
            )

    def remove_command_scope(self, chat_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM telegram_command_scopes WHERE chat_id = ?",
                (int(chat_id),),
            )

    def create_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        command: str,
        args_json: str,
        state_fingerprint: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO admin_action_challenges
                   (token_hash, admin_id, chat_id, command, args_json,
                    state_fingerprint, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    token_hash,
                    int(admin_id),
                    int(chat_id),
                    command,
                    args_json,
                    state_fingerprint,
                    created_at,
                    expires_at,
                ),
            )

    def consume_admin_challenge(
        self,
        token_hash: str,
        admin_id: int,
        chat_id: int,
        state_fingerprint: str,
        now: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT command, args_json, state_fingerprint
                   FROM admin_action_challenges
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND state_fingerprint = ? AND status = 'pending'
                     AND expires_at > ?""",
                (token_hash, int(admin_id), int(chat_id), state_fingerprint, now),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'consumed', consumed_at = ?
                   WHERE token_hash = ? AND status = 'pending'""",
                (now, token_hash),
            )
            if getattr(updated, "rowcount", 1) != 1:
                return None
            result = dict(row)
        try:
            result["args"] = json.loads(result.pop("args_json") or "[]")
        except json.JSONDecodeError:
            return None
        return result

    def peek_admin_challenge(self, token_hash: str) -> dict[str, Any] | None:
        """Read a pending challenge envelope without consuming it."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT command, args_json FROM admin_action_challenges
                   WHERE token_hash = ? AND status = 'pending'""",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        return {
            "command": str(row["command"] if isinstance(row, dict) else row[0]),
            "args_json": row["args_json"] if isinstance(row, dict) else row[1],
        }

    def cancel_admin_challenge(
        self, token_hash: str, admin_id: int, chat_id: int, now: str
    ) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE admin_action_challenges
                   SET status = 'cancelled', cancelled_at = ?
                   WHERE token_hash = ? AND admin_id = ? AND chat_id = ?
                     AND status = 'pending'""",
                (now, token_hash, int(admin_id), int(chat_id)),
            )
        return getattr(updated, "rowcount", 1) == 1

    def prune_admin_challenges(self, now: str, retention_days: int = 30) -> int:
        cutoff = (
            datetime.fromisoformat(now) - timedelta(days=max(1, int(retention_days)))
        ).isoformat()
        with self.connect() as connection:
            deleted = connection.execute(
                """DELETE FROM admin_action_challenges
                   WHERE (status = 'pending' AND expires_at <= ?)
                      OR (status <> 'pending' AND created_at < ?)""",
                (now, cutoff),
            )
        return int(getattr(deleted, "rowcount", 0) or 0)

    def save_interaction_state(
        self,
        telegram_id: int,
        state_key: str,
        payload: dict[str, Any],
        expires_at: str,
    ) -> None:
        """Persist short-lived Telegram input state across process restarts.

        Only workflow metadata (never receipt images, access URLs, or secrets)
        belongs here.  The transport still keeps a hot in-memory copy; this
        table is the restart/retry safety net for a user who replies after a
        deploy or process restart.
        """
        if not isinstance(payload, dict):
            raise ValueError("interaction state payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if len(encoded) > 4096:
            raise ValueError("interaction state payload is too large")
        key = str(state_key).strip()
        if not key or len(key) > 48:
            raise ValueError("interaction state key is invalid")
        with self.connect() as connection:
            self.begin_write(connection)
            connection.execute(
                """INSERT INTO interaction_states
                   (telegram_id, state_key, payload_json, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id, state_key) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     expires_at = excluded.expires_at,
                     updated_at = excluded.updated_at""",
                (int(telegram_id), key, encoded, str(expires_at), _now_text()),
            )

    def load_interaction_state(
        self, telegram_id: int, state_key: str, now: str | None = None
    ) -> dict[str, Any] | None:
        """Return one unexpired workflow state, rejecting malformed payloads."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM interaction_states
                   WHERE telegram_id = ? AND state_key = ? AND expires_at > ?""",
                (int(telegram_id), str(state_key), str(now or _now_text())),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def clear_interaction_state(self, telegram_id: int, state_key: str) -> bool:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM interaction_states WHERE telegram_id = ? AND state_key = ?",
                (int(telegram_id), str(state_key)),
            )
        return int(getattr(deleted, "rowcount", 0) or 0) == 1

    def prune_interaction_states(self, now: str | None = None) -> int:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM interaction_states WHERE expires_at <= ?",
                (str(now or _now_text()),),
            )
        return int(getattr(deleted, "rowcount", 0) or 0)

    def maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        now = _now_text()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO maintenance_heartbeat
                   (id, last_started_at, last_completed_at, last_success_at,
                    last_stage, last_error, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     last_started_at = COALESCE(EXCLUDED.last_started_at, maintenance_heartbeat.last_started_at),
                     last_completed_at = COALESCE(EXCLUDED.last_completed_at, maintenance_heartbeat.last_completed_at),
                     last_success_at = COALESCE(EXCLUDED.last_success_at, maintenance_heartbeat.last_success_at),
                     last_stage = EXCLUDED.last_stage,
                     last_error = EXCLUDED.last_error,
                     updated_at = EXCLUDED.updated_at""",
                (started_at, completed_at, success_at, stage, error, now),
            )

    def get_maintenance_heartbeat(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM maintenance_heartbeat WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def initialize(self) -> None:
        initialize_postgres(self)
