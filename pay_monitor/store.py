"""Idempotent local evidence storage, correlation, and order suggestions."""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from persistence import open_sqlite_connection

from .events import NormalizedObservation


UTC = timezone.utc


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    inserted: bool
    correlation_id: str
    review_state: str
    suggested_order_id: str | None
    reconciliation_request_id: str | None = None


@dataclass(frozen=True)
class HistoryItemResult:
    is_new: bool
    detail_was_checked: bool
    lookup_count: int
    provider_reference_hash: str | None = None
    transaction_time: str | None = None


MIN_ROUTINE_INTERVAL_SECONDS = 60
MAX_ROUTINE_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_ROUTINE_INTERVAL_SECONDS = 5 * 60


class ObservationStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS device_observations (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    amount_minor INTEGER,
                    currency TEXT,
                    provider_reference TEXT,
                    normalized_reference TEXT,
                    transaction_time TEXT,
                    observed_at TEXT NOT NULL,
                    evidence_sha256 TEXT,
                    confidence REAL NOT NULL,
                    flags_json TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    raw_envelope BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(provider, source, source_event_id)
                );
                CREATE INDEX IF NOT EXISTS device_observations_reference
                    ON device_observations(provider, normalized_reference);
                CREATE INDEX IF NOT EXISTS device_observations_correlation
                    ON device_observations(correlation_id, observed_at);
                CREATE TABLE IF NOT EXISTS device_order_suggestions (
                    event_id TEXT PRIMARY KEY REFERENCES device_observations(event_id),
                    order_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_history_items (
                    provider TEXT NOT NULL,
                    account_hash TEXT NOT NULL,
                    item_fingerprint TEXT NOT NULL,
                    provider_reference_hash TEXT,
                    transaction_time TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    lookup_count INTEGER NOT NULL DEFAULT 1,
                    detail_checked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(provider, account_hash, item_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS device_history_items_reference
                    ON device_history_items(provider, provider_reference_hash);
                CREATE INDEX IF NOT EXISTS device_history_items_time
                    ON device_history_items(provider, account_hash, transaction_time);
                CREATE TABLE IF NOT EXISTS device_reconciliation_requests (
                    request_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    account_hash TEXT,
                    since_time TEXT,
                    after_reference_hash TEXT,
                    target_reference_hash TEXT,
                    max_items INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS device_reconciliation_requests_queue
                    ON device_reconciliation_requests(status, created_at);
                CREATE TABLE IF NOT EXISTS device_monitor_settings (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    device_id TEXT,
                    device_interval_seconds INTEGER NOT NULL,
                    server_interval_seconds INTEGER,
                    device_updated_at TEXT,
                    server_updated_at TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO device_monitor_settings
                    (singleton_id, device_interval_seconds, updated_at)
                VALUES (1, 300, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(singleton_id) DO NOTHING;
                """
            )

    def get_monitor_settings(self) -> dict[str, object]:
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            row = connection.execute(
                """SELECT device_id, device_interval_seconds, server_interval_seconds,
                          device_updated_at, server_updated_at, updated_at
                   FROM device_monitor_settings WHERE singleton_id = 1"""
            ).fetchone()
        if row is None:
            raise RuntimeError("monitor settings are not initialized")
        result = dict(row)
        override = result["server_interval_seconds"]
        result["effective_interval_seconds"] = (
            int(override) if override is not None else int(result["device_interval_seconds"])
        )
        result["effective_source"] = "server" if override is not None else "device"
        result["passive_capture"] = "immediate"
        return result

    def update_monitor_settings(
        self,
        *,
        source: str,
        interval_seconds: int | None,
        device_id: str | None = None,
    ) -> dict[str, object]:
        if source not in {"device", "server"}:
            raise ValueError("settings source must be device or server")
        if source == "device" and interval_seconds is None:
            raise ValueError("device interval is required")
        if interval_seconds is not None and not (
            MIN_ROUTINE_INTERVAL_SECONDS <= interval_seconds <= MAX_ROUTINE_INTERVAL_SECONDS
        ):
            raise ValueError("routine interval must be between 60 and 86400 seconds")
        if device_id is not None and (
            len(device_id) != 64
            or any(char not in "0123456789abcdef" for char in device_id)
        ):
            raise ValueError("device_id must be a SHA-256 fingerprint")
        now = datetime.now(UTC).isoformat()
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            if source == "device":
                current = connection.execute(
                    "SELECT device_id FROM device_monitor_settings WHERE singleton_id = 1"
                ).fetchone()
                if current and current["device_id"] and current["device_id"] != device_id:
                    raise ValueError("settings device does not match the enrolled device")
                connection.execute(
                    """UPDATE device_monitor_settings
                       SET device_id = COALESCE(?, device_id),
                           device_interval_seconds = ?, device_updated_at = ?, updated_at = ?
                       WHERE singleton_id = 1""",
                    (device_id, interval_seconds, now, now),
                )
            else:
                connection.execute(
                    """UPDATE device_monitor_settings
                       SET server_interval_seconds = ?, server_updated_at = ?, updated_at = ?
                       WHERE singleton_id = 1""",
                    (interval_seconds, now, now),
                )
        return self.get_monitor_settings()

    def ingest(
        self,
        observation: NormalizedObservation,
        raw_envelope: bytes,
        commerce_database: Path | None = None,
    ) -> IngestResult:
        correlation_id = self._correlation_id(observation)
        review_state = self._maximum_review_state(observation)
        created_at = datetime.now(UTC).isoformat()
        suggested_order = (
            self._suggest_order(observation, commerce_database) if commerce_database else None
        )
        reconciliation_request_id = None
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            before = connection.total_changes
            connection.execute(
                """INSERT INTO device_observations
                   (event_id, device_id, provider, package_name, app_version, source,
                    source_event_id, direction, status, amount_minor, currency,
                    provider_reference, normalized_reference, transaction_time, observed_at,
                    evidence_sha256, confidence, flags_json, correlation_id, review_state,
                    raw_envelope, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_id) DO NOTHING""",
                (
                    observation.event_id,
                    observation.device_id,
                    observation.provider,
                    observation.package_name,
                    observation.app_version,
                    observation.source,
                    observation.source_event_id,
                    observation.direction,
                    observation.status,
                    observation.amount_minor,
                    observation.currency,
                    observation.provider_reference,
                    _normalize_reference(observation.provider_reference),
                    observation.transaction_time,
                    observation.observed_at,
                    observation.evidence_sha256,
                    observation.confidence,
                    json.dumps(observation.flags),
                    correlation_id,
                    review_state,
                    raw_envelope,
                    created_at,
                ),
            )
            inserted = connection.total_changes > before
            if inserted and suggested_order:
                connection.execute(
                    """INSERT INTO device_order_suggestions
                       (event_id, order_id, reason, created_at) VALUES (?, ?, ?, ?)""",
                    (
                        observation.event_id,
                        suggested_order,
                        "unique_open_order_provider_amount_currency_match",
                        created_at,
                    ),
                )
            if inserted and observation.source in {"notification", "media"}:
                request_id = hashlib.sha256(
                    f"observation\0{observation.event_id}".encode("utf-8")
                ).hexdigest()
                since = observation.transaction_time or (
                    datetime.fromisoformat(observation.observed_at) - timedelta(minutes=10)
                ).isoformat()
                reference_hash = hash_reference(observation.provider_reference)
                connection.execute(
                    """INSERT INTO device_reconciliation_requests
                       (request_id, provider, account_hash, since_time,
                        after_reference_hash, target_reference_hash, max_items,
                        reason, status, result_json, created_at, updated_at)
                       VALUES (?, ?, NULL, ?, NULL, ?, 20, ?, 'pending', NULL, ?, ?)
                       ON CONFLICT(request_id) DO NOTHING""",
                    (
                        request_id,
                        observation.provider,
                        since,
                        reference_hash,
                        f"{observation.source}_candidate",
                        created_at,
                        created_at,
                    ),
                )
                reconciliation_request_id = request_id
        return IngestResult(
            observation.event_id,
            inserted,
            correlation_id,
            review_state,
            suggested_order,
            reconciliation_request_id,
        )

    def remember_history_item(
        self,
        *,
        provider: str,
        account_hash: str | None,
        item_fingerprint: str,
        provider_reference_hash: str | None = None,
        transaction_time: str | None = None,
        detail_checked: bool = False,
    ) -> HistoryItemResult:
        now = datetime.now(UTC).isoformat()
        account = account_hash or ""
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            existing = connection.execute(
                """SELECT detail_checked, lookup_count, provider_reference_hash,
                          transaction_time FROM device_history_items
                   WHERE provider = ? AND account_hash = ? AND item_fingerprint = ?""",
                (provider, account, item_fingerprint),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO device_history_items
                       (provider, account_hash, item_fingerprint, provider_reference_hash,
                        transaction_time, first_seen_at, last_seen_at, lookup_count,
                        detail_checked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        provider,
                        account,
                        item_fingerprint,
                        provider_reference_hash,
                        transaction_time,
                        now,
                        now,
                        int(detail_checked),
                    ),
                )
                return HistoryItemResult(True, False, 1)
            prior_checked = bool(existing["detail_checked"])
            lookup_count = int(existing["lookup_count"]) + 1
            connection.execute(
                """UPDATE device_history_items
                   SET last_seen_at = ?, lookup_count = ?,
                       provider_reference_hash = COALESCE(?, provider_reference_hash),
                       transaction_time = COALESCE(?, transaction_time),
                       detail_checked = ?
                   WHERE provider = ? AND account_hash = ? AND item_fingerprint = ?""",
                (
                    now,
                    lookup_count,
                    provider_reference_hash,
                    transaction_time,
                    int(prior_checked or detail_checked),
                    provider,
                    account,
                    item_fingerprint,
                ),
            )
        return HistoryItemResult(
            False,
            prior_checked,
            lookup_count,
            provider_reference_hash or existing["provider_reference_hash"],
            transaction_time or existing["transaction_time"],
        )

    def create_reconciliation_request(
        self,
        *,
        provider: str,
        account_hash: str | None = None,
        since_time: str | None = None,
        after_reference_hash: str | None = None,
        target_reference_hash: str | None = None,
        max_items: int = 20,
        reason: str = "server_request",
        request_id: str | None = None,
    ) -> str:
        if provider not in {"kpay", "wavepay", "ayapay", "uabpay", "cbpay"}:
            raise ValueError("unsupported provider")
        if not 1 <= max_items <= 100:
            raise ValueError("max_items must be between 1 and 100")
        if since_time:
            _validated_time(since_time)
        for value in (account_hash, after_reference_hash, target_reference_hash):
            if value and (len(value) < 8 or any(char not in "0123456789abcdef" for char in value)):
                raise ValueError("request fingerprints must be lowercase hexadecimal")
        identifier = request_id or uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            connection.execute(
                """INSERT INTO device_reconciliation_requests
                   (request_id, provider, account_hash, since_time, after_reference_hash,
                    target_reference_hash, max_items, reason, status, result_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                   ON CONFLICT(request_id) DO NOTHING""",
                (
                    identifier,
                    provider,
                    account_hash,
                    since_time,
                    after_reference_hash,
                    target_reference_hash,
                    max_items,
                    reason[:80],
                    now,
                    now,
                ),
            )
        return identifier

    def list_reconciliation_requests(
        self, *, status: str = "pending", limit: int = 20
    ) -> list[dict[str, object]]:
        if status not in {"pending", "running", "completed", "blocked"}:
            raise ValueError("invalid request status")
        if not 1 <= limit <= 100:
            raise ValueError("invalid request limit")
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            rows = connection.execute(
                """SELECT request_id, provider, account_hash, since_time,
                          after_reference_hash, target_reference_hash, max_items,
                          reason, status, created_at, updated_at
                   FROM device_reconciliation_requests
                   WHERE status = ? ORDER BY created_at LIMIT ?""",
                (status, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_reconciliation_request(self) -> dict[str, object] | None:
        now = datetime.now(UTC)
        stale = (now - timedelta(minutes=15)).isoformat()
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            connection.execute(
                """UPDATE device_reconciliation_requests
                   SET status = 'pending', updated_at = ?
                   WHERE status = 'running' AND updated_at < ?""",
                (now.isoformat(), stale),
            )
            row = connection.execute(
                """SELECT request_id, provider, account_hash, since_time,
                          after_reference_hash, target_reference_hash, max_items,
                          reason, status, created_at, updated_at
                   FROM device_reconciliation_requests
                   WHERE status = 'pending' ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                """UPDATE device_reconciliation_requests
                   SET status = 'running', updated_at = ?
                   WHERE request_id = ? AND status = 'pending'""",
                (now.isoformat(), row["request_id"]),
            ).rowcount
            if changed != 1:
                return None
        result = dict(row)
        result["status"] = "running"
        return result

    def complete_reconciliation_request(
        self, request_id: str, *, status: str, result: dict[str, object]
    ) -> None:
        if status not in {"completed", "blocked"}:
            raise ValueError("invalid completion status")
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            connection.execute(
                """UPDATE device_reconciliation_requests
                   SET status = ?, result_json = ?, updated_at = ?
                   WHERE request_id = ?""",
                (
                    status,
                    json.dumps(result, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    request_id,
                ),
            )

    def defer_reconciliation_request(
        self, request_id: str, *, reason: str = "foreground_interrupted"
    ) -> None:
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            connection.execute(
                """UPDATE device_reconciliation_requests
                   SET status = 'pending', result_json = ?, updated_at = ?
                   WHERE request_id = ? AND status = 'running'""",
                (
                    json.dumps({"status": "interrupted", "reason": reason}),
                    datetime.now(UTC).isoformat(),
                    request_id,
                ),
            )

    def history_checkpoint(
        self, *, provider: str, account_hash: str | None = None
    ) -> dict[str, object]:
        with open_sqlite_connection(self.path, busy_timeout_ms=5000) as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS item_count, MAX(last_seen_at) AS last_seen_at,
                          MAX(transaction_time) AS latest_transaction_time
                   FROM device_history_items
                   WHERE provider = ? AND (? IS NULL OR account_hash = ?)""",
                (provider, account_hash, account_hash),
            ).fetchone()
        return dict(row)

    def _correlation_id(self, observation: NormalizedObservation) -> str:
        if observation.provider_reference:
            return f"{observation.provider}:ref:{_normalize_reference(observation.provider_reference)}"
        if observation.evidence_sha256:
            return f"{observation.provider}:sha:{observation.evidence_sha256}"
        if observation.amount_minor is not None and observation.currency:
            observed = datetime.fromisoformat(observation.observed_at)
            bucket = int(observed.timestamp()) // 60
            return (
                f"{observation.provider}:fallback:{observation.direction}:"
                f"{observation.amount_minor}:{observation.currency}:{bucket}"
            )
        return f"{observation.provider}:event:{observation.event_id}"

    @staticmethod
    def _maximum_review_state(observation: NormalizedObservation) -> str:
        if (
            observation.direction == "incoming"
            and observation.status == "successful"
            and observation.provider_reference
        ):
            return "corroborated"
        return "detected"

    @staticmethod
    def _suggest_order(observation: NormalizedObservation, database: Path) -> str | None:
        if (
            observation.direction != "incoming"
            or observation.status != "successful"
            or observation.amount_minor is None
            or not observation.currency
        ):
            return None
        if not database.is_file():
            return None
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with open_sqlite_connection(database, busy_timeout_ms=5000) as connection:
            rows = connection.execute(
                """SELECT id FROM orders
                   WHERE status IN ('awaiting_payment', 'payment_submitted')
                     AND amount_minor = ? AND UPPER(currency) = ? AND created_at >= ?
                     AND (selected_payment_provider IS NULL
                          OR LOWER(REPLACE(selected_payment_provider, ' ', '')) = ?)
                   ORDER BY created_at""",
                (
                    observation.amount_minor,
                    observation.currency.upper(),
                    cutoff,
                    observation.provider,
                ),
            ).fetchall()
        return str(rows[0]["id"]) if len(rows) == 1 else None


def _normalize_reference(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(value.split()).casefold()


def hash_reference(value: str | None) -> str | None:
    normalized = _normalize_reference(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def _validated_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid reconciliation time") from exc
    if parsed.tzinfo is None:
        raise ValueError("reconciliation time must include timezone")
    return parsed.astimezone(UTC).isoformat()
