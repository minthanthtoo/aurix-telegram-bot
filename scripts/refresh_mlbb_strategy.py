#!/usr/bin/env python3
"""Refresh an operator-approved, artwork-free MLBB strategy snapshot.

This command performs only public GET requests to MLBBDex. It stores a small
hero/item projection with attribution metadata and never accepts credentials,
player identifiers, login cookies, match history, or supplier data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from aurix_games.strategy_guides import (
    MLBBDex_BASE_URL,
    DEFAULT_MLBB_SNAPSHOT_PATH,
    StrategyGuideInputError,
    fetch_mlbbdex,
    write_mlbb_snapshot,
)


DEFAULT_OUTPUT = DEFAULT_MLBB_SNAPSHOT_PATH

LANE_LABELS = {
    "Gold": "Gold Lane",
    "Exp": "Exp Lane",
    "Mid": "Mid Lane",
    "Jungle": "Jungle",
    "Roam": "Roam",
}


def _project_heroes(rows: list[Any], rankings: list[Any] | None = None) -> list[dict[str, Any]]:
    ranking_by_slug = {
        row["slug"]: row
        for row in rankings or []
        if isinstance(row, dict) and isinstance(row.get("slug"), str)
    }
    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not isinstance(row.get("name"), str):
            continue
        roles = row.get("roles")
        lanes = row.get("lanes")
        hero = {
            "slug": row["slug"],
            "name": row["name"],
            "roles": [value for value in roles if isinstance(value, str)] if isinstance(roles, list) else [],
            "lanes": [LANE_LABELS.get(value, value) for value in lanes if isinstance(value, str)] if isinstance(lanes, list) else [],
        }
        ranking = ranking_by_slug.get(row["slug"])
        if ranking:
            if isinstance(ranking.get("tier"), str):
                hero["tier"] = ranking["tier"]
            for source_key, target_key in (
                ("winRate", "win_rate"),
                ("banRate", "ban_rate"),
                ("pickRate", "pick_rate"),
                ("score", "score"),
            ):
                value = ranking.get(source_key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    hero[target_key] = value
            if isinstance(ranking.get("lowSample"), bool):
                hero["low_sample"] = ranking["lowSample"]
        projected.append(hero)
    if not projected:
        raise StrategyGuideInputError("MLBBDex hero response had no safe rows")
    return projected


def _project_items(rows: list[Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not isinstance(row.get("name"), str):
            continue
        stats = row.get("stats")
        projected.append(
            {
                "slug": row["slug"],
                "name": row["name"],
                "category": row.get("category") if isinstance(row.get("category"), str) else "Unknown",
                "price": row.get("price") if isinstance(row.get("price"), int) else None,
                "stats": stats if isinstance(stats, list) else [],
                "summary": row.get("summary") if isinstance(row.get("summary"), str) else "",
                "bonus": row.get("bonus") if isinstance(row.get("bonus"), str) else "",
                "unique": row.get("unique") if isinstance(row.get("unique"), str) else "",
                "passive": row.get("passive") if isinstance(row.get("passive"), str) else "",
                "active": row.get("active") if isinstance(row.get("active"), str) else "",
                "recipe": [value for value in row.get("recipe", []) if isinstance(value, str)] if isinstance(row.get("recipe"), list) else [],
                "best_for": row.get("bestFor") if isinstance(row.get("bestFor"), str) else "",
            }
        )
    if not projected:
        raise StrategyGuideInputError("MLBBDex item response had no safe rows")
    return projected


def refresh(*, output: Path, timeout: float, base_url: str) -> dict[str, Any]:
    heroes_payload = fetch_mlbbdex("heroes", timeout=timeout, base_url=base_url)
    items_payload = fetch_mlbbdex("items", timeout=timeout, base_url=base_url)
    patches_payload = fetch_mlbbdex("patches", timeout=timeout, base_url=base_url)
    rankings_payload = fetch_mlbbdex("rankings", timeout=timeout, base_url=base_url)
    for payload in (heroes_payload, items_payload, patches_payload, rankings_payload):
        source = payload.get("source")
        if not isinstance(source, dict) or source.get("license") != "CC BY-SA":
            raise StrategyGuideInputError("MLBBDex attribution metadata is missing or changed")
    patch_rows = patches_payload.get("data")
    if not isinstance(patch_rows, list) or not patch_rows or not isinstance(patch_rows[0], dict):
        raise StrategyGuideInputError("MLBBDex patch response has no current version")
    current_patch = patch_rows[0].get("version")
    if not isinstance(current_patch, str) or not current_patch.strip() or len(current_patch) > 40:
        raise StrategyGuideInputError("MLBBDex current patch version is invalid")
    ranking_data = rankings_payload.get("data")
    if not isinstance(ranking_data, dict) or not isinstance(ranking_data.get("heroes"), list):
        raise StrategyGuideInputError("MLBBDex ranking response shape is invalid")
    measured_at = ranking_data.get("measuredAt")
    if not isinstance(measured_at, str) or not measured_at:
        raise StrategyGuideInputError("MLBBDex ranking timestamp is missing")
    heroes = _project_heroes(heroes_payload["data"], ranking_data["heroes"])
    items = _project_items(items_payload["data"])
    captured_at = datetime.now(timezone.utc).isoformat()
    write_mlbb_snapshot(
        output,
        heroes=heroes,
        items=items,
        patch=current_patch.strip(),
        captured_at=captured_at,
        rankings_measured_at=measured_at,
    )
    return {
        "output": str(output),
        "captured_at": captured_at,
        "heroes": len(heroes),
        "items": len(items),
        "patch": current_patch.strip(),
        "ranking_heroes": sum("tier" in hero for hero in heroes),
        "rankings_measured_at": measured_at,
        "source": "MLBBDex public API / Mobile Legends Wiki (CC BY-SA)",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--base-url", default=MLBBDex_BASE_URL)
    args = parser.parse_args(argv)
    try:
        result = refresh(output=args.output, timeout=args.timeout, base_url=args.base_url)
    except (OSError, ValueError, StrategyGuideInputError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
