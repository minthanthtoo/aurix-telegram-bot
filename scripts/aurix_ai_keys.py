#!/usr/bin/env python3
"""Operator-only account and API-key management for the AuriX AI gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aurix_ai.api_keys import APIKeyStore, APIKeyStoreError  # noqa: E402
from aurix_ai.router import MODEL_CATALOG, MODE_INSTRUCTIONS  # noqa: E402


def _store(database_path: str | None, database_url: str | None) -> APIKeyStore:
    resolved_url = database_url or os.environ.get("AURIX_AI_DATABASE_URL", "").strip()
    if resolved_url:
        return APIKeyStore(database_url=resolved_url)
    return APIKeyStore(
        Path(
            database_path
            or os.environ.get("AURIX_AI_API_KEYS_DB_PATH", "")
            or "/var/lib/aurix-ai/api-keys.db"
        )
    )


def _scope(value: str, *, name: str, allowed: set[str]) -> list[str]:
    values = [part.strip() for part in value.split(",") if part.strip()]
    if values == ["*"]:
        return values
    invalid = sorted(set(values) - allowed)
    if invalid:
        raise APIKeyStoreError(f"unsupported {name}: {', '.join(invalid)}")
    if not values:
        raise APIKeyStoreError(f"{name} must not be empty")
    return sorted(set(values))


def _usage_period(start_at: str | None, end_at: str | None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    default_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()
    return start_at or default_start, end_at or now.isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="API-key SQLite path")
    parser.add_argument("--database-url", help="Existing AuriX PostgreSQL URL")
    commands = parser.add_subparsers(dest="command", required=True)

    account = commands.add_parser("create-account")
    account.add_argument("name")
    account.add_argument("--account-id")
    account.add_argument("--modes", default="*")
    account.add_argument("--models", default="*")
    account.add_argument("--requests-per-minute", type=int, default=60)
    account.add_argument("--owner-type", default="external_site")
    account.add_argument("--owner-id")

    issue = commands.add_parser("issue-key")
    issue.add_argument("account_id")
    issue.add_argument("--label", default="default")
    expiry = issue.add_mutually_exclusive_group()
    expiry.add_argument("--expires-in-days", type=int, default=90)
    expiry.add_argument("--no-expiry", dest="expires_in_days", action="store_const", const=None)

    update = commands.add_parser("update-account")
    update.add_argument("account_id")
    update.add_argument("--name")
    update.add_argument("--modes")
    update.add_argument("--models")
    update.add_argument("--requests-per-minute", type=int)
    update.add_argument("--owner-type")
    update.add_argument("--owner-id")

    list_accounts = commands.add_parser("list-accounts")
    list_keys = commands.add_parser("list-keys")
    list_keys.add_argument("--account-id")

    usage = commands.add_parser("usage")
    usage.add_argument("--account-id")
    usage.add_argument("--from", dest="start_at")
    usage.add_argument("--to", dest="end_at")
    usage.add_argument("--limit", type=int, default=100)
    usage.add_argument("--format", choices=("summary", "9router"), default="summary")

    revoke_key = commands.add_parser("revoke-key")
    revoke_key.add_argument("key_id")

    revoke_account = commands.add_parser("revoke-account")
    revoke_account.add_argument("account_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = _store(args.database, args.database_url)
    store.initialize()
    try:
        if args.command == "create-account":
            result = store.create_account(
                args.name,
                account_id=args.account_id,
                allowed_modes=_scope(
                    args.modes, name="modes", allowed=set(MODE_INSTRUCTIONS)
                ),
                allowed_models=_scope(
                    args.models, name="models", allowed=set(MODEL_CATALOG)
                ),
                requests_per_minute=args.requests_per_minute,
                owner_type=args.owner_type,
                owner_id=args.owner_id,
            )
        elif args.command == "issue-key":
            if args.expires_in_days is not None and args.expires_in_days < 1:
                raise APIKeyStoreError("expires-in-days must be positive")
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=args.expires_in_days)
            ).isoformat() if args.expires_in_days is not None else None
            issued = store.issue_key(
                args.account_id,
                label=args.label,
                expires_at=expires_at,
            )
            # This is the only command that prints a bearer secret. Store it in
            # the consuming website's secret manager immediately.
            result = {
                "account_id": issued.account_id,
                "key_id": issued.key_id,
                "label": issued.label,
                "token": issued.token,
                "expires_at": issued.expires_at,
                "warning": "The token is shown once; it cannot be recovered later.",
            }
        elif args.command == "update-account":
            result = store.update_account(
                args.account_id,
                name=args.name,
                allowed_modes=(
                    _scope(args.modes, name="modes", allowed=set(MODE_INSTRUCTIONS))
                    if args.modes is not None
                    else None
                ),
                allowed_models=(
                    _scope(args.models, name="models", allowed=set(MODEL_CATALOG))
                    if args.models is not None
                    else None
                ),
                requests_per_minute=args.requests_per_minute,
                owner_type=args.owner_type,
                owner_id=args.owner_id,
            )
        elif args.command == "list-accounts":
            result = store.list_accounts()
        elif args.command == "list-keys":
            result = store.list_keys(args.account_id)
        elif args.command == "usage":
            start_at, end_at = _usage_period(args.start_at, args.end_at)
            if args.format == "9router":
                result = {
                    "source": "aurix",
                    "format": "9router.usageHistory.v1",
                    "period": {"start_at": start_at, "end_at": end_at},
                    "events": store.usage_9router_events(
                        account_id=args.account_id,
                        start_at=start_at,
                        end_at=end_at,
                        limit=args.limit,
                    ),
                }
            else:
                result = {
                    "period": {"start_at": start_at, "end_at": end_at},
                    "accounts": store.usage_summary(
                        account_id=args.account_id,
                        start_at=start_at,
                        end_at=end_at,
                    ),
                    "requests": store.usage_events(
                        account_id=args.account_id,
                        start_at=start_at,
                        end_at=end_at,
                        limit=args.limit,
                    ),
                }
        elif args.command == "revoke-key":
            result = {"revoked": store.revoke_key(args.key_id), "key_id": args.key_id}
        elif args.command == "revoke-account":
            result = {
                "revoked": store.revoke_account(args.account_id),
                "account_id": args.account_id,
            }
        else:
            raise APIKeyStoreError("unknown command")
    except APIKeyStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
