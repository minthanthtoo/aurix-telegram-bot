"""Telegram polling and update-loop runtime behavior."""

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


class TelegramRuntimeTransportMixin:

    def _latency_action(self, update: dict[str, Any]) -> str:
        """Return a bounded operation label without logging user text or IDs."""
        message = update.get("message")
        if isinstance(message, dict):
            if message.get("photo") or message.get("document"):
                return "receipt"
            text = message.get("text")
            if not isinstance(text, str):
                return "message"
            normalized = self.CUSTOMER_BUTTON_COMMANDS.get(text.strip(), text.strip())
            if normalized == text.strip():
                normalized = self.ADMIN_BUTTON_COMMANDS.get(text.strip(), text.strip())
            command = normalized.split(maxsplit=1)[0].split("@", 1)[0].lower()
            return command[:48] if command.startswith("/") else "text"
        query = update.get("callback_query")
        data = query.get("data") if isinstance(query, dict) else None
        if not isinstance(data, str):
            return "callback"
        parts = data.split(":", 2)[:2]
        if all(part.replace("_", "").isalnum() for part in parts):
            return ("callback:" + ":".join(parts))[:48]
        return "callback"

    def run(self) -> None:
        self._maintenance_stop.clear()
        maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="aurix-maintenance",
            daemon=True,
        )
        self._maintenance_thread = maintenance_thread
        maintenance_thread.start()
        try:
            while self.running:
                try:
                    updates = self.request(
                        "getUpdates",
                        {
                            "offset": self.offset,
                            "timeout": 20,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    print(f"bot poll error: {type(exc).__name__}: {exc}", file=sys.stderr)
                    self._maintenance_stop.wait(5)
                    continue
                for update in updates:
                    self.offset = update["update_id"] + 1
                    if not self.service.database.mark_update_seen(update["update_id"]):
                        continue
                    started_at = time.perf_counter()
                    action = self._latency_action(update)
                    try:
                        if "message" in update:
                            self.handle(update["message"])
                        elif "callback_query" in update:
                            self.handle_callback(update["callback_query"])
                    except Exception as exc:
                        print(
                            f"update handler error: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                        _latency_log(
                            "update_handler",
                            started_at,
                            update_id=update["update_id"],
                            kind="message" if "message" in update else "callback",
                            action=action,
                            status="error",
                        )
                        continue
                    _latency_log(
                        "update_handler",
                        started_at,
                        update_id=update["update_id"],
                        kind="message" if "message" in update else "callback",
                        action=action,
                        status="ok",
                    )
        finally:
            self.stop()
            maintenance_thread.join(timeout=5)
            self._maintenance_thread = None
