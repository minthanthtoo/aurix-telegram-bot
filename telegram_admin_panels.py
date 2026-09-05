"""Administrator panels, confirmations, and protected operation views."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from commerce import CommerceError
from telegram_formatting import format_user_datetime

UTC = timezone.utc
ADMIN_CONFIRMATION_TTL = timedelta(minutes=5)


class TelegramAdminMixin:
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

    @staticmethod
    def _capacity_text(snapshot: dict[str, Any]) -> str:
        servers = snapshot.get("servers") or []
        lines = [
            "📈 AuriX Server Capacity",
            "",
            f"Active paid keys: {snapshot.get('active_keys', 0)} · pending jobs: {snapshot.get('pending_jobs', 0)}",
            "Observed values come from Outline; limits and plan slots are owner policy.",
        ]
        advice = snapshot.get("scale_advice") or {}
        advice_icon = {
            "stable": "🟢",
            "prepare": "🟡",
            "urgent": "🔴",
            "blocked": "🔴",
            "unconfigured": "⚪️",
        }.get(str(advice.get("status")), "⚪️")
        utilization = advice.get("utilization_percent")
        utilization_text = "" if utilization is None else f" · {float(utilization):g}% allocated"
        lines.extend(
            [
                "",
                f"{advice_icon} Scale posture: {str(advice.get('status') or 'unknown').title()}{utilization_text}",
                str(advice.get("message") or "Capacity recommendation unavailable."),
                "Scaling mode: assisted · no automatic purchase or server deletion.",
            ]
        )
        if str(advice.get("status") or "") in {"prepare", "urgent"}:
            observed = int(advice.get("consecutive_observations") or 0)
            required = int(advice.get("required_observations") or 2)
            gate_text = "ready" if advice.get("observation_ready") else "observe again before queueing"
            lines.append(f"Scale evidence: {observed}/{required} consecutive observations · {gate_text}")
        for item in servers:
            max_keys = item.get("max_keys")
            remaining = item.get("remaining_key_slots")
            key_limit = (
                "not capped"
                if max_keys is None
                else f"{remaining} saleable left · max {max_keys}, reserve {item.get('reserved_keys') or 0}"
            )
            traffic = item.get("remote_transfer_bytes")
            traffic_text = (
                "-" if traffic is None else f"{int(traffic) / 1_000_000_000:.1f} GB / 30d"
            )
            orphan_count = int(item.get("remote_orphan_key_count") or 0)
            budget = item.get("monthly_traffic_bytes")
            commitment = int(item.get("committed_traffic_bytes") or 0)
            connectivity = item.get("connectivity") or {}
            connectivity_text = (
                f"Connectivity: {connectivity.get('provider_name', connectivity.get('provider_id', 'unknown'))} · "
                f"{connectivity.get('region_name', connectivity.get('region_id', 'unknown'))} · "
                f"{connectivity.get('transport_name', connectivity.get('protocol', 'unknown'))}"
                if connectivity
                else "Connectivity: registry pending"
            )
            lines.extend(
                [
                    "",
                    f"{'🟢' if item.get('health_status') == 'healthy' else '🔴'} {item.get('label') or item.get('server_id')} · {str(item.get('lifecycle_state') or 'active').title()}",
                    (
                        "Health evidence: "
                        f"{str(item.get('health_status') or 'unknown')} · "
                        f"last probe {float(item.get('health_last_latency_ms')):g} ms"
                        if item.get("health_last_latency_ms") is not None
                        else "Health evidence: no latency recorded"
                    ),
                    connectivity_text,
                    (
                        "Admission: ✅ eligible"
                        if item.get("admission_status") == "eligible"
                        else "Admission: ⛔ blocked · "
                        + ", ".join(str(value).replace("_", " ") for value in item.get("admission_blockers") or [])
                    ),
                    f"Remote keys: {item.get('remote_key_count') or 0} · {key_limit}",
                    (
                        f"⚠️ Untracked remote keys: {orphan_count} · audit before strict allocation"
                        if orphan_count
                        else "Untracked remote keys: 0"
                    ),
                    f"Traffic observed: {traffic_text}",
                    "Traffic allocation: "
                    + (
                        f"{commitment / 1_000_000_000:g}/{int(budget) / 1_000_000_000:g} GB committed"
                        if budget
                        else "monitor only"
                    ),
                    (
                        "Drain: ✅ empty and ready to retire"
                        if item.get("drain_ready_to_retire")
                        else "Drain: ⏳ "
                        + ", ".join(
                            label
                            for label, value in (
                                ("free keys", item.get("active_free_key_count")),
                                ("paid keys", item.get("active_paid_key_count")),
                                ("open orders", item.get("open_order_count")),
                                ("pending setup", item.get("pending_provisioning_count")),
                                ("remote keys", item.get("remote_key_count")),
                                ("unreviewed remote", item.get("remote_orphan_key_count")),
                            )
                            if value is None or int(value or 0) > 0
                        )
                    ),
                ]
            )
            allocation_status = str(item.get("allocation_policy_status") or "unknown")
            allocation_total = int(item.get("allocation_total_slots") or 0)
            allocation_gap = item.get("allocation_remaining_slots")
            if allocation_status == "overallocated":
                lines.append(
                    f"⚠️ Policy: over-allocated {allocation_total} slots; "
                    f"{abs(int(allocation_gap or 0))} above saleable headroom"
                )
            elif allocation_status == "audit_required":
                lines.append(
                    f"Policy audit: ⚠️ {orphan_count} untracked key(s) must be classified "
                    "before strict validation"
                )
            elif allocation_status == "unconfigured":
                lines.append("Policy: capacity is not declared; node cannot be sized safely")
            else:
                lines.append(
                    f"Policy: ✅ {allocation_total} allocated slot(s) · "
                    f"{max(0, int(allocation_gap or 0))} unallocated"
                )
            allocations = item.get("allocations") or []
            if allocations:
                lines.append(
                    "Plan slots: "
                    + " · ".join(
                        f"{allocation['name']} {allocation['remaining_slots']}/{allocation['slot_limit']}"
                        for allocation in allocations
                    )
                )
            else:
                lines.append("Plan slots: not allocated (server headroom only)")
            tier_allocations = item.get("tier_allocations") or []
            if tier_allocations:
                labels = {"FREE300MB": "Daily", "FREE3GB": "Monthly", "PROMO": "Promo"}
                lines.append(
                    "Free/promo slots: "
                    + " · ".join(
                        f"{labels.get(allocation['tier_code'], allocation['tier_code'])} "
                        f"{allocation['remaining_slots']}/{allocation['slot_limit']}"
                        for allocation in tier_allocations
                    )
                )
        if not servers:
            lines.extend(["", "No environment-configured Outline servers were registered."])
        return "\n".join(lines)[:4096]

    def _show_capacity(self, chat_id: int, telegram_id: int, message_id: int | None = None) -> None:
        snapshot = self._admin_call(telegram_id, "capacity_snapshot")
        rows = [
            [(f"⚙️ {str(item.get('label') or item['server_id'])[:24]}", f"a:S:{item['server_id']}")]
            for item in snapshot.get("servers", [])
        ]
        advice_status = str((snapshot.get("scale_advice") or {}).get("status") or "")
        queue_enabled = os.environ.get("AURIX_INFRASTRUCTURE_QUEUE_ENABLED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if queue_enabled and advice_status in {"prepare", "urgent"}:
            if (snapshot.get("scale_advice") or {}).get("observation_ready"):
                rows.append([("🛠 Prepare next node", "a:n:prepare")])
            else:
                rows.append([("⏱ Collect another observation", "a:n:capacity")])
        rows.append([("🔄 Reconcile", "a:n:capacity"), ("🏠 Admin Home", "a:n:admin")])
        text = self._capacity_text(snapshot)
        markup = self._inline_keyboard(rows)
        if message_id is not None:
            try:
                self.edit_message(chat_id, int(message_id), text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _show_server_allocation(
        self,
        chat_id: int,
        telegram_id: int,
        server_id: str,
        message_id: int | None = None,
    ) -> None:
        snapshot = self._admin_call(telegram_id, "capacity_snapshot")
        server = next(
            (item for item in snapshot.get("servers", []) if str(item["server_id"]) == server_id),
            None,
        )
        if server is None:
            self.send(chat_id, "That Outline server is no longer configured.")
            return
        max_keys = server.get("max_keys")
        traffic = server.get("monthly_traffic_bytes")
        lines = [
            f"⚙️ {server.get('label') or server_id}",
            f"Lifecycle: {str(server.get('lifecycle_state') or 'active').title()} · {'admission enabled' if server.get('enabled') else 'admission disabled'}",
            (
                "Connectivity: "
                f"{(server.get('connectivity') or {}).get('provider_name', 'unknown')} · "
                f"{(server.get('connectivity') or {}).get('region_name', 'unknown')} · "
                f"{(server.get('connectivity') or {}).get('transport_name', 'unknown')}"
            ),
            f"Health: {server.get('health_status')} · remote keys: {server.get('remote_key_count') or 0}",
            (
                "Probe: "
                f"{float(server.get('health_last_latency_ms')):g} ms · "
                f"success streak {int(server.get('health_success_streak') or 0)} · "
                f"failure streak {int(server.get('health_failure_streak') or 0)}"
                if server.get("health_last_latency_ms") is not None
                else "Probe: no latency recorded"
            ),
            (
                f"Inventory audit: ⚠️ {int(server.get('remote_orphan_key_count') or 0)} untracked"
                if int(server.get("remote_orphan_key_count") or 0)
                else "Inventory audit: ✅ all observed keys are managed"
            ),
            f"Maximum keys: {max_keys or 'not capped'} · protected headroom: {server.get('reserved_keys') or 0}",
            "Monthly traffic budget: "
            + (f"{int(traffic) / 1_000_000_000:g} GB" if traffic else "monitor only"),
            "",
            "Choose declared server capacity, then allocate paid and free/promo tiers. Changes apply to new issuance; existing keys are never silently moved.",
            "Drain mode blocks new issuance but keeps existing keys alive. Retirement is allowed only after every local and remotely observed key/order/setup is gone.",
        ]
        rows: list[list[tuple[str, str]]] = [
            [("🔎 Remote inventory", f"a:I:{server_id}:present:0")],
            [("🔁 Migrate active keys", f"a:G:{server_id}:0")],
            [
                ("Keys 25", f"a:C:{server_id}|keys|25"),
                ("50", f"a:C:{server_id}|keys|50"),
                ("100", f"a:C:{server_id}|keys|100"),
            ],
            [
                ("Reserve 1", f"a:C:{server_id}|reserve|1"),
                ("2", f"a:C:{server_id}|reserve|2"),
                ("5", f"a:C:{server_id}|reserve|5"),
            ],
            [
                ("Traffic 500GB", f"a:C:{server_id}|traffic|500"),
                ("1TB", f"a:C:{server_id}|traffic|1000"),
                ("2TB", f"a:C:{server_id}|traffic|2000"),
            ],
        ]
        lifecycle = str(server.get("lifecycle_state") or "active")
        if self._is_owner(telegram_id):
            if lifecycle == "active":
                rows.append([("🚧 Start drain", f"a:L:{server_id}|draining")])
            elif lifecycle == "draining":
                rows.append(
                    [
                        ("▶ Resume admission", f"a:L:{server_id}|active"),
                        ("⏹ Retire when empty", f"a:L:{server_id}|retired"),
                    ]
                )
            else:
                rows.append([("▶ Re-open endpoint", f"a:L:{server_id}|active")])
        allocations = {item["plan_code"]: item for item in server.get("allocations", [])}
        for plan in self.commerce.plans():
            current = int((allocations.get(plan.code) or {}).get("slot_limit") or 0)
            lines.append(f"{plan.name}: {current} allocated")
            rows.append(
                [
                    (f"{plan.name} 0", f"a:C:{server_id}|{plan.code}|0"),
                    ("10", f"a:C:{server_id}|{plan.code}|10"),
                    ("25", f"a:C:{server_id}|{plan.code}|25"),
                    ("50", f"a:C:{server_id}|{plan.code}|50"),
                ]
            )
        tier_labels = {"FREE300MB": "Daily 300 MB", "FREE3GB": "Monthly 3 GB", "PROMO": "Promo"}
        tier_allocations = {item["tier_code"]: item for item in server.get("tier_allocations", [])}
        for tier_code, label in tier_labels.items():
            current = int((tier_allocations.get(tier_code) or {}).get("slot_limit") or 0)
            lines.append(f"{label}: {current} allocated")
            rows.append(
                [
                    (f"{label} 0", f"a:C:{server_id}|{tier_code}|0"),
                    ("10", f"a:C:{server_id}|{tier_code}|10"),
                    ("25", f"a:C:{server_id}|{tier_code}|25"),
                    ("50", f"a:C:{server_id}|{tier_code}|50"),
                ]
            )
        rows.append([("◀ All Servers", "a:n:capacity"), ("🔄 Refresh", f"a:S:{server_id}")])
        markup = self._inline_keyboard(rows)
        text = "\n".join(lines)[:4096]
        if message_id is not None:
            try:
                self.edit_message(chat_id, int(message_id), text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    @staticmethod
    def _inventory_bytes(value: Any) -> str:
        try:
            amount = max(0, int(value or 0))
        except (TypeError, ValueError):
            return "-"
        units = ("B", "KB", "MB", "GB", "TB")
        number = float(amount)
        unit = units[0]
        for unit in units:
            if number < 1000 or unit == units[-1]:
                break
            number /= 1000
        return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"

    def _show_remote_inventory(
        self,
        chat_id: int,
        telegram_id: int,
        server_id: str,
        status: str = "present",
        page: int = 0,
        message_id: int | None = None,
    ) -> None:
        normalized_status = str(status or "present").lower()
        if normalized_status not in {"present", "missing", "all"}:
            normalized_status = "present"
        try:
            all_rows = list(
                self._admin_call(
                    telegram_id,
                    "remote_key_inventory",
                    server_id,
                    status="all",
                    limit=500,
                )
                or []
            )
        except (CommerceError, ValueError) as exc:
            self.send(chat_id, str(exc) or "Remote inventory is unavailable.")
            return
        rows = [
            row for row in all_rows
            if normalized_status == "all" or str(row.get("status") or "") == normalized_status
        ]
        page_size = 5
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        current_page = max(0, min(int(page or 0), pages - 1))
        current = rows[current_page * page_size : (current_page + 1) * page_size]
        present_count = sum(1 for row in all_rows if row.get("status") == "present")
        missing_count = sum(1 for row in all_rows if row.get("status") == "missing")
        unreviewed_count = sum(
            1
            for row in all_rows
            if row.get("status") == "present"
            and not row.get("managed")
            and str(row.get("review_state") or "unreviewed") != "accepted_external"
        )
        lines = [
            f"🔎 Remote inventory · {server_id}",
            "",
            f"Present {present_count} · Missing {missing_count} · showing {normalized_status}",
            "IDs and telemetry only; access URLs are deliberately never shown here.",
            f"Unreviewed external keys: {unreviewed_count}",
            f"Page {current_page + 1}/{pages}",
        ]
        for row in current:
            key_id = str(row.get("outline_key_id") or "-")
            name = str(row.get("remote_name") or "unnamed")[:48]
            state = "✅ managed" if row.get("managed") else "⚠️ untracked"
            review_state = str(row.get("review_state") or "unreviewed")
            if not row.get("managed") and review_state == "accepted_external":
                state = "⚪ untracked · reviewed external"
            if row.get("status") == "missing":
                state = "🗃 missing · " + ("was managed" if row.get("managed") else "was untracked")
            lines.extend(
                [
                    "",
                    f"• `{key_id}` · {name}",
                    f"  {state} · usage {self._inventory_bytes(row.get('last_usage_bytes'))}",
                    f"  last seen {format_user_datetime(row.get('last_seen_at'))}",
                ]
            )
        if not current:
            lines.extend(["", "No audit records match this filter."])
        rows_markup: list[list[tuple[str, str]]] = [
            [
                (f"✅ Present ({present_count})", f"a:I:{server_id}:present:0"),
                (f"🗃 Missing ({missing_count})", f"a:I:{server_id}:missing:0"),
            ],
            [("📋 All", f"a:I:{server_id}:all:0")],
        ]
        navigation: list[tuple[str, str]] = []
        if current_page > 0:
            navigation.append(("⏮ First", f"a:I:{server_id}:{normalized_status}:0"))
            navigation.append(("◀ Previous", f"a:I:{server_id}:{normalized_status}:{current_page - 1}"))
        navigation.append((f"{current_page + 1}/{pages}", f"a:I:{server_id}:{normalized_status}:{current_page}"))
        if current_page + 1 < pages:
            navigation.append(("Next ▶", f"a:I:{server_id}:{normalized_status}:{current_page + 1}"))
            navigation.append(("Last ⏭", f"a:I:{server_id}:{normalized_status}:{pages - 1}"))
        rows_markup.append(navigation)
        if self._is_owner(telegram_id):
            for row in current:
                if row.get("status") != "present" or row.get("managed"):
                    continue
                key_id = str(row.get("outline_key_id") or "").strip()
                if not key_id:
                    continue
                review_state = str(row.get("review_state") or "unreviewed")
                next_state = (
                    "unreviewed" if review_state == "accepted_external" else "accepted_external"
                )
                action_data = f"a:R:{server_id}|{key_id}|{next_state}|{current_page}"
                if len(action_data.encode("utf-8")) <= 64:
                    label = "↩ Reopen" if review_state == "accepted_external" else "✅ Mark reviewed"
                    rows_markup.append([(f"{label} · {key_id[:12]}", action_data)])
        rows_markup.append(
            [
                ("🔄 Refresh", f"a:I:{server_id}:{normalized_status}:{current_page}"),
                ("⬅ Server policy", f"a:S:{server_id}"),
            ]
        )
        markup = self._inline_keyboard(rows_markup)
        text = "\n".join(lines)[:4096]
        if message_id is not None:
            try:
                self.edit_message(chat_id, int(message_id), text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _show_migration_candidates(
        self,
        chat_id: int,
        telegram_id: int,
        source_server_id: str,
        page: int = 0,
        message_id: int | None = None,
    ) -> None:
        """Show owner-only active credentials eligible for endpoint migration."""
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        try:
            candidates = list(
                self._admin_call(telegram_id, "migratable_credentials", source_server_id) or []
            )
        except Exception as exc:
            self.send(chat_id, str(exc) or "Migration inventory is unavailable.")
            return
        page_size = 5
        pages = max(1, (len(candidates) + page_size - 1) // page_size)
        current_page = max(0, min(int(page or 0), pages - 1))
        current = candidates[current_page * page_size : (current_page + 1) * page_size]
        lines = [
            f"🔁 Endpoint migration · {source_server_id}",
            "",
            "Choose an active managed credential. A healthy target is selected next.",
            f"Active credentials: {len(candidates)} · Page {current_page + 1}/{pages}",
            "Usage is rechecked before replacement creation; old access is removed only after cutover.",
        ]
        rows: list[list[tuple[str, str]]] = []
        for index, item in enumerate(current):
            absolute = current_page * page_size + index
            external_id = str(item.get("external_id") or "-")
            kind = str(item.get("profile_kind") or "-").upper()
            customer = str(item.get("telegram_id") or "-")[-6:]
            lines.extend(["", f"#{absolute + 1} · {kind} · tg:{customer}", f"Key: {external_id[:24]}"])
            rows.append([(f"#{absolute + 1} · Choose target", f"a:G:{source_server_id}:c:{absolute}")])
        if not current:
            lines.extend(["", "No active managed credentials are available on this endpoint."])
        navigation: list[tuple[str, str]] = []
        if current_page > 0:
            navigation.extend(
                [
                    ("⏮ First", f"a:G:{source_server_id}:p:0"),
                    ("◀ Previous", f"a:G:{source_server_id}:p:{current_page - 1}"),
                ]
            )
        navigation.append((f"{current_page + 1}/{pages}", f"a:G:{source_server_id}:p:{current_page}"))
        if current_page + 1 < pages:
            navigation.extend(
                [
                    ("Next ▶", f"a:G:{source_server_id}:p:{current_page + 1}"),
                    ("Last ⏭", f"a:G:{source_server_id}:p:{pages - 1}"),
                ]
            )
        rows.append(navigation)
        rows.append([("🔄 Refresh", f"a:G:{source_server_id}:p:{current_page}"), ("⬅ Server policy", f"a:S:{source_server_id}")])
        markup = self._inline_keyboard(rows)
        text = "\n".join(lines)[:4096]
        if message_id is not None:
            try:
                self.edit_message(chat_id, int(message_id), text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _show_migration_targets(
        self,
        chat_id: int,
        telegram_id: int,
        source_server_id: str,
        candidate_index: int,
        page: int = 0,
        message_id: int | None = None,
    ) -> None:
        if not self._is_owner(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        try:
            candidates = list(
                self._admin_call(telegram_id, "migratable_credentials", source_server_id) or []
            )
            candidate = candidates[int(candidate_index)]
            endpoints = list(self._admin_call(telegram_id, "connectivity_snapshot") or [])
        except Exception as exc:
            self.send(chat_id, str(exc) or "Migration target list is unavailable.")
            return
        target_endpoints = [
            item
            for item in endpoints
            if str(item.get("outline_server_id")) != str(source_server_id)
            and str(item.get("status")) == "active"
            and bool(item.get("accepts_new_keys"))
        ]
        external_id = str(candidate.get("external_id") or "")
        lines = [
            "🔁 Choose migration target",
            "",
            f"Credential: {external_id[:32]} · {str(candidate.get('profile_kind') or '-').upper()}",
            f"Customer: tg:{str(candidate.get('telegram_id') or '-')[-6:]}",
            "Only active, healthy, admitting endpoints are shown.",
            "",
            "A confirmation screen will appear before any remote key change.",
        ]
        rows: list[list[tuple[str, str]]] = []
        for endpoint in target_endpoints:
            target = str(endpoint.get("outline_server_id") or "")
            label = str(endpoint.get("provider_name") or target)[:16]
            region = str(endpoint.get("region_name") or "")[:16]
            rows.append([(f"✅ {target} · {label} {region}", f"a:H:{source_server_id}|{candidate_index}|{target}|{page}")])
        if not target_endpoints:
            lines.extend(["", "No healthy target endpoint currently accepts new keys."])
        rows.append([("⬅ Credentials", f"a:G:{source_server_id}:p:{page}"), ("🏠 Owner Home", "a:n:owner")])
        markup = self._inline_keyboard(rows)
        text = "\n".join(lines)[:4096]
        if message_id is not None:
            try:
                self.edit_message(chat_id, int(message_id), text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _show_migration_detail(
        self,
        chat_id: int,
        telegram_id: int,
        job: dict[str, Any],
        message_id: int | None = None,
    ) -> None:
        """Render one migration operation without exposing credential secrets."""
        job_id = str(job.get("job_id") or "-")
        status = str(job.get("job_status") or "unknown").replace("_", " ").title()
        lines = [
            "🔁 Endpoint migration",
            "",
            f"Job: {job_id[:16]}",
            f"Status: {status} · attempts: {int(job.get('attempts') or 0)}",
            f"Customer: tg:{str(job.get('telegram_id') or '-')[-6:]} · {str(job.get('profile_kind') or '-').upper()}",
            f"Endpoint: {str(job.get('source_server_id') or '-')[:24]} → {str(job.get('target_server_id') or '-')[:24]}",
            f"Credential: {str(job.get('source_external_id') or '-')[:32]}",
            f"Replacement: {str(job.get('target_external_id') or '-')[:32]}",
            f"Quota at queue: {int(job.get('quota_bytes') or 0):,} bytes",
        ]
        if job.get("source_used_bytes") is not None:
            lines.append(f"Source usage at cutover: {int(job.get('source_used_bytes') or 0):,} bytes")
        if job.get("last_error"):
            lines.extend(["", f"Last error: {str(job.get('last_error'))[:500]}"])
        if status.lower() in {"failed", "source delete pending"}:
            lines.extend(["", "The worker retries safely; source access is retained until replacement cutover and verified deletion."])
        markup = self._inline_keyboard(
            [[("🔄 Refresh", "a:n:migrations"), ("⬅ Admin Home", "a:n:admin")]]
        )
        text = "\n".join(lines)[:4096]
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

    def _show_managed_repair_detail(
        self,
        chat_id: int,
        telegram_id: int,
        repair: dict[str, Any],
        message_id: int | None = None,
    ) -> None:
        """Render a missing-key repair with explicit owner-only decisions."""
        repair_id = str(repair.get("id") or repair.get("job_id") or "-")
        status = str(repair.get("status") or "unknown").replace("_", " ").title()
        kind = str(repair.get("kind") or "-").upper()
        quota = int(repair.get("quota_bytes") or 0)
        used = repair.get("used_bytes")
        usage = "unknown — fresh Outline metrics required" if used is None else f"{int(used):,} bytes"
        error = str(repair.get("last_error") or "")
        lines = [
            "🧩 Managed-key repair",
            "",
            f"Job: {repair_id[:24]}",
            f"Status: {status} · {kind}",
            f"Customer: tg:{str(repair.get('telegram_id') or '-')[-6:]}",
            f"Server: {str(repair.get('server_id') or '-')[:32]}",
            f"Old key: {str(repair.get('source_external_id') or '-')[:32]}",
            f"Planned name: {str(repair.get('key_name') or '-')[:48]}",
            f"Observed usage: {usage} / quota {quota:,} bytes",
            f"Expires: {str(repair.get('expires_at') or '-')[:32]}",
            f"Attempts: {int(repair.get('attempts') or 0)}",
        ]
        if error:
            lines.extend(["", f"Reason: {error[:500]}"])
        lines.extend(
            [
                "",
                "Automatic repair never guesses unknown usage or resets quota. "
                "A successful replacement keeps the measured remaining allowance.",
            ]
        )
        rows: list[list[tuple[str, str]]] = []
        if self._is_owner(telegram_id) and status.lower() in {"manual", "failed"} and error != "quota_already_exhausted":
            rows.append([("✅ Approve · preserve usage", f"a:j:{repair_id}:safe")])
            if error == "usage_observation_required":
                rows.append([("⚠️ Approve full quota (explicit)", f"a:j:{repair_id}:full")])
        rows.append([("🔄 Refresh Repairs", "a:n:repairs"), ("⬅ Admin Home", "a:n:admin")])
        text = "\n".join(lines)[:4096]
        markup = self._inline_keyboard(rows)
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

    def _show_probes(
        self, chat_id: int, telegram_id: int, message_id: int | None = None
    ) -> None:
        """Show authenticated server-agent probe evidence and route scores."""
        if not self._is_admin(telegram_id):
            self._send_customer_fallback(chat_id, telegram_id)
            return
        summary = self._admin_probe_call(telegram_id, "summary")
        routes = summary.get("routes") or []
        lines = [
            "🛰 AuriX Fleet Probes",
            "",
            f"Servers: {summary.get('server_count', 0)} · "
            f"healthy {summary.get('healthy', 0)} · degraded {summary.get('degraded', 0)} · "
            f"unreachable {summary.get('unreachable', 0)} · unknown {summary.get('unknown', 0)}",
            "Measurements are performed by AuriX server agents; they are not phone-to-server latency.",
        ]
        for route in routes[:12]:
            status = str(route.get("status") or "unknown")
            icon = {"healthy": "🟢", "degraded": "🟡", "unreachable": "🔴"}.get(status, "⚪️")
            score = route.get("score")
            score_text = "-" if score is None else f"{float(score):.1f}/100"
            observed = str(route.get("last_observed_at") or "never")[:19]
            lines.append(
                f"\n{icon} {str(route.get('label') or route.get('server_id'))[:32]} · "
                f"{route.get('region') or 'Unknown'}\n"
                f"Status: {status} · score {score_text} · samples {route.get('sample_count', 0)}\n"
                f"Last evidence: {observed}"
            )
        if not routes:
            lines.extend(["", "No routes are configured for server-agent probing."])
        markup = self._inline_keyboard(
            [[("🔄 Enqueue Due Probes", "a:p:enqueue")], [("🏠 Admin Home", "a:n:admin")]]
        )
        text = "\n".join(lines)[:4096]
        if isinstance(message_id, int):
            try:
                self.edit_message(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        self.send(chat_id, text, markup)

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

        if command == "/receiptmode":
            try:
                policy = self._admin_call(telegram_id, "receipt_policy")
                snapshot.update(
                    {
                        "state": "present",
                        "current_mode": policy.get("mode"),
                        "version": policy.get("version"),
                        "requested_mode": args[0] if args else None,
                    }
                )
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
        if command == "/approverepair":
            try:
                repairs = self._admin_call(
                    telegram_id, "managed_key_repair_jobs", status="all", limit=500
                )
                repair = next(
                    (item for item in repairs if str(item.get("id")) == target_id), None
                )
                if repair is None:
                    snapshot.update({"state": "missing"})
                else:
                    snapshot.update({"state": "present", "repair": repair})
            except Exception as exc:
                snapshot.update({"state": "unavailable", "error_type": type(exc).__name__})
            return snapshot
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
        if self.commerce is None or not target_id:
            snapshot["state"] = "missing"
            return snapshot
        try:
            if command == "/migratekey":
                if len(args) != 3:
                    snapshot["state"] = "missing"
                else:
                    source, external_id, target = (str(value).strip() for value in args)
                    candidates = self._admin_call(telegram_id, "migratable_credentials", source)
                    candidate = next(
                        (item for item in candidates if str(item.get("external_id")) == external_id),
                        None,
                    )
                    endpoints = self._admin_call(telegram_id, "connectivity_snapshot")
                    endpoint = next(
                        (item for item in endpoints if str(item.get("outline_server_id")) == target),
                        None,
                    )
                    if candidate is None or endpoint is None:
                        snapshot["state"] = "missing"
                    else:
                        snapshot.update(
                            {
                                "state": "present",
                                "source_server_id": source,
                                "target_server_id": target,
                                "external_id": external_id,
                                "profile_kind": candidate.get("profile_kind"),
                                "telegram_id": candidate.get("telegram_id"),
                                "target_status": endpoint.get("status"),
                                "target_accepts_new_keys": endpoint.get("accepts_new_keys"),
                            }
                        )
            elif command == "/serverstate":
                if len(args) != 2 or str(args[1]).lower() not in {"active", "draining", "retired"}:
                    snapshot["state"] = "missing"
                else:
                    readiness = self._admin_owner_call(
                        telegram_id,
                        "server_drain_readiness",
                        target_id,
                    )
                    snapshot.update(
                        {
                            "state": "present",
                            "server_id": target_id,
                            "requested_state": str(args[1]).lower(),
                            "lifecycle_state": readiness.get("lifecycle_state"),
                            "ready_to_retire": readiness.get("ready_to_retire"),
                            "blockers": readiness.get("blockers") or [],
                        }
                    )
            elif command == "/retryjob":
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
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
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
        if command == "/receiptmode":
            return "\n".join(
                [
                    f"Current receipt mode: {snapshot.get('current_mode') or '-'}",
                    f"New receipt mode: {snapshot.get('requested_mode') or '-'}",
                    "AI-Assisted mode extracts fields only; staff still verifies the receiving account.",
                    "Automatic approval remains locked without an authoritative payment verifier.",
                ]
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
        if command == "/serverstate":
            requested = str(snapshot.get("requested_state") or args[1] if len(args) > 1 else "-").lower()
            lines = [
                f"Endpoint: {snapshot.get('server_id') or (args[0] if args else '-')}",
                f"Current lifecycle: {str(snapshot.get('lifecycle_state') or '-').title()}",
                f"Requested lifecycle: {requested.title()}",
            ]
            blockers = [str(value).replace("_", " ") for value in snapshot.get("blockers") or []]
            if requested == "retired" and blockers:
                lines.append("Retirement is currently blocked by: " + ", ".join(blockers))
            else:
                lines.append("Result: change admission state only; no provider VM action is performed.")
            return "\n".join(lines)
        if command == "/migratekey":
            return "\n".join(
                [
                    f"Credential: {snapshot.get('external_id') or (args[1] if len(args) > 1 else '-')} · {snapshot.get('profile_kind') or '-'}",
                    f"Customer: {snapshot.get('telegram_id') or '-'}",
                    f"Source endpoint: {snapshot.get('source_server_id') or (args[0] if args else '-')}",
                    f"Target endpoint: {snapshot.get('target_server_id') or (args[2] if len(args) > 2 else '-')}",
                    f"Target state: {snapshot.get('target_status') or '-'} · admission {'enabled' if snapshot.get('target_accepts_new_keys') else 'blocked'}",
                    "Result: create a replacement with fresh source-usage accounting, cut over locally, notify the customer, then delete the old remote key.",
                ]
            )
        if command == "/approverepair":
            repair = snapshot.get("repair") or {}
            quota = int(repair.get("quota_bytes") or 0)
            used = repair.get("used_bytes")
            usage = "unknown" if used is None else f"{int(used):,} bytes"
            full = len(args) == 2 and args[1].lower() == "full"
            return "\n".join(
                [
                    f"Repair: {repair.get('id') or (args[0] if args else '-')} · {str(repair.get('status') or '-').title()}",
                    f"Customer: tg:{str(repair.get('telegram_id') or '-')[-6:]} · server: {repair.get('server_id') or '-'}",
                    f"Old key: {repair.get('source_external_id') or '-'}",
                    f"Observed usage: {usage} / quota {quota:,} bytes",
                    (
                        "Result: restore a replacement with the full original quota because fresh usage is unavailable; this is an explicit owner override."
                        if full
                        else "Result: restore a replacement using fresh usage and preserve the remaining quota; no quota reset."
                    ),
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
                try:
                    verified_amount = f"{int(str(args[2]).replace(',', '')):,}"
                except (TypeError, ValueError):
                    verified_amount = str(args[2])
                lines.extend(
                    [
                        f"Transaction to verify: {args[1]}",
                        f"Amount to verify: {verified_amount} {snapshot.get('currency') or ''}".strip(),
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
            f"Created: {format_user_datetime(order['created_at'])}",
        ]
        if order.get("expires_at"):
            lines.append(f"Expires: {format_user_datetime(order['expires_at'])}")
        if order.get("payment_method"):
            lines.append(f"Selected method: {str(order['payment_method']).upper()}")
        if order.get("evidence_id"):
            lines.append(f"Evidence ID: {order['evidence_id']}")
        return "\n".join(lines)

    def _payment_method_keyboard(
        self,
        order_id: str,
        *,
        selected: str | None = None,
        qr_view: bool = False,
        allow_wallet: bool = True,
    ) -> dict[str, Any]:
        buttons = []
        for method in self.PAYMENT_METHOD_ORDER:
            item = self.PAYMENT_METHODS[method]
            label = str(item["button"])
            if method == selected:
                label = "✓ " + label
            buttons.append((label, f"m:s:{method}:{order_id}"))
        rows = [buttons[:2], buttons[2:4], buttons[4:]]
        if qr_view:
            rows.append([("✅ I’ve Paid · Send Receipt", f"o:u:{order_id}")])
        footer = [("🧾 Order", f"o:v:{order_id}")]
        if allow_wallet:
            footer.insert(0, ("💰 Pay Wallet", f"o:w:{order_id}"))
        rows.append(footer)
        return self._inline_keyboard(rows)

    def _send_payment_method_chooser(
        self, chat_id: int, telegram_id: int, order_id: str, heading: str | None = None
    ) -> None:
        if self.commerce is None:
            self.send(chat_id, "Payment is not configured.")
            return
        order = self.commerce.order_detail(order_id, telegram_id)
        if order is None:
            self.send(chat_id, "Order not found.")
            return
        if order.get("status") != "awaiting_payment":
            self._send_order_detail(chat_id, telegram_id, order_id)
            return
        method = str(order.get("payment_method") or "").lower()
        if method not in self.PAYMENT_METHODS:
            method = self.PAYMENT_METHOD_ORDER[0]
        self._show_payment_qr(
            {"message": {}},
            chat_id,
            telegram_id,
            order_id,
            method,
            heading=heading,
        )

    def _show_payment_qr(
        self,
        query: dict[str, Any],
        chat_id: int,
        telegram_id: int,
        order_id: str,
        method: str,
        heading: str | None = None,
    ) -> None:
        order = self.commerce.choose_payment_method(telegram_id, order_id, method)
        item = self.PAYMENT_METHODS[method]
        path = self.PAYMENT_QR_DIR / str(item["asset"])
        if not path.is_file():
            raise RuntimeError("Payment QR asset is unavailable")
        method_number = self.PAYMENT_METHOD_ORDER.index(method) + 1
        prefix = f"{heading}\n\n" if heading else ""
        caption = (
            prefix + f"🏦 Payment QR {method_number}/5 · {item['label']}\n"
            f"Order #{str(order_id)[:8]}\n\n"
            f"Pay exactly {int(order['amount_minor']):,} {order['currency']}.\n"
            "1. Scan this QR in the selected wallet.\n"
            "2. Verify the recipient and amount before confirming.\n"
            "3. Tap “I’ve Paid” and send the completed receipt screenshot.\n\n"
            "Use the numbered buttons below to switch QR in this same message.\n"
            "Never send your PIN, password or OTP."
        )
        markup = self._payment_method_keyboard(
            order_id,
            selected=method,
            qr_view=True,
            allow_wallet=str(order.get("plan_code") or "") != "wallet_topup",
        )
        message = query.get("message") or {}
        message_id = message.get("message_id")
        if isinstance(message_id, int) and message.get("photo"):
            self.edit_local_photo(chat_id, message_id, path, caption, markup)
        else:
            self.send_local_photo(chat_id, path, caption, markup)

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
            if (
                order.get("plan_code") != "wallet_topup"
                and order.get("refund_status") != "refunded"
                and (order.get("status") == "approved" or order.get("payment_status") == "verified")
            ):
                rows.append([("💸 Refund", f"a:f:{order_id}")])
            if order.get("status") == "payment_submitted" and (
                order.get("receipt_status") == "verified"
                or order.get("wallet_reservation_status") == "reserved"
            ):
                label = (
                    "✅ Credit Wallet" if order.get("plan_code") == "wallet_topup" else "✅ Approve"
                )
                rows.append([(label, f"a:a:{order_id}")])
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
            allow_wallet = str(order.get("plan_code") or "") != "wallet_topup"
            if (
                order.get("status") == "awaiting_payment"
                and not order.get("payment_status")
                and not order.get("receipt_status")
            ):
                selected_method = str(order.get("payment_method") or "").lower()
                if selected_method in self.PAYMENT_METHODS:
                    selected_label = str(self.PAYMENT_METHODS[selected_method]["label"])
                    rows.append(
                        [(f"🖼 Open {selected_label} QR", f"m:s:{selected_method}:{order_id}")]
                    )
                    rows.append(
                        [("🔁 Change QR", f"o:p:{order_id}")]
                        + ([("💰 Pay Wallet", f"o:w:{order_id}")] if allow_wallet else [])
                    )
                else:
                    rows.append(
                        [("🏦 Choose Payment QR", f"o:p:{order_id}")]
                        + ([("💰 Pay Wallet", f"o:w:{order_id}")] if allow_wallet else [])
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
            text = "Order tracking is not configured."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        is_admin = bool(admin_view and self._is_admin(telegram_id))
        order = self.commerce.order_detail(order_id, telegram_id, is_admin=is_admin)
        if order is None:
            text = "Order not found."
            if message_id is not None:
                self.edit_message(chat_id, message_id, text)
            else:
                self.send(chat_id, text)
            return
        text = ((heading + "\n\n") if heading else "") + self._order_detail_text(order)
        markup = self._order_actions(order, is_admin)
        if message_id is not None:
            self.edit_message(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)
