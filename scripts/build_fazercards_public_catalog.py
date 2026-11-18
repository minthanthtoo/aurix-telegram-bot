#!/usr/bin/env python3
"""Build a customer-safe, dynamically priced catalog from a private snapshot.

The snapshot and mapping file are operator data.  The output contains only
AuriX-owned IDs, labels, final prices, currency, region, and required input
labels.  Missing mappings, stale observations, unverified rights, and
unavailable rows are omitted by the projection gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator

from aurix_games.eligibility import RegionDimension, RegionRule
from aurix_games.live_catalog import OfferMapping, SupplierOfferRecord, build_priced_offers
from aurix_games.pricing_store import PricingPolicyStore, catalog_config_revision
from aurix_games.catalog_policy import env_catalog_policy_store
from aurix_games.public_catalog import _safe_offers, DEFAULT_PUBLIC_CATALOG_MAX_AGE


UTC = timezone.utc
DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_CONFIG = Path(".local/fazercards/public-catalog-config.json")
DEFAULT_OUTPUT = Path(".local/fazercards/public-catalog.json")
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON input: {path}") from exc


def _records(snapshot: dict[str, Any], observed_at: datetime) -> Iterator[SupplierOfferRecord]:
    families = snapshot.get("families")
    if not isinstance(families, dict):
        raise SystemExit("snapshot omitted families")
    for family in ("topups", "giftcards", "gamekeys"):
        container = families.get(family, {})
        if not isinstance(container, dict):
            continue
        details = container.get("details_by_id", {})
        if not isinstance(details, dict):
            continue
        detail_times = container.get("details_observed_at_by_id", {})
        category_time = _parse_timestamp(container.get("categories_observed_at"))
        for category_id, detail in details.items():
            if not isinstance(detail, dict):
                continue
            category = str(detail.get("category_id") or detail.get("game_id") or category_id)
            detail_time = (
                _parse_timestamp(detail_times.get(str(category_id)))
                if isinstance(detail_times, dict)
                else None
            )
            row_observed_at = (
                min(detail_time, category_time)
                if detail_time and category_time
                else observed_at
            )
            offers = detail.get("offers") if family != "gamekeys" else detail.get("keys")
            if not isinstance(offers, list):
                continue
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                offer_id = offer.get("offer_id") if family == "topups" else offer.get("card_id") if family == "giftcards" else offer.get("key_id")
                if not isinstance(offer_id, str):
                    continue
                raw_cost = offer.get("price_usd")
                try:
                    cost = Decimal(str(raw_cost))
                except Exception:
                    continue
                stock = offer.get("stock")
                available = type(stock) is int and stock > 0
                if family in {"giftcards", "gamekeys"}:
                    minimum, maximum = offer.get("min_order_quantity"), offer.get("max_order_quantity")
                    available = (isinstance(stock, int) and not isinstance(stock, bool) and stock > 0
                        and type(minimum) is int and minimum == 1
                        and type(maximum) is int and maximum >= 1)
                yield SupplierOfferRecord(
                    family=family,
                    provider_category_id=category,
                    provider_offer_id=offer_id,
                    display_name=str(offer.get("name") or offer_id),
                    supplier_cost_usd=cost,
                    observed_at=row_observed_at,
                    available=available,
                )

    # Manual services are a separate supplier family with exact item IDs and
    # prices. Keep them in the same blocked-by-default projection pipeline;
    # their mapping still requires explicit manual_service fulfillment review.
    manual = families.get("manual_services", {})
    if not isinstance(manual, dict):
        return
    categories = manual.get("items", [])
    details = manual.get("offers_by_id", {})
    detail_times = manual.get("offers_observed_at_by_id", {})
    category_time = _parse_timestamp(manual.get("items_observed_at"))
    if not isinstance(categories, list) or not isinstance(details, dict):
        return
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_id = category.get("id")
        detail = details.get(category_id) if isinstance(category_id, str) else None
        detail_time = (
            _parse_timestamp(detail_times.get(category_id))
            if isinstance(detail_times, dict) and isinstance(category_id, str)
            else None
        )
        row_observed_at = (
            min(detail_time, category_time)
            if detail_time and category_time
            else observed_at
        )
        offers = detail.get("items", []) if isinstance(detail, dict) else []
        if not isinstance(offers, list):
            continue
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            offer_id = offer.get("id")
            if not isinstance(offer_id, str):
                continue
            try:
                cost = Decimal(str(offer.get("price_usd")))
            except Exception:
                continue
            if not cost.is_finite() or cost < 0:
                continue
            stock = offer.get("stock")
            available = stock is None or (isinstance(stock, int) and not isinstance(stock, bool) and stock > 0)
            yield SupplierOfferRecord(
                family="manual_services",
                provider_category_id=category_id,
                provider_offer_id=offer_id,
                display_name=str(offer.get("name") or offer_id),
                supplier_cost_usd=cost,
                observed_at=row_observed_at,
                available=available,
            )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _mapping(row: dict[str, Any]) -> tuple[tuple[str, str, str], OfferMapping]:
    def text(key: str, max_length: int = 160) -> str:
        value = row.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise SystemExit(f"mapping field {key} is invalid")
        return value.strip()

    raw_rule = row.get("region_rule", {})
    if not isinstance(raw_rule, dict):
        raise SystemExit("mapping region_rule is invalid")
    def flag(container: dict[str, Any], key: str, default: bool) -> bool:
        value = container.get(key, default)
        if not isinstance(value, bool):
            raise SystemExit(f"mapping field {key} must be boolean")
        return value

    dimension = raw_rule.get("dimension")
    region_dimension = RegionDimension(dimension) if dimension is not None else None
    allowed = raw_rule.get("allowed_values")
    if allowed is not None and (
        not isinstance(allowed, list)
        or not all(isinstance(value, str) and value.strip() for value in allowed)
    ):
        raise SystemExit("mapping allowed_values must be a list of non-empty strings")
    rule = RegionRule(
        verified=flag(raw_rule, "verified", False),
        allow_all=flag(raw_rule, "allow_all", False),
        dimension=region_dimension,
        allowed_values=None if allowed is None else frozenset(allowed),
    )
    raw_labels = row.get("required_input_labels", ())
    if not isinstance(raw_labels, (list, tuple)) or not all(
        isinstance(label, str) and label.strip() for label in raw_labels
    ):
        raise SystemExit("mapping required_input_labels must be a list of non-empty strings")
    region_label = row.get("region_label")
    if region_label is not None and (
        not isinstance(region_label, str) or not region_label.strip()
    ):
        raise SystemExit("mapping region_label is invalid")
    review_state = row.get("review_state", "blocked")
    if not isinstance(review_state, str) or review_state not in {
        "blocked", "pending_review", "approved", "paused"
    }:
        raise SystemExit("mapping review_state is invalid")
    key = (text("family", 32), text("provider_category_id"), text("provider_offer_id"))
    return key, OfferMapping(
        offer_id=text("offer_id"),
        product_slug=text("product_slug"),
        product_name=text("product_name"),
        fulfillment_kind=text("fulfillment_kind", 64),
        denomination_label=text("denomination_label"),
        review_state=review_state,
        customer_visible=flag(row, "customer_visible", False),
        supplier_enabled=flag(row, "supplier_enabled", False),
        resale_authorized=flag(row, "resale_authorized", False),
        region_rule=rule,
        region_label=region_label,
        region_dimension=region_dimension,
        required_input_labels=tuple(raw_labels),
    )


def _write_private_output(
    path: Path, payload: dict[str, Any], *, config_path: Path,
) -> None:
    """Fence competing publishers and revalidate approval before atomic publication."""

    if path.is_symlink():
        raise SystemExit("public catalog output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise SystemExit("public catalog output directory must be private")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(data) > MAX_OUTPUT_BYTES:
        raise SystemExit("public catalog output exceeded the 16 MiB safety limit")
    temporary = None
    lock_fd = os.open(path.with_name(f'.{path.name}.lock'),
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = datetime.now(UTC)
        _safe_offers(payload, now=current, max_age=DEFAULT_PUBLIC_CATALOG_MAX_AGE)
        config, _, _ = PricingPolicyStore(config_path).approved_snapshot(
            now=current,
            allow_expired=True,
        )
        if payload.get('config_revision') != catalog_config_revision(config):
            raise ValueError('catalog approval changed during refresh; previous catalog preserved')
        if path.is_symlink():
            raise ValueError('public catalog output must not be a symlink')
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeDecodeError):
                previous = None
            if isinstance(previous, dict):
                for field in ('generated_at', 'source_observed_at'):
                    old = _parse_timestamp(previous.get(field))
                    if old is not None and old > datetime.fromisoformat(payload[field]):
                        raise ValueError('newer catalog already published; previous catalog preserved')
        fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise SystemExit("public catalog output could not be published") from exc
    finally:
        os.close(lock_fd)
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-age-minutes", type=int, default=15)
    args = parser.parse_args(argv)
    if not 1 <= args.max_age_minutes <= 15:
        parser.error('--max-age-minutes must be between 1 and 15')
    if len({p.resolve() for p in (args.snapshot, args.config, args.output)}) != 3:
        parser.error('snapshot, config and output must be separate files')
    snapshot = _read(args.snapshot)
    if not isinstance(snapshot, dict):
        raise SystemExit("snapshot must be a JSON object")
    try:
        observed_at = _parse_timestamp(snapshot.get('captured_at'))
        if observed_at is None:
            raise ValueError('snapshot capture time must include a timezone')
        config, policy, _ = PricingPolicyStore(args.config).approved_snapshot(allow_expired=True)
        mapping_rows = config.get("mappings", [])
        if not isinstance(mapping_rows, list) or not all(
            isinstance(row, dict) for row in mapping_rows
        ):
            raise SystemExit("config mappings must be an array")
        mapping_pairs = [_mapping(row) for row in mapping_rows]
        if len({key for key, _ in mapping_pairs}) != len(mapping_pairs):
            raise SystemExit("config mappings contain duplicate provider keys")
        mappings = dict(mapping_pairs)
        now = datetime.now(UTC)
        source_records = tuple(_records(snapshot, observed_at))
        priced = build_priced_offers(
            source_records,
            mappings,
            policy=policy,
            now=now,
            max_age=timedelta(minutes=args.max_age_minutes),
            catalog_policy=env_catalog_policy_store(args.config.parent).active_policy(),
        )
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"catalog build input is invalid: {exc}") from exc
    public_offers = []
    for item in priced:
        public = item.public_offer.as_dict()
        public["promo_discount_minor"] = item.breakdown.applied_promo_discount_minor
        public_offers.append(public)
    projected_offer_ids = {item.public_offer.offer_id for item in priced}
    projected_observations = []
    for record in source_records:
        mapping = mappings.get((record.family, record.provider_category_id, record.provider_offer_id))
        if mapping is not None and mapping.offer_id in projected_offer_ids:
            projected_observations.append(record.observed_at)
    source_observed_at = min(projected_observations, default=observed_at)
    result = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "source_observed_at": source_observed_at.isoformat(),
        "valid_until": (source_observed_at + timedelta(minutes=args.max_age_minutes)).isoformat(),
        "config_revision": catalog_config_revision(config),
        "status": "approved_projection",
        "currency": policy.sell_currency,
        "offers": public_offers,
        "offer_count": len(priced),
    }
    _write_private_output(args.output, result, config_path=args.config)
    print(json.dumps({"status": result["status"], "offer_count": len(priced), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
