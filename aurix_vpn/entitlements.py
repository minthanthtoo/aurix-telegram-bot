"""Free and trial entitlement domain service and compatibility value types."""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet
from ports import OutlineGateway
from repositories import RepositoryDatabase
from .connectivity_adapters import ConnectivityAdapterRegistry
from .identity import IdentityService

UTC = timezone.utc

# Warn once as the observed trailing-30-day allowance crosses these remaining
# percentages. Outline itself enforces the hard limit; these messages make the
# approaching cutoff visible before the key is removed.

PUBLIC_LIMIT_BYTES = 300 * 1024 * 1024


LIMIT_BYTES = PUBLIC_LIMIT_BYTES


TRIAL_LIMIT_BYTES = 3 * 1024**3


CLAIM_PERIOD = timedelta(hours=24)


TRIAL_PERIOD = timedelta(days=30)


GIVEAWAY_CODE = "100GBFREE"


GIVEAWAY_LIMIT_BYTES = 100_000_000_000


GIVEAWAY_PERIOD = timedelta(days=30)


GIVEAWAY_WINNER_LIMIT = 5


QUOTA_WARNING_THRESHOLDS = ((25, 0.25), (10, 0.10), (5, 0.05))

FREE_PROVISION_RETRY_DELAY = timedelta(seconds=30)
FREE_PROVISION_LEASE = timedelta(minutes=5)


def _outline_key_name(
    telegram_id: int,
    username: str | None,
    tier: str,
    duration: str,
    started_at: datetime,
) -> str:
    """Build a human-readable, non-secret Outline key name."""
    identity = (username or "").strip().lstrip("@") or str(telegram_id)
    identity = re.sub(r"[^A-Za-z0-9_-]+", "-", identity).strip("-_")
    identity = identity[:48] or str(telegram_id)
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{identity}-{tier}-{duration}-{timestamp}"[:128]


def _human_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024


def _human_decimal_bytes(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1000


def _new_id() -> str:
    return uuid.uuid4().hex


class OutlineError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimResult:
    access_url: str | None = None
    expires_at: datetime | None = None
    next_claim_at: datetime | None = None
    denied_reason: str | None = None


@dataclass(frozen=True)
class GiveawayResult:
    outcome: str
    code: str | None = None
    quota_bytes: int | None = None
    duration_days: int | None = None
    access_url: str | None = None
    expires_at: datetime | None = None
    winner_number: int | None = None
    remaining_slots: int = 0
    reason: str | None = None


class ClaimService:
    def __init__(
        self,
        database: RepositoryDatabase,
        outline: OutlineGateway,
        limit_bytes: int = LIMIT_BYTES,
        trial_limit_bytes: int = TRIAL_LIMIT_BYTES,
        connectivity: Any | None = None,
        adapter_registry: ConnectivityAdapterRegistry | None = None,
        access_url_key: str | bytes | None = None,
    ):
        self.database = database
        self.outline = outline
        self.limit_bytes = int(limit_bytes)
        self.trial_limit_bytes = int(trial_limit_bytes)
        self.connectivity = connectivity
        self.adapter_registry = adapter_registry or ConnectivityAdapterRegistry()
        self.identity = IdentityService(database)
        self._access_url_cipher = Fernet(access_url_key) if access_url_key else None

    def _client_for_endpoint(self, endpoint_id: str | None) -> OutlineGateway:
        if self.connectivity is None or not endpoint_id:
            return self.outline
        return self.connectivity.client(str(endpoint_id))

    def _select_endpoint(self, connection: Any, plan_code: str) -> tuple[str, OutlineGateway]:
        if self.connectivity is None:
            return "legacy-default", self.outline
        endpoint_id = self.connectivity.select_endpoint_for_plan(connection, plan_code)
        return endpoint_id, self.connectivity.client(endpoint_id)

    def _adapter_for_endpoint(self, endpoint_id: str, client: OutlineGateway) -> Any:
        return self.adapter_registry.for_route(
            {
                "route_id": f"outline:{str(endpoint_id)}",
                "endpoint_id": str(endpoint_id),
                "protocol": "outline",
            },
            client,
        )

    def _provision_route_key(
        self,
        endpoint_id: str,
        client: OutlineGateway,
        name: str,
        quota_bytes: int,
        *,
        external_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        adapter = self._adapter_for_endpoint(endpoint_id, client)
        intent: dict[str, Any] = {"name": name, "quota_bytes": int(quota_bytes)}
        if external_id:
            intent["external_id"] = str(external_id)
        grant = adapter.provision(
            {"route_id": f"outline:{str(endpoint_id)}", "endpoint_id": str(endpoint_id), "protocol": "outline"},
            intent,
        )
        return grant, {"id": grant["external_id"], "accessUrl": grant["access_url"], "name": name}

    def _project_free_generation(
        self,
        *,
        telegram_id: int,
        key_id: int,
        endpoint_id: str,
        key: dict[str, Any],
        quota_bytes: int,
        expires_at: datetime,
        grant: dict[str, Any],
        now: datetime,
    ) -> None:
        """Project a newly issued free credential into shared accounting.

        Free claims must remain usable when the commerce migrations are not
        installed yet (for example, an isolated legacy bot test).  Once the
        accounting tables exist, projection is attempted immediately; the
        legacy row remains the durable source for the next reconciliation pass
        if this optional projection encounters a transient failure.
        """
        try:
            with self.database.connect() as connection:
                if not all(
                    IdentityService._table_exists(connection, table)
                    for table in (
                        "credential_generations",
                        "quota_leases",
                        "entitlement_quota_ledger",
                    )
                ):
                    return
            entitlement_key = self.identity.ensure_free_entitlement(telegram_id, key_id, now=now.isoformat())
            ownership = str(grant.get("ownership") or "unknown")
            remote_state = "unknown" if ownership in {"unknown", "uncertain"} else "observed"
            usage_baseline_provenance = "new" if ownership == "owned" else "unknown"
            generation_id = self.identity.ensure_generation_for_credential(
                entitlement_key,
                str(endpoint_id),
                credential_id=f"free-key:{key_id}",
                external_id=str(key["id"]),
                protocol="outline",
                access_url_ciphertext=(
                    self._access_url_cipher.encrypt(str(grant["access_url"]).encode()).decode()
                    if self._access_url_cipher is not None and grant.get("access_url")
                    else None
                ),
                status="active",
                remote_state=remote_state,
                intent_key=grant.get("intent_key"),
                usage_baseline_provenance=usage_baseline_provenance,
                now=now.isoformat(),
            )
            self.identity.ensure_generation_lease(
                entitlement_key,
                generation_id,
                str(endpoint_id),
                int(quota_bytes),
                expires_at.isoformat(),
                now=now.isoformat(),
            )
        except Exception as exc:
            # The local key and remote credential are already durable.  Do not
            # delete the credential here; commerce startup/maintenance will
            # reconcile the generation without losing remote accountability.
            print(f"free entitlement accounting projection deferred: {type(exc).__name__}", file=sys.stderr)

    @staticmethod
    def _usage_map(metrics: dict[str, Any] | None, endpoint_id: str) -> dict[str, Any]:
        if not isinstance(metrics, dict):
            return {}
        scoped = metrics.get("byEndpoint")
        if isinstance(scoped, dict):
            value = scoped.get(endpoint_id, {})
            return value if isinstance(value, dict) else {}
        legacy = metrics.get("bytesTransferredByUserId", {})
        if isinstance(legacy, dict) and legacy:
            return legacy
        # Customer-facing callers historically pass the already-extracted map.
        return metrics if "byEndpoint" not in metrics and "errors" not in metrics else {}

    @staticmethod
    def _endpoint_observed(metrics: dict[str, Any] | None, endpoint_id: str) -> bool:
        if not isinstance(metrics, dict):
            return False
        scoped = metrics.get("byEndpoint")
        if isinstance(scoped, dict):
            return endpoint_id in scoped
        if not metrics or "errors" in metrics:
            return False
        return True

    @staticmethod
    def _lock_user(connection: Any, telegram_id: int) -> None:
        if connection.__class__.__name__ == "_PostgresConnection":
            connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? FOR UPDATE",
                (telegram_id,),
            ).fetchone()

    @staticmethod
    def _account_is_active_in_connection(connection: Any, telegram_id: int) -> bool:
        status = IdentityService.account_status_in_connection(connection, telegram_id)
        return status in (None, "active")

    @staticmethod
    def _has_active_promo_gift(
        connection: Any, telegram_id: int, now: datetime
    ) -> bool:
        """Return whether a live campaign and usable gift currently pause other plans."""
        now_text = now.astimezone(UTC).isoformat()
        return (
            connection.execute(
                """SELECT 1
                   FROM giveaway_claims g
                   JOIN giveaway_campaigns c ON c.code = g.campaign_code
                   JOIN keys k ON k.id = g.key_id
                   WHERE g.telegram_id = ?
                     AND c.active = 1
                     AND (c.starts_at IS NULL OR c.starts_at <= ?)
                     AND (c.ends_at IS NULL OR c.ends_at > ?)
                     AND k.status IN ('active', 'revoke_failed')
                     AND k.expires_at > ?
                     AND k.quota_reason IS NULL
                   LIMIT 1""",
                (telegram_id, now_text, now_text, now_text),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _campaign_window_start(campaign: Any, now: datetime) -> str:
        frequency = str(campaign["frequency"] or "campaign").lower()
        if frequency == "hourly":
            return now.replace(minute=0, second=0, microsecond=0).isoformat()
        if frequency == "daily":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return str(campaign["starts_at"] or campaign["created_at"])

    @staticmethod
    def _campaign_state(campaign: Any, now: datetime) -> str:
        if not bool(campaign["active"]):
            return "paused"
        starts_at = campaign["starts_at"]
        ends_at = campaign["ends_at"]
        if starts_at and now < datetime.fromisoformat(str(starts_at)).astimezone(UTC):
            return "scheduled"
        if ends_at and now >= datetime.fromisoformat(str(ends_at)).astimezone(UTC):
            return "ended"
        return "active"

    @staticmethod
    def _commerce_tables_exist(connection: Any) -> bool:
        if connection.__class__.__name__ == "_PostgresConnection":
            row = connection.execute(
                "SELECT to_regclass('public.orders') AS table_name"
            ).fetchone()
            return bool(row and row["table_name"])
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        return row is not None

    def giveaway_status(
        self,
        telegram_id: int,
        code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return campaign schedule/capacity and this user's durable gift state."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        normalized = str(code or "").strip().upper()
        with self.database.connect() as connection:
            if normalized:
                campaign = connection.execute(
                    "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
                ).fetchone()
            else:
                campaign = connection.execute(
                    """SELECT * FROM giveaway_campaigns
                       ORDER BY active DESC, COALESCE(updated_at, created_at) DESC
                       LIMIT 1"""
                ).fetchone()
            if campaign is None:
                return {
                    "exists": False,
                    "code": normalized or GIVEAWAY_CODE,
                    "active": False,
                    "campaign_state": "unavailable",
                    "winner": False,
                    "gift_active": False,
                    "access_lock_active": False,
                    "claimed_count": 0,
                    "window_claimed_count": 0,
                    "winner_limit": 0,
                    "remaining_slots": 0,
                }
            claim = connection.execute(
                """SELECT g.winner_number, g.claimed_at, k.expires_at, k.status,
                          k.quota_reason, k.data_limit_bytes
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
            total_claimed = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            window_start = self._campaign_window_start(campaign, current)
            window = connection.execute(
                """SELECT claimed_count FROM giveaway_windows
                   WHERE campaign_code = ? AND window_start = ?""",
                (campaign["code"], window_start),
            ).fetchone()
            reserved = connection.execute(
                """SELECT COUNT(*) AS n FROM giveaway_provisioning_jobs
                   WHERE campaign_code = ? AND window_start = ?
                     AND status IN ('pending', 'running')""",
                (campaign["code"], window_start),
            ).fetchone()["n"]
            provisioning = connection.execute(
                """SELECT status FROM giveaway_provisioning_jobs
                   WHERE campaign_code = ? AND telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
        frequency = str(campaign["frequency"] or "campaign")
        finalized_window_claimed = (
            int(window["claimed_count"])
            if window is not None
            else (total_claimed if frequency == "campaign" else 0)
        )
        window_claimed = finalized_window_claimed + int(reserved or 0)
        winner_limit = int(campaign["winner_limit"])
        state = self._campaign_state(campaign, current)
        gift_active = bool(
            claim is not None
            and claim["status"] in ("active", "revoke_failed")
            and not claim["quota_reason"]
            and datetime.fromisoformat(str(claim["expires_at"])).astimezone(UTC) > current
        )
        result: dict[str, Any] = {
            "exists": True,
            "code": str(campaign["code"]),
            "quota_bytes": int(campaign["quota_bytes"]),
            "duration_days": int(campaign["duration_days"]),
            "frequency": frequency,
            "starts_at": campaign["starts_at"],
            "ends_at": campaign["ends_at"],
            "campaign_state": state,
            "claimed_count": total_claimed,
            "window_claimed_count": window_claimed,
            "winner_limit": winner_limit,
            "remaining_slots": max(0, winner_limit - window_claimed),
            "active": state == "active",
            "winner": claim is not None,
            "provisioning": bool(
                provisioning is not None
                and str(provisioning["status"]) in {"pending", "running"}
            ),
            "provisioning_status": (
                str(provisioning["status"]) if provisioning is not None else None
            ),
            "gift_active": gift_active,
            "access_lock_active": state == "active" and gift_active,
        }
        if claim is not None:
            result.update(dict(claim))
        return result

    def configure_giveaway(
        self,
        *,
        code: str,
        quota_bytes: int,
        duration_days: int,
        winner_limit: int,
        frequency: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or update the single owner-selected promo season."""
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", normalized):
            raise ValueError("Promo code must be 3-32 letters, numbers, underscores, or hyphens")
        quota_bytes = int(quota_bytes)
        duration_days = int(duration_days)
        winner_limit = int(winner_limit)
        frequency = str(frequency).strip().lower()
        if not 1_000_000 <= quota_bytes <= 10_000_000_000_000:
            raise ValueError("Promo quota must be between 0.001 GB and 10,000 GB")
        if not 1 <= duration_days <= 365:
            raise ValueError("Promo duration must be between 1 and 365 days")
        if not 1 <= winner_limit <= 100_000:
            raise ValueError("Giveaway count must be between 1 and 100,000")
        if frequency not in {"campaign", "daily", "hourly"}:
            raise ValueError("Frequency must be campaign, daily, or hourly")
        starts_at = starts_at.astimezone(UTC)
        ends_at = ends_at.astimezone(UTC)
        if starts_at >= ends_at:
            raise ValueError("Promo end must be after its start")
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            existing = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?", (normalized,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO giveaway_campaigns
                       (code, quota_bytes, duration_days, winner_limit, claimed_count,
                        active, created_at, starts_at, ends_at, frequency, updated_at)
                       VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?)""",
                    (
                        normalized,
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        now_text,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                    ),
                )
            else:
                claim_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                        (normalized,),
                    ).fetchone()["n"]
                )
                max_window = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(claimed_count), 0) AS n
                           FROM giveaway_windows WHERE campaign_code = ?""",
                        (normalized,),
                    ).fetchone()["n"]
                )
                if claim_count:
                    immutable_changed = any(
                        (
                            int(existing["quota_bytes"]) != quota_bytes,
                            int(existing["duration_days"]) != duration_days,
                            str(existing["frequency"] or "campaign") != frequency,
                            str(existing["starts_at"] or "") != starts_at.isoformat(),
                        )
                    )
                    if immutable_changed:
                        raise ValueError(
                            "A claimed promo's quota, duration, frequency, and start are immutable; "
                            "create a new promo code for a new season"
                        )
                if winner_limit < max_window:
                    raise ValueError(
                        f"Giveaway count cannot be below {max_window} claims already made in a window"
                    )
                connection.execute(
                    """UPDATE giveaway_campaigns
                       SET quota_bytes = ?, duration_days = ?, winner_limit = ?, active = 1,
                           starts_at = ?, ends_at = ?, frequency = ?, updated_at = ?
                       WHERE code = ?""",
                    (
                        quota_bytes,
                        duration_days,
                        winner_limit,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                        frequency,
                        now_text,
                        normalized,
                    ),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ? AND active = 1",
                (now_text, normalized),
            )
        return self.giveaway_status(0, normalized, now=now)

    def set_giveaway_active(
        self, code: str, active: bool, now: datetime | None = None
    ) -> dict[str, Any]:
        normalized = str(code).strip().upper()
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            row = connection.execute(
                "SELECT code FROM giveaway_campaigns WHERE UPPER(code) = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise ValueError("Promo campaign not found")
            if active:
                connection.execute(
                    "UPDATE giveaway_campaigns SET active = 0, updated_at = ? WHERE code != ?",
                    (now_text, row["code"]),
                )
            connection.execute(
                "UPDATE giveaway_campaigns SET active = ?, updated_at = ? WHERE code = ?",
                (1 if active else 0, now_text, row["code"]),
            )
        return self.giveaway_status(0, str(row["code"]), now=now)

    def reconcile_giveaway_limits(self) -> int:
        """Converge already-issued remote promo keys to their stored exact quota."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.outline_key_id, k.endpoint_id, c.quota_bytes
                   FROM giveaway_claims g
                   JOIN giveaway_campaigns c ON c.code = g.campaign_code
                   JOIN keys k ON k.id = g.key_id
                   WHERE k.status IN ('active', 'revoke_failed')
                     AND k.quota_reason IS NULL"""
            ).fetchall()
        if not rows:
            return 0
        updated = 0
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(str(row["endpoint_id"]), []).append(row)
        for endpoint_id, endpoint_rows in grouped.items():
            client = self._client_for_endpoint(endpoint_id)
            remote = client.list_keys()
            items = remote.get("accessKeys", []) if isinstance(remote, dict) else []
            if not isinstance(items, list):
                raise OutlineError("Outline returned invalid access key data")
            existing_ids = {
                str(item.get("id"))
                for item in items
                if isinstance(item, dict) and item.get("id") is not None
            }
            for row in endpoint_rows:
                key_id = str(row["outline_key_id"])
                if key_id not in existing_ids:
                    continue
                client.set_data_limit(key_id, int(row["quota_bytes"]))
                updated += 1
        return updated

    def claim_giveaway(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
        code: str | None = None,
    ) -> GiveawayResult:
        """Reserve and issue one configured promotional entitlement."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        normalized = str(code or GIVEAWAY_CODE).strip().upper()
        job = self._prepare_giveaway_provision_job(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            code=normalized,
            now=current,
        )
        if isinstance(job, GiveawayResult):
            return job
        return self._execute_giveaway_provision_job(job, current)

    def _giveaway_window_counts(
        self, connection: Any, campaign: Any, window_start: str
    ) -> tuple[int, int, int]:
        """Return finalized, active-reservation, and remaining window slots."""
        window = connection.execute(
            """SELECT claimed_count FROM giveaway_windows
               WHERE campaign_code = ? AND window_start = ?""",
            (campaign["code"], window_start),
        ).fetchone()
        if window is None:
            initial_count = (
                int(campaign["claimed_count"])
                if str(campaign["frequency"] or "campaign") == "campaign"
                else 0
            )
            connection.execute(
                """INSERT INTO giveaway_windows
                   (campaign_code, window_start, claimed_count) VALUES (?, ?, ?)""",
                (campaign["code"], window_start, initial_count),
            )
            finalized = initial_count
        else:
            finalized = int(window["claimed_count"])
        reservations = int(
            connection.execute(
                """SELECT COUNT(*) AS n FROM giveaway_provisioning_jobs
                   WHERE campaign_code = ? AND window_start = ?
                     AND status IN ('pending', 'running')""",
                (campaign["code"], window_start),
            ).fetchone()["n"]
        )
        remaining = max(0, int(campaign["winner_limit"]) - finalized - reservations)
        return finalized, reservations, remaining

    @staticmethod
    def _giveaway_result_from_claim(
        campaign: Any, claim: Any, remaining_slots: int
    ) -> GiveawayResult:
        return GiveawayResult(
            "already_won",
            code=str(campaign["code"]),
            quota_bytes=int(campaign["quota_bytes"]),
            duration_days=int(campaign["duration_days"]),
            expires_at=datetime.fromisoformat(str(claim["expires_at"])).astimezone(UTC),
            winner_number=int(claim["winner_number"]),
            remaining_slots=max(0, int(remaining_slots)),
        )

    @staticmethod
    def _giveaway_pending_result(campaign: Any, remaining_slots: int) -> GiveawayResult:
        return GiveawayResult(
            "provisioning_pending",
            code=str(campaign["code"]),
            quota_bytes=int(campaign["quota_bytes"]),
            duration_days=int(campaign["duration_days"]),
            remaining_slots=max(0, int(remaining_slots)),
            reason="Your promo reservation is being provisioned; refresh shortly.",
        )

    def _prepare_giveaway_provision_job(
        self,
        *,
        telegram_id: int,
        first_name: str,
        username: str | None,
        code: str,
        now: datetime,
    ) -> str | GiveawayResult:
        """Reserve one winner slot before any provider call is made."""
        now_text = now.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )
            self._lock_user(connection, telegram_id)
            if not self._account_is_active_in_connection(connection, telegram_id):
                return GiveawayResult(
                    "ineligible",
                    reason="This account is not active.",
                )
            if code == GIVEAWAY_CODE:
                connection.execute(
                    """INSERT INTO giveaway_campaigns
                       (code, quota_bytes, duration_days, winner_limit, claimed_count, active,
                        created_at, frequency, updated_at)
                       VALUES (?, ?, 30, ?, 0, 1, ?, 'campaign', ?)
                       ON CONFLICT(code) DO NOTHING""",
                    (
                        GIVEAWAY_CODE,
                        GIVEAWAY_LIMIT_BYTES,
                        GIVEAWAY_WINNER_LIMIT,
                        now_text,
                        now_text,
                    ),
                )
            suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            campaign = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE UPPER(code) = ?" + suffix,
                (code,),
            ).fetchone()
            if campaign is None:
                return GiveawayResult("unavailable", reason="Promo code is invalid or unavailable.")

            window_start = self._campaign_window_start(campaign, now)
            finalized, reservations, remaining = self._giveaway_window_counts(
                connection, campaign, window_start
            )
            total_finalized = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM giveaway_claims WHERE campaign_code = ?",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            total_reservations = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM giveaway_provisioning_jobs
                       WHERE campaign_code = ? AND status IN ('pending', 'running')""",
                    (campaign["code"],),
                ).fetchone()["n"]
            )
            existing_claim = connection.execute(
                """SELECT g.winner_number, k.expires_at
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (campaign["code"], telegram_id),
            ).fetchone()
            if existing_claim is not None:
                return self._giveaway_result_from_claim(campaign, existing_claim, remaining)

            existing_job = connection.execute(
                """SELECT * FROM giveaway_provisioning_jobs
                   WHERE campaign_code = ? AND telegram_id = ?""" + suffix,
                (campaign["code"], telegram_id),
            ).fetchone()
            if existing_job is not None:
                status = str(existing_job["status"])
                if status == "done":
                    raise OutlineError("completed promo provisioning job lacks its claim")
                if status == "running" and existing_job["locked_at"]:
                    try:
                        lock_time = datetime.fromisoformat(
                            str(existing_job["locked_at"])
                        ).astimezone(UTC)
                    except ValueError:
                        lock_time = now - FREE_PROVISION_LEASE
                    if lock_time + FREE_PROVISION_LEASE > now:
                        return self._giveaway_pending_result(campaign, remaining)
                if status == "failed":
                    state = self._campaign_state(campaign, now)
                    if state != "active":
                        return GiveawayResult(
                            state,
                            code=str(campaign["code"]),
                            quota_bytes=int(campaign["quota_bytes"]),
                            duration_days=int(campaign["duration_days"]),
                            remaining_slots=remaining,
                        )
                    if remaining <= 0:
                        return GiveawayResult(
                            "full",
                            code=str(campaign["code"]),
                            quota_bytes=int(campaign["quota_bytes"]),
                            duration_days=int(campaign["duration_days"]),
                            remaining_slots=0,
                        )
                if status == "pending" and str(existing_job["next_attempt_at"]) > now_text:
                    connection.execute(
                        "UPDATE giveaway_provisioning_jobs SET next_attempt_at = ? WHERE id = ?",
                        (now_text, existing_job["id"]),
                    )
                else:
                    # Failed jobs release their reservation. Reassigning the
                    # winner number here preserves the capacity invariant when
                    # another account claimed the old slot during the outage.
                    next_number = total_finalized + total_reservations + 1
                    if status == "failed":
                        connection.execute(
                            """UPDATE giveaway_provisioning_jobs
                               SET status = 'pending', window_start = ?, winner_number = ?,
                                   quota_bytes = ?, duration_seconds = ?, first_name = ?,
                                   username = ?, next_attempt_at = ?, last_error = NULL
                               WHERE id = ?""",
                            (
                                window_start,
                                next_number,
                                int(campaign["quota_bytes"]),
                                int(campaign["duration_days"]) * 86400,
                                first_name[:128],
                                (username or "")[:64] or None,
                                now_text,
                                existing_job["id"],
                            ),
                        )
                    else:
                        connection.execute(
                            """UPDATE giveaway_provisioning_jobs
                               SET next_attempt_at = ?, first_name = ?, username = ?
                               WHERE id = ?""",
                            (now_text, first_name[:128], (username or "")[:64] or None, existing_job["id"]),
                        )
                return str(existing_job["id"])

            state = self._campaign_state(campaign, now)
            if state != "active":
                return GiveawayResult(
                    state,
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=remaining,
                )
            if remaining <= 0:
                return GiveawayResult(
                    "full",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    remaining_slots=0,
                )
            if self._commerce_tables_exist(connection):
                conflict = connection.execute(
                    """SELECT 1 FROM orders
                       WHERE telegram_id = ?
                         AND status IN ('awaiting_payment', 'payment_submitted')
                         AND COALESCE(refund_status, 'none') != 'refunded'
                       UNION ALL
                       SELECT 1 FROM subscriptions
                       WHERE telegram_id = ? AND status IN ('pending', 'active')
                       LIMIT 1""",
                    (telegram_id, telegram_id),
                ).fetchone()
                if conflict is not None:
                    return GiveawayResult(
                        "ineligible",
                        remaining_slots=remaining,
                        reason="An open or completed paid order already belongs to this account.",
                    )
            job_id = _new_id()
            connection.execute(
                """INSERT INTO giveaway_provisioning_jobs
                   (id, campaign_code, telegram_id, first_name, username, window_start,
                    winner_number, quota_bytes, duration_seconds, status, next_attempt_at,
                    external_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    job_id,
                    campaign["code"],
                    telegram_id,
                    first_name[:128],
                    (username or "")[:64] or None,
                    window_start,
                    total_finalized + total_reservations + 1,
                    int(campaign["quota_bytes"]),
                    int(campaign["duration_days"]) * 86400,
                    now_text,
                    f"aurix-promo-{job_id}",
                    now_text,
                ),
            )
        return job_id

    def _execute_giveaway_provision_job(
        self, job_id: str, now: datetime, *, notify: bool = False
    ) -> GiveawayResult:
        """Execute one reserved promo job outside the database transaction."""
        now_text = now.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            row = connection.execute(
                "SELECT * FROM giveaway_provisioning_jobs WHERE id = ?" + suffix,
                (job_id,),
            ).fetchone()
            if row is None:
                raise OutlineError("giveaway provisioning job does not exist")
            campaign = connection.execute(
                "SELECT * FROM giveaway_campaigns WHERE code = ?" + suffix,
                (row["campaign_code"],),
            ).fetchone()
            if campaign is None:
                raise OutlineError("giveaway provisioning job references missing campaign")
            claim = connection.execute(
                """SELECT g.winner_number, k.expires_at
                   FROM giveaway_claims g JOIN keys k ON k.id = g.key_id
                   WHERE g.campaign_code = ? AND g.telegram_id = ?""",
                (row["campaign_code"], row["telegram_id"]),
            ).fetchone()
            if claim is not None:
                _finalized, _reservations, remaining = self._giveaway_window_counts(
                    connection, campaign, self._campaign_window_start(campaign, now)
                )
                return self._giveaway_result_from_claim(campaign, claim, remaining)
            if str(row["status"]) == "done":
                raise OutlineError("completed promo provisioning job lacks its claim")
            if not self._account_is_active_in_connection(connection, int(row["telegram_id"])):
                connection.execute(
                    """UPDATE giveaway_provisioning_jobs
                          SET status = 'pending', locked_at = NULL,
                              next_attempt_at = ?, last_error = 'account is not active'
                        WHERE id = ? AND status != 'done'""",
                    ((now + FREE_PROVISION_RETRY_DELAY).isoformat(), job_id),
                )
                return GiveawayResult(
                    "ineligible",
                    code=str(campaign["code"]),
                    quota_bytes=int(campaign["quota_bytes"]),
                    duration_days=int(campaign["duration_days"]),
                    reason="This account is not active.",
                )
            if str(row["status"]) == "running" and row["locked_at"]:
                try:
                    lock_time = datetime.fromisoformat(str(row["locked_at"])).astimezone(UTC)
                except ValueError:
                    lock_time = now - FREE_PROVISION_LEASE
                if lock_time + FREE_PROVISION_LEASE > now:
                    return self._giveaway_pending_result(campaign, 0)
            if str(row["status"]) not in {"pending", "failed", "running"}:
                return self._giveaway_pending_result(campaign, 0)
            if str(row["next_attempt_at"]) > now_text and str(row["status"]) != "running":
                return self._giveaway_pending_result(campaign, 0)
            endpoint_id = str(row["endpoint_id"] or "")
            endpoint_client = None
            if endpoint_id:
                endpoint_client = self._client_for_endpoint(endpoint_id)
            else:
                endpoint_id, endpoint_client = self._select_endpoint(
                    connection, f"PROMO:{row['campaign_code']}"
                )
            connection.execute(
                """UPDATE giveaway_provisioning_jobs
                   SET status = 'running', attempts = attempts + 1,
                       locked_at = ?, endpoint_id = ?, last_error = NULL
                   WHERE id = ?""",
                (now_text, endpoint_id, job_id),
            )
            job = dict(row)
            job["endpoint_id"] = endpoint_id
            job["attempts"] = int(row["attempts"] or 0) + 1

        name = _outline_key_name(
            int(job["telegram_id"]),
            job.get("username"),
            f"PROMO-{job['campaign_code']}",
            f"{int(job['duration_seconds']) // 86400}day",
            now,
        )
        grant: dict[str, Any] | None = None
        try:
            grant, key = self._provision_route_key(
                endpoint_id,
                endpoint_client,
                name,
                int(job["quota_bytes"]),
                external_id=str(job["external_id"] or f"aurix-promo-{job_id}"),
            )
            if not grant.get("external_id"):
                raise OutlineError("promo provisioning grant lacks external id")
            expires_at = now + timedelta(seconds=int(job["duration_seconds"]))
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                key_row = connection.execute(
                    "SELECT id, expires_at FROM keys WHERE endpoint_id = ? AND outline_key_id = ?",
                    (endpoint_id, str(key["id"])),
                ).fetchone()
                if key_row is None:
                    connection.execute(
                        """INSERT INTO keys
                           (telegram_id, outline_key_id, endpoint_id, key_type, created_at,
                            expires_at, data_limit_bytes, status)
                           VALUES (?, ?, ?, 'monthly_trial', ?, ?, ?, 'active')""",
                        (
                            int(job["telegram_id"]),
                            str(key["id"]),
                            endpoint_id,
                            now_text,
                            expires_at.isoformat(),
                            int(job["quota_bytes"]),
                        ),
                    )
                    key_row = connection.execute(
                        "SELECT id, expires_at FROM keys WHERE endpoint_id = ? AND outline_key_id = ?",
                        (endpoint_id, str(key["id"])),
                    ).fetchone()
                    if self.connectivity is not None:
                        self.connectivity.assign_free_key(
                            connection,
                            endpoint_id=endpoint_id,
                            free_key_id=int(key_row["id"]),
                            plan_code=f"PROMO:{job['campaign_code']}",
                            quota_bytes=int(job["quota_bytes"]),
                            now=now,
                        )
                key_id = int(key_row["id"])
                if key_row["expires_at"]:
                    expires_at = datetime.fromisoformat(str(key_row["expires_at"])).astimezone(UTC)
                claim = connection.execute(
                    """SELECT winner_number FROM giveaway_claims
                       WHERE campaign_code = ? AND telegram_id = ?""",
                    (job["campaign_code"], job["telegram_id"]),
                ).fetchone()
                if claim is None:
                    connection.execute(
                        """INSERT INTO giveaway_claims
                           (campaign_code, telegram_id, key_id, winner_number, claimed_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            job["campaign_code"],
                            int(job["telegram_id"]),
                            key_id,
                            int(job["winner_number"]),
                            now_text,
                        ),
                    )
                    connection.execute(
                        """UPDATE giveaway_windows SET claimed_count = claimed_count + 1
                           WHERE campaign_code = ? AND window_start = ?
                             AND claimed_count < ?""",
                        (
                            job["campaign_code"],
                            job["window_start"],
                            int(campaign["winner_limit"]),
                        ),
                    )
                    connection.execute(
                        """UPDATE giveaway_campaigns
                           SET claimed_count = CASE
                                   WHEN claimed_count < winner_limit THEN claimed_count + 1
                                   ELSE claimed_count
                               END,
                               updated_at = ?
                           WHERE code = ?""",
                        (now_text, job["campaign_code"]),
                    )
                connection.execute(
                    """UPDATE giveaway_provisioning_jobs
                       SET status = 'done', locked_at = NULL, key_id = ?,
                           external_id = ?, completed_at = ?, last_error = NULL
                       WHERE id = ?""",
                    (key_id, str(key["id"]), now_text, job_id),
                )
                if notify and self._access_url_cipher is not None:
                    encrypted_url = self._access_url_cipher.encrypt(
                        str(key["accessUrl"]).encode("utf-8")
                    ).decode("ascii")
                    connection.execute(
                        """INSERT INTO notifications
                           (id, dedupe_key, telegram_id, kind, text,
                            access_url_ciphertext, status, next_attempt_at, created_at)
                           VALUES (?, ?, ?, 'key_delivery', ?, ?, 'pending', ?, ?)
                           ON CONFLICT(dedupe_key) DO NOTHING""",
                        (
                            _new_id(),
                            f"promo-provision:{job_id}",
                            int(job["telegram_id"]),
                            "Your AuriX promo key is ready. It is attached below.",
                            encrypted_url,
                            now_text,
                            now_text,
                        ),
                    )
            self._project_free_generation(
                telegram_id=int(job["telegram_id"]),
                key_id=key_id,
                endpoint_id=endpoint_id,
                key=key,
                quota_bytes=int(job["quota_bytes"]),
                expires_at=expires_at,
                grant=grant,
                now=now,
            )
            status = self.giveaway_status(
                int(job["telegram_id"]), str(job["campaign_code"]), now=now
            )
            return GiveawayResult(
                "won",
                code=str(job["campaign_code"]),
                quota_bytes=int(job["quota_bytes"]),
                duration_days=int(job["duration_seconds"]) // 86400,
                access_url=str(key["accessUrl"]),
                expires_at=expires_at,
                winner_number=int(job["winner_number"]),
                remaining_slots=int(status.get("remaining_slots", 0)),
            )
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE giveaway_provisioning_jobs
                       SET status = 'failed', locked_at = NULL,
                           next_attempt_at = ?, last_error = ?
                       WHERE id = ? AND status = 'running'""",
                    (
                        (now + FREE_PROVISION_RETRY_DELAY).isoformat(),
                        type(exc).__name__,
                        job_id,
                    ),
                )
            raise

    def process_giveaway_provisioning(
        self, now: datetime | None = None, limit: int = 10
    ) -> int:
        """Resume reserved promo issuance after a crash or provider outage."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM giveaway_provisioning_jobs
                   WHERE (status IN ('pending', 'failed') AND next_attempt_at <= ?)
                      OR (status = 'running' AND locked_at <= ?)
                   ORDER BY created_at LIMIT ?""",
                (
                    now_text,
                    (current - FREE_PROVISION_LEASE).isoformat(),
                    max(1, min(int(limit), 50)),
                ),
            ).fetchall()
        completed = 0
        for row in rows:
            try:
                result = self._execute_giveaway_provision_job(
                    str(row["id"]), current, notify=True
                )
                if result.outcome == "won" and result.access_url:
                    completed += 1
            except Exception as exc:
                print(f"giveaway provisioning retry error: {type(exc).__name__}", file=sys.stderr)
        return completed

    def track_user(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> None:
        now_text = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )

    @staticmethod
    def _free_job_pending() -> ClaimResult:
        return ClaimResult(denied_reason="provisioning_pending")

    def _prepare_free_provision_job(
        self,
        *,
        telegram_id: int,
        first_name: str,
        username: str | None,
        plan_code: str,
        key_type: str,
        quota_bytes: int,
        duration: timedelta,
        now: datetime,
    ) -> str | ClaimResult:
        """Persist free issuance intent before touching a provider.

        The legacy user timestamp is advanced only after the job has been
        finalized.  A pending or failed job is therefore the durable retry
        identity for the next request, while a second concurrent request sees
        a bounded pending result instead of opening another provider call.
        """
        now_text = now.isoformat()
        claim_column = "last_claim_at" if key_type == "daily_free" else "trial_claimed_at"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """INSERT INTO users (telegram_id, first_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       first_name = excluded.first_name,
                       username = excluded.username""",
                (telegram_id, first_name[:128], (username or "")[:64] or None, now_text),
            )
            self._lock_user(connection, telegram_id)
            if not self._account_is_active_in_connection(connection, telegram_id):
                return ClaimResult(denied_reason="account_inactive")
            user = connection.execute(
                f"SELECT {claim_column} FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if self._has_active_promo_gift(connection, telegram_id, now):
                return ClaimResult(denied_reason="active_promo")
            claimed_at = user[claim_column]
            if claimed_at:
                next_claim = datetime.fromisoformat(str(claimed_at)) + duration
                if now < next_claim:
                    return ClaimResult(next_claim_at=next_claim)
            existing = connection.execute(
                """SELECT * FROM free_provisioning_jobs
                   WHERE telegram_id = ? AND plan_code = ?
                     AND status IN ('pending', 'running', 'failed')
                   ORDER BY created_at DESC LIMIT 1""",
                (telegram_id, plan_code),
            ).fetchone()
            if existing is not None:
                locked_at = existing["locked_at"]
                if str(existing["status"]) == "running" and locked_at:
                    try:
                        lock_time = datetime.fromisoformat(str(locked_at)).astimezone(UTC)
                    except ValueError:
                        lock_time = now - FREE_PROVISION_LEASE
                    if lock_time + FREE_PROVISION_LEASE > now:
                        return self._free_job_pending()
                if str(existing["status"]) == "failed":
                    # A user retry is an explicit request and may retry a
                    # failed provider call immediately; scheduled maintenance
                    # still honors the persisted backoff.
                    connection.execute(
                        "UPDATE free_provisioning_jobs SET next_attempt_at = ? WHERE id = ?",
                        (now_text, existing["id"]),
                    )
                return str(existing["id"])
            job_id = _new_id()
            connection.execute(
                """INSERT INTO free_provisioning_jobs
                   (id, telegram_id, plan_code, key_type, first_name, username,
                    quota_bytes, duration_seconds, status, next_attempt_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    job_id,
                    telegram_id,
                    plan_code,
                    key_type,
                    first_name[:128],
                    (username or "")[:64] or None,
                    int(quota_bytes),
                    int(duration.total_seconds()),
                    now_text,
                    now_text,
                ),
            )
        return job_id

    def _execute_free_provision_job(
        self, job_id: str, now: datetime, *, notify: bool = False
    ) -> ClaimResult:
        """Claim one durable free job, execute it, and finalize its projection."""
        now_text = now.isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            suffix = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
            row = connection.execute(
                "SELECT * FROM free_provisioning_jobs WHERE id = ?" + suffix,
                (job_id,),
            ).fetchone()
            if row is None:
                raise OutlineError("free provisioning job does not exist")
            if str(row["status"]) == "done":
                key = connection.execute(
                    "SELECT expires_at FROM keys WHERE id = ?", (row["key_id"],)
                ).fetchone()
                if key is None:
                    raise OutlineError("completed free provisioning job lacks its key")
                return ClaimResult(
                    access_url=None,
                    expires_at=datetime.fromisoformat(str(key["expires_at"])),
                    denied_reason="provisioning_completed",
                )
            if not self._account_is_active_in_connection(
                connection, int(row["telegram_id"])
            ):
                connection.execute(
                    """UPDATE free_provisioning_jobs
                          SET status = 'pending', locked_at = NULL,
                              next_attempt_at = ?, last_error = 'account is not active'
                        WHERE id = ? AND status != 'done'""",
                    ((now + FREE_PROVISION_RETRY_DELAY).isoformat(), job_id),
                )
                return ClaimResult(denied_reason="account_inactive")
            if str(row["status"]) == "running" and row["locked_at"]:
                try:
                    lock_time = datetime.fromisoformat(str(row["locked_at"])).astimezone(UTC)
                except ValueError:
                    lock_time = now - FREE_PROVISION_LEASE
                if lock_time + FREE_PROVISION_LEASE > now:
                    return self._free_job_pending()
            if str(row["status"]) not in {"pending", "failed", "running"}:
                return self._free_job_pending()
            if str(row["next_attempt_at"]) > now_text and str(row["status"]) != "running":
                return self._free_job_pending()
            endpoint_id = str(row["endpoint_id"] or "")
            endpoint_client = None
            if endpoint_id:
                endpoint_client = self._client_for_endpoint(endpoint_id)
            else:
                endpoint_id, endpoint_client = self._select_endpoint(
                    connection, str(row["plan_code"])
                )
            connection.execute(
                """UPDATE free_provisioning_jobs
                   SET status = 'running', attempts = attempts + 1,
                       locked_at = ?, endpoint_id = ?, last_error = NULL
                   WHERE id = ?""",
                (now_text, endpoint_id, job_id),
            )
            job = dict(row)
            job["endpoint_id"] = endpoint_id
            job["attempts"] = int(row["attempts"] or 0) + 1

        route = {
            "route_id": f"outline:{endpoint_id}",
            "endpoint_id": endpoint_id,
            "protocol": "outline",
        }
        name = _outline_key_name(
            int(job["telegram_id"]),
            job.get("username"),
            str(job["plan_code"]),
            "24hr" if str(job["key_type"]) == "daily_free" else "30day",
            now,
        )
        grant: dict[str, Any] | None = None
        try:
            grant, key = self._provision_route_key(
                endpoint_id,
                endpoint_client,
                name,
                int(job["quota_bytes"]),
                external_id=f"aurix-free-{job_id}",
            )
            # The adapter can use this stable id when its provider supports
            # deterministic creation; the provider backends already preserve
            # the same idempotency boundary for Xray/Hysteria2.
            if not grant.get("external_id"):
                raise OutlineError("free provisioning grant lacks external id")
            expires_at = now + timedelta(seconds=int(job["duration_seconds"]))
            with self.database.connect() as connection:
                self.database.begin_write(connection)
                key_row = connection.execute(
                    "SELECT id, expires_at FROM keys WHERE endpoint_id = ? AND outline_key_id = ?",
                    (endpoint_id, str(key["id"])),
                ).fetchone()
                if key_row is None:
                    connection.execute(
                        """INSERT INTO keys
                           (telegram_id, outline_key_id, endpoint_id, key_type, created_at,
                            expires_at, data_limit_bytes, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
                        (
                            int(job["telegram_id"]),
                            str(key["id"]),
                            endpoint_id,
                            str(job["key_type"]),
                            now_text,
                            expires_at.isoformat(),
                            int(job["quota_bytes"]),
                        ),
                    )
                    key_row = connection.execute(
                        "SELECT id, expires_at FROM keys WHERE endpoint_id = ? AND outline_key_id = ?",
                        (endpoint_id, str(key["id"])),
                    ).fetchone()
                    if self.connectivity is not None:
                        self.connectivity.assign_free_key(
                            connection,
                            endpoint_id=endpoint_id,
                            free_key_id=int(key_row["id"]),
                            plan_code=str(job["plan_code"]),
                            quota_bytes=int(job["quota_bytes"]),
                            now=now,
                        )
                key_id = int(key_row["id"])
                if key_row["expires_at"]:
                    expires_at = datetime.fromisoformat(str(key_row["expires_at"])).astimezone(UTC)
                claim_column = (
                    "last_claim_at"
                    if str(job["key_type"]) == "daily_free"
                    else "trial_claimed_at"
                )
                connection.execute(
                    f"UPDATE users SET {claim_column} = ? WHERE telegram_id = ?",
                    (now_text, int(job["telegram_id"])),
                )
                connection.execute(
                    """UPDATE free_provisioning_jobs
                       SET status = 'done', locked_at = NULL, key_id = ?,
                           external_id = ?, completed_at = ?, last_error = NULL
                       WHERE id = ?""",
                    (key_id, str(key["id"]), now_text, job_id),
                )
                if notify and self._access_url_cipher is not None:
                    encrypted_url = self._access_url_cipher.encrypt(
                        str(key["accessUrl"]).encode("utf-8")
                    ).decode("ascii")
                    connection.execute(
                        """INSERT INTO notifications
                           (id, dedupe_key, telegram_id, kind, text,
                            access_url_ciphertext, status, next_attempt_at, created_at)
                           VALUES (?, ?, ?, 'key_delivery', ?, ?, 'pending', ?, ?)
                           ON CONFLICT(dedupe_key) DO NOTHING""",
                        (
                            _new_id(),
                            f"free-provision:{job_id}",
                            int(job["telegram_id"]),
                            "Your AuriX key is ready. It is attached below.",
                            encrypted_url,
                            now_text,
                            now_text,
                        ),
                    )
            self._project_free_generation(
                telegram_id=int(job["telegram_id"]),
                key_id=key_id,
                endpoint_id=endpoint_id,
                key=key,
                quota_bytes=int(job["quota_bytes"]),
                expires_at=expires_at,
                grant=grant,
                now=now,
            )
            return ClaimResult(access_url=str(key["accessUrl"]), expires_at=expires_at)
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE free_provisioning_jobs
                       SET status = 'failed', locked_at = NULL,
                           next_attempt_at = ?, last_error = ?
                       WHERE id = ? AND status = 'running'""",
                    (
                        (now + FREE_PROVISION_RETRY_DELAY).isoformat(),
                        type(exc).__name__,
                        job_id,
                    ),
                )
            raise

    def process_free_provisioning(
        self, now: datetime | None = None, limit: int = 10
    ) -> int:
        """Resume due free/trial issuance intents after a crash or outage."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM free_provisioning_jobs
                   WHERE (status IN ('pending', 'failed') AND next_attempt_at <= ?)
                      OR (status = 'running' AND locked_at <= ?)
                   ORDER BY created_at LIMIT ?""",
                (now_text, (current - FREE_PROVISION_LEASE).isoformat(), max(1, min(int(limit), 50))),
            ).fetchall()
        completed = 0
        for row in rows:
            try:
                result = self._execute_free_provision_job(
                    str(row["id"]), current, notify=True
                )
                if result.access_url:
                    completed += 1
            except Exception as exc:
                print(f"free provisioning retry error: {type(exc).__name__}", file=sys.stderr)
        return completed

    def claim(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        job = self._prepare_free_provision_job(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            plan_code="FREE300MB",
            key_type="daily_free",
            quota_bytes=self.limit_bytes,
            duration=CLAIM_PERIOD,
            now=now,
        )
        if isinstance(job, ClaimResult):
            return job
        return self._execute_free_provision_job(job, now)

    def claim_trial(
        self,
        telegram_id: int,
        first_name: str,
        now: datetime | None = None,
        username: str | None = None,
    ) -> ClaimResult:
        """Issue one 3 GiB entitlement per rolling 30 days."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        job = self._prepare_free_provision_job(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            plan_code="FREE3GB",
            key_type="monthly_trial",
            quota_bytes=self.trial_limit_bytes,
            duration=TRIAL_PERIOD,
            now=now,
        )
        if isinstance(job, ClaimResult):
            return job
        return self._execute_free_provision_job(job, now)

    def _terminate_key(
        self,
        row: Any,
        reason: str,
        now: datetime,
        used_bytes: int | None = None,
    ) -> bool:
        """Record, delete, and (when supported) verify one remote credential."""
        now_text = now.astimezone(UTC).isoformat()
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute(
                """UPDATE keys SET status = 'active', last_usage_bytes = COALESCE(?, last_usage_bytes),
                          last_usage_observed_at = CASE WHEN ? IS NULL THEN last_usage_observed_at ELSE ? END,
                          quota_reason = CASE WHEN ? = 'quota' THEN 'quota' ELSE quota_reason END
                   WHERE id = ? AND status != 'revoked'""",
                (used_bytes, now_text if used_bytes is not None else None,
                 now_text if used_bytes is not None else None, reason, row["id"]),
            )
            connection.execute(
                """INSERT INTO key_termination_events
                   (key_id, telegram_id, outline_key_id, reason, used_bytes, quota_bytes,
                    expires_at, detected_at, remote_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'retrying')
                   ON CONFLICT(key_id, reason) DO UPDATE SET
                       used_bytes = COALESCE(excluded.used_bytes, key_termination_events.used_bytes)""",
                (
                    row["id"],
                    row["telegram_id"],
                    str(row["outline_key_id"]),
                    reason,
                    used_bytes,
                    int(row["data_limit_bytes"]),
                    row["expires_at"],
                    now_text,
                ),
            )
        try:
            endpoint_id = str(row.get("endpoint_id") if hasattr(row, "get") else row["endpoint_id"])
            endpoint_client = self._client_for_endpoint(endpoint_id)
            adapter = self._adapter_for_endpoint(endpoint_id, endpoint_client)
            grant = {
                "protocol": "outline",
                "route_id": f"outline:{endpoint_id}",
                "endpoint_id": endpoint_id,
                "external_id": str(row["outline_key_id"]),
                "access_url": "ss://legacy-redacted",
            }
            adapter.revoke_auth(grant)
            getter = getattr(endpoint_client, "get_key", None)
            verified = callable(getter)
            if verified and getter(str(row["outline_key_id"])) is not None:
                raise OutlineError("Outline key still exists after delete")
        except Exception as exc:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE key_termination_events
                       SET remote_state = CASE
                               WHEN delete_attempts + 1 >= 10 THEN 'escalated'
                               ELSE 'retrying'
                           END,
                           delete_attempts = delete_attempts + 1,
                           last_error = ? WHERE key_id = ? AND reason = ?""",
                    (type(exc).__name__, row["id"], reason),
                )
            return False
        remote_state = "deleted_verified" if verified else "delete_accepted"
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            connection.execute("UPDATE keys SET status = 'revoked' WHERE id = ?", (row["id"],))
            if self.connectivity is not None:
                connection.execute(
                    """UPDATE endpoint_assignments SET status = 'released', released_at = ?
                       WHERE free_key_id = ? AND status = 'active'""",
                    (now_text, row["id"]),
                )
            connection.execute(
                """UPDATE key_termination_events
                   SET remote_state = ?, delete_attempts = delete_attempts + 1,
                       last_error = NULL, deletion_verified_at = ?
                   WHERE key_id = ? AND reason = ?""",
                (remote_state, now_text if verified else None, row["id"], reason),
            )
        identity = getattr(self, "identity", None)
        if identity is not None:
            try:
                with self.database.connect() as connection:
                    generation = None
                    if IdentityService._table_exists(connection, "credential_generations"):
                        generation = connection.execute(
                            """SELECT generation_id FROM credential_generations
                                WHERE entitlement_key = ? AND endpoint_id = ?
                                  AND external_id = ?
                                ORDER BY generation_no DESC LIMIT 1""",
                            (
                                f"free:{row['id']}",
                                endpoint_id,
                                str(row["outline_key_id"]),
                            ),
                        ).fetchone()
                if generation is not None:
                    session_result = adapter.terminate_sessions(grant)
                    sessions_terminated = bool(
                        isinstance(session_result, dict)
                        and session_result.get("supported")
                        and session_result.get("terminated")
                    )
                    identity.mark_remote_revoked(
                        str(generation["generation_id"]),
                        verified=verified,
                        # Preserve the legacy free-claim contract: its
                        # verified Outline deletion has historically been the
                        # terminal enforcement proof. Paid/multi-protocol
                        # generations use the stricter worker path above.
                        sessions_terminated=sessions_terminated or grant["protocol"] == "outline",
                        now=now_text,
                    )
            except Exception as exc:
                # The termination event is durable; startup reconciliation can
                # repair the accounting projection if this bookkeeping write
                # loses a race with another worker.
                print(
                    f"free entitlement revocation projection deferred: {type(exc).__name__}",
                    file=sys.stderr,
                )
        return True

    def enforce_quota(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Fail closed and revoke free/trial keys whose Outline metric hit its cap."""
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            self.queue_quota_warnings(current, metrics)
        except Exception as exc:
            # A notification outage must never delay the hard quota revoke.
            print(f"quota warning error: {type(exc).__name__}", file=sys.stderr)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, endpoint_id, data_limit_bytes, expires_at FROM keys
                   WHERE status = 'active' OR (status = 'revoke_failed' AND quota_reason = 'quota')"""
            ).fetchall()
        revoked = 0
        for row in rows:
            endpoint_id = str(row["endpoint_id"])
            if not self._endpoint_observed(metrics, endpoint_id):
                continue
            by_key = self._usage_map(metrics, endpoint_id)
            try:
                used = int(by_key.get(str(row["outline_key_id"]), 0) or 0)
            except (TypeError, ValueError):
                continue
            if used < int(row["data_limit_bytes"]):
                with self.database.connect() as connection:
                    connection.execute(
                        """UPDATE keys SET last_usage_bytes = ?, last_usage_observed_at = ?
                           WHERE id = ? AND status = 'active'""",
                        (used, current.astimezone(UTC).isoformat(), row["id"]),
                    )
                continue
            if self._terminate_key(row, "quota", current, used):
                revoked += 1
        return revoked

    def queue_quota_warnings(
        self,
        now: datetime | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> int:
        """Queue one Telegram warning as each remaining-quota threshold is crossed.

        The warning level is persisted per key, so repeated maintenance passes
        and temporary metric fluctuations cannot spam a customer. The final
        hard stop remains ``enforce_quota`` and never depends on delivery.
        """
        if metrics is None:
            try:
                metrics = self.outline.transfer_metrics()
            except Exception:
                return 0
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        queued = 0
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            rows = connection.execute(
                """SELECT keys.id, keys.telegram_id, keys.outline_key_id, keys.endpoint_id,
                          keys.data_limit_bytes, keys.expires_at,
                          keys.quota_warning_percent, g.campaign_code
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.status = 'active'"""
            ).fetchall()
            for row in rows:
                endpoint_id = str(row["endpoint_id"])
                if not self._endpoint_observed(metrics, endpoint_id):
                    continue
                by_key = self._usage_map(metrics, endpoint_id)
                try:
                    used = max(0, int(by_key.get(str(row["outline_key_id"]), 0) or 0))
                    quota = int(row["data_limit_bytes"])
                except (TypeError, ValueError):
                    continue
                if quota <= 0 or used >= quota:
                    continue
                remaining = quota - used
                reached = next(
                    (
                        percent
                        for percent, fraction in reversed(QUOTA_WARNING_THRESHOLDS)
                        if remaining <= quota * fraction
                    ),
                    None,
                )
                if reached is None:
                    continue
                previous = row["quota_warning_percent"]
                if previous is not None and int(previous) <= reached:
                    continue
                dedupe_key = f"quota-warning:free:{row['id']}:{reached}"
                try:
                    existing = connection.execute(
                        "SELECT id FROM notifications WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is None:
                        if row["campaign_code"]:
                            tier = f"promo {row['campaign_code']}"
                        elif quota == TRIAL_LIMIT_BYTES:
                            tier = "monthly 3 GiB"
                        elif quota == PUBLIC_LIMIT_BYTES:
                            tier = "daily 300 MiB"
                        else:
                            tier = "free"
                        remaining_percent = remaining * 100 / quota
                        formatter = _human_decimal_bytes if row["campaign_code"] else _human_bytes
                        text = (
                            f"Quota warning: your AuriX {tier} key has "
                            f"{formatter(remaining)} remaining "
                            f"({remaining_percent:.1f}% of {formatter(quota)}).\n"
                            "This is based on Outline's trailing-30-day usage. "
                            "When no quota remains, the key will be blocked and deleted. "
                            f"Expires: {row['expires_at']}"
                        )
                        connection.execute(
                            """INSERT INTO notifications
                               (id, dedupe_key, telegram_id, kind, text, status,
                                next_attempt_at, created_at)
                               VALUES (?, ?, ?, 'quota_warning', ?, 'pending', ?, ?)""",
                            (_new_id(), dedupe_key, row["telegram_id"], text, now_text, now_text),
                        )
                        queued += 1
                    connection.execute(
                        "UPDATE keys SET quota_warning_percent = ? WHERE id = ?",
                        (reached, row["id"]),
                    )
                except Exception as exc:
                    if self.database.is_integrity_error(exc):
                        continue
                    raise
        return queued

    def user_usage(
        self,
        telegram_id: int,
        usage_by_key: dict[str, Any],
        access_by_key: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return this user's current free/trial key state for the customer dashboard."""
        access_by_key = access_by_key or {}
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT keys.outline_key_id, keys.endpoint_id, keys.key_type, keys.created_at,
                          keys.expires_at, keys.data_limit_bytes, keys.status,
                          keys.last_usage_bytes, keys.quota_reason,
                          g.campaign_code,
                          (SELECT remote_state FROM key_termination_events e
                           WHERE e.key_id = keys.id ORDER BY e.detected_at DESC LIMIT 1) AS termination_state
                   FROM keys
                   LEFT JOIN giveaway_claims g ON g.key_id = keys.id
                   WHERE keys.telegram_id = ?
                     AND (keys.status IN ('active', 'revoke_failed') OR keys.quota_reason = 'quota')
                   ORDER BY keys.created_at DESC LIMIT 10""",
                (telegram_id,),
            ).fetchall()
        tiers = {
            300 * 1024**2: "Daily Free 300 MiB",
            3 * 1024**3: "Monthly Free 3 GiB",
        }
        result = []
        for row in rows:
            key_id = str(row["outline_key_id"])
            endpoint_id = str(row["endpoint_id"])
            endpoint_usage = self._usage_map(usage_by_key, endpoint_id)
            scoped_access = access_by_key.get("byEndpoint") if isinstance(access_by_key, dict) else None
            endpoint_access = (
                scoped_access.get(endpoint_id, {})
                if isinstance(scoped_access, dict) and isinstance(scoped_access.get(endpoint_id, {}), dict)
                else access_by_key
            )
            observed = key_id in endpoint_usage
            raw_used = endpoint_usage.get(key_id, row["last_usage_bytes"] or 0)
            try:
                used = max(0, int(raw_used or 0))
            except (TypeError, ValueError):
                used = max(0, int(row["last_usage_bytes"] or 0))
                observed = False
            quota = int(row["data_limit_bytes"])
            effective_status = (
                "quota exhausted"
                if row["quota_reason"] == "quota"
                else (
                    "revocation failed"
                    if row["termination_state"] == "escalated"
                    else (
                        "revocation pending"
                        if row["termination_state"] in ("retrying", "delete_accepted")
                        or row["status"] == "revoke_failed"
                        else (
                            "expired"
                            if datetime.fromisoformat(row["expires_at"]).astimezone(UTC) <= now
                            else row["status"]
                        )
                    )
                )
            )
            result.append(
                {
                    "outline_key_id": key_id,
                    "protocol": "outline",
                    "endpoint_id": endpoint_id,
                    "key_type": row["key_type"],
                    "tier": (
                        f"{quota / 1_000_000_000:g} GB Promo · {row['campaign_code']}"
                        if row["campaign_code"]
                        else tiers.get(quota, "Free access")
                    ),
                    "decimal_quota": bool(row["campaign_code"]),
                    "used_bytes": used,
                    "quota_bytes": quota,
                    "remaining_bytes": max(0, quota - used),
                    "usage_observed": observed,
                    "expires_at": row["expires_at"],
                    "status": effective_status,
                    "access_url": endpoint_access.get(key_id)
                    if effective_status == "active"
                    else None,
                    "created_at": row["created_at"],
                }
            )
        return result

    def persist_access_url_snapshot(self, inventory: dict[str, Any] | None) -> int:
        """Encrypt provider inventory into free generations during maintenance."""
        scoped = inventory.get("byEndpoint") if isinstance(inventory, dict) else None
        if self._access_url_cipher is None or not isinstance(scoped, dict):
            return 0
        normalized: dict[str, dict[str, str]] = {}
        for endpoint_id, values in scoped.items():
            if not isinstance(values, dict):
                continue
            endpoint = str(endpoint_id or "").strip()
            if not endpoint:
                continue
            for external_id, raw_url in values.items():
                key_id = str(external_id or "").strip()
                access_url = str(raw_url or "").replace("\r", "").replace("\n", "").strip()
                if key_id and access_url and len(access_url) <= 4096:
                    normalized.setdefault(endpoint, {})[key_id] = access_url
        if not normalized:
            return 0
        try:
            with self.database.connect() as connection:
                if not IdentityService._table_exists(connection, "credential_generations"):
                    return 0
                rows = connection.execute(
                    """SELECT generation_id, endpoint_id, external_id,
                              access_url_ciphertext
                         FROM credential_generations
                        WHERE source_type = 'free'
                          AND protocol = 'outline'
                          AND status IN ('active', 'retiring', 'unknown')"""
                ).fetchall()
                updates: list[tuple[str, str]] = []
                for row in rows:
                    access_url = normalized.get(str(row["endpoint_id"]), {}).get(
                        str(row["external_id"])
                    )
                    if not access_url:
                        continue
                    current = str(row["access_url_ciphertext"] or "")
                    if current:
                        try:
                            if self._access_url_cipher.decrypt(current.encode()).decode() == access_url:
                                continue
                        except Exception:
                            pass
                    updates.append(
                        (
                            self._access_url_cipher.encrypt(access_url.encode()).decode(),
                            str(row["generation_id"]),
                        )
                    )
                if not updates:
                    return 0
                self.database.begin_write(connection)
                for ciphertext, generation_id in updates:
                    connection.execute(
                        "UPDATE credential_generations SET access_url_ciphertext = ? WHERE generation_id = ?",
                        (ciphertext, generation_id),
                    )
                return len(updates)
        except Exception:
            return 0

    def cached_access_urls(self, telegram_id: int) -> dict[str, Any]:
        """Return encrypted free-key URLs without contacting Outline.

        New free credentials persist their encrypted URL in the shared
        generation record. Legacy credentials without that projection remain
        visible as active but report a cache miss until maintenance repairs the
        projection; an interactive request never performs provider inventory.
        """
        empty = {"byEndpoint": {}, "errors": {}, "source": "durable_generation"}
        if self._access_url_cipher is None:
            empty["errors"] = {"cache": "encryption_unavailable"}
            return empty
        try:
            with self.database.connect() as connection:
                if not all(
                    IdentityService._table_exists(connection, table)
                    for table in ("keys", "credential_generations")
                ):
                    empty["errors"] = {"cache": "unavailable"}
                    return empty
                active = connection.execute(
                    """SELECT COUNT(*) AS n FROM keys
                        WHERE telegram_id = ? AND status IN ('active', 'revoke_failed')""",
                    (int(telegram_id),),
                ).fetchone()
                rows = connection.execute(
                    """SELECT k.endpoint_id, k.outline_key_id, g.access_url_ciphertext
                         FROM keys k
                         JOIN credential_generations g
                           ON g.source_type = 'free'
                          AND g.source_id = CAST(k.id AS TEXT)
                          AND g.external_id = k.outline_key_id
                        WHERE k.telegram_id = ?
                          AND k.status IN ('active', 'revoke_failed')
                          AND g.status IN ('active', 'retiring', 'unknown')
                        ORDER BY g.created_at DESC""",
                    (int(telegram_id),),
                ).fetchall()
        except Exception:
            empty["errors"] = {"cache": "unavailable"}
            return empty
        by_endpoint: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for row in rows:
            encrypted = str(row["access_url_ciphertext"] or "")
            if not encrypted:
                errors[str(row["outline_key_id"])] = "missing"
                continue
            try:
                access_url = self._access_url_cipher.decrypt(encrypted.encode()).decode()
            except Exception:
                errors[str(row["outline_key_id"])] = "invalid"
                continue
            if access_url:
                by_endpoint.setdefault(str(row["endpoint_id"]), {})[
                    str(row["outline_key_id"])
                ] = access_url
        if int(active["n"] or 0) > len(
            {
                str(row["outline_key_id"])
                for row in rows
                if row["access_url_ciphertext"]
            }
        ):
            errors.setdefault("cache", "incomplete")
        return {"byEndpoint": by_endpoint, "errors": errors, "source": "durable_generation"}

    def revoke_expired(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        now_text = current.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, telegram_id, outline_key_id, endpoint_id, data_limit_bytes, expires_at FROM keys
                   WHERE status IN ('active', 'revoke_failed') AND expires_at <= ?""",
                (now_text,),
            ).fetchall()
        revoked = 0
        for row in rows:
            if self._terminate_key(row, "expiry", current):
                revoked += 1
        return revoked

    def reconcile_terminations(self, now: datetime | None = None, limit: int = 20) -> int:
        """Retry recorded remote deletions, including paid-upgrade cleanup."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT k.id, k.telegram_id, k.outline_key_id, k.endpoint_id, k.data_limit_bytes,
                          k.expires_at, e.reason, e.used_bytes
                   FROM keys k JOIN key_termination_events e ON e.key_id = k.id
                   WHERE e.remote_state IN ('retrying', 'escalated') AND k.status != 'revoked'
                   ORDER BY e.detected_at LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        completed = 0
        for row in rows:
            if self._terminate_key(row, str(row["reason"]), current, row["used_bytes"]):
                completed += 1
        return completed

    def pending_termination_notices(self, audience: str) -> list[dict[str, Any]]:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    f"""SELECT * FROM key_termination_events
                        WHERE COALESCE({column}, '') != remote_state
                        ORDER BY detected_at LIMIT 50"""
                ).fetchall()
            ]

    def mark_termination_notice(self, event_id: int, audience: str, state: str) -> None:
        column = "admin_notice_state" if audience == "admin" else "user_notice_state"
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE key_termination_events SET {column} = ? WHERE id = ?",
                (state, event_id),
            )

    def termination_summary(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM key_termination_events
                   ORDER BY detected_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]
