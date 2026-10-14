"""Canonical display names for AuriX VPN fleet endpoints.

Endpoint IDs are persistent database identifiers. This module maps both the
current stable IDs and known Singapore bootstrap aliases to display names
without changing persisted IDs or routing references.
"""

from __future__ import annotations


_SERVER_NAMES_BY_ID = {
    "bkk-a": "AURIX_NUBE_TH_A",
    "sg-a": "AURIX_DIG_SG_A",
    "sg-b": "AURIX_DIG_SG_B",
    "nyc-a": "AURIX_DIG_US_A",
    "sjc-a": "AURIX_NUBE_US_A",
    "tyo-a": "AURIX_NUBE_JP_A",
    # Existing bootstrap IDs remain stable internally and display as Singapore A.
    "legacy-default": "AURIX_DIG_SG_A",
    "primary": "AURIX_DIG_SG_A",
}


def canonical_server_name(endpoint_id: object) -> str | None:
    """Return the canonical fleet label for a known stable or legacy ID."""
    normalized = str(endpoint_id or "").strip().lower()
    return _SERVER_NAMES_BY_ID.get(normalized)


def endpoint_display_name(endpoint_id: object, legacy_code: object = None) -> str:
    """Format an operator label while retaining the stable database ID."""
    canonical = canonical_server_name(endpoint_id)
    if canonical:
        stable_id = str(endpoint_id).strip()
        return f"{canonical} ({stable_id})" if stable_id else canonical
    return str(legacy_code or endpoint_id or "-")
