"""Pure capacity snapshot projections shared by worker read models."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def usage_rows(
    rows: Iterable[Mapping[str, Any]],
    metrics_by_server: Mapping[str, Mapping[str, Any]],
    default_server_id: str | None,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        server_id = row["server_id"] or default_server_id
        by_key = metrics_by_server.get(server_id, {})
        raw_used = by_key.get(str(row["outline_key_id"]), 0)
        try:
            used_bytes = max(0, int(raw_used or 0))
        except (TypeError, ValueError):
            used_bytes = 0
        projected.append(
            {
                "outline_key_id": row["outline_key_id"],
                "telegram_id": row["telegram_id"],
                "quota_bytes": row["quota_bytes"],
                "used_bytes": used_bytes,
                "server_id": server_id,
            }
        )
    return projected


def allocations_by_server(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    projected: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        item["remaining_slots"] = max(
            0,
            int(item["slot_limit"]) - int(item["active_count"]) - int(item["reserved_count"]),
        )
        projected.setdefault(str(item["server_id"]), []).append(item)
    return projected


def free_counts(
    rows: Iterable[Mapping[str, Any]], default_server_id: str | None
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        tier_code = (
            "PROMO"
            if int(row["is_promo"])
            else "FREE300MB"
            if row["key_type"] == "daily_free"
            else "FREE3GB"
        )
        identity = (str(row["server_id"] or default_server_id), tier_code)
        counts[identity] = counts.get(identity, 0) + 1
    return counts


def tier_allocations_by_server(
    rows: Iterable[Mapping[str, Any]], counts: Mapping[tuple[str, str], int]
) -> dict[str, list[dict[str, Any]]]:
    projected: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        active_count = counts.get((str(item["server_id"]), str(item["tier_code"])), 0)
        item["active_count"] = active_count
        item["remaining_slots"] = max(0, int(item["slot_limit"]) - active_count)
        projected.setdefault(str(item["server_id"]), []).append(item)
    return projected
