"""Capacity, inventory, migration, repair, and probe views."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from commerce import CommerceError
from telegram_admin_capacity_view import render_capacity_text
from telegram_admin_capacity_detail_view import render_remote_inventory, render_server_allocation
from telegram_formatting import format_user_datetime
from telegram_transport_support import UTC


class TelegramAdminCapacityMixin:
    @staticmethod
    def _capacity_text(snapshot: dict[str, Any]) -> str:
        return render_capacity_text(snapshot)

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
        text, rows = render_server_allocation(
            server,
            server_id,
            self.commerce.plans(),
            is_owner=self._is_owner(telegram_id),
        )
        markup = self._inline_keyboard(rows)
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
        text, rows_markup = render_remote_inventory(
            all_rows,
            server_id,
            normalized_status,
            page,
            is_owner=self._is_owner(telegram_id),
            inventory_bytes=self._inventory_bytes,
            format_datetime=format_user_datetime,
        )
        markup = self._inline_keyboard(rows_markup)
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
