"""Deterministic roster-based lane matching, without inferred match statistics."""
from collections.abc import Mapping
import hashlib
import json

from .strategy_guides import StrategyGuideInputError

LANES = ("Gold Lane", "Exp Lane", "Mid Lane", "Roam", "Jungle")


def analyze_draft(payload, guide):
    if not isinstance(payload, Mapping) or set(payload) - {"ally", "enemy"}:
        raise StrategyGuideInputError("draft must contain only ally and enemy hero slugs")
    teams = {}
    for side in ("ally", "enemy"):
        values = payload.get(side, [])
        if not isinstance(values, list) or len(values) > 5 or any(not isinstance(x, str) or not x or len(x) > 80 for x in values):
            raise StrategyGuideInputError("each team must contain at most five hero slugs")
        teams[side] = values
    chosen = teams["ally"] + teams["enemy"]
    if len(set(chosen)) != len(chosen):
        raise StrategyGuideInputError("a hero cannot appear twice or on both teams")
    sections = guide.get("sections", []) if isinstance(guide, dict) else []
    heroes = next((s.get("heroes", []) for s in sections if s.get("id") == "hero-tiers"), [])
    roster = {}
    for hero in heroes:
        if not isinstance(hero, dict) or not isinstance(hero.get("slug"), str) or hero["slug"] in roster:
            raise StrategyGuideInputError("guide roster is ambiguous")
        lanes = hero.get("lanes", [])
        if not isinstance(lanes, list) or any(lane not in LANES for lane in lanes):
            raise StrategyGuideInputError("guide roster has unsupported lane data")
        roster[hero["slug"]] = hero
    if any(slug not in roster for slug in chosen):
        raise StrategyGuideInputError("selected hero is absent from the current roster")

    def coverage(slugs):
        best = []
        def search(index, used, assignments):
            nonlocal best
            if index == len(slugs):
                if len(assignments) > len(best):
                    best = list(assignments)
                return
            slug = slugs[index]
            for lane in LANES:
                if lane not in used and lane in roster[slug]["lanes"]:
                    search(index + 1, used | {lane}, assignments + [{"hero_slug": slug, "lane": lane}])
            search(index + 1, used, assignments)
        search(0, set(), [])
        assigned = {row["hero_slug"] for row in best}
        covered = {row["lane"] for row in best}
        return {
            "selected_count": len(slugs), "matched_count": len(best),
            "assignments": best, "open_lanes": [lane for lane in LANES if lane not in covered],
            "unassigned_heroes": [slug for slug in slugs if slug not in assigned],
        }

    revision = hashlib.sha256(json.dumps(heroes, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {
        "schema_version": 1, "algorithm": "maximum_distinct_lane_matching_v1",
        "roster_revision": revision, "source": guide.get("source", {}),
        "snapshot": guide.get("snapshot", {}), "rankings_freshness": guide.get("rankings_freshness", {}),
        "verification": "community_snapshot_not_independently_fact_checked",
        "teams": {side: coverage(sorted(slugs)) for side, slugs in teams.items()},
        "notice": "Lane feasibility from the recorded roster only. Open lanes are one possible assignment, not mandatory hero roles. No counter, synergy or win-probability prediction.",
    }
