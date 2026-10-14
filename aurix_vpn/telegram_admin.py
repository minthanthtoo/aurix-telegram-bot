"""Authorization boundary for privileged Telegram operations."""

from __future__ import annotations

from typing import Any

from .commerce import CommerceError, CommerceService


class AdminOperations:
    """Authorization boundary for privileged commerce operations.

    Telegram remains the presentation/transport layer; all admin commerce
    calls made by it pass through this object so a future admin transport can
    reuse the same allowlist check instead of trusting a UI decision.
    """

    COMMERCE_OPERATIONS = frozenset(
        {
            "list_pending_orders",
            "list_pending_receipts",
            "get_receipt",
            "order_detail",
            "verify_receipt",
            "reject_receipt",
            "approve_order",
            "reject_order",
            "refund_order",
            "retry_failed_job",
            "retry_job",
            "failed_jobs",
            "consistency_report",
            "capacity_snapshot",
            "configure_endpoint_capacity",
            "configure_endpoint_plan_limit",
            "endpoint_plan_capacity",
            "endpoint_drain_preview",
            "request_endpoint_drain",
            "failover_safety_controls",
            "configure_failover_safety",
            "endpoint_lifecycle_preview",
            "set_endpoint_lifecycle",
            "queue_infrastructure_provision",
            "protocol_profile_status",
            "protocol_profile_promotion_readiness",
            "promote_protocol_profile",
            "disable_protocol_profile",
            "request_account_suspension",
            "reactivate_account",
            "account_access_status",
            "wallet_balance",
            "wallet_history",
        }
    )
    SERVICE_OPERATIONS = frozenset(
        {
            "termination_summary",
            "pending_termination_notices",
            "giveaway_status",
            "configure_giveaway",
            "set_giveaway_active",
        }
    )

    OPERATION_CAPABILITIES = {
        "list_pending_orders": "sales.read",
        "list_pending_receipts": "sales.read",
        "get_receipt": "sales.read",
        "order_detail": "sales.read",
        "verify_receipt": "sales.decide",
        "reject_receipt": "sales.decide",
        "approve_order": "sales.decide",
        "reject_order": "sales.decide",
        "refund_order": "sales.decide",
        "retry_failed_job": "recovery.manage",
        "retry_job": "recovery.manage",
        "failed_jobs": "recovery.manage",
        "consistency_report": "recovery.manage",
        "capacity_snapshot": "fleet.read",
        "configure_endpoint_capacity": "fleet.manage",
        "configure_endpoint_plan_limit": "fleet.manage",
        "endpoint_plan_capacity": "fleet.read",
        "endpoint_drain_preview": "fleet.read",
        "request_endpoint_drain": "fleet.manage",
        "failover_safety_controls": "fleet.read",
        "configure_failover_safety": "fleet.manage",
        "endpoint_lifecycle_preview": "fleet.read",
        "set_endpoint_lifecycle": "fleet.manage",
        "queue_infrastructure_provision": "fleet.manage",
        "protocol_profile_status": "fleet.read",
        "protocol_profile_promotion_readiness": "fleet.read",
        "promote_protocol_profile": "fleet.manage",
        "disable_protocol_profile": "fleet.manage",
        "request_account_suspension": "accounts.manage",
        "reactivate_account": "accounts.manage",
        "account_access_status": "accounts.read",
        "wallet_balance": "wallet.read",
        "wallet_history": "wallet.read",
        "termination_summary": "recovery.manage",
        "pending_termination_notices": "recovery.manage",
        "giveaway_status": "promotions.manage",
        "configure_giveaway": "promotions.manage",
        "set_giveaway_active": "promotions.manage",
    }

    def __init__(
        self,
        commerce: CommerceService | None,
        admin_ids: set[int],
        service: Any | None = None,
        staff_access: Any | None = None,
    ):
        self.commerce = commerce
        self.admin_ids = admin_ids
        self.service = service
        self.staff_access = staff_access

    def require_admin(self, telegram_id: int) -> None:
        if self.staff_access is not None and self.staff_access.enabled:
            self.staff_access.require_admin(int(telegram_id))
        elif int(telegram_id) not in self.admin_ids:
            raise PermissionError("administrator access required")

    def _require_operation_access(self, telegram_id: int, operation: str) -> None:
        if self.staff_access is not None and self.staff_access.enabled:
            capability = self.OPERATION_CAPABILITIES.get(operation)
            if capability is None:
                raise CommerceError("That administrator operation is unavailable.")
            self.staff_access.require_capability(int(telegram_id), capability)
            return
        self.require_admin(telegram_id)

    def call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        if self.commerce is None:
            raise CommerceError("Commerce is not configured.")
        if operation not in self.COMMERCE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        self._require_operation_access(telegram_id, operation)
        method = getattr(self.commerce, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)

    def call_service(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        if self.service is None:
            raise CommerceError("Service is not configured.")
        if operation not in self.SERVICE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        self._require_operation_access(telegram_id, operation)
        method = getattr(self.service, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)
