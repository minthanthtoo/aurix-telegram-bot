#!/usr/bin/env python3
"""Read-only smoke test of the latest Telegram receipt through vision extraction."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def mask(value: Any, prefix: int = 3, suffix: int = 3) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= prefix + suffix:
        return "****"
    return f"{text[:prefix]}****{text[-suffix:]}"


def telegram_json(token: str, method: str, payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Telegram {method} failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Telegram {method} rejected the request")
    return result.get("result")


def latest_receipt(
    database_path: Path | None = None, database_url: str | None = None
) -> dict[str, Any]:
    """Read the newest receipt from the configured SQLite or PostgreSQL authority."""
    query = """SELECT e.id, e.telegram_file_id, e.mime_type, e.submitted_at,
                      e.extraction_status, e.review_status, e.provider,
                      o.amount_minor, o.currency
               FROM payment_evidence e
               JOIN orders o ON o.id = e.order_id
               ORDER BY e.submitted_at DESC LIMIT 1"""
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(database_url, connect_timeout=10, row_factory=dict_row) as connection:
                row = connection.execute(query).fetchone()
        except Exception as exc:
            raise RuntimeError(f"Receipt database read failed: {type(exc).__name__}") from exc
    else:
        if database_path is None:
            raise RuntimeError("Receipt database path is not configured")
        database_uri = f"file:{database_path.resolve()}?mode=ro"
        try:
            with sqlite3.connect(database_uri, uri=True, timeout=5) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(query).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Receipt database read failed: {type(exc).__name__}") from exc
    if row is None:
        raise RuntimeError("No receipt evidence is available for the smoke test")
    return dict(row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=os.environ.get("DATABASE_PATH", ""))
    parser.add_argument("--database-url", default=os.environ.get("COMMERCE_DATABASE_URL", ""))
    args = parser.parse_args()
    if not args.database and not args.database_url:
        raise SystemExit("Receipt smoke test failed: configure DATABASE_PATH or COMMERCE_DATABASE_URL")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Receipt smoke test failed: TELEGRAM_BOT_TOKEN is not configured")

    # Keep the CLI help/preflight paths dependency-light.  The actual smoke
    # run still imports the configured vision extractor here.
    from receipt_llm import ReceiptExtractionError, build_receipt_extractor

    receipt = latest_receipt(
        Path(args.database) if args.database and not args.database_url else None,
        args.database_url or None,
    )
    file_info = telegram_json(token, "getFile", {"file_id": receipt["telegram_file_id"]})
    file_path = file_info.get("file_path") if isinstance(file_info, dict) else None
    if not isinstance(file_path, str) or not file_path:
        raise SystemExit("Receipt smoke test failed: Telegram returned no file path")
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30
        ) as response:
            image = response.read(20 * 1024 * 1024 + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise SystemExit(
            f"Receipt smoke test failed: image download {type(exc).__name__}"
        ) from exc
    if not image or len(image) > 20 * 1024 * 1024:
        raise SystemExit("Receipt smoke test failed: image size is invalid")

    extractor = build_receipt_extractor()
    try:
        extraction, diagnostics = extractor.extract_with_diagnostics(
            image, str(receipt.get("mime_type") or "image/jpeg")
        )
    except ReceiptExtractionError as exc:
        diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "endpoint": mask(diagnostics.get("endpoint_host")),
                    "attempts": diagnostics.get("attempts", []),
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc

    expected_amount = int(receipt["amount_minor"])
    extracted_amount = extraction.amount_minor
    output = {
        "status": "parsed",
        "evidence": mask(receipt["id"]),
        "provider_expected": str(receipt.get("provider") or "-").lower(),
        "provider_extracted": str(extraction.provider or "-").lower(),
        "transaction_id": mask(extraction.transaction_id),
        "amount_match": extracted_amount == expected_amount,
        "currency_match": str(extraction.currency or "").upper()
        == str(receipt["currency"]).upper(),
        "timestamp_present": bool(extraction.timestamp),
        "confidence": extraction.confidence,
        "flags": list(extraction.flags),
        "selected_model": mask(diagnostics.get("selected_model") or diagnostics.get("model")),
        "endpoint": mask(diagnostics.get("endpoint_host")),
        "duration_ms": diagnostics.get("duration_ms"),
        "attempts": diagnostics.get("attempts", []),
    }
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
