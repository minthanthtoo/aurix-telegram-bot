"""Telegram-facing durable maintenance and notification delivery."""

from __future__ import annotations

import json
import html
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observability import latency_log as _latency_log
from telegram_formatting import format_user_datetime

UTC = timezone.utc


class TelegramMaintenanceMixin:
    @staticmethod
    def _staff_alert_html(text: str) -> str:
        """Apply restrained Telegram styling after escaping every stored value."""
        lines = str(text).splitlines()
        rendered: list[str] = []
        for index, line in enumerate(lines):
            safe = html.escape(line)
            if index == 0 and safe:
                rendered.append(f"<b>{safe}</b>")
            elif ": " in safe:
                label, value = safe.split(": ", 1)
                rendered.append(f"<b>{label}:</b> {value}")
            else:
                rendered.append(safe)
        return "\n".join(rendered)

    def _send_pending_notifications(self) -> None:
        if self.commerce is None:
            return
        for notification in self.commerce.pending_notifications():
            if notification.get("secret_unavailable"):
                self.commerce.mark_notification_failed(notification["id"])
                print("notification secret unavailable", file=sys.stderr)
                continue
            try:
                markup = None
                if notification.get("access_url"):
                    markup = self._key_delivery_keyboard(notification["access_url"])
                elif str(notification.get("kind") or "").startswith("staff_"):
                    parts = str(notification.get("dedupe_key") or "").split(":")
                    entity_id = parts[2] if len(parts) >= 4 else ""
                    if notification.get("kind") == "staff_order_created":
                        markup = self._inline_keyboard(
                            [[("🛒 Open Order", f"a:o:{entity_id}")], [("⚙ Alerts", "a:n:notifications")]]
                        )
                    elif notification.get("kind") == "staff_rejected" and entity_id.startswith(
                        "order-"
                    ):
                        markup = self._inline_keyboard(
                            [
                                [("🛒 Open Order", f"a:o:{entity_id.removeprefix('order-')}")],
                                [("⚙ Alerts", "a:n:notifications")],
                            ]
                        )
                    elif entity_id:
                        markup = self._inline_keyboard(
                            [[("🧾 Open Receipt", f"a:r:{entity_id}")], [("⚙ Alerts", "a:n:notifications")]]
                        )
                is_staff_alert = str(notification.get("kind") or "").startswith("staff_")
                text = (
                    self._staff_alert_html(notification["text"])
                    if is_staff_alert
                    else notification["text"]
                )
                self.send(
                    notification["telegram_id"],
                    text,
                    markup,
                    parse_mode="HTML" if is_staff_alert else None,
                )
            except Exception as exc:
                self.commerce.mark_notification_failed(notification["id"])
                print(f"notification error: {type(exc).__name__}", file=sys.stderr)
            else:
                self.commerce.mark_notification_sent(notification["id"])

    def _process_receipt_extraction(self) -> None:
        """Process one durable assisted-extraction job outside the Telegram update loop."""
        if self.commerce is None:
            return
        receipt_policy = getattr(self.commerce, "receipt_policy", None)
        claim_job = getattr(self.commerce, "claim_receipt_extraction_job", None)
        if not callable(receipt_policy) or not callable(claim_job):
            return
        if str(receipt_policy().get("mode") or "manual") != "assisted":
            return
        job = claim_job()
        if job is None:
            return
        try:
            storage = getattr(self.commerce, "receipt_storage", None)
            download = getattr(storage, "download", None)
            if (
                job.get("storage_status") == "stored"
                and job.get("storage_path")
                and callable(download)
            ):
                image = download(str(job["storage_path"]))
                mime_type = str(job.get("mime_type") or "image/jpeg")
            else:
                image, mime_type = self._download_telegram_file(str(job["telegram_file_id"]))
            if not isinstance(image, bytes) or not image:
                raise RuntimeError("Receipt evidence could not be loaded")
            extraction, diagnostics = self.receipt_extractor.extract_with_diagnostics(
                image, mime_type, expected_provider=str(job.get("provider") or "")
            )
            self.commerce.finish_receipt_extraction(
                str(job["job_id"]),
                str(job["evidence_id"]),
                extraction.as_dict(),
                diagnostics,
            )
        except Exception as exc:
            self.commerce.fail_receipt_extraction_job(str(job["job_id"]), exc)
            print(f"receipt extraction job error: {type(exc).__name__}", file=sys.stderr)

    def _send_termination_notices(self) -> None:
        for event in self.service.pending_termination_notices("user"):
            reason = (
                "data quota reached"
                if event["reason"] == "quota"
                else "24-hour/monthly access expired"
            )
            remote = (
                "Outline confirmed the credential is deleted."
                if event["remote_state"] == "deleted_verified"
                else "Deletion could not be confirmed and has been escalated to the operator; the key remains blocked in AuriX."
                if event["remote_state"] == "escalated"
                else "Remote deletion is being retried; AuriX will not disclose or reactivate this key."
                if event["remote_state"] == "retrying"
                else "Outline accepted the deletion request."
            )
            usage = ""
            if event.get("used_bytes") is not None:
                usage = f"\nObserved usage: {self._format_bytes(int(event['used_bytes']))} / {self._format_bytes(int(event['quota_bytes']))}"
            try:
                self.send(
                    event["telegram_id"],
                    f"VPN access terminated\nReason: {reason}{usage}\nExpired at: {format_user_datetime(event['expires_at'])}\n{remote}",
                )
            except Exception:
                continue
            self.service.mark_termination_notice(event["id"], "user", event["remote_state"])
        if not self.admin_ids:
            return
        for event in self.service.pending_termination_notices("admin"):
            message = (
                f"VPN enforcement | tg:{event['telegram_id']} | key:{event['outline_key_id']}\n"
                f"Reason: {event['reason']} | remote:{event['remote_state']} | attempts:{event['delete_attempts']}\n"
                f"Used/quota: {event.get('used_bytes') or '-'} / {event['quota_bytes']} | "
                f"detected:{format_user_datetime(event.get('detected_at'))} | error:{event.get('last_error') or '-'}"
            )
            delivered = False
            for admin_id in self.admin_ids:
                try:
                    self.send(admin_id, message)
                    delivered = True
                except Exception:
                    pass
            if delivered:
                self.service.mark_termination_notice(event["id"], "admin", event["remote_state"])

    def _record_maintenance_heartbeat(
        self,
        *,
        started_at: str | None = None,
        completed_at: str | None = None,
        success_at: str | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        """Persist housekeeping health without allowing health reporting to fail it."""
        if started_at is not None:
            self._maintenance_last_status["last_started_at"] = started_at
        if completed_at is not None:
            self._maintenance_last_status["last_completed_at"] = completed_at
        if success_at is not None:
            self._maintenance_last_status["last_success_at"] = success_at
        if stage is not None:
            self._maintenance_last_status["last_stage"] = stage
        self._maintenance_last_status["last_error"] = error
        self._maintenance_last_status["status"] = (
            "ok" if success_at else ("error" if error else "running")
        )
        store = getattr(self.service, "database", None)
        recorder = getattr(store, "maintenance_heartbeat", None)
        if callable(recorder):
            try:
                recorder(
                    started_at=started_at,
                    completed_at=completed_at,
                    success_at=success_at,
                    stage=stage,
                    error=error,
                )
            except Exception as exc:
                print(
                    f"maintenance heartbeat persistence error: {type(exc).__name__}",
                    file=sys.stderr,
                )
        heartbeat_path = os.environ.get("AURIX_MAINTENANCE_HEARTBEAT_PATH")
        if heartbeat_path:
            try:
                Path(heartbeat_path).write_text(
                    json.dumps(self._maintenance_last_status, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                print(f"maintenance heartbeat file error: {type(exc).__name__}", file=sys.stderr)

    def _run_maintenance(self) -> None:
        """Serialize housekeeping passes across manual and scheduled invocations."""
        if not self._maintenance_lock.acquire(blocking=False):
            return
        try:
            self._run_maintenance_pass()
        finally:
            self._maintenance_lock.release()

    def _run_maintenance_pass(self) -> None:
        """Run one bounded housekeeping pass outside the Telegram poll loop."""
        started_at = time.perf_counter()
        started_text = datetime.now(UTC).isoformat()
        self._record_maintenance_heartbeat(started_at=started_text, stage="starting")
        failures: list[tuple[str, Exception]] = []

        def run_stage(name: str, callback: Any) -> Any:
            self._record_maintenance_heartbeat(stage=name)
            try:
                return callback()
            except Exception as exc:
                failures.append((name, exc))
                print(
                    f"maintenance stage={name} error={type(exc).__name__}: {exc}", file=sys.stderr
                )
                self._record_maintenance_heartbeat(stage=name, error=f"{type(exc).__name__}: {exc}")
                return None

        if (
            self._command_menu_retry_enabled
            and self._command_menu_configure_attempted
            and not self._command_menu_ready
        ):
            run_stage("command_menu", self.configure_commands)

        collect_metrics = getattr(self.service, "collect_metrics", self.service.outline.transfer_metrics)
        metrics_result = run_stage("metrics", collect_metrics)
        metrics = metrics_result if isinstance(metrics_result, dict) else {}
        if metrics_result is None:
            _latency_log("maintenance_metrics", started_at, status="error")
        # Quota first preserves the more informative cause when a key is both
        # over quota and past its wall-clock entitlement.
        run_stage("free_quota", lambda: self.service.enforce_quota(metrics=metrics))
        run_stage("free_expiry", self.service.revoke_expired)
        reconcile_terminations = getattr(self.service, "reconcile_terminations", None)
        if callable(reconcile_terminations):
            run_stage("free_revocation_retry", reconcile_terminations)
        run_stage("termination_notices", self._send_termination_notices)
        if self.commerce is not None:
            # Structured fleet snapshots are scoped by server; commerce
            # collects its own in that mode. Preserve the legacy single-server
            # snapshot reuse contract for standalone adapters and tests.
            run_stage(
                "paid_quota",
                self.commerce.enforce_quotas
                if isinstance(metrics.get("byServer"), dict)
                else lambda: self.commerce.enforce_quotas(metrics=metrics),
            )
            run_stage("paid_expiry", self.commerce.expire_and_process)
            refresh_inventory = getattr(self.commerce, "refresh_server_inventory", None)
            if callable(refresh_inventory):
                run_stage("server_inventory", refresh_inventory)
            capacity_snapshot = getattr(self.commerce, "capacity_snapshot", None)
            if callable(capacity_snapshot):
                # Inventory has just been refreshed above. Reuse that observed
                # state while recording durable scale evidence, avoiding a
                # second round of Outline requests in the same maintenance pass.
                run_stage(
                    "scale_observation",
                    lambda: capacity_snapshot(refresh_inventory=False),
                )
            run_stage("notifications", self._send_pending_notifications)
            # Slow model calls run last so quota enforcement and customer/staff
            # notifications are never held behind optional assisted extraction.
            run_stage("receipt_extraction", self._process_receipt_extraction)
            interaction_store = getattr(self.commerce, "database", None)
            prune_interactions = getattr(interaction_store, "prune_interaction_states", None)
            if callable(prune_interactions):
                run_stage("interaction_state_cleanup", prune_interactions)
        challenge_store = getattr(self.service, "database", None)
        prune = getattr(challenge_store, "prune_admin_challenges", None)
        if callable(prune):
            run_stage("challenge_cleanup", lambda: prune(datetime.now(UTC).isoformat()))
        completed_text = datetime.now(UTC).isoformat()
        success_text = completed_text if not failures else None
        error_text = "; ".join(f"{name}: {type(exc).__name__}" for name, exc in failures) or None
        self._record_maintenance_heartbeat(
            completed_at=completed_text,
            success_at=success_text,
            stage="completed",
            error=error_text,
        )
        _latency_log("maintenance", started_at)

    def _maintenance_loop(self) -> None:
        while self.running and not self._maintenance_stop.is_set():
            try:
                self._run_maintenance()
            except Exception as exc:
                print(
                    f"maintenance error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            if self._maintenance_stop.wait(self.maintenance_interval_seconds):
                break

    def stop(self) -> None:
        self.running = False
        self._maintenance_stop.set()
