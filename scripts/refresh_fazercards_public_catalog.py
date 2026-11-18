#!/usr/bin/env python3
"""Refresh FazerCards catalogs and publish the sanitized AuriX projection.

This is an operator-side, read-only catalog refresh. It chains the existing
cursor-checkpointed FazerCards inventory collector with the public pricing
builder. It never calls an order, invoice, validation-submit, webhook, or
settings endpoint. A failed refresh leaves the previous public artifact in
place; the private inventory checkpoint remains available for ``--resume``.

Provide the rotated API key through ``FAZERCARDS_API_KEY`` or one time on
stdin with ``--key-stdin``. The key is passed only to the in-memory GET client
and is never written to the private snapshot, public artifact, logs, or error
messages.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from aurix_games.providers.fazercards import CatalogReadError, FazerCardsReadOnlyClient
from aurix_games.live_catalog import SupplierOfferRecord, build_priced_offers
from aurix_games.pricing_store import PricingPolicyStore, catalog_config_revision
from aurix_games.catalog_policy import env_catalog_policy_store
from scripts.build_fazercards_public_catalog import _mapping, _write_private_output
from scripts.build_fazercards_public_catalog import main as build_public_catalog
from scripts.fazercards_catalog_inventory import (
    _write_snapshot,
    collect_inventory,
)


DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_CONFIG = Path(".local/fazercards/public-catalog-config.json")
DEFAULT_OUTPUT = Path(".local/fazercards/public-catalog.json")


def _load_resume(path: Path, *, resume: bool) -> dict[str, Any] | None:
    if path.exists() and not resume:
        raise ValueError("snapshot exists; pass --resume or choose a new path")
    if not resume:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("resume snapshot is missing or invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("snapshot_version") != 1:
        raise ValueError("resume snapshot has an unsupported format")
    return payload


def _read_api_key(*, key_stdin: bool) -> str:
    api_key = (
        sys.stdin.readline().rstrip("\r\n")
        if key_stdin
        else os.environ.get("FAZERCARDS_API_KEY", "")
    )
    if not api_key:
        raise ValueError("provide FAZERCARDS_API_KEY or use --key-stdin")
    return api_key


def _require_core_families(payload: dict[str, Any]) -> None:
    """Do not replace a good public artifact with a partial core refresh."""

    families = payload.get("families")
    if not isinstance(families, dict):
        raise RuntimeError("inventory omitted catalog families")
    incomplete = [
        family
        for family in ("topups", "giftcards", "gamekeys")
        if not isinstance(families.get(family), dict)
        or families[family].get("details_complete") is not True
    ]
    if incomplete:
        raise RuntimeError(
            "core catalog refresh is incomplete; previous public catalog was preserved"
        )


def refresh(
    *,
    snapshot_path: Path,
    config_path: Path,
    output_path: Path,
    api_key: str,
    resume: bool,
    min_interval: float,
    max_age_minutes: int,
) -> dict[str, Any]:
    """Run the two safe stages and return aggregate status metadata."""

    if len({path.resolve() for path in (snapshot_path, config_path, output_path)}) != 3:
        raise ValueError("snapshot, config and output must be separate files")
    if not 1 <= max_age_minutes <= 15:
        raise ValueError("catalog freshness must be between 1 and 15 minutes")
    initial = _load_resume(snapshot_path, resume=resume)
    client = FazerCardsReadOnlyClient(api_key, min_interval_seconds=min_interval)
    payload = collect_inventory(
        client,
        initial=initial,
        checkpoint=lambda current: _write_snapshot(snapshot_path, current),
    )
    _write_snapshot(snapshot_path, payload)
    _require_core_families(payload)

    # The builder reads the private snapshot/config and atomically replaces the
    # public artifact only after a complete, fresh, approved projection exists.
    try:
        result = build_public_catalog(
            [
                "--snapshot",
                str(snapshot_path),
                "--config",
                str(config_path),
                "--output",
                str(output_path),
                "--max-age-minutes",
                str(max_age_minutes),
            ]
        )
    except SystemExit as exc:
        raise RuntimeError("public catalog builder rejected the refresh inputs") from exc
    if result != 0:
        raise RuntimeError("public catalog builder failed")
    return {
        "inventory_status": payload.get("status", "unknown"),
        "inventory_coverage_complete": bool(
            isinstance(payload.get("coverage"), dict)
            and payload["coverage"].get("complete") is True
        ),
        "core_catalog_complete": True,
        "snapshot": str(snapshot_path),
        "public_catalog": str(output_path),
    }


def _positive_price(value: Any) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError("price must be numeric")
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid supplier price") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("supplier price must be positive and finite")
    return price


def refresh_shortlist(
    *, shortlist_path: Path, snapshot_path: Path, config_path: Path, output_path: Path,
    api_key: str, min_interval: float, max_age_minutes: int,
) -> dict[str, Any]:
    """Read exact top-up categories and atomically publish only eligible shortlist rows."""
    paths = (shortlist_path, snapshot_path, config_path, output_path)
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("shortlist, config, evidence and output must be separate files")
    if snapshot_path.exists():
        raise ValueError("choose a new evidence snapshot path")
    if not math.isfinite(min_interval) or min_interval < 0:
        raise ValueError("request interval must be finite and non-negative")
    if not 1 <= max_age_minutes <= 15:
        raise ValueError("shortlist freshness must be between 1 and 15 minutes")
    shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    pricing_store = PricingPolicyStore(config_path)
    pricing_store.refresh_auto_fx_if_due()
    config, policy, _ = pricing_store.approved_snapshot(allow_expired=True)
    if not isinstance(shortlist, list) or not 1 <= len(shortlist) <= 30:
        raise ValueError("shortlist must contain between 1 and 30 rows")
    if not isinstance(config, dict) or not isinstance(config.get("mappings"), list):
        raise ValueError("invalid mapping config")
    try:
        mapping_pairs = [_mapping(row) for row in config["mappings"] if isinstance(row, dict)]
    except SystemExit as exc:
        raise ValueError("invalid mapping config") from exc
    mappings = dict(mapping_pairs)
    if len(mappings) != len(config["mappings"]):
        raise ValueError("invalid or duplicate mapping rows")
    selections = {}
    for row in shortlist:
        if not isinstance(row, dict) or set(row) != {
            "category_id", "offer_id", "max_cost_usd", "allow_unknown_stock",
        }:
            raise ValueError("invalid shortlist row")
        category, offer_id = row["category_id"], row["offer_id"]
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
               for value in (category, offer_id)):
            raise ValueError("invalid shortlist identifier")
        key = ("topups", category, offer_id)
        mapping = mappings.get(key)
        if key in selections or type(row["allow_unknown_stock"]) is not bool:
            raise ValueError("duplicate offer or invalid stock policy")
        if (mapping is None or mapping.review_state != "approved" or not mapping.customer_visible
                or not mapping.supplier_enabled or not mapping.resale_authorized or not mapping.region_rule.verified
                or mapping.fulfillment_kind != "direct_topup"):
            raise ValueError("shortlist requires approved direct top-up mappings")
        selections[key] = (_positive_price(row["max_cost_usd"]), row["allow_unknown_stock"])
    if len({mappings[key].offer_id for key in selections}) != len(selections):
        raise ValueError("shortlist maps multiple supplier offers to the same store offer")

    client = FazerCardsReadOnlyClient(api_key, min_interval_seconds=min_interval)
    started = datetime.now(timezone.utc)
    details = {}
    for category in dict.fromkeys(key[1] for key in selections):
        observed = datetime.now(timezone.utc)
        detail = client.get("/topups/offers", category_id=category)
        if detail.get("category_id") not in (None, category) or not isinstance(detail.get("offers"), list):
            raise ValueError("unexpected supplier category response")
        offers = {}
        for offer in detail["offers"]:
            if not isinstance(offer, dict) or not isinstance(offer.get("offer_id"), str):
                raise ValueError("supplier response has a malformed offer")
            sku = offer["offer_id"]
            if sku in offers:
                raise ValueError("supplier response has a duplicate offer")
            offers[sku] = offer
        details[category] = (observed, offers)

    records, observations = [], []
    for (family, category, sku), (cap, allow_unknown) in selections.items():
        observed, offers = details[category]
        offer = offers.get(sku)
        status, stock, cost = "missing_offer", None, None
        if offer is not None:
            stock = offer.get("stock")
            try:
                cost = _positive_price(offer.get("price_usd"))
            except ValueError:
                status = "invalid_price"
            else:
                if cost > cap:
                    status = "above_cost_ceiling"
                elif stock is None:
                    status = "eligible_unknown_stock" if allow_unknown else "unknown_stock"
                elif type(stock) is not int or stock < 0:
                    status = "invalid_stock"
                else:
                    status = "eligible" if stock > 0 else "out_of_stock"
        if status in {"eligible", "eligible_unknown_stock"}:
            records.append(SupplierOfferRecord(
                family=family, provider_category_id=category, provider_offer_id=sku,
                display_name=str(offer.get("name") or sku), supplier_cost_usd=cost,
                observed_at=observed, available=True,
            ))
        observations.append({
            "category_id": category, "offer_id": sku, "observed_at": observed.isoformat(),
            "stock": stock, "price_usd": str(cost) if cost is not None else None,
            "max_cost_usd": str(cap), "status": status,
        })

    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=max_age_minutes)
    if now - started >= max_age:
        raise RuntimeError("shortlist refresh exceeded its freshness window; previous catalog preserved")
    priced = build_priced_offers(records, mappings, policy=policy, now=now, max_age=max_age,
                                catalog_policy=env_catalog_policy_store(config_path.parent).active_policy())
    public_offers = []
    for item in priced:
        public = item.public_offer.as_dict()
        public["promo_discount_minor"] = item.breakdown.applied_promo_discount_minor
        public_offers.append(public)
    _write_snapshot(snapshot_path, {
        "scope": "topup_shortlist", "captured_at": started.isoformat(),
        "completed_at": now.isoformat(), "observations": observations,
    })
    _write_private_output(output_path, {
        "schema_version": 1, "generated_at": now.isoformat(),
        "source_observed_at": started.isoformat(), "status": "approved_projection",
        "valid_until": (started + max_age).isoformat(),
        "config_revision": catalog_config_revision(config),
        "currency": policy.sell_currency, "offers": public_offers, "offer_count": len(public_offers),
    }, config_path=config_path)
    return {"scope": "topup_shortlist", "offer_count": len(public_offers),
            "selected_count": len(selections), "snapshot": str(snapshot_path), "public_catalog": str(output_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--shortlist", type=Path, help="private JSON list of exact approved top-up offers")
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument("--max-age-minutes", type=int, default=15)
    args = parser.parse_args(argv)
    if not 1 <= args.max_age_minutes <= 15:
        parser.error("--max-age-minutes must be between 1 and 15")
    if args.shortlist and (args.resume or args.snapshot.resolve() == DEFAULT_SNAPSHOT.resolve()
                           or args.output.resolve() == DEFAULT_OUTPUT.resolve()):
        parser.error("--shortlist requires a new --snapshot and a dedicated --output; --resume is not supported")
    try:
        options = dict(snapshot_path=args.snapshot, config_path=args.config, output_path=args.output,
                       api_key=_read_api_key(key_stdin=args.key_stdin), min_interval=args.min_interval,
                       max_age_minutes=args.max_age_minutes)
        if args.shortlist:
            summary = refresh_shortlist(shortlist_path=args.shortlist, **options)
        else:
            summary = refresh(resume=args.resume, **options)
    except (CatalogReadError, OSError, RuntimeError, ValueError) as exc:
        print(f"catalog refresh failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
