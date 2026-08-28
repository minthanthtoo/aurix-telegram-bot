#!/usr/bin/env python3
"""Receipt-image extraction through an optional OpenAI-compatible vision API.

The model is an untrusted parser only.  It never verifies a payment or grants
access; a staff member must compare the extracted fields with the receiving
wallet/account transaction history before approval.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ReceiptExtractionError(RuntimeError):
    """Base class for safe, user-facing extraction failures."""


class ReceiptLLMUnavailable(ReceiptExtractionError):
    """The optional vision provider is not configured or reachable."""


@dataclass(frozen=True)
class ReceiptExtraction:
    provider: str | None
    transaction_id: str | None
    amount_minor: int | None
    currency: str | None
    timestamp: str | None
    recipient: str | None
    confidence: float
    flags: tuple[str, ...]
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "transaction_id": self.transaction_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "timestamp": self.timestamp,
            "recipient": self.recipient,
            "confidence": self.confidence,
            "flags": list(self.flags),
            "notes": list(self.notes),
        }


def _clean_text(value: Any, max_length: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = " ".join(value.split())[:max_length]
    return value or None


def validate_extraction(value: Any) -> ReceiptExtraction:
    """Validate and normalize the model response before it reaches commerce."""
    if not isinstance(value, dict):
        raise ReceiptExtractionError("Receipt extraction was not a JSON object")
    try:
        confidence = float(value.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise ReceiptExtractionError("Receipt confidence was invalid") from exc
    if confidence < 0 or confidence > 1:
        raise ReceiptExtractionError("Receipt confidence was outside 0..1")
    amount = value.get("amount_minor")
    if amount is not None:
        try:
            amount = int(amount)
        except (TypeError, ValueError) as exc:
            raise ReceiptExtractionError("Receipt amount was invalid") from exc
        if amount < 0 or amount > 10**12:
            raise ReceiptExtractionError("Receipt amount was outside the safe range")
    def clean_list(name: str) -> tuple[str, ...]:
        raw = value.get(name, [])
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ReceiptExtractionError(f"Receipt {name} was not a list")
        return tuple(item for item in (_clean_text(item, 256) for item in raw) if item)[:12]

    return ReceiptExtraction(
        provider=_clean_text(value.get("provider"), 64),
        transaction_id=_clean_text(value.get("transaction_id"), 128),
        amount_minor=amount,
        currency=_clean_text(value.get("currency"), 16),
        timestamp=_clean_text(value.get("timestamp"), 64),
        recipient=_clean_text(value.get("recipient"), 128),
        confidence=confidence,
        flags=clean_list("flags"),
        notes=clean_list("notes"),
    )


class OpenAICompatibleReceiptExtractor:
    """Small standard-library client for a configured vision-capable endpoint.

    Configuration is deliberately explicit.  If no validated provider/model is
    configured, extraction raises and the receipt remains in manual-review
    state instead of silently guessing.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int = 45,
    ):
        self.base_url = (base_url or os.environ.get("RECEIPT_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("RECEIPT_LLM_MODEL", "")
        self.api_key = api_key or os.environ.get("RECEIPT_LLM_API_KEY", "")
        try:
            self.timeout = max(5, min(int(timeout), 120))
        except (TypeError, ValueError):
            self.timeout = 45

    def extract(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> ReceiptExtraction:
        if not self.base_url or not self.model or not self.api_key:
            raise ReceiptLLMUnavailable("Receipt vision extraction is not configured")
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise ReceiptExtractionError("Receipt image is empty or too large")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        schema_hint = {
            "provider": "string|null",
            "transaction_id": "string|null",
            "amount_minor": "integer|null",
            "currency": "string|null",
            "timestamp": "ISO-8601 string|null",
            "recipient": "string|null",
            "confidence": "number 0..1",
            "flags": ["string"],
            "notes": ["string"],
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Extract receipt facts only. Never decide whether payment is valid. "
                        "Use null for unreadable fields; do not invent values. Return JSON with "
                        f"exactly these fields: {json.dumps(schema_hint)}"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Parse this payment receipt."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            raise ReceiptLLMUnavailable("Receipt vision provider is temporarily unavailable") from exc
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if not isinstance(content, str):
                raise KeyError("content")
            content = content.strip()
            if content.startswith("```"):
                content = content.strip("`").removeprefix("json").strip()
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReceiptExtractionError("Receipt vision response was not valid JSON") from exc
        return validate_extraction(parsed)

