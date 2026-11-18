#!/usr/bin/env python3
"""Seed a private, all-blocked mapping queue from an exact supplier snapshot.

This is a recovery and review aid, not an approval shortcut.  Every generated
row is deliberately non-sellable until an operator supplies the AuriX product
mapping, region evidence, resale evidence, and enables the supplier/customer
flags.  The command performs no network request and never writes to FazerCards.
Use --merge-existing with a separate output to reconcile without resetting decisions.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from aurix_games.pricing_store import PricingPolicyStore
from scripts.build_fazercards_public_catalog import _read, _records
from scripts.approve_fazercards_mapping import _read_private


UTC = timezone.utc
DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_OUTPUT = Path(".local/fazercards/public-catalog-config.json")


def _key_digest(family: str, category_id: str, offer_id: str) -> str:
    raw = "\x1f".join((family, category_id, offer_id)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def build_blocked_queue(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Create a non-sellable config for every mapping-capable snapshot row."""

    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a JSON object")
    captured = datetime.fromisoformat(str(snapshot["captured_at"]))
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError('snapshot capture time must include a timezone')
    captured = captured.astimezone(UTC)
    rows: list[dict[str, Any]] = []
    for record in _records(snapshot, captured):
        family_kind = {
            "topups": "direct_topup",
            "giftcards": "gift_card",
            "gamekeys": "game_key",
            "manual_services": "manual_service",
        }.get(record.family)
        if family_kind is None:
            continue
        digest = _key_digest(record.family, record.provider_category_id, record.provider_offer_id)
        source = snapshot['families'][record.family]
        manual = record.family == 'manual_services'
        category_key = 'id' if manual else 'game_id' if record.family == 'gamekeys' else 'category_id'
        category = next((row for row in source.get('items' if manual else 'categories', [])
                         if str(row.get(category_key)) == record.provider_category_id), {})
        detail = source.get('offers_by_id' if manual else 'details_by_id', {}).get(record.provider_category_id, {})
        offer_key = {'topups': 'offer_id', 'giftcards': 'card_id', 'gamekeys': 'key_id', 'manual_services': 'id'}[record.family]
        offer_rows = detail.get('items' if manual else 'keys' if record.family == 'gamekeys' else 'offers', [])
        offer = next((row for row in offer_rows if row.get(offer_key) == record.provider_offer_id), {})
        # Price/stock vary routinely; names, inputs and territory terms require renewed review.
        contract = {
            'category': {key: category.get(key) for key in ('name', 'note', 'region', 'platform', 'region_restriction', 'kind', 'info', 'chat')},
            'detail': {key: detail.get(key) for key in ('name', 'GameName', 'note', 'fields', 'region', 'platform', 'region_restriction', 'region_restrictions')},
            'offer': {key: offer.get(key) for key in ('name', 'min_order_quantity', 'region', 'platform', 'region_restriction', 'region_restrictions')},
        }
        contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        rows.append({
            "family": record.family,
            "provider_category_id": record.provider_category_id,
            "provider_offer_id": record.provider_offer_id,
            "offer_id": f"pending-{digest}",
            "product_slug": "pending-review",
            "product_name": "Pending operator review",
            "fulfillment_kind": family_kind,
            "denomination_label": record.display_name,
            "source_observed_at": record.observed_at.isoformat(),
            "source_contract_sha256": contract_hash,
            "review_state": "blocked",
            "customer_visible": False,
            "supplier_enabled": False,
            "resale_authorized": False,
            "region_rule": {"verified": False, "allow_all": False},
            "region_label": None,
            "region_dimension": None,
            "required_input_labels": [],
        })
    rows.sort(key=lambda row: (
        row["family"], row["provider_category_id"], row["provider_offer_id"]
    ))
    return {
        "schema_version": 1,
        "queue_status": "all_blocked_template",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_captured_at": captured.isoformat(),
        "source_status": snapshot.get("status"),
        "mapping_rows": len(rows),
        "approval_required": [
            "product_slug",
            "product_name",
            "fulfillment_kind",
            "denomination_label",
            "region_rule",
            "required_input_labels",
            "resale_authorized",
            "supplier_enabled",
            "customer_visible",
            "review_state",
        ],
        "mappings": rows,
    }


def reconcile_queue(snapshot: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Build a separate review candidate; never discard old rows or approval history."""
    generated = build_blocked_queue(snapshot)
    def index(rows: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
        if not isinstance(rows, list):
            raise ValueError('mapping rows must be an array')
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('mapping rows must be objects')
            key = tuple(row.get(k) for k in ('family', 'provider_category_id', 'provider_offer_id'))
            if not all(isinstance(v, str) and v.strip() for v in key) or key in result:
                raise ValueError('invalid or duplicate provider key')
            result[key] = row
        return result

    existing, observed = index(previous.get('mappings')), index(generated['mappings'])
    candidate = deepcopy(previous)
    rows = []
    paused = 0
    for key, original in existing.items():
        row = deepcopy(original)
        current = observed.get(key)
        row['source_presence'] = 'observed' if current else 'not_observed'
        if current:
            changed = row.get('source_contract_sha256') != current['source_contract_sha256']
            if changed and row.get('review_state') in {'approved', 'pending_review'}:
                row.update(review_state='paused', customer_visible=False, supplier_enabled=False,
                           source_review_required=True)
                paused += 1
            row['source_contract_sha256'] = current['source_contract_sha256']
            row['source_observed_at'] = current['source_observed_at']
        rows.append(row)
    for key in observed.keys() - existing.keys():
        rows.append({**observed[key], 'source_presence': 'observed'})
    identities = [row.get('offer_id') for row in rows]
    if any(not isinstance(value, str) or not value.strip() for value in identities) or len(set(identities)) != len(identities):
        raise ValueError('invalid or duplicate AuriX offer identity')
    rows.sort(key=lambda row: (row['family'], row['provider_category_id'], row['provider_offer_id']))
    candidate.update(mappings=rows, mapping_rows=len(rows), queue_status='reconciled_review_candidate',
                     source_captured_at=generated['source_captured_at'], source_status=generated['source_status'])
    candidate['reconciliation'] = {
        'created_at': datetime.now(UTC).isoformat(),
        'base_config_sha256': hashlib.sha256(json.dumps(previous, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        'observed': len(observed), 'added_blocked': len(observed.keys() - existing.keys()),
        'retained_not_observed': len(existing.keys() - observed.keys()),
        'contract_reviews_paused': paused,
    }
    return candidate


def _write_private(path: Path, payload: dict[str, Any], *, force: bool = False) -> None:
    if path.is_symlink():
        raise ValueError("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("output directory must be private")
    if path.exists() and not force:
        raise FileExistsError(f"output exists; pass --force to replace: {path}")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true", help="replace an existing private queue")
    parser.add_argument('--merge-existing', type=Path, help='preserve an existing private queue in a separate reconciliation candidate')
    args = parser.parse_args(argv)
    if args.merge_existing and args.merge_existing.resolve() == args.output.resolve():
        parser.error('reconciliation output must differ from the existing config')
    if args.snapshot.resolve() == args.output.resolve():
        parser.error('output must differ from the source snapshot')
    snapshot = _read(args.snapshot)
    queue = reconcile_queue(snapshot, _read_private(args.merge_existing)) if args.merge_existing else build_blocked_queue(snapshot)
    if not args.merge_existing and args.output.exists() and args.force:
        previous = PricingPolicyStore(args.output).status()
        if previous["policy_status"] in {"approved", "expired"}:
            queue["policy"] = previous["policy"]
            queue["pricing_policy_approval"] = {
                "status": "approved",
                **previous["approval"],
            }
    _write_private(args.output, queue, force=args.force)
    print(json.dumps({
        "status": queue["queue_status"],
        "mapping_rows": queue["mapping_rows"],
        "output": str(args.output),
        "writes": False,
        "supplier_key_used": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
