"""Administrator panels, confirmations, and protected operation views."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)
_PROTOCOL_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INFRA_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ACCOUNT_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class TelegramAdminMixin:
    @staticmethod
    def _account_status_args(args: list[str]) -> str:
        """Parse one opaque, bounded account identifier for read-only status."""
        if len(args) != 1:
            raise ValueError("Usage: /accountstatus <account-id>")
        account_id = str(args[0]).strip()
        if not _ACCOUNT_TOKEN.fullmatch(account_id):
            raise ValueError("Account identifier is invalid")
        return account_id

    @staticmethod
    def _account_suspension_args(args: list[str]) -> tuple[str, str]:
        """Parse an account suspension request without accepting unbounded text."""
        if not args or len(args) > 65:
            raise ValueError("Usage: /suspendaccount <account-id> [reason]")
        account_id = str(args[0]).strip()
        if not _ACCOUNT_TOKEN.fullmatch(account_id):
            raise ValueError("Account identifier is invalid")
        reason = " ".join(str(value) for value in args[1:]).strip()
        if len(reason) > 500 or any(ord(char) < 32 for char in reason):
            raise ValueError("Suspension reason must be 500 printable characters or fewer")
        return account_id, reason

    @staticmethod
    def _account_reactivation_args(args: list[str]) -> str:
        """Parse one opaque, bounded account identifier for reactivation."""
        if len(args) != 1:
            raise ValueError("Usage: /reactivateaccount <account-id>")
        account_id = str(args[0]).strip()
        if not _ACCOUNT_TOKEN.fullmatch(account_id):
            raise ValueError("Account identifier is invalid")
        return account_id

    @staticmethod
    def _protocol_promotion_args(args: list[str]) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        """Parse the bounded operator syntax for protocol readiness/promotion."""
        if len(args) not in {3, 4}:
            raise ValueError(
                "Usage: /protocolreadiness <endpoint> <protocol> <signals> [capabilities]"
            )
        endpoint, protocol = (str(value).strip().lower() for value in args[:2])
        if not _PROTOCOL_TOKEN.fullmatch(endpoint) or not _PROTOCOL_TOKEN.fullmatch(protocol):
            raise ValueError("Endpoint and protocol names are invalid")

        def tokens(raw: str, *, required: bool) -> tuple[str, ...]:
            if raw.strip() in {"", "-"}:
                if required:
                    raise ValueError("At least one protocol evidence signal is required")
                return ()
            values = tuple(sorted({item.strip().lower() for item in raw.split(",") if item.strip()}))
            if not values or any(not _PROTOCOL_TOKEN.fullmatch(item) for item in values):
                raise ValueError("Protocol signals and capabilities must be comma-separated names")
            return values

        signals = tokens(args[2], required=True)
        capabilities = tokens(args[3], required=False) if len(args) == 4 else ()
        return endpoint, protocol, signals, capabilities

    @staticmethod
    def _protocol_profile_args(args: list[str]) -> tuple[str, str]:
        """Parse the bounded operator syntax for profile state changes."""
        if len(args) != 2:
            raise ValueError("Usage: /disableprotocol <endpoint> <protocol>")
        endpoint, protocol = (str(value).strip().lower() for value in args)
        if not _PROTOCOL_TOKEN.fullmatch(endpoint) or not _PROTOCOL_TOKEN.fullmatch(protocol):
            raise ValueError("Endpoint and protocol names are invalid")
        return endpoint, protocol

    @staticmethod
    def _endpoint_lifecycle_args(args: list[str]) -> tuple[str, str]:
        """Parse the owner-confirmed endpoint lifecycle command."""
        if len(args) != 2:
            raise ValueError("Usage: /serverstate <endpoint> <active|draining|retired>")
        endpoint, state = (str(value).strip().lower() for value in args)
        if not _PROTOCOL_TOKEN.fullmatch(endpoint) or state not in {
            "active",
            "draining",
            "retired",
        }:
            raise ValueError("Endpoint and lifecycle state are invalid")
        return endpoint, state

    @staticmethod
    def _failover_safety_args(
        args: list[str],
    ) -> tuple[str, str, bool, int, int]:
        """Parse deterministic failover pause/budget settings."""
        if len(args) not in {4, 5}:
            raise ValueError(
                "Usage: /setsafety <global> <pause|resume> <max-migrations> <window-seconds> "
                "or /setsafety <region|endpoint> <key> <pause|resume> "
                "<max-migrations> <window-seconds>"
            )
        scope = str(args[0]).strip().lower()
        if scope not in {"global", "region", "endpoint"}:
            raise ValueError("Safety scope must be global, region, or endpoint")
        if scope == "global":
            if len(args) != 4:
                raise ValueError("Global safety controls do not take a scope key")
            scope_key = "global"
            action, maximum, window = args[1:]
        else:
            if len(args) != 5:
                raise ValueError("Regional and endpoint safety controls require a scope key")
            scope_key = str(args[1]).strip().lower()
            if not _PROTOCOL_TOKEN.fullmatch(scope_key):
                raise ValueError("Safety scope key is invalid")
            action, maximum, window = args[2:]
        action = str(action).strip().lower()
        if action not in {"pause", "resume"}:
            raise ValueError("Safety action must be pause or resume")
        try:
            max_migrations = int(maximum)
            window_seconds = int(window)
        except (TypeError, ValueError) as exc:
            raise ValueError("Migration budget and window must be integers") from exc
        if not 1 <= max_migrations <= 100_000:
            raise ValueError("Migration budget must be between 1 and 100000")
        if not 1 <= window_seconds <= 86_400:
            raise ValueError("Migration window must be between 1 and 86400 seconds")
        return scope, scope_key, action == "pause", max_migrations, window_seconds

    @staticmethod
    def _infrastructure_provision_args(args: list[str]) -> tuple[str, str, str]:
        if len(args) != 3:
            raise ValueError("Usage: /provisionnode <region> <size> <image>")
        values = tuple(str(value).strip().lower() for value in args)
        if any(not _INFRA_TOKEN.fullmatch(value) for value in values):
            raise ValueError("Region, size, and image must be simple allowlisted names")
        return values

    @staticmethod
    def _infrastructure_intents_enabled() -> bool:
        return os.environ.get("AURIX_INFRASTRUCTURE_INTENTS_ENABLED", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _new_panel(self, chat_id: int, telegram_id: int, view: str) -> str:
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        with self._panel_lock:
            cutoff = time.monotonic() - 1800
            self._panels = {
                key: value
                for key, value in self._panels.items()
                if float(value.get("updated_at", 0)) >= cutoff
            }
            self._panels[token] = {
                "chat_id": int(chat_id),
                "telegram_id": int(telegram_id),
                "view": view,
                "page": 0,
                "updated_at": time.monotonic(),
                "message_id": None,
                "items": [],
            }
        return token

    def _panel_markup(self, token: str, page: int, pages: int) -> dict[str, Any]:
        rows: list[list[tuple[str, str]]] = []
        state = self._panels[token]
        for index, item in enumerate(state.get("items", [])):
            label = str(item.get("label") or item.get("id") or "Open")[:40]
            rows.append([(label, f"v2:{token}:item:{index}")])
        navigation: list[tuple[str, str]] = []
        if page > 0:
            navigation.append(("◀ Previous", f"v2:{token}:prev"))
        navigation.append((f"{page + 1}/{max(1, pages)}", f"v2:{token}:refresh"))
        if page + 1 < pages:
            navigation.append(("Next ▶", f"v2:{token}:next"))
        rows.append(navigation)
        rows.append([("🔄 Refresh", f"v2:{token}:refresh"), ("🏠 Admin Home", "a:n:admin")])
        return self._inline_keyboard(rows)

    @staticmethod
    def _panel_item(item: dict[str, Any], view: str) -> tuple[str, str]:
        item_id = str(item.get("id") or item.get("job_id") or "-")
        short_id = item_id[:10]
        if view == "orders":
            text = f"#{short_id} · tg:{str(item.get('telegram_id') or '-')[-6:]} · {item.get('plan_code') or '-'}\n{item.get('stage') or item.get('status') or '-'} · {item.get('receipt_status') or 'no receipt'}"
        elif view == "receipts":
            text = f"Receipt {short_id} · order:{str(item.get('order_id') or '-')[:10]}\ntg:{str(item.get('telegram_id') or '-')[-6:]} · {int(item.get('amount_minor') or 0):,} {item.get('currency') or ''}"
        elif view == "failed":
            text = f"{item.get('operation') or '-'} · job:{short_id}\norder:{str(item.get('order_id') or '-')[:10]} · attempts:{item.get('attempts') or 0}"
        else:
            text = f"tg:{str(item.get('telegram_id') or '-')[-6:]} · key:{str(item.get('outline_key_id') or '-')[:12]}\n{item.get('reason') or '-'} · {item.get('remote_state') or '-'}"
        return text[:700], short_id

    def _panel_data(self, telegram_id: int, view: str) -> list[dict[str, Any]]:
        if view == "orders":
            return list(self._admin_call(telegram_id, "list_pending_orders", limit=100) or [])
        if view == "receipts":
            return list(self._admin_call(telegram_id, "list_pending_receipts", limit=100) or [])
        if view == "failed":
            return list(
                self._admin_call(telegram_id, "failed_jobs", limit=100, include_nonterminal=True)
                or []
            )
        if view == "enforcement":
            return list(
                self._admin_service_call(telegram_id, "termination_summary", limit=100) or []
            )
        return []

    def _render_panel(self, token: str) -> tuple[str, dict[str, Any]]:
        with self._panel_lock:
            state = self._panels.get(token)
            if state is None:
                raise KeyError(token)
            view = state["view"]
            page = max(0, int(state.get("page", 0)))
            items = list(state.get("all_items", []))
        page_size = 5
        pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        current = items[page * page_size : (page + 1) * page_size]
        prepared = []
        blocks = []
        for item in current:
            block, _short = self._panel_item(item, view)
            prepared.append(item)
            blocks.append(block)
        title = {
            "orders": "📥 Pending Orders",
            "receipts": "🧾 Receipt Review",
            "failed": "🔁 Worker Jobs",
            "enforcement": "🚨 Enforcement",
        }.get(view, "AuriX Admin")
        text = f"{title} · {len(items)} open\nPage {page + 1}/{pages} · updated {datetime.now(UTC).strftime('%H:%M UTC')}"
        if blocks:
            text += "\n\n" + "\n\n".join(blocks)
        else:
            text += "\n\nNothing needs attention."
        with self._panel_lock:
            state = self._panels[token]
            state["page"] = page
            state["items"] = prepared
            state["updated_at"] = time.monotonic()
        return text[:4096], self._panel_markup(token, page, pages)

    def _open_admin_panel(
        self, chat_id: int, telegram_id: int, view: str, message_id: int | None = None
    ) -> None:
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        token = self._new_panel(chat_id, telegram_id, view)
        items = self._panel_data(telegram_id, view)
        if not items:
            empty = {
                "orders": "No pending orders.",
                "receipts": "No unreviewed receipts.",
                "failed": "No terminal worker failures.",
                "enforcement": "No free/trial termination events recorded.",
            }.get(view, "Nothing needs attention.")
            self.send(chat_id, empty)
            return
        with self._panel_lock:
            self._panels[token]["all_items"] = items
        text, markup = self._render_panel(token)
        if message_id is not None:
            try:
                self.edit_message(chat_id, message_id, text, markup)
                with self._panel_lock:
                    self._panels[token]["message_id"] = int(message_id)
                return
            except Exception:
                pass
        result = self.send(chat_id, text, markup)
        if isinstance(result, dict) and result.get("message_id"):
            with self._panel_lock:
                self._panels[token]["message_id"] = int(result["message_id"])

    def _admin_keyboard(self, telegram_id: int) -> dict[str, Any]:
        if not self._is_admin(telegram_id):
            raise PermissionError("admin keyboard requested by non-admin")
        return self._inline_keyboard(
            [
                [("📥 Pending Orders", "a:n:orders"), ("🧾 Receipt Review", "a:n:receipts")],
                [("📈 Capacity", "a:n:capacity"), ("🔎 Consistency", "a:n:reconcile")],
                [("🔁 Failed Jobs", "a:n:failed"), ("🚨 Enforcement", "a:n:enforcement")],
                [("🎁 Promo Settings", "a:n:promo")],
                [("🏠 Customer Menu", "n:start")],
            ]
        )

    def _admin_call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a commerce operation through the admin authorization boundary."""
        return self.admin_operations.call(telegram_id, operation, *args, **kwargs)

    def _admin_service_call(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        return self.admin_operations.call_service(telegram_id, operation, *args, **kwargs)

    def _send_customer_fallback(self, chat_id: int, telegram_id: int) -> None:
        """Return a role-neutral response for unknown or unauthorized input."""
        self.send(
            chat_id,
            self.UNKNOWN_ACTION_TEXT,
            self._customer_keyboard(telegram_id),
        )

    def _admin_state_snapshot(
        self, command: str, args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        """Read the state an administrator is about to mutate.

        This is deliberately a read-only snapshot. Domain methods still own
        their invariants and transactions; the snapshot prevents a stale
        confirmation from silently applying to a changed order or receipt.
        """
        target_id = str(args[0]) if args else ""
        snapshot: dict[str, Any] = {
            "command": command,
            "target_id": target_id,
            "state": "unavailable",
        }
        if command in {"/setpromo", "/stoppromo", "/resumepromo"}:
            try:
                promo = self._admin_service_call(
                    telegram_id,
                    "giveaway_status",
                    telegram_id,
                    target_id or None,
                )
                snapshot.update({"state": "present", "promo": promo})
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command in {"/accountstatus", "/suspendaccount", "/reactivateaccount"}:
            try:
                if command == "/accountstatus":
                    account_id = self._account_status_args(args)
                    reason = ""
                elif command == "/suspendaccount":
                    account_id, reason = self._account_suspension_args(args)
                else:
                    account_id = self._account_reactivation_args(args)
                    reason = ""
                access = self._admin_call(telegram_id, "account_access_status", account_id)
                if access is None:
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(access)
                    snapshot["access_state"] = snapshot.get("state")
                    snapshot.update({"state": "present", "reason": reason})
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command in {"/protocolreadiness", "/promoteprotocol"}:
            try:
                endpoint, protocol, signals, capabilities = self._protocol_promotion_args(args)
                snapshot.update(
                    self._admin_call(
                        telegram_id,
                        "protocol_profile_promotion_readiness",
                        endpoint,
                        protocol,
                        required_signals=signals,
                        required_capabilities=capabilities,
                    )
                )
                snapshot["state"] = "present" if snapshot.get("profile_exists") else "missing"
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/provisionnode":
            try:
                region, size, image = self._infrastructure_provision_args(args)
                controller = getattr(self.commerce, "fleet_controller", None)
                snapshot.update(
                    {
                        "state": (
                            "present"
                            if self._infrastructure_intents_enabled()
                            and callable(getattr(controller, "queue_provision", None))
                            else "unavailable"
                        ),
                        "region": region,
                        "size": size,
                        "image": image,
                    }
                )
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/serverstate":
            try:
                endpoint, requested_state = self._endpoint_lifecycle_args(args)
                snapshot.update(
                    self._admin_call(
                        telegram_id,
                        "endpoint_lifecycle_preview",
                        endpoint,
                        requested_state,
                    )
                )
                snapshot["state"] = "present"
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/disableprotocol":
            try:
                endpoint, protocol = self._protocol_profile_args(args)
                snapshot.update(
                    self._admin_call(
                        telegram_id,
                        "protocol_profile_status",
                        endpoint,
                        protocol,
                    )
                )
                snapshot["state"] = "present" if snapshot.get("status") == "enabled" else "missing"
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/drain":
            if not args or len(args) > 3:
                snapshot["state"] = "missing"
                return snapshot
            target = args[1] if len(args) >= 2 else None
            try:
                limit = int(args[2]) if len(args) == 3 else 50
                snapshot.update(
                    self._admin_call(
                        telegram_id,
                        "endpoint_drain_preview",
                        args[0],
                        target_endpoint_id=target,
                        limit=limit,
                    )
                )
                snapshot["state"] = "present"
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/setsafety":
            try:
                scope, scope_key, paused, maximum, window = self._failover_safety_args(args)
                controls = self._admin_call(telegram_id, "failover_safety_controls")
                current = next(
                    (
                        item
                        for item in controls
                        if str(item.get("scope")) == scope
                        and str(item.get("scope_key")) == scope_key
                    ),
                    None,
                )
                snapshot.update(
                    {
                        "state": "present",
                        "scope": scope,
                        "scope_key": scope_key,
                        "current": current or {},
                        "paused": paused,
                        "max_migrations_per_window": maximum,
                        "window_seconds": window,
                    }
                )
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if self.commerce is None or not target_id:
            snapshot["state"] = "missing"
            return snapshot
        try:
            if command == "/retryjob":
                jobs = self._admin_call(
                    telegram_id, "failed_jobs", limit=100, include_nonterminal=True
                )
                job = next((item for item in jobs if str(item.get("job_id")) == target_id), None)
                if job is None or job.get("job_status") != "failed":
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(
                        {
                            "state": "present",
                            "job_id": target_id,
                            "operation": job.get("operation"),
                            "order_id": job.get("order_id"),
                            "attempts": job.get("attempts"),
                            "last_error": job.get("last_error"),
                        }
                    )
            elif command in {"/verify", "/rejectreceipt"}:
                receipt = self._admin_call(telegram_id, "get_receipt", target_id)
                if receipt is None:
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(
                        {
                            "state": "present",
                            "evidence_id": receipt.get("id"),
                            "order_id": receipt.get("order_id"),
                            "telegram_id": receipt.get("telegram_id"),
                            "review_status": receipt.get("review_status"),
                            "storage_status": receipt.get("storage_status"),
                            "amount_minor": receipt.get("amount_minor"),
                            "currency": receipt.get("currency"),
                            "verified_provider_reference": receipt.get(
                                "verified_provider_reference"
                            ),
                            "verified_amount_minor": receipt.get("verified_amount_minor"),
                            "verified_currency": receipt.get("verified_currency"),
                        }
                    )
                    order_id = receipt.get("order_id")
                    order = (
                        self._admin_call(
                            telegram_id,
                            "order_detail",
                            str(order_id),
                            telegram_id,
                            is_admin=True,
                        )
                        if order_id
                        else None
                    )
                    if order:
                        snapshot.update(
                            {
                                "order_status": order.get("status"),
                                "payment_status": order.get("payment_status"),
                                "order_amount_minor": order.get("amount_minor"),
                            }
                        )
            else:
                order = self._admin_call(
                    telegram_id,
                    "order_detail",
                    target_id,
                    telegram_id,
                    is_admin=True,
                )
                if order is None:
                    snapshot["state"] = "missing"
                else:
                    snapshot.update(
                        {
                            "state": "present",
                            "order_id": order.get("id"),
                            "telegram_id": order.get("telegram_id"),
                            "plan_code": order.get("plan_code"),
                            "plan_name": order.get("plan_name"),
                            "amount_minor": order.get("amount_minor"),
                            "currency": order.get("currency"),
                            "order_status": order.get("status"),
                            "refund_status": order.get("refund_status"),
                            "payment_status": order.get("payment_status"),
                            "receipt_status": order.get("receipt_status"),
                            "subscription_status": order.get("subscription_status"),
                            "provisioning_status": order.get("provisioning_status"),
                            "wallet_reservation_status": order.get("wallet_reservation_status"),
                            "evidence_id": order.get("evidence_id"),
                        }
                    )
                if command == "/retry" and snapshot.get("state") == "present":
                    jobs = self._admin_call(telegram_id, "failed_jobs", limit=100)
                    matching = [job for job in jobs if str(job.get("order_id")) == target_id]
                    snapshot["failed_job"] = (
                        {
                            "operation": matching[0].get("operation"),
                            "attempts": matching[0].get("attempts"),
                            "last_error": matching[0].get("last_error"),
                        }
                        if matching
                        else None
                    )
        except Exception as exc:
            # A preview must fail closed rather than fabricate financial state.
            snapshot = {
                "command": command,
                "target_id": target_id,
                "state": "unavailable",
                "error_type": type(exc).__name__,
            }
        return snapshot

    def _admin_state_fingerprint(
        self, command: str, args: list[str], telegram_id: int
    ) -> tuple[str, dict[str, Any]]:
        snapshot = self._admin_state_snapshot(command, args, telegram_id)
        fingerprint_snapshot = dict(snapshot)
        # Readiness previews deliberately stamp the instant they were checked.
        # That timestamp must not invalidate an otherwise unchanged confirmation;
        # evidence timestamps, profile state, and all decision fields remain bound.
        fingerprint_snapshot.pop("checked_at", None)
        encoded = json.dumps(
            fingerprint_snapshot, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(encoded.encode()).hexdigest(), snapshot

    @staticmethod
    def _admin_preview_text(
        command: str, args: list[str], fallback_prompt: str, snapshot: dict[str, Any]
    ) -> str:
        if snapshot.get("state") != "present":
            return (
                fallback_prompt
                + "\n\nCurrent state could not be loaded; it will be rechecked before execution."
            )
        if command in {"/setpromo", "/stoppromo", "/resumepromo"}:
            promo = snapshot.get("promo") or {}
            lines = [
                f"Promo: {args[0] if args else promo.get('code') or '-'}",
                f"Current state: {promo.get('campaign_state') or 'not configured'}",
            ]
            if command == "/setpromo" and len(args) == 7:
                lines.extend(
                    [
                        f"New quota: {args[1]} GB · gift duration: {args[2]} day(s)",
                        f"Giveaway count: {args[3]} · frequency: {args[4]}",
                        f"Season: {args[5]} → {args[6]}",
                        "Result: activate this campaign and pause every other promo.",
                    ]
                )
            elif command == "/stoppromo":
                lines.append("Result: stop the season; normal plans return immediately.")
            else:
                lines.append("Result: resume the saved season if it is within its dates.")
            return "\n".join(lines)
        if command in {"/accountstatus", "/suspendaccount", "/reactivateaccount"}:
            counts = snapshot.get("counts") or {}
            lines = [
                f"Account: {snapshot.get('account_id') or (args[0] if args else '-')}",
                f"Status: {snapshot.get('account_status') or '-'}",
                f"Access action: {snapshot.get('target_status') or '-'} · {snapshot.get('access_state') or '-'}",
                f"Active paid keys: {counts.get('active_paid_keys', 0)}",
                f"Active free keys: {counts.get('active_free_keys', 0)}",
                f"Pending revoke jobs: {counts.get('pending_revoke_jobs', 0)}",
                f"Active credential generations: {counts.get('active_generations', 0)}",
                f"Outstanding enforcement: {snapshot.get('outstanding', 0)}",
            ]
            if snapshot.get("last_error"):
                lines.append(f"Last reconcile error: {snapshot['last_error']}")
            if command == "/suspendaccount":
                lines.extend(
                    [
                        f"Reason: {snapshot.get('reason') or 'operator-requested suspension'}",
                        "Result: suspend account, revoke device access, and queue provider-verified credential termination.",
                        "Reactivation remains blocked until all remote revoke and session proof is complete.",
                    ]
                )
            elif command == "/reactivateaccount":
                if snapshot.get("ready_to_reactivate"):
                    lines.append("Result: reactivate account admission after verified revocation.")
                    lines.append("Previously revoked entitlements are not restored; the customer must claim or purchase access again.")
                else:
                    lines.append("Result: BLOCKED until the account has zero outstanding enforcement work.")
            return "\n".join(lines)
        if command == "/drain":
            missing_protocols = sorted(
                {str(value).strip().lower() for value in snapshot.get("missing_protocols", []) if value}
            )
            blocked_assignments = int(snapshot.get("unmigratable_assignments") or 0)
            blocked_protocols = sorted(
                {
                    str(value).strip().lower()
                    for value in snapshot.get("unmigratable_assignment_protocols", [])
                    if value
                }
            )
            blocked = (
                snapshot.get("target_available") is False
                or bool(missing_protocols)
                or blocked_assignments > 0
            )
            lines = [
                f"Source: {snapshot.get('source_code') or args[0]}",
                f"Target: {snapshot.get('target_code') or snapshot.get('target_endpoint_id') or '-'}",
                f"Target available: {'yes' if snapshot.get('target_available') else 'no'}",
                f"Active generations in cohort: {snapshot.get('active_generations') or 0}",
                f"Already queued: {snapshot.get('queued_decisions') or 0}",
                f"Cohort limit: {snapshot.get('limit') or 50}",
            ]
            if missing_protocols:
                lines.append("Missing target protocol profiles: " + ", ".join(missing_protocols))
            if blocked_assignments:
                suffix = f" ({', '.join(blocked_protocols)})" if blocked_protocols else ""
                lines.append(
                    f"Unmigratable active assignments: {blocked_assignments}{suffix}"
                )
            if blocked:
                lines.append("Result: BLOCKED; no endpoint state will change until this is resolved.")
            else:
                lines.extend(
                    [
                        "Result: pause new assignments on the source and queue verified migrations.",
                        "Existing credentials are not revoked until a target is provisioned and probed.",
                    ]
                )
            return "\n".join(lines)
        if command == "/setsafety":
            current = snapshot.get("current") or {}
            old_state = "not configured"
            if current:
                old_state = "paused" if bool(current.get("paused")) else "open"
                old_state += (
                    f", {current.get('migration_count', 0)}/"
                    f"{current.get('max_migrations_per_window', 0)} used"
                )
            new_state = "paused" if snapshot.get("paused") else "open"
            return "\n".join(
                [
                    f"Scope: {snapshot.get('scope')}:{snapshot.get('scope_key')}",
                    f"Current: {old_state}",
                    f"New state: {new_state}",
                    f"New budget: {snapshot.get('max_migrations_per_window')} migrations per "
                    f"{snapshot.get('window_seconds')} seconds",
                    "Result: update the local failover admission guard and write an audit event.",
                    "No provider call, credential revocation, or customer route mutation occurs here.",
                ]
            )
        if command == "/provisionnode":
            return "\n".join(
                [
                    f"Region: {snapshot.get('region') or args[0]}",
                    f"Size: {snapshot.get('size') or args[1]}",
                    f"Image: {snapshot.get('image') or args[2]}",
                    "Result: record one guarded infrastructure intent for the dedicated worker.",
                    "No provider request runs in this Telegram confirmation.",
                ]
            )
        if command == "/serverstate":
            blockers = [str(value) for value in snapshot.get("blockers") or []]
            lines = [
                f"Endpoint: {snapshot.get('endpoint_code') or args[0]}",
                f"Current state: {snapshot.get('current_state') or '-'}",
                f"Requested state: {snapshot.get('requested_state') or args[1]}",
            ]
            if blockers:
                lines.append("Current blockers: " + "; ".join(blockers))
            lines.extend(
                [
                    "Result: change local admission lifecycle only; no provider VM action is performed.",
                    "Retirement is terminal and requires zero durable assignments and unresolved credentials.",
                ]
            )
            return "\n".join(lines)
        if command in {"/protocolreadiness", "/promoteprotocol"}:
            status = "READY" if snapshot.get("promotable") else "BLOCKED"

            def label(name: str, key: str) -> str:
                values = snapshot.get(key) or []
                return f"{name}: " + (", ".join(str(value) for value in values) or "—")

            lines = [
                f"Endpoint: {snapshot.get('endpoint_id') or (args[0] if args else '-')} · "
                f"Protocol: {snapshot.get('protocol') or (args[1] if len(args) > 1 else '-')}",
                f"Profile: {snapshot.get('profile_status') or 'missing'} · {status}",
                label("Required signals", "required_signals"),
                label("Fresh healthy signals", "fresh_healthy_signals"),
                label("Missing signals", "missing_signals"),
                label("Missing evidence", "missing_evidence"),
                label("Missing capabilities", "missing_capabilities"),
            ]
            if command == "/promoteprotocol" and snapshot.get("promotable"):
                lines.append("Result: explicitly enable this profile after confirmation.")
            elif snapshot.get("reasons"):
                lines.append("Reasons: " + "; ".join(str(item) for item in snapshot["reasons"]))
            return "\n".join(lines)
        if command == "/disableprotocol":
            return "\n".join(
                [
                    f"Endpoint: {snapshot.get('endpoint_id') or (args[0] if args else '-')}",
                    f"Protocol: {snapshot.get('protocol') or (args[1] if len(args) > 1 else '-')}",
                    f"Profile: {snapshot.get('status') or 'missing'}",
                    "Result: block new assignments for this profile.",
                    "Existing credentials remain unchanged and are not revoked.",
                ]
            )
        if command == "/retryjob":
            return "\n".join(
                [
                    f"Worker job: {snapshot.get('job_id') or args[0]}",
                    f"Operation: {snapshot.get('operation') or '-'}",
                    f"Order: {snapshot.get('order_id') or '-'}",
                    f"Attempts: {snapshot.get('attempts') or 0}",
                    f"Failure: {snapshot.get('last_error') or '-'}",
                    "Result: requeue this exact failed worker job.",
                ]
            )
        if command in {"/verify", "/rejectreceipt"}:
            target = str(snapshot.get("evidence_id") or args[0])
            lines = [
                f"Evidence: {target}",
                f"Order: {snapshot.get('order_id') or '-'}",
                f"Customer: {snapshot.get('telegram_id') or '-'}",
                f"Current receipt status: {snapshot.get('review_status') or '-'}",
                f"Stored image: {snapshot.get('storage_status') or '-'}",
            ]
            if command == "/verify" and len(args) >= 3:
                lines.extend(
                    [
                        f"Transaction to verify: {args[1]}",
                        f"Amount to verify: {args[2]} {snapshot.get('currency') or ''}".strip(),
                    ]
                )
                lines.append("Verify against the receiving account before confirming.")
            else:
                lines.append("The order remains open so the customer can submit a replacement.")
            return "\n".join(lines)
        target = str(snapshot.get("order_id") or args[0])
        try:
            amount_text = f"{int(snapshot.get('amount_minor') or 0):,}"
        except (TypeError, ValueError):
            amount_text = str(snapshot.get("amount_minor") or "0")
        lines = [
            f"Order: {target}",
            f"Customer: {snapshot.get('telegram_id') or '-'}",
            f"Plan: {snapshot.get('plan_name') or snapshot.get('plan_code') or '-'}",
            f"Amount: {amount_text} {snapshot.get('currency') or ''}".strip(),
            f"Order state: {snapshot.get('order_status') or '-'}",
            f"Payment: {snapshot.get('payment_status') or '-'} · Receipt: {snapshot.get('receipt_status') or '-'}",
        ]
        impact = {
            "/approve": "Result: approve payment and queue VPN provisioning.",
            "/reject": "Result: close the order and notify the customer.",
            "/refund": "Result: credit the wallet and revoke or cancel paid access.",
            "/retry": "Result: requeue the reviewed failed provisioning job.",
        }.get(command)
        if impact:
            lines.append(impact)
        if command == "/retry":
            failed_job = snapshot.get("failed_job") or {}
            lines.append(
                f"Failure: {failed_job.get('operation') or '-'} · attempts: {failed_job.get('attempts') or 0} · {failed_job.get('last_error') or '-'}"
            )
        return "\n".join(lines)

    def _queue_admin_confirmation(
        self,
        chat_id: int,
        telegram_id: int,
        command: str,
        args: list[str],
        prompt: str,
        confirm_label: str = "✅ Confirm",
        cancel_data: str = "a:n:orders",
    ) -> None:
        token = secrets.token_urlsafe(18)
        expires_at = datetime.now(UTC) + ADMIN_CONFIRMATION_TTL
        state_fingerprint, snapshot = self._admin_state_fingerprint(command, args, telegram_id)
        prompt = self._admin_preview_text(command, args, prompt, snapshot)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        store = getattr(self.service, "database", None)
        durable = all(
            callable(getattr(store, method, None))
            for method in ("create_admin_challenge", "consume_admin_challenge")
        )
        with self._admin_confirmation_lock:
            now = datetime.now(UTC)
            self._admin_confirmations = {
                key: value
                for key, value in self._admin_confirmations.items()
                if value["expires_at"] > now
            }
            if not durable:
                self._admin_confirmations[token] = {
                    "chat_id": int(chat_id),
                    "telegram_id": int(telegram_id),
                    "command": command,
                    "args": list(args),
                    "expires_at": expires_at,
                    "state_fingerprint": state_fingerprint,
                }
        if durable:
            try:
                store.create_admin_challenge(
                    token_hash,
                    int(telegram_id),
                    int(chat_id),
                    command,
                    json.dumps(list(args), separators=(",", ":")),
                    state_fingerprint,
                    datetime.now(UTC).isoformat(),
                    expires_at.isoformat(),
                )
            except Exception as exc:
                print(
                    f"admin confirmation persistence error: {type(exc).__name__}", file=sys.stderr
                )
                self.send(
                    chat_id,
                    "Administrator confirmation is temporarily unavailable. Try again.",
                    self._admin_keyboard(telegram_id),
                )
                return
        self.send(
            chat_id,
            prompt
            + f"\n\nThis confirmation expires in {int(ADMIN_CONFIRMATION_TTL.total_seconds() // 60)} minutes.",
            self._inline_keyboard([[(confirm_label, f"a:k:{token}"), ("Cancel", f"a:d:{token}")]]),
        )

    def _consume_admin_confirmation(
        self, chat_id: int, telegram_id: int, token: str
    ) -> dict[str, Any] | None:
        store = getattr(self.service, "database", None)
        if all(
            callable(getattr(store, method, None))
            for method in ("consume_admin_challenge", "create_admin_challenge")
        ):
            # The action is stored with the token, so first inspect the pending
            # record through the store's actor-bound consume operation. The
            # fallback below handles legacy in-memory tokens only.
            try:
                with store.connect() as connection:
                    row = connection.execute(
                        "SELECT command, args_json FROM admin_action_challenges WHERE token_hash = ?",
                        (hashlib.sha256(token.encode()).hexdigest(),),
                    ).fetchone()
                if row is None:
                    return None
                command = str(row["command"] if isinstance(row, dict) else row[0])
                raw_args = row["args_json"] if isinstance(row, dict) else row[1]
                args = json.loads(raw_args or "[]")
                if not isinstance(args, list):
                    return None
                current_fingerprint, current_snapshot = self._admin_state_fingerprint(
                    command, [str(value) for value in args], telegram_id
                )
                if current_snapshot.get("state") != "present":
                    return None
                return store.consume_admin_challenge(
                    hashlib.sha256(token.encode()).hexdigest(),
                    int(telegram_id),
                    int(chat_id),
                    current_fingerprint,
                    datetime.now(UTC).isoformat(),
                )
            except Exception as exc:
                print(f"admin confirmation consume error: {type(exc).__name__}", file=sys.stderr)
                return None
        with self._admin_confirmation_lock:
            challenge = self._admin_confirmations.get(token)
            if challenge is None:
                return None
            if (
                challenge["chat_id"] != int(chat_id)
                or challenge["telegram_id"] != int(telegram_id)
                or challenge["expires_at"] <= datetime.now(UTC)
            ):
                return None
            del self._admin_confirmations[token]
            current_fingerprint, current_snapshot = self._admin_state_fingerprint(
                challenge["command"], challenge["args"], telegram_id
            )
            if current_snapshot.get("state") != "present":
                return None
            if current_fingerprint != challenge.get("state_fingerprint"):
                return None
            return challenge

    @staticmethod
    def _order_summary(order: dict[str, Any]) -> str:
        return (
            f"{order['id']}\n"
            f"{order.get('plan_name') or order['plan_code']} · "
            f"{int(order['amount_minor']):,} {order['currency']}\n"
            f"Order: {order['status']} · Payment: {order.get('payment_status') or 'not submitted'} · "
            f"Receipt: {order.get('receipt_status') or 'not submitted'} · Stage: {order.get('stage', 'unknown')}"
        )

    @staticmethod
    def _order_detail_text(order: dict[str, Any]) -> str:
        if str(order.get("order_type") or "vpn") == "wallet_topup":
            return "\n".join(
                [
                    "AuriX Wallet Top-up",
                    "",
                    f"ID: {order['id']}",
                    f"Customer: {order['telegram_id']}",
                    f"Amount: {int(order['amount_minor']):,} {order['currency']}",
                    f"Method: {order.get('selected_payment_provider') or 'not selected'}",
                    f"Status: {order['status']}",
                    f"Payment: {order.get('payment_status') or 'not submitted'}",
                    f"Receipt review: {order.get('receipt_status') or 'not submitted'}",
                    f"Created: {order['created_at']}",
                ]
            )
        lines = [
            "AuriX Order",
            "",
            f"ID: {order['id']}",
            f"Customer: {order['telegram_id']}",
            f"Plan: {order.get('plan_name') or order['plan_code']}",
            f"Amount: {int(order['amount_minor']):,} {order['currency']}",
            f"Order: {order['status']}",
            f"Refund: {order.get('refund_status') or 'none'}",
            f"Customer stage: {order.get('stage', 'unknown')}",
            f"Payment: {order.get('payment_status') or 'not submitted'}",
            f"Receipt review: {order.get('receipt_status') or 'not submitted'}",
            f"Subscription: {order.get('subscription_status') or 'not created'}",
            f"Provisioning: {order.get('provisioning_status') or 'not queued'}",
            f"Revocation: {order.get('revocation_status') or 'not queued'}",
            f"Created: {order['created_at']}",
        ]
        if order.get("expires_at"):
            lines.append(f"Expires: {order['expires_at']}")
        if order.get("evidence_id"):
            lines.append(f"Evidence ID: {order['evidence_id']}")
        return "\n".join(lines)

    def _order_actions(self, order: dict[str, Any], is_admin: bool) -> dict[str, Any]:
        order_id = str(order["id"])
        rows: list[list[tuple[str, str]]] = []
        if is_admin:
            if order.get("evidence_id"):
                rows.append([("🧾 Open Receipt", f"a:r:{order['evidence_id']}")])
                if order.get("receipt_status") == "pending":
                    rows.append([("🛑 Reject Receipt", f"a:q:{order['evidence_id']}")])
            if order.get("status") == "approved" and order.get("provisioning_status") == "failed":
                rows.append([("🔁 Retry Setup", f"a:h:{order_id}")])
            if order.get("revocation_status") in ("pending", "running"):
                rows.append([("⏳ Revocation in progress", f"a:o:{order_id}")])
            elif order.get("revocation_status") == "failed":
                rows.append([("🔁 Retry Revocation", f"a:g:{order_id}")])
            if order.get("telegram_id"):
                rows.append([("💰 View Ledger", f"a:l:{order['telegram_id']}")])
            if order.get("refund_status") != "refunded" and (
                order.get("status") == "approved" or order.get("payment_status") == "verified"
            ):
                rows.append([("💸 Refund", f"a:f:{order_id}")])
            if order.get("status") == "payment_submitted" and (
                order.get("receipt_status") == "verified"
                or order.get("wallet_reservation_status") == "reserved"
            ):
                rows.append([("✅ Approve", f"a:a:{order_id}")])
            if (
                order.get("status") in ("awaiting_payment", "payment_submitted")
                and order.get("refund_status") != "refunded"
            ):
                if (
                    order.get("payment_status") == "verified"
                    or order.get("receipt_status") == "verified"
                ):
                    pass
                else:
                    rows.append([("❌ Reject…", f"a:x:{order_id}")])
            rows.append(
                [
                    ("🔄 Refresh", f"a:o:{order_id}"),
                    ("📥 Orders", "a:n:orders"),
                ]
            )
        else:
            if (
                order.get("status") == "awaiting_payment"
                and not order.get("payment_status")
                and not order.get("receipt_status")
            ):
                rows.append(
                    [
                        ("📷 Send Receipt", f"o:r:{order_id}"),
                        ("💰 Pay Wallet", f"o:w:{order_id}"),
                    ]
                )
                rows.append([("🗑 Cancel Order", f"o:c:{order_id}")])
            elif order.get("receipt_status") == "rejected":
                rows.append([("📷 Send Replacement Receipt", f"o:r:{order_id}")])
            if order.get("stage") == "fulfilled":
                rows.append([("🔐 My VPN", "n:myvpn")])
            rows.append(
                [
                    ("🔄 Refresh", f"o:v:{order_id}"),
                    ("🧾 My Orders", "n:myorders"),
                ]
            )
        return self._inline_keyboard(rows)

    def _send_order_detail(
        self,
        chat_id: int,
        telegram_id: int,
        order_id: str,
        admin_view: bool = False,
        heading: str | None = None,
        message_id: int | None = None,
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Order tracking is not configured.")
            return
        is_admin = bool(admin_view and self._is_admin(telegram_id))
        order = self.commerce.order_detail(order_id, telegram_id, is_admin=is_admin)
        if order is None:
            self.send(chat_id, "Order not found.")
            return
        text = ((heading + "\n\n") if heading else "") + self._order_detail_text(order)
        markup = self._order_actions(order, is_admin)
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)
