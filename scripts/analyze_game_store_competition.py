#!/usr/bin/env python3
"""Offline comparison of the dated TGI reference prices and a private FZ snapshot.

This command makes no HTTP calls and never prints supplier IDs or wholesale
prices.  It emits only price-headroom percentages so an operator can repeat a
competitive study without turning the private supplier tariff into a public
catalog artifact.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

from aurix_games.catalog import _REFERENCE_DETAIL_SNAPSHOTS


DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_FX_RATES = (Decimal("4300"), Decimal("4500"), Decimal("4800"))
DEFAULT_ALLOWANCES = Decimal("0.04")  # payment + marketing + wallet credit


def _norm(value: Any) -> str:
    text = str(value).casefold().replace(",", "")
    text = text.replace("diamonds", "diamond")
    text = text.replace("diamondx", "diamond")
    text = text.replace("(first top-up bonus)", "")
    return re.sub(r"[^a-z0-9]+", "", text)


def _money(value: Any) -> int:
    match = re.search(r"[0-9][0-9,]*", str(value))
    if not match:
        raise ValueError(f"reference price is not numeric: {value!r}")
    return int(match.group(0).replace(",", ""))


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} is not numeric") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be a finite non-negative decimal")
    return result


def _fazer_offers(snapshot: dict[str, Any], category_id: str) -> dict[str, Decimal]:
    family = snapshot.get("families", {}).get("topups", {})
    details = family.get("details_by_id", {}) if isinstance(family, dict) else {}
    detail = details.get(category_id, {}) if isinstance(details, dict) else {}
    offers = detail.get("offers", []) if isinstance(detail, dict) else []
    result: dict[str, Decimal] = {}
    for offer in offers:
        if not isinstance(offer, dict) or not offer.get("name"):
            continue
        try:
            result[_norm(offer["name"])] = _decimal(offer.get("price_usd"), "supplier price")
        except ValueError:
            continue
    return result


def _reference_rows() -> list[dict[str, Any]]:
    detail = _REFERENCE_DETAIL_SNAPSHOTS["mobile-legends"]
    rows: list[dict[str, Any]] = []
    for region in detail["regions"]:
        region_id = str(region["id"])
        for offer in region["offers"]:
            rows.append(
                {
                    "region": region_id,
                    "label": str(offer["name"]),
                    "reference_price_mmk": _money(offer["observed_price"]),
                }
            )
    return rows


def _supplier_category_for_region(region: str) -> tuple[str, ...]:
    if region == "global":
        return ("mobile_legends_global",)
    # The dated reference says SG/Malay/Indo without a single canonical
    # server.  Return each aligned regional tariff; callers can see the
    # sensitivity instead of silently using a cross-region price.
    return (
        "mobile_legends_singapore",
        "mobile_legends_malaysia",
        "mobile_legends_indonesia",
    )


def build_analysis(
    snapshot: dict[str, Any],
    *,
    fx_rates: tuple[Decimal, ...] = DEFAULT_FX_RATES,
    allowances_pct: Decimal = DEFAULT_ALLOWANCES,
) -> dict[str, Any]:
    """Return sanitized headroom percentages for matched reference rows."""

    if not fx_rates or any(rate <= 0 for rate in fx_rates):
        raise ValueError("fx_rates must be positive")
    if allowances_pct < 0 or allowances_pct >= 1:
        raise ValueError("allowances_pct must be in [0, 1)")

    output_rows: list[dict[str, Any]] = []
    for row in _reference_rows():
        for category_id in _supplier_category_for_region(row["region"]):
            offers = _fazer_offers(snapshot, category_id)
            cost = offers.get(_norm(row["label"]))
            if cost is None:
                continue
            item: dict[str, Any] = {
                "region": row["region"],
                "supplier_region": category_id.rsplit("_", 1)[-1],
                "label": row["label"],
                "reference_price_mmk": row["reference_price_mmk"],
                "scenarios": [],
            }
            for fx in fx_rates:
                converted = cost * fx
                headroom = (Decimal(row["reference_price_mmk"]) - converted) / Decimal(
                    row["reference_price_mmk"]
                )
                after_allowances = headroom - allowances_pct
                item["scenarios"].append(
                    {
                        "fx_rate": format(fx, "f"),
                        "headroom_pct": round(float(headroom * 100), 2),
                        "after_allowances_pct": round(float(after_allowances * 100), 2),
                    }
                )
            output_rows.append(item)

    return {
        "snapshot_captured_at": str(snapshot.get("captured_at", "unknown")),
        "reference_observed_on": str(_REFERENCE_DETAIL_SNAPSHOTS["mobile-legends"].get("observed_on")),
        "allowances_pct": format(allowances_pct, "f"),
        "rows": output_rows,
        "limitations": [
            "Reference prices are dated observations, not a live TGI quote.",
            "A missing exact denomination is not treated as a match.",
            "Headroom is not a competitor marketing-spend estimate.",
            "Regional tariffs are kept separate; no cross-region arbitrage is assumed.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--fx-rate", action="append", default=[])
    parser.add_argument("--allowances-pct", default=str(DEFAULT_ALLOWANCES))
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        rates = tuple(_decimal(value, "fx rate") for value in args.fx_rate) or DEFAULT_FX_RATES
        result = build_analysis(
            snapshot,
            fx_rates=rates,
            allowances_pct=_decimal(args.allowances_pct, "allowances percentage"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"competitive analysis failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
