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


def _handle_orders_or_receipts(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    view = "receipts" if context.command == "/receipts" else "orders"
    items = host._panel_data(context.telegram_id, view)
    if not items:
        host.send(
            context.chat_id,
            "No unreviewed receipts." if view == "receipts" else "No pending orders.",
        )
        return
    host._open_admin_panel(context.chat_id, context.telegram_id, view)


def _handle_capacity(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    try:
        host._show_capacity(context.chat_id, context.telegram_id)
    except Exception as exc:
        host.send(context.chat_id, "Outline capacity metrics are temporarily unavailable.")
        print(f"capacity error: {type(exc).__name__}", file=sys.stderr)


def _handle_probes(host: Any, context: TelegramCommandContext) -> None:
    if host.probe_service is None:
        host.send(
            context.chat_id,
            "Fleet probes are not configured.",
            host._admin_keyboard(context.telegram_id),
        )
        return
    try:
        host._show_probes(context.chat_id, context.telegram_id)
    except Exception as exc:
        host.send(
            context.chat_id,
            "Fleet probe results are temporarily unavailable.",
            host._admin_keyboard(context.telegram_id),
        )
        print(f"probe panel error: {type(exc).__name__}", file=sys.stderr)


def _handle_reconcile(host: Any, context: TelegramCommandContext) -> None:
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    report = host._admin_call(context.telegram_id, "consistency_report")
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
    lines.extend(f"{key.replace('_', ' ').title()}: {value}" for key, value in report.items())
    host.send(context.chat_id, "\n".join(lines), host._admin_keyboard(context.telegram_id))


def _handle_panel_list(host: Any, context: TelegramCommandContext) -> None:
    if context.command == "/enforcement":
        events = host._admin_service_call(context.telegram_id, "termination_summary")
        if not events:
            host.send(
                context.chat_id,
                "No free/trial termination events recorded.",
                host._admin_keyboard(context.telegram_id),
            )
        else:
            host._open_admin_panel(context.chat_id, context.telegram_id, "enforcement")
        return
    if host.commerce is None:
        host.send(context.chat_id, "Commerce is not configured.")
        return
    requests = {
        "/failed": ("failed_jobs", {"include_nonterminal": True}, "No terminal worker failures."),
        "/repairs": ("managed_key_repair_jobs", {"status": "open", "limit": 100}, "No managed-key repairs are waiting for action."),
        "/migrations": ("endpoint_migration_jobs", {"limit": 100}, "No open endpoint migrations."),
        "/failover": ("route_failover_decisions", {"limit": 100}, "No route failover decisions recorded."),
    }
    service_name, kwargs, empty_text = requests[context.command]
    items = host._admin_call(context.telegram_id, service_name, **kwargs)
    if not items:
        host.send(context.chat_id, empty_text, host._admin_keyboard(context.telegram_id))
        return
    host._open_admin_panel(context.chat_id, context.telegram_id, context.command.removeprefix("/"))


def dispatch_operations_command(host: Any, context: TelegramCommandContext) -> bool:
    """Handle read-only operational panels and queues."""

    command = context.command
    if command not in OPERATIONS_COMMANDS:
        return False
    if command in {"/orders", "/receipts"}:
        _handle_orders_or_receipts(host, context)
    elif command == "/capacity":
        _handle_capacity(host, context)
    elif command == "/probes":
        _handle_probes(host, context)
    elif command == "/reconcile":
        _handle_reconcile(host, context)
    else:
        _handle_panel_list(host, context)
    return True
