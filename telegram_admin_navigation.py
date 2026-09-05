"""Admin navigation panels and dashboard views."""

from __future__ import annotations

import secrets
import sys
import time
from datetime import datetime
from typing import Any

from telegram_transport_support import UTC


class TelegramAdminNavigationMixin:
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
        if page > 1:
            navigation.append(("⏮ First", f"v2:{token}:first"))
        if page > 0:
            navigation.append(("◀ Previous", f"v2:{token}:prev"))
        navigation.append((f"{page + 1}/{max(1, pages)}", f"v2:{token}:refresh"))
        if page + 1 < pages:
            navigation.append(("Next ▶", f"v2:{token}:next"))
        if page + 2 < pages:
            navigation.append(("Last ⏭", f"v2:{token}:last"))
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
        elif view == "migrations":
            source = str(item.get("source_server_id") or "-")[:18]
            target = str(item.get("target_server_id") or "-")[:18]
            error = str(item.get("last_error") or "")[:180]
            text = (
                f"{str(item.get('job_status') or '-').replace('_', ' ').title()} · job:{short_id}\n"
                f"{str(item.get('profile_kind') or '-').upper()} · tg:{str(item.get('telegram_id') or '-')[-6:]} · attempts:{item.get('attempts') or 0}\n"
                f"{source} → {target}"
                + (f"\n{error}" if error else "")
            )
        elif view == "repairs":
            status = str(item.get("status") or "-").replace("_", " ").title()
            quota = int(item.get("quota_bytes") or 0)
            used = item.get("used_bytes")
            usage = "unknown" if used is None else f"{int(used):,}/{quota:,} B"
            error = str(item.get("last_error") or "")[:180]
            text = (
                f"{status} · {str(item.get('kind') or '-').upper()} · job:{short_id}\n"
                f"tg:{str(item.get('telegram_id') or '-')[-6:]} · server:{str(item.get('server_id') or '-')[:18]}\n"
                f"key:{str(item.get('source_external_id') or '-')[:18]} · usage:{usage}\n"
                f"expires:{str(item.get('expires_at') or '-')[:19]}"
                + (f"\n{error}" if error else "")
            )
        elif view == "failover":
            source = str(item.get("source_endpoint_id") or "-")[:16]
            target = str(item.get("target_endpoint_id") or "-")[:16]
            error = str(item.get("last_error") or "")[:180]
            text = (
                f"{str(item.get('state') or '-').replace('_', ' ').title()} · decision:{short_id}\n"
                f"ent:{str(item.get('entitlement_id') or '-')[-16:]} · attempts:{item.get('attempts') or 0}\n"
                f"{source} → {target} · trigger:{str(item.get('trigger') or '-')[:80]}"
                + (f"\n{error}" if error else "")
            )
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
        if view == "migrations":
            return list(
                self._admin_call(telegram_id, "endpoint_migration_jobs", limit=100)
                or []
            )
        if view == "repairs":
            return list(
                self._admin_call(telegram_id, "managed_key_repair_jobs", status="open", limit=100)
                or []
            )
        if view == "failover":
            return list(
                self._admin_call(telegram_id, "route_failover_decisions", limit=100) or []
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
            "migrations": "🔁 Endpoint Migrations",
            "repairs": "🧩 Managed Key Repairs",
            "failover": "🛡 Route Failover",
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
                "migrations": "No open endpoint migrations.",
                "repairs": "No managed-key repairs are waiting for action.",
                "failover": "No route failover decisions are pending.",
                "enforcement": "No free/trial termination events recorded.",
            }.get(view, "Nothing needs attention.")
            if message_id is not None:
                self.edit_message(chat_id, message_id, empty, self._admin_keyboard(telegram_id))
            else:
                self.send(chat_id, empty)
            return
        with self._panel_lock:
            self._panels[token]["all_items"] = items
        text, markup = self._render_panel(token)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
            with self._panel_lock:
                self._panels[token]["message_id"] = int(message_id)
            return
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
                [("📈 Capacity", "a:n:capacity"), ("🛰 Fleet Probes", "a:n:probes")],
                [("🔎 Consistency", "a:n:reconcile")],
                [
                    ("🔁 Failed Jobs", "a:n:failed"),
                    ("🧩 Key Repairs", "a:n:repairs"),
                    ("🔁 Migrations", "a:n:migrations"),
                    ("🛡 Failover", "a:n:failover"),
                    ("🚨 Enforcement", "a:n:enforcement"),
                ],
                [("🧪 Receipt System", "a:n:receiptsystem"), ("🎁 Promotions", "a:n:promo")],
                [("🔔 My Alerts", "a:n:notifications")],
                *([[("👑 Owner Controls", "a:n:owner")]] if self._is_owner(telegram_id) else []),
                [("🏠 Customer Menu", "n:start")],
            ]
        )

    def _send_admin_home(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        """Render the admin dashboard, editing the active message when possible."""
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        summary = ""
        if self.commerce is not None:
            try:
                report = self._admin_call(telegram_id, "consistency_report")
                summary = (
                    f"\n\nQueue: {report.get('pending_receipts', 0)} receipt(s) pending · "
                    f"{report.get('pending_receipt_uploads', 0)} upload(s) pending · "
                    f"{report.get('failed_receipt_uploads', 0)} upload(s) failed · "
                    f"{report.get('failed_jobs', 0)} failed job(s) · "
                    f"{report.get('managed_key_repairs_pending', 0)} key repair(s) pending · "
                    f"{report.get('managed_key_repairs_manual', 0)} key repair(s) manual · "
                    f"{report.get('stale_receipts', 0)} stale review(s) · "
                    f"{report.get('dead_notifications', 0)} dead notification(s)"
                )
            except Exception as exc:
                print(f"admin dashboard error: {type(exc).__name__}", file=sys.stderr)
        text = (
            "AuriX Admin\n\n"
            "Daily flow: Pending Orders → open receipt → verify the transaction "
            "against your receiving account → Approve.\n"
            "Use Failed Jobs to retry a reviewed Outline failure, open an order "
            "to inspect its wallet ledger, and run Consistency before taking "
            "payment decisions."
            + summary
        )
        markup = self._admin_keyboard(telegram_id)
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _send_owner_home(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        """Render the owner control center without creating duplicate messages."""
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        staff = self.staff_access.list_staff() if self.staff_access is not None else []
        admins = sum(1 for item in staff if item.get("role") == "admin")
        control_group = self.staff_access.control_group() if self.staff_access is not None else None
        snapshot = self._admin_call(telegram_id, "receipt_system_snapshot")
        mode = str((snapshot.get("policy") or {}).get("mode") or "manual")
        text = (
            "👑 AuriX Owner\n\n"
            f"Receipt workflow  {mode.title()}\n"
            f"Receipt storage   {'Ready' if snapshot.get('storage_configured') else 'Not configured'}\n"
            f"Administrators    {admins} active\n"
            f"Control group     {(control_group or {}).get('title') or 'Not connected'}\n"
            f"Review queue      {snapshot.get('pending_receipts', 0)} receipt(s)\n\n"
            "Full owner access is active. Use the controls below for operations, "
            "orders, receipts, promotions, enforcement and administrator management.\n\n"
            "Staff access is database-backed. Initial human administrators are imported "
            "only when you connect a group with no active admin roster; later role changes "
            "stay preview-only until owner review."
        )
        markup = self._owner_keyboard()
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _owner_keyboard(self) -> dict[str, Any]:
        return self._inline_keyboard(
            [
                [("📊 Admin Dashboard", "a:n:admin"), ("👥 Staff & Access", "a:n:staff")],
                [("📥 Pending Orders", "a:n:orders"), ("🧾 Receipt Review", "a:n:receipts")],
                [("🧪 Receipt System", "a:n:receiptsystem"), ("🎁 Promotions", "a:n:promo")],
                [("🔔 My Alerts", "a:n:notifications")],
                [("📈 Capacity", "a:n:capacity"), ("🛰 Fleet Probes", "a:n:probes")],
                [("🔎 Consistency", "a:n:reconcile")],
                [
                    ("🔁 Failed Jobs", "a:n:failed"),
                    ("🧩 Key Repairs", "a:n:repairs"),
                    ("🔁 Migrations", "a:n:migrations"),
                    ("🚨 Enforcement", "a:n:enforcement"),
                ],
                [("🏢 Control Group", "a:s:group"), ("🔄 Group Sync", "a:n:groupsync")],
                [("🏠 Customer Menu", "n:start")],
            ]
        )

    def _send_staff_panel(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        """Render owner staff management in one editable, role-gated panel."""
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        staff = self.staff_access.list_staff() if self.staff_access is not None else []
        lines = [
            "👥 Staff & Access",
            "",
            "Owner-only controls · changes are audited and take effect immediately.",
        ]
        rows: list[list[tuple[str, str]]] = []
        for item in staff:
            staff_id = int(item["telegram_id"])
            role = str(item["role"])
            name = item.get("effective_username") or item.get("effective_name") or str(staff_id)
            prefix = "👑" if role == "owner" else "🛠"
            lines.append(f"{prefix} {name} · {role} · tg:{staff_id}")
            if role == "admin":
                rows.append([(f"🛑 Remove {str(name)[:22]}", f"a:s:remove:{staff_id}")])
        if not staff:
            lines.append("No active staff accounts are configured.")
        lines.extend(
            [
                "",
                "To add someone, ask them to open this bot and use /whoami first.",
                "Group sync is preview-only; it never changes roles without owner review.",
            ]
        )
        rows.append([("➕ Add Administrator", "a:s:add")])
        rows.append([("🏢 Choose Control Group", "a:s:group"), ("🔄 Sync Preview", "a:n:groupsync")])
        rows.append([("🔄 Refresh", "a:n:staff"), ("⬅ Owner Home", "a:n:owner")])
        text = "\n".join(lines)[:4096]
        markup = self._inline_keyboard(rows)
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _send_staff_notifications(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        if self.staff_access is None:
            self.send(chat_id, "Staff notification controls are not configured.")
            return
        preferences = self.staff_access.notification_preferences(telegram_id)
        labels = {
            "order_created": "New orders",
            "receipt_submitted": "Receipts awaiting review",
            "rejected": "Receipt/order rejections",
            "key_repairs": "Missing-key repairs",
        }
        lines = [
            "🔔 My Operational Alerts",
            "",
            "These settings apply only to your Telegram account.",
            "These are staff-only order and receipt operations. They never control your personal VPN usage alerts.",
            "Customer confirmations and critical VPN enforcement notices are unaffected.",
            "",
        ]
        rows: list[list[tuple[str, str]]] = []
        for event, label in labels.items():
            enabled = bool(preferences.get(event, True))
            lines.append(f"{'✅' if enabled else '🔕'} {label}: {'On' if enabled else 'Off'}")
            rows.append(
                [
                    (
                        f"{'🔕 Turn off' if enabled else '🔔 Turn on'} · {label}",
                        f"a:u:{event}",
                    )
                ]
            )
        rows.append([("🔄 Refresh", "a:n:notifications"), ("⬅ Admin Home", "a:n:admin")])
        text = "\n".join(lines)
        markup = self._inline_keyboard(rows)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _receipt_system_keyboard(self) -> dict[str, Any]:
        return self._inline_keyboard(
            [
                [("Manual Only", "a:m:manual"), ("AI Triage", "a:m:assisted")],
                [("🧪 Test Actual Receipt", "a:t:start"), ("📋 Last Test", "a:t:last")],
                [("🔬 Technical Details", "a:t:details")],
                [("🧾 Review Queue", "a:n:receipts"), ("🔄 Refresh", "a:n:receiptsystem")],
                [("⬅ Admin Home", "a:n:admin")],
            ]
        )

    def _send_receipt_system(
        self, chat_id: int, telegram_id: int, *, message_id: int | None = None
    ) -> None:
        snapshot = self._admin_call(telegram_id, "receipt_system_snapshot")
        policy = snapshot.get("policy") or {}
        last = snapshot.get("last_diagnostic") or {}
        text = (
            "🧾 Receipt Verification\n\n"
            f"Current mode       {str(policy.get('mode') or 'manual').title()}\n"
            f"LLM extraction     {'Ready' if getattr(self.receipt_extractor, 'base_url', '') and getattr(self.receipt_extractor, 'model', '') else 'Not configured'}\n"
            f"Receipt storage    {'Ready' if snapshot.get('storage_configured') else 'Not configured'}\n"
            "Payment verifier   Not connected\n"
            "Automatic approval Locked\n"
            f"Pending review     {snapshot.get('pending_receipts', 0)}\n"
            f"Last safe test     {last.get('status') or 'not run'}\n\n"
            "AI Triage applies provider, amount, time, recipient and reference-label rules. "
            "It never credits or approves from a screenshot; staff must confirm the receiving account."
        )
        markup = self._receipt_system_keyboard()
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def _send_receipt_diagnostic_result(
        self, chat_id: int, telegram_id: int, diagnostic: dict[str, Any] | None
    ) -> None:
        if not diagnostic:
            self.send(
                chat_id,
                "No completed receipt test is available yet.",
                self._receipt_system_keyboard(),
            )
            return
        result = diagnostic.get("result") or {}
        llm = result.get("llm") or {}
        extraction = result.get("extraction") or {}
        passed = diagnostic.get("status") == "passed"
        lines = [
            f"🧪 Receipt Test · {'PASSED' if passed else 'FAILED'}",
            "",
            f"Run: {str(diagnostic.get('id') or '-')[:12]}",
            f"Summary: {result.get('summary') or '-'}",
            f"LLM host: {self._mask_technical_value(llm.get('endpoint_host'))}",
            f"Model: {llm.get('model') or '-'}",
            f"HTTP: {llm.get('http_status') or '-'} · {llm.get('duration_ms') or '-'} ms",
            f"Selected method: {result.get('selected_payment_method') or '-'}",
            f"Document: {extraction.get('document_type') or '-'} · {extraction.get('completion_status') or '-'}",
            f"Transaction: {extraction.get('transaction_id') or '-'}",
            f"Transaction label: {extraction.get('transaction_id_label') or '-'}",
            f"Amount: {extraction.get('amount_minor') or '-'} {extraction.get('currency') or ''}".strip(),
            f"Timestamp: {extraction.get('timestamp') or '-'}",
            f"Recipient: {extraction.get('recipient') or '-'}",
            f"Confidence: {extraction.get('confidence') if extraction else '-'}",
            "",
            str(result.get("simulated_decision") or "No financial action was taken."),
            "This diagnostic never creates an order, wallet credit, subscription or VPN key.",
        ]
        self.send(chat_id, "\n".join(lines)[:4096], self._receipt_system_keyboard())

    def _admin_call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a commerce operation through the admin authorization boundary."""
        return self.admin_operations.call(telegram_id, operation, *args, **kwargs)

    def _admin_owner_call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke an owner-only operation through the stronger auth boundary."""
        return self.admin_operations.call_owner(telegram_id, operation, *args, **kwargs)

    def _admin_service_call(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        return self.admin_operations.call_service(telegram_id, operation, *args, **kwargs)

    def _admin_probe_call(
        self, telegram_id: int, operation: str, *args: Any, **kwargs: Any
    ) -> Any:
        return self.admin_operations.call_probe(telegram_id, operation, *args, **kwargs)

    def _send_customer_fallback(self, chat_id: int, telegram_id: int) -> None:
        """Return a role-neutral response for unknown or unauthorized input."""
        self.send(
            chat_id,
            self.UNKNOWN_ACTION_TEXT,
            self._customer_keyboard(telegram_id),
        )
