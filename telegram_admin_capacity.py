"""Capacity, inventory, migration, repair, and probe views."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from commerce import CommerceError
from telegram_admin_capacity_view import render_capacity_text
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
