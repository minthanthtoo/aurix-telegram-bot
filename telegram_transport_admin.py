"""Telegram staff authorization, scopes, panel callbacks, and command menus."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import urllib3
from urllib3.filepost import encode_multipart_formdata

from commerce import CommerceError, CommerceService
from commerce_models import summarize_entitlement_usage
from observability import latency_log as _latency_log
from ports import ReceiptExtractorGateway
from quota_alerts import MODE_STEPS, alert_level_labels
from telegram_admin import AdminOperations
from telegram_formatting import format_user_datetime
from telegram_transport_support import ADMIN_CONFIRMATION_TTL, INTERACTION_STATE_TTL, TelegramAPIError, UTC


class TelegramAdminTransportMixin:

    def _handle_panel_callback(
        self, query: dict[str, Any], token: str, action: str, arg: str | None
    ) -> bool:
        user = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        telegram_id, chat_id = user.get("id"), chat.get("id")
        with self._panel_lock:
            state = self._panels.get(token)
            if (
                state is None
                or state.get("telegram_id") != telegram_id
                or state.get("chat_id") != chat_id
            ):
                return False
            if time.monotonic() - float(state.get("updated_at", 0)) > 1800:
                self._panels.pop(token, None)
                return False
            if action == "next":
                state["page"] = int(state.get("page", 0)) + 1
            elif action == "prev":
                state["page"] = max(0, int(state.get("page", 0)) - 1)
            elif action == "first":
                state["page"] = 0
            elif action == "last":
                state["page"] = max(0, (len(state.get("all_items", [])) - 1) // 5)
            elif action == "refresh":
                pass
            elif action == "item":
                items = state.get("items", [])
                try:
                    item = items[int(arg or "-1")]
                except (ValueError, IndexError):
                    item = None
                if item is not None:
                    view = state["view"]
                    target = item.get("id") or item.get("job_id")
                    if view == "orders":
                        self._send_order_detail(
                            chat_id,
                            telegram_id,
                            str(target),
                            admin_view=True,
                            message_id=message.get("message_id"),
                        )
                    elif view == "receipts":
                        self.handle(
                            {
                                "chat": {"id": chat_id, "type": "private"},
                                "from": {"id": telegram_id},
                                "text": f"/receipt {target}",
                            }
                        )
                    elif view == "failed":
                        order_id = item.get("order_id")
                        if order_id:
                            self._send_order_detail(
                                chat_id,
                                telegram_id,
                                str(order_id),
                                admin_view=True,
                                message_id=message.get("message_id"),
                            )
                        else:
                            self.send(chat_id, "This worker item has no customer order reference.")
                    elif view == "migrations":
                        self._show_migration_detail(
                            chat_id,
                            telegram_id,
                            item,
                            message_id=message.get("message_id"),
                        )
                    elif view == "repairs":
                        self._show_managed_repair_detail(
                            chat_id,
                            telegram_id,
                            item,
                            message_id=message.get("message_id"),
                        )
                    return True
            state["all_items"] = self._panel_data(telegram_id, state["view"])
            message_id = message.get("message_id") or state.get("message_id")
        text, markup = self._render_panel(token)
        if isinstance(message_id, int):
            self.edit_message(chat_id, message_id, text, markup)
            return True
        self.send(chat_id, text, markup)
        return True

    def configure_commands(self) -> None:
        # Startup configures scopes asynchronously while maintenance starts
        # immediately. Serialize the whole set-and-verify sequence so a retry
        # cannot rewrite a scope between its setMyCommands/getMyCommands calls.
        with self._command_menu_lock:
            self._configure_commands_locked()

    def _configure_commands_locked(self) -> None:
        self._command_menu_configure_attempted = True
        customer_commands = [
            {"command": "start", "description": "Open the AuriX menu"},
            {"command": "myvpn", "description": "Keys, status and data usage"},
            {"command": "alerts", "description": "Configure personal usage alerts"},
            {"command": "claim", "description": "Claim free 300 MB for 24 hours"},
            {"command": "trial", "description": "Claim free 3 GB for 30 days"},
            {"command": "plans", "description": "View current plans and prices"},
            {"command": "wallet", "description": "Show wallet balance"},
            {"command": "topup", "description": "Add money to your wallet"},
            {"command": "myorders", "description": "Track your recent orders"},
            {"command": "whoami", "description": "Show your Telegram ID"},
            {"command": "help", "description": "Show customer help"},
        ]
        errors: list[str] = []

        def set_and_verify(
            scope: dict[str, Any], commands: list[dict[str, str]], label: str
        ) -> bool:
            try:
                self.request("setMyCommands", {"commands": commands, "scope": scope})
                current = self.request("getMyCommands", {"scope": scope})
                current_names = (
                    {str(item.get("command")) for item in current}
                    if isinstance(current, list)
                    else set()
                )
                expected = {item["command"] for item in commands}
                if current_names != expected:
                    raise RuntimeError("Telegram returned an unexpected command list")
                return True
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}")
                return False

        set_and_verify({"type": "default"}, customer_commands, "default command scope")

        scope_store = getattr(self.service, "database", None)
        try:
            list_scopes = getattr(scope_store, "list_command_scope_ids", None)
            known_scopes = set(list_scopes()) if callable(list_scopes) else set()
        except Exception as exc:
            known_scopes = set()
            errors.append(f"load command scope state: {type(exc).__name__}")

        stale_scopes = (known_scopes | self.command_scope_cleanup_ids) - self.admin_ids
        for admin_id in sorted(stale_scopes):
            try:
                scope = {"type": "chat", "chat_id": admin_id}
                self.request(
                    "deleteMyCommands",
                    {"scope": scope},
                )
                remaining = self.request("getMyCommands", {"scope": scope})
                if not isinstance(remaining, list) or remaining:
                    raise RuntimeError("Telegram retained commands for removed admin scope")
                if scope_store and hasattr(scope_store, "remove_command_scope"):
                    scope_store.remove_command_scope(admin_id)
            except Exception as exc:
                errors.append(f"remove admin command scope {admin_id}: {type(exc).__name__}")

        admin_commands = customer_commands + [
            {"command": "admin", "description": "Open the admin panel"},
            {"command": "migrations", "description": "Monitor endpoint migrations"},
            {"command": "notifications", "description": "Choose operational alerts"},
        ]
        for admin_id in self.admin_ids:
            scope = {"type": "chat", "chat_id": admin_id}
            if set_and_verify(scope, admin_commands, f"admin command scope {admin_id}"):
                if scope_store and hasattr(scope_store, "record_command_scope"):
                    try:
                        scope_store.record_command_scope(admin_id)
                    except Exception as exc:
                        errors.append(
                            f"record admin command scope {admin_id}: {type(exc).__name__}"
                        )
        owner_id = self.staff_access.owner_id() if self.staff_access is not None else None
        if owner_id:
            owner_commands = admin_commands + [
                {"command": "owner", "description": "Open owner controls"},
            ]
            scope = {"type": "chat", "chat_id": int(owner_id)}
            if set_and_verify(scope, owner_commands, f"owner command scope {owner_id}"):
                if scope_store and hasattr(scope_store, "record_command_scope"):
                    scope_store.record_command_scope(int(owner_id))
        if errors:
            self._command_menu_ready = False
            raise RuntimeError("Telegram command menu degraded: " + "; ".join(errors))
        self._command_menu_ready = True

    def _control_group_staff(
        self,
        control_group_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        target_group_id = (
            int(control_group_id) if control_group_id is not None else self.control_group_id
        )
        if target_group_id is None:
            raise RuntimeError("AURIX_CONTROL_GROUP_ID is not configured")
        members = self.request("getChatAdministrators", {"chat_id": target_group_id})
        if not isinstance(members, list):
            raise RuntimeError("Telegram returned an invalid administrator list")
        owner = None
        administrators = []
        for member in members:
            user = member.get("user") if isinstance(member, dict) else None
            if (
                not isinstance(user, dict)
                or user.get("is_bot")
                or not isinstance(user.get("id"), int)
            ):
                continue
            profile = {
                "id": int(user["id"]),
                "username": user.get("username"),
                "display_name": " ".join(
                    value
                    for value in (
                        str(user.get("first_name") or "").strip(),
                        str(user.get("last_name") or "").strip(),
                    )
                    if value
                ),
                "is_bot": False,
            }
            if member.get("status") == "creator":
                owner = profile
            elif member.get("status") == "administrator":
                administrators.append(profile)
        return owner, administrators

    def _is_admin(self, telegram_id: int) -> bool:
        if self.staff_access is not None:
            return bool(self.staff_access.is_admin(telegram_id))
        return telegram_id in self.admin_ids

    def _is_owner(self, telegram_id: int) -> bool:
        if self.staff_access is not None:
            return bool(self.staff_access.is_owner(telegram_id))
        return False

    def _refresh_staff_scopes(self) -> None:
        if self.staff_access is not None:
            self.admin_ids = set(self.staff_access.admin_ids())
            self.admin_operations.admin_ids = self.admin_ids
        threading.Thread(
            target=self.configure_commands,
            name="aurix-staff-command-scopes",
            daemon=True,
        ).start()

    def _trial_allowed(self, telegram_id: int) -> bool:
        """Keep the optional trial allow-list consistent across every entrypoint."""
        return not self.trial_ids or int(telegram_id) in self.trial_ids
