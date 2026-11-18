"""Private exact-SKU routes used only after an AuriX offer is approved.

Provider IDs are deliberately absent from the public catalog. This small
private boundary joins an AuriX offer ID to its exact approved supplier tuple
for encrypted checkout storage and later fulfillment.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
from typing import Any, Mapping


class SupplierRouteError(ValueError):
    """A private offer-to-supplier route is missing or unsafe to use."""


_FAMILY_BY_KIND = {
    "direct_topup": "topups",
    "gift_card": "giftcards",
    "game_key": "gamekeys",
    "manual_service": "manual_services",
}


@dataclass(frozen=True)
class PrivateSupplierRoute:
    family: str
    category_id: str
    sku: str
    approval_fingerprint: str
    validation_category_id: str | None = None
    validation_field_map: tuple[tuple[str, str], ...] = ()
    validation_region_map: tuple[tuple[str, str], ...] = ()
    detect_region: bool = False
    excluded_regions: tuple[str, ...] = ()

    def as_vault_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "family": self.family,
            "category_id": self.category_id,
            "sku": self.sku,
            "approval_fingerprint": self.approval_fingerprint,
        }
        if self.validation_category_id is not None:
            result["validation_category_id"] = self.validation_category_id
        if self.validation_field_map:
            result["validation_field_map"] = dict(self.validation_field_map)
        if self.validation_region_map:
            result["validation_region_map"] = dict(self.validation_region_map)
        if self.detect_region:
            result["validation_region_policy"] = {"mode": "detected", "excluded_values": list(self.excluded_regions)}
        return result


class PrivateSupplierRouteStore:
    """Resolve only one fully approved, exact mapping from a private config."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def resolve(
        self,
        *,
        offer_id: str,
        product_slug: str,
        fulfillment_kind: str,
        region_rule: Any | None = None,
        region_label: str | None = None,
        denomination_label: str | None = None,
        required_input_labels: tuple[str, ...] | None = None,
    ) -> PrivateSupplierRoute:
        if not all(isinstance(value, str) and value.strip() for value in (offer_id, product_slug, fulfillment_kind)):
            raise SupplierRouteError("offer route identity is invalid")
        expected_family = _FAMILY_BY_KIND.get(fulfillment_kind)
        if expected_family is None:
            raise SupplierRouteError("fulfillment kind has no FazerCards mapping route")
        payload = self._read()
        mappings = payload.get("mappings")
        if not isinstance(mappings, list):
            raise SupplierRouteError("private mapping config is malformed")
        matches = [
            row for row in mappings
            if isinstance(row, dict) and row.get("offer_id") == offer_id
        ]
        if len(matches) != 1:
            raise SupplierRouteError("offer does not have one exact private supplier mapping")
        row = matches[0]
        stored_region_rule = row.get("region_rule")
        if (
            row.get("product_slug") != product_slug
            or row.get("fulfillment_kind") != fulfillment_kind
            or row.get("family") != expected_family
            or row.get("review_state") != "approved"
            or row.get("customer_visible") is not True
            or row.get("supplier_enabled") is not True
            or row.get("resale_authorized") is not True
            or not isinstance(stored_region_rule, dict)
            or stored_region_rule.get("verified") is not True
        ):
            raise SupplierRouteError("offer supplier mapping is not fully approved")
        if region_rule is not None and not _matches_region_projection(
            row,
            region_rule=region_rule,
            region_label=region_label,
        ):
            raise SupplierRouteError("private supplier route region no longer matches the public offer")
        if denomination_label is not None and row.get("denomination_label") != denomination_label:
            raise SupplierRouteError("private supplier route label no longer matches the public offer")
        if required_input_labels is not None:
            stored_labels = row.get("required_input_labels", [])
            if not isinstance(stored_labels, list) or not all(
                isinstance(label, str) and label.strip() for label in stored_labels
            ):
                raise SupplierRouteError("private supplier mapping recipient labels are malformed")
            if tuple(stored_labels) != required_input_labels:
                raise SupplierRouteError("private supplier route inputs no longer match the public offer")
        category = row.get("provider_category_id")
        sku = row.get("provider_offer_id")
        if not all(isinstance(value, str) and value.strip() and len(value) <= 255 for value in (category, sku)):
            raise SupplierRouteError("private supplier mapping identifiers are malformed")
        try:
            fingerprint = hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError) as exc:
            raise SupplierRouteError("private supplier mapping is malformed") from exc
        return PrivateSupplierRoute(expected_family, category.strip(), sku.strip(), fingerprint,
            *validation_settings(row))

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise SupplierRouteError("private mapping config must not be a symlink")
        try:
            info = self.path.stat()
            parent_info = self.path.parent.stat()
            if not stat.S_ISREG(info.st_mode):
                raise SupplierRouteError("private mapping config is not a regular file")
            if stat.S_IMODE(info.st_mode) & 0o077 or stat.S_IMODE(parent_info.st_mode) & 0o077:
                raise SupplierRouteError("private mapping config permissions are too broad")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except SupplierRouteError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SupplierRouteError("private mapping config is unavailable") from exc
        if not isinstance(value, dict):
            raise SupplierRouteError("private mapping config is malformed")
        return value


def _matches_region_projection(
    row: Mapping[str, Any], *, region_rule: Any, region_label: str | None
) -> bool:
    stored = row.get("region_rule")
    dimension = getattr(region_rule, "dimension", None)
    allowed_values = getattr(region_rule, "allowed_values", None)
    stored_mapping = stored if isinstance(stored, Mapping) else {}
    stored_dimension = stored_mapping.get("dimension")
    stored_values = stored_mapping.get("allowed_values", [])
    dimension_value = getattr(dimension, "value", None)
    if stored_values is None:
        stored_values = []
    if isinstance(stored_values, (str, bytes)) or not isinstance(stored_values, (list, tuple, set, frozenset)):
        return False
    if allowed_values is None:
        allowed = set()
    else:
        try:
            allowed = set(allowed_values)
        except TypeError:
            return False
    try:
        stored_allowed = set(stored_values)
    except TypeError:
        return False
    expected_label = region_label or ("Global" if getattr(region_rule, "allow_all", False) else None)
    stored_label = row.get("region_label") or ("Global" if stored_mapping.get("allow_all") is True else None)
    return (
        bool(stored_mapping)
        and stored_mapping.get("verified") is getattr(region_rule, "verified", None)
        and stored_mapping.get("allow_all") is getattr(region_rule, "allow_all", None)
        and stored_dimension == dimension_value
        and stored_allowed == allowed
        and stored_label == expected_label
    )


def fazercards_order_payload(
    *,
    route: PrivateSupplierRoute,
    fulfillment_kind: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate a private route to typed worker payload keys."""

    if not isinstance(route, PrivateSupplierRoute) or _FAMILY_BY_KIND.get(fulfillment_kind) != route.family:
        raise SupplierRouteError("supplier route does not match fulfillment kind")
    if not isinstance(fields, Mapping):
        raise SupplierRouteError("private recipient fields are malformed")
    recipient_fields = dict(fields)
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        for key, value in recipient_fields.items()
    ):
        raise SupplierRouteError("provider recipient fields must contain string values")
    if fulfillment_kind == "direct_topup":
        if not recipient_fields:
            raise SupplierRouteError("top-up recipient fields are required")
        return {"category_id": route.category_id, "offer_id": route.sku, "fields": recipient_fields}
    if fulfillment_kind == "gift_card":
        return {"category_id": route.category_id, "card_id": route.sku, "quantity": 1}
    if fulfillment_kind == "game_key":
        return {"game_id": route.category_id, "key_id": route.sku, "quantity": 1}
    if fulfillment_kind == "manual_service":
        payload: dict[str, Any] = {
            "manual_service_id": route.category_id,
            "product_id": route.sku,
        }
        if recipient_fields:
            payload["fields"] = recipient_fields
        return payload
    raise SupplierRouteError("fulfillment kind has no FazerCards order payload")


__all__ = [
    "PrivateSupplierRoute",
    "PrivateSupplierRouteStore",
    "SupplierRouteError",
    "fazercards_order_payload",
]


def validation_settings(row):
    """Validate saved recipient lookup settings before approval or checkout."""
    expected_family = row.get("family")
    stored_region_rule = row.get("region_rule", {})
    category = row.get("provider_category_id", "")
    validation_category = row.get("validation_category_id")
    if validation_category is not None and (
        expected_family != "topups" or not isinstance(validation_category, str)
        or not validation_category.strip() or validation_category != validation_category.strip()
        or len(validation_category) > 255
        or any(ord(char) < 32 for char in validation_category)
    ):
        raise SupplierRouteError("private recipient validation category is malformed")
    field_map = row.get("validation_field_map", {})
    if not isinstance(field_map, dict) or (field_map and validation_category is None):
        raise SupplierRouteError("private validation field map is malformed")
    for target, source in field_map.items():
        if not all(isinstance(value, str) and value and len(value) <= 128
                   and value.replace("_", "").isalnum() for value in (target, source)):
            raise SupplierRouteError("private validation field map is malformed")
        if source not in row.get("required_input_labels", []):
            raise SupplierRouteError("private validation field source is not an approved input")
    if len(set(field_map.values())) != len(field_map):
        raise SupplierRouteError("private validation field map reuses an input")
    region_map = row.get("validation_region_map", {})
    allowed_regions = stored_region_rule.get("allowed_values", [])
    if not isinstance(region_map, dict) or (region_map and (
        validation_category is None or stored_region_rule.get("allow_all") is True
        or not isinstance(allowed_regions, list)
        or any(not isinstance(value, str) for value in allowed_regions)
    )):
        raise SupplierRouteError("private validation region map is malformed")
    normalized_regions = {}
    for label, token in region_map.items():
        if not isinstance(label, str) or not label.strip() or len(label) > 128 or any(ord(c) < 32 for c in label):
            raise SupplierRouteError("private validation region label is malformed")
        key = label.strip().casefold()
        if (not isinstance(token, str) or token not in allowed_regions or key in normalized_regions):
            raise SupplierRouteError("private validation region is not uniquely approved")
        normalized_regions[key] = token.casefold()
    detected = row.get("validation_region_policy")
    exclusions = ()
    if detected is not None:
        if (not isinstance(detected, dict) or set(detected) != {"mode", "excluded_values", "evidence"}
                or detected["mode"] != "detected" or validation_category is None
                or expected_family != "topups" or stored_region_rule.get("allow_all") is not True
                or normalized_regions or not isinstance(detected["evidence"], str)
                or not detected["evidence"].strip() or len(detected["evidence"]) > 512
                or not isinstance(detected["excluded_values"], list)
                or any(not isinstance(value, str) or not value.strip() or len(value) > 128
                    or any(ord(c) < 32 for c in value) for value in detected["excluded_values"])):
            raise SupplierRouteError("reviewed detected-region policy is malformed")
        exclusions = tuple(sorted({value.strip().casefold() for value in detected["excluded_values"]}))
    if category.strip() == "mobile_legends_global" and not normalized_regions and detected is None:
        raise SupplierRouteError("MLBB Global requires explicit offer-level validation regions")
    return (validation_category, tuple(sorted(field_map.items())),
            tuple(sorted(normalized_regions.items())), detected is not None, exclusions)
