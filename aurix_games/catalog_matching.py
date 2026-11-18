"""Conservative supplier-title candidate matching for private review only."""

from __future__ import annotations

import re
from typing import Any


def normalize_title(value: Any) -> str:
    """Case-fold and retain ASCII letters/digits, matching the existing report."""
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def candidate_match_method(reference_name: str, supplier_name: Any) -> str | None:
    """Return a heuristic label match, never a canonical product identity."""
    reference = normalize_title(reference_name)
    supplier = normalize_title(supplier_name)
    if not reference or not supplier:
        return None
    if reference == supplier:
        return "exact_title"
    if len(reference) >= 10 and reference in supplier:
        return "reference_title_substring"
    return None
