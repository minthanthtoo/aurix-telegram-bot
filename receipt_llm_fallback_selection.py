"""Candidate ranking and consensus policy for receipt extraction fallbacks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable


def select_fallback_candidate(
    *,
    candidates: list[tuple[Any, dict[str, Any]]],
    attempts: list[dict[str, Any]],
    selection_mode: str,
    normalized_byte_size: int,
    is_acceptable: Callable[[Any], bool],
    score: Callable[[Any], float],
    consensus_key: Callable[[Any], tuple[str, str, str, str]],
) -> tuple[Any, dict[str, Any]] | None:
    """Rank collected routes and annotate disagreements for manual review."""
    if not candidates:
        return None
    acceptable = [item for item in candidates if is_acceptable(item[0])]
    ranked = sorted(
        enumerate(acceptable or candidates),
        key=lambda item: (score(item[1][0]), -item[0]),
        reverse=True,
    )
    extraction, diagnostics = ranked[0][1]
    if selection_mode in {"rank_all", "consensus"}:
        consensus = {consensus_key(item[0]) for item in acceptable}
        review_flags: set[str] = set()
        if len(acceptable) < 2 and selection_mode == "consensus":
            review_flags.add("consensus_unavailable")
        elif len(consensus) > 1:
            review_flags.add("model_disagreement")
        if review_flags:
            extraction = replace(
                extraction,
                flags=tuple(sorted(set(extraction.flags) | review_flags)),
            )
    diagnostics = dict(diagnostics)
    diagnostics["attempts"] = attempts
    diagnostics["selected_model"] = diagnostics.get("model")
    diagnostics["normalized_byte_size"] = normalized_byte_size
    diagnostics["selection_mode"] = selection_mode
    diagnostics["candidate_scores"] = [
        {
            "model": item[1].get("model"),
            "score": round(score(item[0]), 3),
            "acceptable": is_acceptable(item[0]),
        }
        for item in candidates
    ]
    if selection_mode == "consensus" and len(acceptable) >= 2:
        diagnostics["consensus"] = len(
            {consensus_key(item[0]) for item in acceptable}
        ) == 1
    return extraction, diagnostics
