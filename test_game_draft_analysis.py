import unittest

from aurix_games.draft_analysis import analyze_draft
from aurix_games.strategy_guides import StrategyGuideInputError


def guide():
    return {"sections": [{"id": "hero-tiers", "heroes": [
        {"slug": "flex", "lanes": ["Roam", "Mid Lane"]},
        {"slug": "roamer", "lanes": ["Roam"]},
        {"slug": "no-lane", "lanes": []},
    ]}], "snapshot": {"patch": None}}


class DraftAnalysisTests(unittest.TestCase):
    def test_exact_matching_resolves_greedy_conflict_and_reports_missing_data(self):
        result = analyze_draft({"ally": ["flex", "roamer", "no-lane"]}, guide())
        team = result["teams"]["ally"]
        self.assertEqual(team["matched_count"], 2)
        self.assertIn({"hero_slug": "flex", "lane": "Mid Lane"}, team["assignments"])
        self.assertEqual(team["unassigned_heroes"], ["no-lane"])
        self.assertEqual(result["teams"]["enemy"]["selected_count"], 0)
        self.assertNotIn("win_rate", result)
        self.assertEqual(result["snapshot"]["patch"], None)
        self.assertEqual(result, analyze_draft({"ally": ["roamer", "flex", "no-lane"]}, guide()))

    def test_invalid_selection_and_roster_fail_closed(self):
        for payload in (None, {"ally": "flex"}, {"ally": ["flex"] * 6},
                        {"ally": ["flex"], "enemy": ["flex"]}, {"ally": ["unknown"]},
                        {"ally": [None]}, {"ally": [], "win_rate": 100}):
            with self.subTest(payload=payload), self.assertRaises(StrategyGuideInputError):
                analyze_draft(payload, guide())
        bad = guide()
        bad["sections"][0]["heroes"].append({"slug": "flex", "lanes": ["Roam"]})
        with self.assertRaises(StrategyGuideInputError):
            analyze_draft({}, bad)
