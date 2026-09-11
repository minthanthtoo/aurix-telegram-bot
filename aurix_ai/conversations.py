"""Durable, owner-scoped AI conversations and generation attempts.

The browser is allowed to keep a draft locally, but the server owns submitted
turns.  This module deliberately stores the full submitted transcript and a
frozen context snapshot separately: a retry or reconnect can inspect the
original attempt without silently resending it or trusting a changed browser
history.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migrations import Migration, apply_migrations
from persistence import open_sqlite_connection


UTC = timezone.utc
MAX_CONVERSATION_TITLE = 120
MAX_SOURCE_CHARS = 32_000
MAX_CONTEXT_MESSAGES = 100
MAX_LIST_LIMIT = 100


class ConversationStoreError(ValueError):
    """Raised when a conversation operation cannot be completed safely."""


class ConversationNotFoundError(ConversationStoreError):
    """Raised when an owner cannot access an active conversation or attempt."""


AI_CONVERSATION_MIGRATIONS = (
    Migration(
        1,
        "durable_conversations_and_attempts",
        sqlite_statements=(
            """CREATE TABLE IF NOT EXISTS ai_conversations (
                   id TEXT PRIMARY KEY,
                   owner_telegram_id INTEGER NOT NULL,
                   title TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   direction TEXT,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'deleted')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   deleted_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS ai_conversations_owner_time
               ON ai_conversations(owner_telegram_id, status, updated_at)""",
            """CREATE TABLE IF NOT EXISTS ai_turns (
                   id TEXT PRIMARY KEY,
                   conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
                   owner_telegram_id INTEGER NOT NULL,
                   sequence INTEGER NOT NULL CHECK (sequence > 0),
                   mode TEXT NOT NULL,
                   direction TEXT,
                   submitted_source TEXT NOT NULL,
                   context_json TEXT NOT NULL,
                   client_submission_id TEXT,
                   created_at TEXT NOT NULL,
                   UNIQUE(conversation_id, sequence),
                   UNIQUE(conversation_id, client_submission_id)
               )""",
            """CREATE INDEX IF NOT EXISTS ai_turns_conversation_sequence
               ON ai_turns(conversation_id, sequence)""",
            """CREATE TABLE IF NOT EXISTS ai_attempts (
                   id TEXT PRIMARY KEY,
                   turn_id TEXT NOT NULL REFERENCES ai_turns(id),
                   conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
                   owner_telegram_id INTEGER NOT NULL,
                   model_id TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
                   output_text TEXT,
                   error_code TEXT,
                   request_id TEXT UNIQUE,
                   upstream_request_id TEXT,
                   usage_json TEXT,
                   cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
                   created_at TEXT NOT NULL,
                   started_at TEXT NOT NULL,
                   finished_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS ai_attempts_turn_time
               ON ai_attempts(turn_id, created_at)""",
            """CREATE INDEX IF NOT EXISTS ai_attempts_owner_status
               ON ai_attempts(owner_telegram_id, status, created_at)""",
        ),
        postgres_statements=(
            """CREATE TABLE IF NOT EXISTS ai_conversations (
                   id TEXT PRIMARY KEY,
                   owner_telegram_id BIGINT NOT NULL,
                   title TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   direction TEXT,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'deleted')),
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   deleted_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS ai_conversations_owner_time
               ON ai_conversations(owner_telegram_id, status, updated_at)""",
            """CREATE TABLE IF NOT EXISTS ai_turns (
                   id TEXT PRIMARY KEY,
                   conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
                   owner_telegram_id BIGINT NOT NULL,
                   sequence INTEGER NOT NULL CHECK (sequence > 0),
                   mode TEXT NOT NULL,
                   direction TEXT,
                   submitted_source TEXT NOT NULL,
                   context_json TEXT NOT NULL,
                   client_submission_id TEXT,
                   created_at TEXT NOT NULL,
                   UNIQUE(conversation_id, sequence),
                   UNIQUE(conversation_id, client_submission_id)
               )""",
            """CREATE INDEX IF NOT EXISTS ai_turns_conversation_sequence
               ON ai_turns(conversation_id, sequence)""",
            """CREATE TABLE IF NOT EXISTS ai_attempts (
                   id TEXT PRIMARY KEY,
                   turn_id TEXT NOT NULL REFERENCES ai_turns(id),
                   conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
                   owner_telegram_id BIGINT NOT NULL,
                   model_id TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')),
                   output_text TEXT,
                   error_code TEXT,
                   request_id TEXT UNIQUE,
                   upstream_request_id TEXT,
                   usage_json TEXT,
                   cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
                   created_at TEXT NOT NULL,
                   started_at TEXT NOT NULL,
                   finished_at TEXT
               )""",
            """CREATE INDEX IF NOT EXISTS ai_attempts_turn_time
               ON ai_attempts(turn_id, created_at)""",
            """CREATE INDEX IF NOT EXISTS ai_attempts_owner_status
               ON ai_attempts(owner_telegram_id, status, created_at)""",
        ),
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    raise ConversationStoreError("conversation storage returned an invalid row")


def _text(value: Any, *, name: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise ConversationStoreError(f"{name} must be text")
    result = value.strip()
    if required and not result:
        raise ConversationStoreError(f"{name} is required")
    if len(result) > maximum:
        raise ConversationStoreError(f"{name} is too long")
    return result


class AIConversationStore:
    """SQLite/PostgreSQL store for owner-scoped conversation state.

    ``connection_factory`` is useful when the application already owns a
    PostgreSQL pool (the API-key store shares that pool).  The factory must
    return a context-managed connection with ``execute`` and ``fetchone`` /
    ``fetchall`` semantics matching the repository's existing database layer.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        dialect: str = "sqlite",
    ) -> None:
        if (path is None) == (connection_factory is None):
            raise ValueError("provide exactly one conversation path or connection factory")
        if dialect not in {"sqlite", "postgres"}:
            raise ValueError("conversation storage dialect is invalid")
        if dialect == "postgres" and connection_factory is None:
            raise ValueError("PostgreSQL conversations require a connection factory")
        self.path = Path(path) if path is not None else None
        self._connection_factory = connection_factory
        self.dialect = dialect

    def connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        assert self.path is not None
        return open_sqlite_connection(self.path, busy_timeout_ms=30_000)

    def initialize(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if self.dialect == "sqlite":
                connection.execute("PRAGMA journal_mode = WAL")
            apply_migrations(
                connection,
                component="ai_conversations",
                dialect=self.dialect,
                migrations=AI_CONVERSATION_MIGRATIONS,
            )
            # A process restart must never leave a previously active provider
            # call looking retryable. The caller can inspect it and create a
            # deliberate retry attempt instead.
            connection.execute(
                """UPDATE ai_attempts
                   SET status = 'interrupted', error_code = 'process_restart',
                       finished_at = ?, cancel_requested = 0
                   WHERE status = 'running'""",
                (_now(),),
            )

    @staticmethod
    def _owner(owner_telegram_id: int) -> int:
        try:
            value = int(owner_telegram_id)
        except (TypeError, ValueError) as exc:
            raise ConversationStoreError("owner identity is invalid") from exc
        if value <= 0:
            raise ConversationStoreError("owner identity is invalid")
        return value

    @staticmethod
    def _conversation_id(value: Any) -> str:
        return _text(value, name="conversation_id", maximum=100)

    @staticmethod
    def _attempt_id(value: Any) -> str:
        return _text(value, name="attempt_id", maximum=100)

    def create_conversation(
        self,
        owner_telegram_id: int,
        *,
        mode: str = "english",
        direction: str | None = None,
        title: str = "New conversation",
    ) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_mode = _text(mode, name="mode", maximum=40)
        clean_title = _text(title, name="title", maximum=MAX_CONVERSATION_TITLE)
        clean_direction = _text(direction, name="direction", maximum=40, required=False) or None
        now = _now()
        conversation = {
            "id": _id("conv"),
            "owner_telegram_id": owner,
            "title": clean_title,
            "mode": clean_mode,
            "direction": clean_direction,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO ai_conversations
                   (id, owner_telegram_id, title, mode, direction, status,
                    created_at, updated_at, deleted_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL)""",
                (
                    conversation["id"], owner, clean_title, clean_mode,
                    clean_direction, now, now,
                ),
            )
        return conversation | {"turns": []}

    def list_conversations(self, owner_telegram_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        owner = self._owner(owner_telegram_id)
        bounded_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, owner_telegram_id, title, mode, direction, status,
                          created_at, updated_at, deleted_at
                   FROM ai_conversations
                   WHERE owner_telegram_id = ? AND status = 'active'
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (owner, bounded_limit),
            ).fetchall()
        return [_row_dict(row) | {"turn_count": self._turn_count(owner, row["id"])} for row in rows]

    def _turn_count(self, owner: int, conversation_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count
                   FROM ai_turns AS t
                   JOIN ai_conversations AS c ON c.id = t.conversation_id
                   WHERE t.owner_telegram_id = ? AND t.conversation_id = ?
                     AND c.status = 'active'""",
                (owner, conversation_id),
            ).fetchone()
        return int(_row_dict(row)["count"]) if row is not None else 0

    def _owned_conversation(self, connection: Any, owner: int, conversation_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT id, owner_telegram_id, title, mode, direction, status,
                      created_at, updated_at, deleted_at
               FROM ai_conversations
               WHERE id = ? AND owner_telegram_id = ? AND status = 'active'""",
            (conversation_id, owner),
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError("conversation not found")
        return _row_dict(row)

    def get_conversation(self, owner_telegram_id: int, conversation_id: str) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._conversation_id(conversation_id)
        with self.connect() as connection:
            conversation = self._owned_conversation(connection, owner, clean_id)
            turn_rows = connection.execute(
                """SELECT id, sequence, mode, direction, submitted_source, created_at
                   FROM ai_turns
                   WHERE conversation_id = ? AND owner_telegram_id = ?
                   ORDER BY sequence ASC""",
                (clean_id, owner),
            ).fetchall()
            turns: list[dict[str, Any]] = []
            for turn_row in turn_rows:
                turn = _row_dict(turn_row)
                attempt_rows = connection.execute(
                    """SELECT id, model_id, status, output_text, error_code,
                              request_id, upstream_request_id, usage_json,
                              cancel_requested, created_at, started_at, finished_at
                       FROM ai_attempts
                       WHERE turn_id = ? AND owner_telegram_id = ?
                       ORDER BY created_at ASC""",
                    (turn["id"], owner),
                ).fetchall()
                turn["attempts"] = [self._attempt_payload(_row_dict(row)) for row in attempt_rows]
                turns.append(turn)
        return conversation | {"turns": turns}

    @staticmethod
    def _attempt_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        usage: Any = None
        if row.get("usage_json"):
            try:
                parsed = json.loads(str(row["usage_json"]))
                usage = parsed if isinstance(parsed, dict) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                usage = None
        return {
            "id": str(row["id"]),
            "model_id": str(row["model_id"]),
            "status": str(row["status"]),
            "output_text": row.get("output_text"),
            "error_code": row.get("error_code"),
            "request_id": row.get("request_id"),
            "upstream_request_id": row.get("upstream_request_id"),
            "usage": usage,
            "cancel_requested": bool(row.get("cancel_requested")),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
        }

    def context_messages(self, owner_telegram_id: int, conversation_id: str) -> list[dict[str, str]]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._conversation_id(conversation_id)
        with self.connect() as connection:
            self._owned_conversation(connection, owner, clean_id)
            rows = connection.execute(
                """SELECT t.submitted_source, a.output_text
                   FROM ai_turns AS t
                   LEFT JOIN ai_attempts AS a ON a.turn_id = t.id
                     AND a.status = 'completed'
                   WHERE t.conversation_id = ? AND t.owner_telegram_id = ?
                   ORDER BY t.sequence ASC""",
                (clean_id, owner),
            ).fetchall()
        result: list[dict[str, str]] = []
        for row in rows:
            value = _row_dict(row)
            result.append({"role": "user", "content": str(value["submitted_source"])})
            if value.get("output_text") is not None:
                result.append({"role": "assistant", "content": str(value["output_text"])})
        return result[-MAX_CONTEXT_MESSAGES:]

    def create_turn(
        self,
        owner_telegram_id: int,
        conversation_id: str,
        *,
        source: str,
        mode: str,
        direction: str | None,
        model_id: str,
        context: Sequence[Mapping[str, Any]],
        client_submission_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._conversation_id(conversation_id)
        clean_source = _text(source, name="message", maximum=MAX_SOURCE_CHARS)
        clean_mode = _text(mode, name="mode", maximum=40)
        clean_model = _text(model_id, name="model_id", maximum=200)
        clean_direction = _text(direction, name="direction", maximum=40, required=False) or None
        if not isinstance(context, Sequence) or isinstance(context, (str, bytes)):
            raise ConversationStoreError("context must be a message list")
        if len(context) > MAX_CONTEXT_MESSAGES:
            raise ConversationStoreError("context is too large")
        context_copy: list[dict[str, Any]] = []
        for index, item in enumerate(context):
            if not isinstance(item, Mapping):
                raise ConversationStoreError(f"context[{index}] is invalid")
            role = _text(item.get("role"), name=f"context[{index}].role", maximum=20)
            content = _text(item.get("content"), name=f"context[{index}].content", maximum=MAX_SOURCE_CHARS)
            if role not in {"user", "assistant", "system"}:
                raise ConversationStoreError(f"context[{index}].role is invalid")
            context_copy.append({"role": role, "content": content})
        clean_client_id = None
        if client_submission_id is not None:
            clean_client_id = _text(
                client_submission_id, name="client_submission_id", maximum=160
            )
        clean_request_id = _text(request_id, name="request_id", maximum=160) if request_id else _id("req")
        now = _now()
        with self.connect() as connection:
            if self.dialect == "sqlite":
                # Serialize sequence allocation and the idempotency lookup.
                # WAL still allows readers, while this short write transaction
                # prevents two browser retries from selecting the same number.
                connection.execute("BEGIN IMMEDIATE")
            self._owned_conversation(connection, owner, clean_id)
            if self.dialect == "postgres":
                connection.execute(
                    "SELECT id FROM ai_conversations WHERE id = ? AND owner_telegram_id = ? FOR UPDATE",
                    (clean_id, owner),
                ).fetchone()
            if clean_client_id is not None:
                existing = connection.execute(
                    """SELECT t.id AS turn_id, a.id AS attempt_id
                       FROM ai_turns AS t JOIN ai_attempts AS a ON a.turn_id = t.id
                       WHERE t.conversation_id = ? AND t.owner_telegram_id = ?
                         AND t.client_submission_id = ?
                       ORDER BY a.created_at DESC LIMIT 1""",
                    (clean_id, owner, clean_client_id),
                ).fetchone()
                if existing is not None:
                    return self.attempt(owner, str(_row_dict(existing)["attempt_id"])), False
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM ai_turns WHERE conversation_id = ?",
                (clean_id,),
            ).fetchone()
            sequence = int(_row_dict(sequence_row)["next_sequence"])
            turn_id = _id("turn")
            attempt_id = _id("attempt")
            connection.execute(
                """INSERT INTO ai_turns
                   (id, conversation_id, owner_telegram_id, sequence, mode, direction,
                    submitted_source, context_json, client_submission_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    turn_id, clean_id, owner, sequence, clean_mode, clean_direction,
                    clean_source, json.dumps(context_copy, ensure_ascii=False),
                    clean_client_id, now,
                ),
            )
            connection.execute(
                """INSERT INTO ai_attempts
                   (id, turn_id, conversation_id, owner_telegram_id, model_id,
                    status, request_id, created_at, started_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                (attempt_id, turn_id, clean_id, owner, clean_model, clean_request_id, now, now),
            )
            connection.execute(
                "UPDATE ai_conversations SET updated_at = ? WHERE id = ? AND owner_telegram_id = ?",
                (now, clean_id, owner),
            )
        return self.attempt(owner, attempt_id), True

    def attempt(self, owner_telegram_id: int, attempt_id: str) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._attempt_id(attempt_id)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.*, t.sequence, t.mode, t.direction, t.submitted_source,
                          t.conversation_id
                   FROM ai_attempts AS a JOIN ai_turns AS t ON t.id = a.turn_id
                   JOIN ai_conversations AS c ON c.id = t.conversation_id
                   WHERE a.id = ? AND a.owner_telegram_id = ? AND c.status = 'active'""",
                (clean_id, owner),
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError("attempt not found")
        value = _row_dict(row)
        return {
            "id": str(value["id"]),
            "turn_id": str(value["turn_id"]),
            "conversation_id": str(value["conversation_id"]),
            "sequence": int(value["sequence"]),
            "mode": str(value["mode"]),
            "direction": value.get("direction"),
            "submitted_source": str(value["submitted_source"]),
            **self._attempt_payload(value),
        }

    def complete_attempt(
        self,
        owner_telegram_id: int,
        attempt_id: str,
        *,
        output_text: str,
        usage: Mapping[str, Any] | None = None,
        upstream_request_id: str | None = None,
    ) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._attempt_id(attempt_id)
        clean_output = _text(output_text, name="output_text", maximum=MAX_SOURCE_CHARS * 4)
        now = _now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE ai_attempts
                   SET status = 'completed', output_text = ?, usage_json = ?,
                       upstream_request_id = ?, finished_at = ?
                   WHERE id = ? AND owner_telegram_id = ? AND status = 'running'
                     AND conversation_id IN
                         (SELECT id FROM ai_conversations WHERE status = 'active')""",
                (
                    clean_output,
                    json.dumps(dict(usage), ensure_ascii=False) if isinstance(usage, Mapping) else None,
                    _text(upstream_request_id, name="upstream_request_id", maximum=200, required=False) or None
                    if upstream_request_id is not None else None,
                    now,
                    clean_id,
                    owner,
                ),
            )
        return self.attempt(owner, clean_id)

    def fail_attempt(
        self,
        owner_telegram_id: int,
        attempt_id: str,
        *,
        error_code: str,
    ) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._attempt_id(attempt_id)
        clean_error = _text(error_code, name="error_code", maximum=80)
        with self.connect() as connection:
            connection.execute(
                """UPDATE ai_attempts
                   SET status = 'failed', error_code = ?, finished_at = ?
                   WHERE id = ? AND owner_telegram_id = ? AND status = 'running'""",
                (clean_error, _now(), clean_id, owner),
            )
        return self.attempt(owner, clean_id)

    def cancel_attempt(self, owner_telegram_id: int, attempt_id: str) -> dict[str, Any]:
        owner = self._owner(owner_telegram_id)
        clean_id = self._attempt_id(attempt_id)
        with self.connect() as connection:
            connection.execute(
                """UPDATE ai_attempts
                   SET status = CASE WHEN status = 'running' THEN 'cancelled' ELSE status END,
                       cancel_requested = CASE WHEN status = 'running' THEN 1 ELSE cancel_requested END,
                       finished_at = CASE WHEN status = 'running' THEN ? ELSE finished_at END
                   WHERE id = ? AND owner_telegram_id = ?""",
                (_now(), clean_id, owner),
            )
        return self.attempt(owner, clean_id)

    def delete_conversation(self, owner_telegram_id: int, conversation_id: str) -> bool:
        owner = self._owner(owner_telegram_id)
        clean_id = self._conversation_id(conversation_id)
        now = _now()
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE ai_conversations
                   SET status = 'deleted', deleted_at = ?, updated_at = ?
                   WHERE id = ? AND owner_telegram_id = ? AND status = 'active'""",
                (now, now, clean_id, owner),
            )
            connection.execute(
                """UPDATE ai_attempts SET status = 'cancelled', cancel_requested = 1,
                       finished_at = COALESCE(finished_at, ?)
                   WHERE conversation_id = ? AND owner_telegram_id = ? AND status = 'running'""",
                (now, clean_id, owner),
            )
        return result.rowcount == 1


__all__ = [
    "AIConversationStore",
    "AI_CONVERSATION_MIGRATIONS",
    "ConversationNotFoundError",
    "ConversationStoreError",
]
