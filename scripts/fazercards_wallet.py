#!/usr/bin/env python3
"""Admin-only FazerCards supplier-wallet status and funding workflow.

Reads are safe by default.  Invoice creation and Binance verification require
both ``--enable-writes`` and ``FAZERCARDS_ENABLE_WRITES=1``; this double gate
prevents a copied command or a stale environment from spending funds.  The
API key is read from ``FAZERCARDS_API_KEY`` or hidden stdin and is never
printed or persisted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sys
from typing import Any

from aurix_games.fazercards_account import (
    SupplierWalletPolicy,
    SupplierWalletSnapshot,
    WalletFundingService,
    WalletFundingIntentStore,
    parse_supplier_wallet_snapshot,
)
from aurix_games.providers.fazercards_commands import FazerCardsCommandClient, SupplierCommandError
from aurix_games.wallet_monitor import SupplierWalletAlertStore


def _key(args: argparse.Namespace) -> str:
    value = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    if value:
        return value
    if args.key_stdin:
        value = sys.stdin.readline().strip()
    if not value:
        raise SystemExit("FAZERCARDS_API_KEY is required (or use --key-stdin)")
    return value


def _allow_writes(args: argparse.Namespace) -> bool:
    if not args.enable_writes:
        return False
    if os.environ.get("FAZERCARDS_ENABLE_WRITES") != "1":
        raise SystemExit("Set FAZERCARDS_ENABLE_WRITES=1 as the second write gate")
    return True


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "status",
            "me",
            "payment-methods",
            "transactions",
            "orders",
            "subscription",
            "subscription-plans",
            "subscription-quote",
            "subscription-activate",
            "subscription-auto-renew",
            "subscription-plan",
            "check-steam-login",
            "invoice",
            "payment-status",
            "verify-binance",
        ),
    )
    parser.add_argument("--key-stdin", action="store_true", help="read the API key from hidden stdin")
    parser.add_argument("--enable-writes", action="store_true", help="first half of the explicit write gate")
    parser.add_argument("--method", default="usdt")
    parser.add_argument("--amount-usd", default="50")
    parser.add_argument("--intent-id", default="")
    parser.add_argument("--payment-id", default="")
    parser.add_argument("--binance-order-id", default="")
    parser.add_argument("--plan", default="")
    parser.add_argument("--promo-code", default=None)
    parser.add_argument("--auto-renew", choices=("on", "off"), default=None)
    parser.add_argument("--steam-login", default="")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--low-threshold-usd", default="10")
    parser.add_argument("--suggested-topup-usd", default="50")
    parser.add_argument(
        "--alert-db",
        default=os.environ.get("AURIX_GAMES_WALLET_ALERTS", ".local/fazercards/wallet-alerts.sqlite3"),
        help="private SQLite path for restart-safe low-wallet reminders",
    )
    parser.add_argument(
        "--intent-db",
        default=os.environ.get("AURIX_GAMES_FUNDING_INTENTS", ".local/fazercards/funding-intents.sqlite3"),
        help="private SQLite path for restart-safe funding intents",
    )
    args = parser.parse_args(argv)
    writes = _allow_writes(args) if args.command in {
        "invoice",
        "verify-binance",
        "subscription-activate",
        "subscription-auto-renew",
        "subscription-plan",
        "check-steam-login",
    } else False
    client = FazerCardsCommandClient(_key(args), allow_writes=writes)

    try:
        if args.command == "status":
            payload = client.get_balance()
            observed = datetime.now(timezone.utc)
            snapshot = parse_supplier_wallet_snapshot(payload, observed_at=observed)
            policy = SupplierWalletPolicy(
                low_balance_threshold_usd=Decimal(args.low_threshold_usd),
                suggested_topup_usd=Decimal(args.suggested_topup_usd),
            )
            monitored = SupplierWalletAlertStore(args.alert_db).observe(
                snapshot, policy, now=observed
            )
            decision = monitored.decision
            _print({"balance": snapshot.as_dict(), "low_wallet": decision.__dict__ | {
                "balance_usd": format(decision.balance_usd, "f"),
                "threshold_usd": format(decision.threshold_usd, "f"),
                "suggested_topup_usd": format(decision.suggested_topup_usd, "f"),
                "alert_id": monitored.alert_id,
            }})
            return 0
        if args.command == "subscription":
            _print(client.get_subscription())
            return 0
        if args.command == "me":
            _print(client.get_me())
            return 0
        if args.command == "payment-methods":
            _print(client.get_payment_methods())
            return 0
        if args.command == "transactions":
            _print(client.list_transactions(page=args.page, limit=args.limit))
            return 0
        if args.command == "orders":
            _print(client.list_orders(page=args.page, limit=args.limit))
            return 0
        if args.command == "subscription-plans":
            _print(client.list_subscription_plans())
            return 0
        if args.command == "subscription-quote":
            if not args.plan:
                raise SystemExit("--plan is required")
            _print(client.get_subscription_activation_quote(args.plan, promo_code=args.promo_code))
            return 0
        if args.command == "subscription-activate":
            if not args.plan:
                raise SystemExit("--plan is required")
            _print(client.activate_subscription_from_balance(args.plan, promo_code=args.promo_code))
            return 0
        if args.command == "subscription-auto-renew":
            if args.auto_renew is None:
                raise SystemExit("--auto-renew on|off is required")
            _print(client.set_subscription_auto_renew(args.auto_renew == "on"))
            return 0
        if args.command == "subscription-plan":
            if not args.plan:
                raise SystemExit("--plan is required")
            _print(client.change_subscription_plan(args.plan))
            return 0
        if args.command == "check-steam-login":
            if not args.steam_login:
                raise SystemExit("--steam-login is required")
            _print(client.check_steam_login(args.steam_login))
            return 0
        if args.command == "invoice":
            if not args.intent_id:
                raise SystemExit("--intent-id is required for invoice creation")
            intent = WalletFundingService(
                client,
                intent_store=WalletFundingIntentStore(args.intent_db),
            ).create_intent(
                intent_id=args.intent_id,
                method=args.method,
                amount_usd=args.amount_usd,
            )
            _print(intent.as_dict())
            return 0
        if args.command == "payment-status":
            if not args.payment_id:
                raise SystemExit("--payment-id is required")
            _print(client.get_payment(args.payment_id))
            return 0
        if not args.payment_id or not args.binance_order_id:
            raise SystemExit("--payment-id and --binance-order-id are required")
        _print(client.verify_binance_payment(args.payment_id, binance_order_id=args.binance_order_id))
        return 0
    except (ValueError, SupplierCommandError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
