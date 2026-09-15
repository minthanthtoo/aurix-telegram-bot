"""Account-scoped capability discovery for the configured 9Router.

The router's model catalog is a discovery source, not a health guarantee.  This
module keeps discovery results explicit and records failures per category so an
admin report can distinguish unavailable endpoints from empty catalogs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .router import MODEL_CATALOG


CAPABILITY_CATEGORIES: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("chat", None, ("chat", "responses", "streaming")),
    ("embeddings", "embedding", ("embeddings",)),
    ("speech_to_text", "stt", ("audio_input",)),
    ("text_to_speech", "tts", ("audio_output",)),
    ("image_generation", "image", ("image_generation",)),
    ("video_generation", "video", ("video_generation",)),
)

_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "bearer_token",
    "client_secret",
    "password",
    "secret",
}


def _safe_data(value: Any, *, depth: int = 0) -> Any:
    """Keep quota metadata useful while preventing accidental secret echoing."""

    if depth > 4:
        return "[depth limited]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if str(key).lower() in _SENSITIVE_KEYS else _safe_data(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_safe_data(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:1000]
    return str(value)[:1000]


def _error_payload(exc: Exception) -> dict[str, str]:
    """Return a safe, intentionally short error suitable for an admin report."""

    return {"type": type(exc).__name__, "message": str(exc)[:240]}


def build_capability_report(router: Any, *, include_quota: bool = True) -> dict[str, Any]:
    """Discover the current router catalog and optional account quota.

    The router client is deliberately duck-typed so this report can be tested
    with a fake client and used by the CLI without coupling the report to HTTP.
    No prompts, bearer credentials, or provider secrets are returned.
    """

    observed_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "schema": "aurix.capabilityReport.v1",
        "observed_at": observed_at,
        "source": "9router",
        "local_model_policy": [
            {
                "id": model_id,
                "route": item["route"],
                "label": item["label"],
                "description": item["description"],
                "capabilities": ["chat", "responses", "streaming"],
            }
            for model_id, item in MODEL_CATALOG.items()
        ],
        "categories": {},
        "account": {},
    }

    for name, category, capabilities in CAPABILITY_CATEGORIES:
        try:
            models = router.list_models(category)
        except Exception as exc:
            report["categories"][name] = {
                "status": "error",
                "category": category,
                "capabilities": list(capabilities),
                "models": [],
                "error": _error_payload(exc),
            }
            continue
        report["categories"][name] = {
            "status": "ok",
            "category": category,
            "capabilities": list(capabilities),
            "models": [
                {
                    "id": item["id"],
                    "object": item.get("object", "model"),
                    "owned_by": item.get("owned_by", "9router"),
                    "capabilities": list(capabilities),
                }
                for item in models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
        }

    if include_quota:
        request_json = getattr(router, "request_json", None)
        if callable(request_json):
            for name, path in (("quota", "/api/quota"), ("usage_today", "/api/usage?period=today")):
                try:
                    report["account"][name] = {
                        "status": "ok",
                        "data": _safe_data(request_json(path, {}, method="GET")),
                    }
                except Exception as exc:
                    report["account"][name] = {
                        "status": "error",
                        "error": _error_payload(exc),
                    }
        else:
            report["account"]["status"] = "unavailable"
            report["account"]["error"] = {
                "type": "client_capability_missing",
                "message": "router client does not expose account quota discovery",
            }

    report["summary"] = {
        "categories_ok": sum(
            1 for item in report["categories"].values() if item["status"] == "ok"
        ),
        "categories_total": len(CAPABILITY_CATEGORIES),
        "models_discovered": sum(
            len(item["models"])
            for item in report["categories"].values()
            if item["status"] == "ok"
        ),
        "account_quota_observed": report["account"].get("quota", {}).get("status") == "ok",
    }
    return report
