"""Endpoint lifecycle state and health observation workflow."""

from __future__ import annotations

from typing import Any

from commerce_models import CommerceError, _new_id, _now_text
from connectivity_registry import ConnectivityRegistry
from lifecycle_policy import normalize_lifecycle_state


def set_server_lifecycle(
    self,
    server_id: str,
    lifecycle_state: str,
    actor_id: int,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Change an endpoint's admission lifecycle without touching its VM.

    ``draining`` stops new assignments while existing credentials continue
    to work. ``retired`` is only accepted after local entitlements,
    reservations, pending intents, and the last authoritative remote
    inventory are empty. Provider deletion remains a separate, explicit
    operation outside this method.
    """
    try:
        state = normalize_lifecycle_state(lifecycle_state, strict=True)
    except ValueError as exc:
        raise CommerceError("Endpoint lifecycle must be active, draining or retired") from exc
    server = str(server_id or "").strip()
    if not server:
        raise CommerceError("Endpoint identity is required")
    now_text = _now_text()
    clean_reason = str(reason or "").strip()[:512] or None
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        row = connection.execute(
            "SELECT * FROM outline_servers WHERE server_id = ?",
            (server,),
        ).fetchone()
        if row is None:
            raise CommerceError("Outline server is not configured")
        previous = str(row.get("lifecycle_state") if hasattr(row, "get") else row["lifecycle_state"] or "active")
        if previous == state:
            return {
                "server_id": server,
                "lifecycle_state": state,
                "previous_state": previous,
                "changed": False,
            }
        if state == "retired":
            blockers: list[str] = []
            active_free = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                    (server,),
                ).fetchone()["n"]
            ) if self._table_exists(connection, "keys") else 0
            active_paid = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM paid_vpn_keys WHERE server_id = ? AND status IN ('active', 'revoke_failed')",
                    (server,),
                ).fetchone()["n"]
            )
            pending_orders = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM orders
                       WHERE server_id = ? AND status IN ('awaiting_payment', 'payment_submitted')""",
                    (server,),
                ).fetchone()["n"]
            )
            pending_subscriptions = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM subscriptions WHERE server_id = ? AND status IN ('pending', 'active')",
                    (server,),
                ).fetchone()["n"]
            )
            pending_intents = 0
            if self._table_exists(connection, "free_provisioning_intents"):
                pending_intents += int(
                    connection.execute(
                        """SELECT COUNT(*) AS n FROM free_provisioning_intents
                           WHERE server_id = ? AND status IN ('pending', 'running')""",
                        (server,),
                    ).fetchone()["n"]
                )
            if active_free:
                blockers.append(f"{active_free} active free/promo key(s)")
            if active_paid:
                blockers.append(f"{active_paid} active paid key(s)")
            if pending_orders:
                blockers.append(f"{pending_orders} open order(s)")
            if pending_subscriptions:
                blockers.append(f"{pending_subscriptions} active/pending subscription(s)")
            if pending_intents:
                blockers.append(f"{pending_intents} pending provisioning intent(s)")
            remote_count = row["remote_key_count"]
            orphan_count = int(row["remote_orphan_key_count"] or 0)
            if remote_count is None:
                blockers.append("remote inventory has not been reconciled")
            elif int(remote_count or 0) != 0:
                blockers.append(f"remote inventory still has {int(remote_count)} key(s)")
            if orphan_count:
                blockers.append(f"{orphan_count} unreviewed remote key(s)")
            if blockers:
                raise CommerceError("Endpoint cannot be retired yet: " + "; ".join(blockers))
        enabled = 1 if state in {"active", "draining"} else 0
        connection.execute(
            """UPDATE outline_servers
                  SET enabled = ?, lifecycle_state = ?, lifecycle_reason = ?,
                      lifecycle_changed_at = ?, updated_at = ?
                WHERE server_id = ?""",
            (enabled, state, clean_reason, now_text, now_text, server),
        )
        ConnectivityRegistry.sync_outline_health(
            connection,
            server_id=server,
            lifecycle_state=state,
            health_status=str(row["health_status"] or "unknown"),
            now_text=now_text,
        )
        self._audit(
            connection,
            "server_lifecycle_changed",
            "outline_server",
            server,
            "owner",
            str(actor_id),
            {
                "previous_state": previous,
                "lifecycle_state": state,
                "reason": clean_reason,
            },
        )
    return {
        "server_id": server,
        "lifecycle_state": state,
        "previous_state": previous,
        "changed": True,
        "changed_at": now_text,
        "reason": clean_reason,
    }


def _record_endpoint_health(
    self,
    connection: Any,
    server_id: str,
    observed_at: str,
    *,
    observed_status: str,
    latency_ms: float | None,
    remote_key_count: int | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Persist one health probe and apply conservative state hysteresis.

    A single failed management call blocks new admission immediately by
    moving a healthy node to ``degraded``. Repeated failures make the
    state ``unreachable``; recovery needs independent successful probes.
    The unique timestamp makes repeated maintenance/UI calls idempotent.
    """
    if observed_status not in {"healthy", "unreachable"}:
        raise CommerceError("Invalid endpoint health observation")
    lock_clause = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
    current = connection.execute(
        """SELECT health_status, health_success_streak,
                  health_failure_streak, health_state_changed_at, last_error
             FROM outline_servers WHERE server_id = ?""" + lock_clause,
        (server_id,),
    ).fetchone()
    if current is None:
        raise CommerceError(f"Unknown Outline server: {server_id}")
    previous = str(current["health_status"] or "unknown")
    if self._table_exists(connection, "endpoint_health_observations"):
        duplicate = connection.execute(
            """SELECT state_after FROM endpoint_health_observations
               WHERE server_id = ? AND probe_type = 'management_inventory'
                 AND observed_at = ?""",
            (server_id, observed_at),
        ).fetchone()
        if duplicate is not None:
            return {
                "state": str(duplicate["state_after"]),
                "success_streak": int(current["health_success_streak"] or 0),
                "failure_streak": int(current["health_failure_streak"] or 0),
                "duplicate": True,
            }
    success_streak = int(current["health_success_streak"] or 0)
    failure_streak = int(current["health_failure_streak"] or 0)
    recovery_threshold = self._health_threshold(
        "AURIX_ENDPOINT_RECOVERY_THRESHOLD", 2
    )
    failure_threshold = self._health_threshold(
        "AURIX_ENDPOINT_FAILURE_THRESHOLD", 3
    )
    if observed_status == "healthy":
        success_streak += 1
        failure_streak = 0
        state_after = (
            "healthy"
            if previous in {"unknown", "healthy"}
            or success_streak >= recovery_threshold
            else previous
        )
    else:
        failure_streak += 1
        success_streak = 0
        state_after = (
            "unreachable"
            if failure_streak >= failure_threshold
            else "degraded"
        )
    changed_at = (
        observed_at
        if state_after != previous
        else current["health_state_changed_at"]
    )
    last_error = (
        None
        if observed_status == "healthy" and state_after == "healthy"
        else (str(error_type or current["last_error"] or "")[:128] or None)
    )
    connection.execute(
        """UPDATE outline_servers
              SET health_status = ?, health_success_streak = ?,
                  health_failure_streak = ?, health_state_changed_at = ?,
                  health_last_latency_ms = ?, last_error = ?,
                  last_synced_at = ?, updated_at = ?
            WHERE server_id = ?""",
        (
            state_after,
            success_streak,
            failure_streak,
            changed_at,
            latency_ms,
            last_error,
            observed_at,
            observed_at,
            server_id,
        ),
    )
    if self._table_exists(connection, "endpoint_health_observations"):
        connection.execute(
            """INSERT INTO endpoint_health_observations
               (id, server_id, probe_type, observed_at, observed_status,
                state_before, state_after, latency_ms, remote_key_count,
                error_type, created_at)
               VALUES (?, ?, 'management_inventory', ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id, probe_type, observed_at) DO NOTHING""",
            (
                _new_id(),
                server_id,
                observed_at,
                observed_status,
                previous,
                state_after,
                latency_ms,
                remote_key_count,
                str(error_type or "")[:128] or None,
                observed_at,
            ),
        )
    return {
        "state": state_after,
        "success_streak": success_streak,
        "failure_streak": failure_streak,
        "duplicate": False,
    }

