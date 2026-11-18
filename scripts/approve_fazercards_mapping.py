#!/usr/bin/env python3
"""Approve one exact supplier SKU mapping in the private review queue.

This command is an explicit operator action, not an automatic rights or region
inference.  It reads and atomically updates a private local mapping file only;
it never contacts FazerCards, publishes a customer catalog, or enables a
supplier order.  The caller must provide the exact provider key, AuriX
identity, account-side region rule, rights evidence, reviewer, timestamp, and
``--confirm-approval``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.eligibility import RegionDimension, SUPPORTED_FULFILLMENT_KINDS  # noqa: E402


UTC = timezone.utc
DEFAULT_CONFIG = Path(".local/fazercards/public-catalog-config.json")


class MappingApprovalError(ValueError):
    """The requested single-row approval is incomplete or unsafe."""


def _read_private(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MappingApprovalError("mapping config must be a regular private file")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise MappingApprovalError("mapping config directory must be private")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MappingApprovalError("mapping config must not be group/world accessible")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MappingApprovalError("mapping config could not be read") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("mappings"), list):
        raise MappingApprovalError("mapping config must contain a mappings array")
    if not all(isinstance(row, dict) for row in payload["mappings"]):
        raise MappingApprovalError("mapping rows must be objects")
    return payload


def _text(value: Any, label: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise MappingApprovalError(f"{label} is required")
    if "\r" in value or "\n" in value:
        raise MappingApprovalError(f"{label} must be single-line")
    return value.strip()


def _iso_timestamp(value: str) -> str:
    text = _text(value, "approved-at", max_length=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MappingApprovalError("approved-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MappingApprovalError("approved-at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _build_region_rule(args: argparse.Namespace) -> dict[str, Any]:
    values = [_text(value, "region-value", max_length=96) for value in args.region_value]
    if args.allow_all_region:
        if args.region_dimension or values:
            raise MappingApprovalError("allow-all region cannot include a dimension or values")
        return {"verified": True, "allow_all": True}
    if not args.region_dimension or not values:
        raise MappingApprovalError("region-dimension and at least one region-value are required")
    try:
        dimension = RegionDimension(args.region_dimension).value
    except ValueError as exc:
        raise MappingApprovalError("region-dimension is unsupported") from exc
    return {"verified": True, "allow_all": False, "dimension": dimension, "allowed_values": sorted(set(values))}


def approve_mapping(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Return an updated config after validating one explicit approval."""

    if not args.confirm_approval:
        raise MappingApprovalError("pass --confirm-approval to apply this approval")
    family = _text(args.family, "family", max_length=32)
    category = _text(args.category, "provider-category-id")
    sku = _text(args.sku, "provider-offer-id")
    key = (family, category, sku)
    rows = payload["mappings"]
    matches = [row for row in rows if tuple(row.get(field) for field in ("family", "provider_category_id", "provider_offer_id")) == key]
    if len(matches) != 1:
        raise MappingApprovalError("exact provider key must match one mapping row")
    row = matches[0]
    if row.get("review_state") == "approved":
        raise MappingApprovalError("mapping is already approved; use a separate reviewed correction workflow")
    offer_id = _text(args.offer_id, "offer-id", max_length=160)
    if any(other is not row and other.get("offer_id") == offer_id for other in rows):
        raise MappingApprovalError("offer-id is already used by another mapping")
    fulfillment = _text(args.fulfillment_kind, "fulfillment-kind", max_length=64)
    if fulfillment not in SUPPORTED_FULFILLMENT_KINDS:
        raise MappingApprovalError("fulfillment-kind is unsupported")
    rights = _text(args.rights_evidence, "rights-evidence", max_length=512)
    reviewer = _text(args.reviewer, "reviewer", max_length=160)
    approved_at = _iso_timestamp(args.approved_at)
    region_rule = _build_region_rule(args)
    labels = [_text(value, "required-input-label", max_length=96) for value in args.required_input_label]
    product_slug = _text(args.product_slug, "product-slug", max_length=160)
    product_name = _text(args.product_name, "product-name", max_length=160)
    denomination = _text(args.denomination_label, "denomination-label", max_length=160)
    row.update({
        "offer_id": offer_id,
        "product_slug": product_slug,
        "product_name": product_name,
        "fulfillment_kind": fulfillment,
        "denomination_label": denomination,
        "review_state": "approved",
        "customer_visible": True,
        "supplier_enabled": True,
        "resale_authorized": True,
        "region_rule": region_rule,
        "region_label": _text(args.region_label, "region-label", max_length=160) if args.region_label else None,
        "region_dimension": region_rule.get("dimension"),
        "required_input_labels": labels,
        "rights_evidence": rights,
        "reviewer": reviewer,
        "approved_at": approved_at,
    })
    result = dict(payload)
    result["mappings"] = rows
    history = payload.get("approval_history", [])
    if not isinstance(history, list) or not all(isinstance(entry, dict) for entry in history):
        raise MappingApprovalError("approval_history must be an array of objects")
    event_basis = json.dumps(
        {
            "provider_key": key,
            "offer_id": offer_id,
            "reviewer": reviewer,
            "approved_at": approved_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    event_id = hashlib.sha256(event_basis).hexdigest()[:24]
    event = {
        "event_id": event_id,
        "provider_key": list(key),
        "offer_id": offer_id,
        "product_slug": product_slug,
        "fulfillment_kind": fulfillment,
        "region_rule": region_rule,
        "rights_evidence": rights,
        "reviewer": reviewer,
        "approved_at": approved_at,
    }
    history = [entry for entry in history if entry.get("event_id") != event_id]
    history.append(event)
    result["approval_history"] = history[-1000:]
    result["queue_status"] = "operator_reviewed"
    result["last_reviewed_at"] = approved_at
    result["last_reviewed_provider_key"] = list(key)
    result["mapping_rows"] = len(rows)
    return result


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise MappingApprovalError("mapping config must remain a regular private file")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077 or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MappingApprovalError("mapping config path must remain private")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise MappingApprovalError("mapping config could not be published atomically") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--family", required=True,
        choices=("topups", "giftcards", "gamekeys", "manual_services"),
    )
    parser.add_argument("--category", required=True, help="exact provider category/game identifier")
    parser.add_argument("--sku", required=True, help="exact provider offer/card/key identifier")
    parser.add_argument("--offer-id", required=True, help="new AuriX-owned offer ID")
    parser.add_argument("--product-slug", required=True)
    parser.add_argument("--product-name", required=True)
    parser.add_argument("--fulfillment-kind", required=True)
    parser.add_argument("--denomination-label", required=True)
    parser.add_argument("--region-dimension", choices=[dimension.value for dimension in RegionDimension])
    parser.add_argument("--region-value", action="append", default=[])
    parser.add_argument("--allow-all-region", action="store_true")
    parser.add_argument("--region-label")
    parser.add_argument("--required-input-label", action="append", default=[])
    parser.add_argument("--rights-evidence", required=True, help="URL or private document reference")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--approved-at", required=True, help="timezone-aware ISO-8601 timestamp")
    parser.add_argument("--confirm-approval", action="store_true", help="required explicit write confirmation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _read_private(args.config)
        updated = approve_mapping(payload, args)
        _write_private(args.config, updated)
    except (OSError, MappingApprovalError) as exc:
        print(json.dumps({"status": "invalid", "writes": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "approved_row",
        "provider_key": [args.family, args.category, args.sku],
        "output": str(args.config),
        "mapping_rows": updated["mapping_rows"],
        "customer_publication": False,
        "supplier_key_used": False,
        "writes": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
