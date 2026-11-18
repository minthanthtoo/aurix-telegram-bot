"""Read-only approval readiness for the private supplier catalog mapping.

This module deliberately returns aggregate gate results only.  It never makes
supplier requests and never publishes the customer catalog.  The operator
console uses it to explain why an offer is still blocked before a private
mapping file is approved.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .live_catalog import build_priced_offers
from .pricing_store import PricingPolicyStore, PricingPolicyStoreError
from .catalog_policy import env_catalog_policy_store
from scripts.build_fazercards_public_catalog import _mapping, _read, _records


UTC = timezone.utc


def _observed_at(snapshot: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(snapshot["captured_at"])).astimezone(UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("snapshot captured_at is invalid") from exc


def _region_ready(rule: Any) -> bool:
    if not rule.verified:
        return False
    if rule.allow_all:
        return True
    return rule.dimension is not None and rule.allowed_values is not None


def review_mapping(
    *,
    snapshot_path: Path,
    config_path: Path,
    max_age_minutes: int = 15,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return aggregate mapping readiness; do not write or contact anything."""

    if not isinstance(max_age_minutes, int) or isinstance(max_age_minutes, bool) or max_age_minutes < 0:
        raise ValueError("max_age_minutes must be a non-negative integer")
    snapshot = _read(snapshot_path)
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a JSON object")
    observed_at = _observed_at(snapshot)
    if not config_path.exists():
        return {
            "status": "blocked",
            "reason": "mapping_config_missing",
            "snapshot": str(snapshot_path),
            "config": str(config_path),
            "captured_at": observed_at.isoformat(),
            "records": sum(1 for _ in _records(snapshot, observed_at)),
            "mapped": 0,
            "projected": 0,
            "writes": False,
        }
    config = _read(config_path)
    if not isinstance(config, dict):
        raise ValueError("mapping config must be a JSON object")
    rows = config.get("mappings", [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("config mappings must be an array of objects")
    pairs = [_mapping(row) for row in rows]
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("config mappings contain duplicate provider keys")
    mappings = dict(pairs)
    current = now or datetime.now(UTC)
    records = tuple(_records(snapshot, observed_at))
    try:
        policy_state = PricingPolicyStore(config_path).status(now=current)
    except PricingPolicyStoreError:
        return {
            "status": "blocked",
            "reason": "pricing_policy_invalid",
            "captured_at": observed_at.isoformat(),
            "records": len(records),
            "mapping_rows": len(rows),
            "mapped": 0,
            "projected": 0,
            "block_reasons": {"pricing_policy_invalid": 1},
            "writes": False,
            "supplier_key_used": False,
        }
    if policy_state["policy_status"] != "approved":
        reason = "pricing_policy_" + str(policy_state["policy_status"])
        return {
            "status": "blocked",
            "reason": reason,
            "captured_at": observed_at.isoformat(),
            "records": len(records),
            "mapping_rows": len(rows),
            "mapped": 0,
            "projected": 0,
            "block_reasons": {reason: 1},
            "writes": False,
            "supplier_key_used": False,
        }
    policy = PricingPolicyStore(config_path).load(now=current)
    max_age = timedelta(minutes=max_age_minutes)
    counters: Counter[str] = Counter()
    for record in records:
        key = (record.family, record.provider_category_id, record.provider_offer_id)
        mapping = mappings.get(key)
        if mapping is None:
            counters["missing_mapping"] += 1
            continue
        counters["mapped"] += 1
        if not record.available:
            counters["supplier_unavailable"] += 1
        if mapping.review_state != "approved":
            counters[f"review_state:{mapping.review_state}"] += 1
        if not mapping.customer_visible:
            counters["customer_visibility_disabled"] += 1
        if not mapping.supplier_enabled:
            counters["supplier_disabled"] += 1
        if not mapping.resale_authorized:
            counters["resale_unverified"] += 1
        if not _region_ready(mapping.region_rule):
            counters["region_rule_incomplete"] += 1
        if current - record.observed_at > max_age:
            counters["stale_observation"] += 1
    priced = build_priced_offers(
        records,
        mappings,
        policy=policy,
        now=current,
        max_age=max_age,
        catalog_policy=env_catalog_policy_store(config_path.parent).active_policy(),
    )
    projected = len(priced)
    return {
        "status": "ready" if projected else "blocked",
        "reason": "approved_projection_available" if projected else "no_offer_passed_all_gates",
        "snapshot": str(snapshot_path),
        "config": str(config_path),
        "captured_at": observed_at.isoformat(),
        "records": len(records),
        "mapping_rows": len(rows),
        "mapped": counters.pop("mapped", 0),
        "projected": projected,
        "block_reasons": dict(sorted(counters.items())),
        "writes": False,
        "supplier_key_used": False,
    }


def mapping_history(
    *,
    config_path: Path,
    limit: int = 50,
) -> dict[str, Any]:
    """Read a bounded, sanitized approval history from the private queue.

    This is intentionally read-only.  Rights evidence is reduced to a boolean
    so an operator can confirm that evidence was recorded without leaking a
    private URL or document reference into the admin response.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("history limit must be between 1 and 100")
    path = Path(config_path)
    if path.is_symlink() or not path.is_file():
        return {"status": "missing", "count": 0, "events": [], "writes": False}
    try:
        import stat
        import json

        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ValueError("mapping config directory must be private")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("mapping config must not be group/world accessible")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("mapping history config could not be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("mapping history config must be an object")
    raw_history = payload.get("approval_history", [])
    if not isinstance(raw_history, list) or not all(isinstance(item, dict) for item in raw_history):
        raise ValueError("approval_history must be an array of objects")
    events: list[dict[str, Any]] = []
    for raw in raw_history[-limit:]:
        provider_key = raw.get("provider_key")
        if not isinstance(provider_key, list) or len(provider_key) != 3 or not all(
            isinstance(value, str) and value.strip() for value in provider_key
        ):
            continue
        required_text = ("event_id", "offer_id", "product_slug", "fulfillment_kind", "reviewer", "approved_at")
        if not all(
            isinstance(raw.get(key), str) and raw[key].strip() and len(raw[key]) <= 256
            for key in required_text
        ):
            continue
        region_rule = raw.get("region_rule")
        if not isinstance(region_rule, dict):
            region_rule = {}
        events.append({
            "event_id": raw["event_id"],
            "provider_key": list(provider_key),
            "offer_id": raw["offer_id"],
            "product_slug": raw["product_slug"],
            "fulfillment_kind": raw["fulfillment_kind"],
            "region_rule": {
                key: region_rule[key]
                for key in ("verified", "allow_all", "dimension", "allowed_values")
                if key in region_rule and (
                    isinstance(region_rule[key], (bool, str))
                    or (
                        isinstance(region_rule[key], list)
                        and len(region_rule[key]) <= 100
                        and all(isinstance(value, str) and len(value) <= 96 for value in region_rule[key])
                    )
                )
            },
            "rights_evidence_recorded": isinstance(raw.get("rights_evidence"), str)
            and bool(raw["rights_evidence"].strip()),
            "reviewer": raw["reviewer"],
            "approved_at": raw["approved_at"],
        })
    return {
        "status": "available" if events else "empty",
        "queue_status": payload.get("queue_status") if isinstance(payload.get("queue_status"), str) else None,
        "last_reviewed_at": payload.get("last_reviewed_at") if isinstance(payload.get("last_reviewed_at"), str) else None,
        "count": len(raw_history),
        "events": events,
        "writes": False,
    }


__all__ = ["mapping_history", "review_mapping"]
