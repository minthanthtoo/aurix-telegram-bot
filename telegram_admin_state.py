"""Read-only administrator confirmation snapshots and previews."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from telegram_admin_state_snapshots import TelegramAdminSnapshotMixin


class TelegramAdminStateMixin(TelegramAdminSnapshotMixin):
    def _admin_state_snapshot(
        self, command: str, args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        """Read the state an administrator is about to mutate.

        This is deliberately a read-only snapshot. Domain methods still own
        their invariants and transactions; the snapshot prevents a stale
        confirmation from silently applying to a changed order or receipt.
        """
        target_id = str(args[0]) if args else ""
        snapshot = self._empty_admin_snapshot(command, target_id)
        try:
            if command == "/receiptmode":
                return self._snapshot_receipt_mode(snapshot, args, telegram_id)
            if command == "/approverepair":
                return self._snapshot_repair(snapshot, telegram_id)
            if command in {"/setpromo", "/stoppromo", "/resumepromo"}:
                return self._snapshot_promo(snapshot, target_id, telegram_id)
            if self.commerce is None or not target_id:
                snapshot["state"] = "missing"
                return snapshot
            if command == "/migratekey":
                return self._snapshot_migration(snapshot, args, telegram_id)
            if command == "/serverstate":
                return self._snapshot_server_state(snapshot, args, telegram_id)
            if command == "/retryjob":
                return self._snapshot_retry_job(snapshot, telegram_id)
            if command in {"/verify", "/rejectreceipt"}:
                return self._snapshot_receipt(snapshot, telegram_id)
            return self._snapshot_order(snapshot, command, telegram_id)
        except Exception as exc:
            return self._unavailable_admin_snapshot(command, target_id, exc)

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
