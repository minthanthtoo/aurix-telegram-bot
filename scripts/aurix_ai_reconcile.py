#!/usr/bin/env python3
"""Compare AuriX AI usage with a read-only 9Router usage export."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aurix_ai.api_keys import APIKeyStore, reconcile_usage_events  # noqa: E402


def _period(start_at: str | None, end_at: str | None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    default_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
    return start_at or default_start, end_at or now.isoformat()


def _store(args: argparse.Namespace) -> APIKeyStore:
    url = args.database_url or os.environ.get("AURIX_AI_DATABASE_URL", "").strip()
    if url:
        return APIKeyStore(database_url=url)
    path = Path(
        args.database
        or os.environ.get("AURIX_AI_API_KEYS_DB_PATH", "")
        or "/var/lib/aurix-ai/api-keys.db"
    )
    return APIKeyStore(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="AuriX AI SQLite path")
    parser.add_argument("--database-url", help="AuriX AI PostgreSQL URL")
    parser.add_argument("--router-report", required=True, help="Saved 9Router usage JSON export")
    parser.add_argument("--account-id")
    parser.add_argument("--from", dest="start_at")
    parser.add_argument("--to", dest="end_at")
    parser.add_argument("--limit", type=int, default=1000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    start_at, end_at = _period(args.start_at, args.end_at)
    try:
        router_payload = json.loads(Path(args.router_report).read_text(encoding="utf-8"))
        if isinstance(router_payload, dict):
            router_events = router_payload.get("events", [])
        else:
            router_events = router_payload
        if not isinstance(router_events, list) or not all(
            isinstance(event, dict) for event in router_events
        ):
            raise ValueError("router report must contain an events array")
        store = _store(args)
        store.initialize()
        aurix_events = store.usage_9router_events(
            account_id=args.account_id,
            start_at=start_at,
            end_at=end_at,
            limit=max(1, min(args.limit, 1_000)),
        )
        result = reconcile_usage_events(aurix_events, router_events)
        result["period"] = {"start_at": start_at, "end_at": end_at}
        result["source"] = "aurix-read-only-reconciliation"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
