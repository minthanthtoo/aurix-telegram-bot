"""Normalized device observations and deterministic extraction."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ocr import parse_receipt_text, run_tesseract


UTC = timezone.utc
ALLOWED_PROVIDERS = {"kpay", "wavepay", "ayapay", "uabpay", "cbpay"}
ALLOWED_SOURCES = {"notification", "media", "history_ui", "sms_notification"}
MAX_MEDIA_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class NormalizedObservation:
    event_id: str
    device_id: str
    provider: str
    package_name: str
    app_version: str
    source: str
    source_event_id: str
    direction: str
    status: str
    amount_minor: int | None
    currency: str | None
    provider_reference: str | None
    transaction_time: str | None
    observed_at: str
    evidence_sha256: str | None
    confidence: float
    flags: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_payload(payload: dict[str, Any]) -> NormalizedObservation:
    provider = _required(payload, "provider")
    source = _required(payload, "source")
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError("unsupported provider")
    if source not in ALLOWED_SOURCES:
        raise ValueError("unsupported source")
    observed_at = _iso8601(_required(payload, "observed_at"))
    source_event_id = _required(payload, "source_event_id")
    text = "\n".join(
        str(payload.get(field) or "") for field in ("title", "text", "big_text", "sub_text")
    )
    evidence_sha256 = _optional_hash(payload.get("evidence_sha256"))
    if source == "media":
        encoded = str(payload.get("content_base64") or "")
        try:
            media = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("invalid media encoding") from exc
        if not media or len(media) > MAX_MEDIA_BYTES:
            raise ValueError("invalid media size")
        actual_hash = hashlib.sha256(media).hexdigest()
        if evidence_sha256 and evidence_sha256 != actual_hash:
            raise ValueError("media digest mismatch")
        evidence_sha256 = actual_hash
        suffix = _image_suffix(str(payload.get("mime_type") or ""))
        with tempfile.NamedTemporaryFile(suffix=suffix) as image:
            image.write(media)
            image.flush()
            text = run_tesseract(Path(image.name))
    extracted = parse_receipt_text(provider, text)
    event_id = hashlib.sha256(
        f"{provider}\0{source}\0{source_event_id}".encode("utf-8")
    ).hexdigest()
    flags = set(extracted.flags)
    if source == "notification" and not text.strip():
        flags.add("notification_text_hidden")
    if source == "media" and not text.strip():
        flags.add("ocr_empty")
    return NormalizedObservation(
        event_id=event_id,
        device_id=_required(payload, "device_id")[:128],
        provider=provider,
        package_name=_required(payload, "package_name")[:160],
        app_version=str(payload.get("app_version") or "unknown")[:64],
        source=source,
        source_event_id=source_event_id[:128],
        direction=extracted.direction,
        status=extracted.status,
        amount_minor=extracted.amount_minor,
        currency=extracted.currency,
        provider_reference=extracted.provider_reference,
        transaction_time=_optional_time(payload.get("transaction_time")),
        observed_at=observed_at,
        evidence_sha256=evidence_sha256,
        confidence=extracted.confidence,
        flags=tuple(sorted(flags)),
    )


def safe_summary(observation: NormalizedObservation) -> str:
    return json.dumps(
        {
            "event_id": observation.event_id[:12],
            "provider": observation.provider,
            "source": observation.source,
            "direction": observation.direction,
            "status": observation.status,
            "has_amount": observation.amount_minor is not None,
            "has_reference": observation.provider_reference is not None,
            "flags": observation.flags,
        },
        sort_keys=True,
    )


def _required(payload: dict[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"missing {name}")
    return value


def _iso8601(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid observed_at") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _optional_time(value: object) -> str | None:
    if not value:
        return None
    return _iso8601(str(value))


def _optional_hash(value: object) -> str | None:
    if not value:
        return None
    normalized = str(value).casefold()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("invalid evidence digest")
    return normalized


def _image_suffix(mime_type: str) -> str:
    return ".png" if mime_type.casefold() == "image/png" else ".jpg"
