"""Operational admin command router for Telegram."""

from __future__ import annotations

import sys
from typing import Any

from telegram_command_context import TelegramCommandContext

OPERATIONS_COMMANDS = frozenset(
    {
        "/orders",
        "/receipts",
        "/capacity",
        "/probes",
        "/reconcile",
        "/enforcement",
        "/failed",
        "/repairs",
        "/migrations",
        "/failover",
    }
)


def dispatch_operations_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle read-only operational panels and queues."""

    command = context.command
    if command not in OPERATIONS_COMMANDS:
        return False
    chat_id = context.chat_id
    telegram_id = context.telegram_id

    if command in {"/orders", "/receipts"}:
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            view = "receipts" if command == "/receipts" else "orders"
            items = host._panel_data(telegram_id, view)
            if not items:
                host.send(
                    chat_id,
                    "No unreviewed receipts." if view == "receipts" else "No pending orders.",
                )
            else:
                host._open_admin_panel(chat_id, telegram_id, view)
    elif command == "/capacity":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            try:
                host._show_capacity(chat_id, telegram_id)
            except Exception as exc:
                host.send(chat_id, "Outline capacity metrics are temporarily unavailable.")
                print(f"capacity error: {type(exc).__name__}", file=sys.stderr)
    elif command == "/probes":
        if host.probe_service is None:
            host.send(
                chat_id,
                "Fleet probes are not configured.",
                host._admin_keyboard(telegram_id),
            )
        else:
            try:
                host._show_probes(chat_id, telegram_id)
            except Exception as exc:
                host.send(
                    chat_id,
                    "Fleet probe results are temporarily unavailable.",
                    host._admin_keyboard(telegram_id),
                )
                print(f"probe panel error: {type(exc).__name__}", file=sys.stderr)
    elif command == "/reconcile":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            report = host._admin_call(telegram_id, "consistency_report")
            issue_keys = {
                "duplicate_open_orders",
                "duplicate_payment_references",
                "approved_missing_subscription",
                "approved_missing_provision_job",
                "stale_receipts",
                "pending_receipt_uploads",
                "failed_receipt_uploads",
                "failed_jobs",
                "failed_activations",
                "failed_revocations",
                "pending_revocations",
                "dead_notifications",
                "wallet_balance_mismatches",
                "managed_key_missing",
                "managed_key_repairs_pending",
                "managed_key_repairs_failed",
                "managed_key_repairs_manual",
            }
            healthy = all(report.get(key, 0) == 0 for key in issue_keys)
            lines = [
                "AuriX consistency scan",
                "Status: " + ("OK" if healthy else "ACTION REQUIRED"),
            ]
            lines.extend(
                f"{key.replace('_', ' ').title()}: {value}"
                for key, value in report.items()
            )
            host.send(chat_id, "\n".join(lines), host._admin_keyboard(telegram_id))
    elif command == "/enforcement":
        events = host._admin_service_call(telegram_id, "termination_summary")
        if not events:
            host.send(
                chat_id,
                "No free/trial termination events recorded.",
                host._admin_keyboard(telegram_id),
            )
        else:
            host._open_admin_panel(chat_id, telegram_id, "enforcement")
    elif command == "/failed":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            jobs = host._admin_call(telegram_id, "failed_jobs", include_nonterminal=True)
            if not jobs:
                host.send(
                    chat_id,
                    "No terminal worker failures.",
                    host._admin_keyboard(telegram_id),
                )
            else:
                host._open_admin_panel(chat_id, telegram_id, "failed")
    elif command == "/repairs":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            jobs = host._admin_call(
                telegram_id,
                "managed_key_repair_jobs",
                status="open",
                limit=100,
            )
            if not jobs:
                host.send(
                    chat_id,
                    "No managed-key repairs are waiting for action.",
                    host._admin_keyboard(telegram_id),
                )
            else:
                host._open_admin_panel(chat_id, telegram_id, "repairs")
    elif command == "/migrations":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            jobs = host._admin_call(telegram_id, "endpoint_migration_jobs", limit=100)
            if not jobs:
                host.send(
                    chat_id,
                    "No open endpoint migrations.",
                    host._admin_keyboard(telegram_id),
                )
            else:
                host._open_admin_panel(chat_id, telegram_id, "migrations")
    elif command == "/failover":
        if host.commerce is None:
            host.send(chat_id, "Commerce is not configured.")
        else:
            decisions = host._admin_call(telegram_id, "route_failover_decisions", limit=100)
            if not decisions:
                host.send(
                    chat_id,
                    "No route failover decisions recorded.",
                    host._admin_keyboard(telegram_id),
                )
            else:
                host._open_admin_panel(chat_id, telegram_id, "failover")
    return True
