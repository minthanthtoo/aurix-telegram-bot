"""Local, deterministic receipt OCR and conservative field extraction."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


AMOUNT_PATTERNS = (
    re.compile(
        r"(?i)(?:amount|received|receive|ငွေပမာဏ)\s*[:\-]?\s*"
        r"([+]?[\d,]+(?:\.\d{1,2})?)\s*(MMK|Ks)?"
    ),
    re.compile(r"(?i)([+]?[\d,]+(?:\.\d{1,2})?)\s*(MMK|Ks)"),
)
REFERENCE_PATTERNS = (
    re.compile(
        r"(?i)(?:transaction\s*(?:id|no|number)?|reference|"
        r"ref(?:erence)?\s*(?:id|no)?|trx\s*id)\s*[:#\-]?\s*([A-Z0-9_-]{6,})"
    ),
)
SUCCESS_WORDS = ("successful", "success", "completed", "received", "အောင်မြင်")
FAILURE_WORDS = ("failed", "declined", "cancelled", "canceled", "မအောင်မြင်")
PENDING_WORDS = ("pending", "processing", "စောင့်ဆိုင်း")
REVERSED_WORDS = ("reversed", "refunded", "refund")


@dataclass(frozen=True)
class LocalReceiptExtraction:
    provider: str
    amount_minor: int | None
    currency: str | None
    provider_reference: str | None
    status: str
    direction: str
    confidence: float
    flags: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_tesseract(image_path: Path, timeout: int = 20) -> str:
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", "eng+mya+mya2", "--psm", "6"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("Local receipt OCR failed")
    return result.stdout


def _amount(text: str) -> tuple[int | None, str | None]:
    for pattern in AMOUNT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).replace(",", "").lstrip("+")
        try:
            # MMK is zero-decimal. Decimal OCR output is accepted only when it
            # is an exact .00 representation.
            if "." in raw and not raw.endswith(".00"):
                return None, None
            return int(float(raw)), "MMK"
        except ValueError:
            continue
    return None, None


def _reference(text: str) -> str | None:
    for pattern in REFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return None


def parse_receipt_text(provider: str, text: str) -> LocalReceiptExtraction:
    folded = " ".join(text.casefold().split())
    amount, currency = _amount(text)
    reference = _reference(text)
    if any(word in folded for word in REVERSED_WORDS):
        status = "reversed"
    elif any(word in folded for word in FAILURE_WORDS):
        status = "failed"
    elif any(word in folded for word in PENDING_WORDS):
        status = "pending"
    elif any(word in folded for word in SUCCESS_WORDS):
        status = "successful"
    else:
        status = "unknown"
    incoming = any(word in folded for word in ("received", "receive money", "ငွေလက်ခံ"))
    outgoing = any(word in folded for word in ("sent", "send money", "paid", "ငွေလွှဲ"))
    direction = "incoming" if incoming and not outgoing else "outgoing" if outgoing else "unknown"
    flags = []
    if amount is None:
        flags.append("missing_amount")
    if reference is None:
        flags.append("missing_reference")
    if status != "successful":
        flags.append(f"status_{status}")
    if direction != "incoming":
        flags.append(f"direction_{direction}")
    facts = sum(value is not None for value in (amount, reference))
    confidence = min(
        0.95,
        0.35 + facts * 0.2 + (status != "unknown") * 0.1 + (direction != "unknown") * 0.1,
    )
    return LocalReceiptExtraction(
        provider=provider,
        amount_minor=amount,
        currency=currency,
        provider_reference=reference,
        status=status,
        direction=direction,
        confidence=confidence,
        flags=tuple(flags),
    )


def extract_receipt(provider: str, image_path: Path) -> LocalReceiptExtraction:
    return parse_receipt_text(provider, run_tesseract(image_path))
