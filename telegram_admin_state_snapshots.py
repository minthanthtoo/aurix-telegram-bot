"""Command-specific read models for administrator confirmation snapshots."""

from __future__ import annotations

from typing import Any


class TelegramAdminSnapshotMixin:
    """Build one narrowly scoped snapshot for each admin command family."""

    @staticmethod
    def _empty_admin_snapshot(command: str, target_id: str) -> dict[str, Any]:
        return {"command": command, "target_id": target_id, "state": "unavailable"}

    @staticmethod
    def _unavailable_admin_snapshot(
        command: str, target_id: str, error: Exception
    ) -> dict[str, Any]:
        return {
            "command": command,
            "target_id": target_id,
            "state": "unavailable",
            "error_type": type(error).__name__,
        }

    def _snapshot_receipt_mode(
        self, snapshot: dict[str, Any], args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        policy = self._admin_call(telegram_id, "receipt_policy")
        snapshot.update(
            {
                "state": "present",
                "current_mode": policy.get("mode"),
                "version": policy.get("version"),
                "requested_mode": args[0] if args else None,
            }
        )
        return snapshot

    def _snapshot_repair(
        self, snapshot: dict[str, Any], telegram_id: int
    ) -> dict[str, Any]:
        repairs = self._admin_call(
            telegram_id, "managed_key_repair_jobs", status="all", limit=500
        )
        repair = next(
            (item for item in repairs if str(item.get("id")) == snapshot["target_id"]),
            None,
        )
        snapshot.update(
            {"state": "present", "repair": repair}
            if repair
            else {"state": "missing"}
        )
        return snapshot

    def _snapshot_promo(
        self, snapshot: dict[str, Any], target_id: str, telegram_id: int
    ) -> dict[str, Any]:
        promo = self._admin_service_call(
            telegram_id, "giveaway_status", telegram_id, target_id or None
        )
        snapshot.update({"state": "present", "promo": promo})
        return snapshot

    def _snapshot_migration(
        self, snapshot: dict[str, Any], args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        if len(args) != 3:
            snapshot["state"] = "missing"
            return snapshot
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
            return snapshot
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
        return snapshot

    def _snapshot_server_state(
        self, snapshot: dict[str, Any], args: list[str], telegram_id: int
    ) -> dict[str, Any]:
        if len(args) != 2 or str(args[1]).lower() not in {"active", "draining", "retired"}:
            snapshot["state"] = "missing"
            return snapshot
        readiness = self._admin_owner_call(
            telegram_id, "server_drain_readiness", snapshot["target_id"]
        )
        snapshot.update(
            {
                "state": "present",
                "server_id": snapshot["target_id"],
                "requested_state": str(args[1]).lower(),
                "lifecycle_state": readiness.get("lifecycle_state"),
                "ready_to_retire": readiness.get("ready_to_retire"),
                "blockers": readiness.get("blockers") or [],
            }
        )
        return snapshot

    def _snapshot_retry_job(
        self, snapshot: dict[str, Any], telegram_id: int
    ) -> dict[str, Any]:
        jobs = self._admin_call(
            telegram_id, "failed_jobs", limit=100, include_nonterminal=True
        )
        job = next(
            (item for item in jobs if str(item.get("job_id")) == snapshot["target_id"]),
            None,
        )
        if job is None or job.get("job_status") != "failed":
            snapshot["state"] = "missing"
            return snapshot
        snapshot.update(
            {
                "state": "present",
                "job_id": snapshot["target_id"],
                "operation": job.get("operation"),
                "order_id": job.get("order_id"),
                "attempts": job.get("attempts"),
                "last_error": job.get("last_error"),
            }
        )
        return snapshot

    def _snapshot_receipt(
        self, snapshot: dict[str, Any], telegram_id: int
    ) -> dict[str, Any]:
        receipt = self._admin_call(telegram_id, "get_receipt", snapshot["target_id"])
        if receipt is None:
            snapshot["state"] = "missing"
            return snapshot
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
                "verified_provider_reference": receipt.get("verified_provider_reference"),
                "verified_amount_minor": receipt.get("verified_amount_minor"),
                "verified_currency": receipt.get("verified_currency"),
            }
        )
        order_id = receipt.get("order_id")
        if order_id:
            order = self._admin_call(
                telegram_id, "order_detail", str(order_id), telegram_id, is_admin=True
            )
            if order:
                snapshot.update(
                    {
                        "order_status": order.get("status"),
                        "payment_status": order.get("payment_status"),
                        "order_amount_minor": order.get("amount_minor"),
                    }
                )
        return snapshot

    def _snapshot_order(
        self, snapshot: dict[str, Any], command: str, telegram_id: int
    ) -> dict[str, Any]:
        order = self._admin_call(
            telegram_id,
            "order_detail",
            snapshot["target_id"],
            telegram_id,
            is_admin=True,
        )
        if order is None:
            snapshot["state"] = "missing"
            return snapshot
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
        if command == "/retry":
            jobs = self._admin_call(telegram_id, "failed_jobs", limit=100)
            matching = [
                job for job in jobs if str(job.get("order_id")) == snapshot["target_id"]
            ]
            snapshot["failed_job"] = (
                {
                    "operation": matching[0].get("operation"),
                    "attempts": matching[0].get("attempts"),
                    "last_error": matching[0].get("last_error"),
                }
                if matching
                else None
            )
        return snapshot
