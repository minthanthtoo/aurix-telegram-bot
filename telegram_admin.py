"""Authorization boundary for privileged Telegram operations."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError, CommerceService


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
            "wallet_balance",
            "wallet_history",
            "receipt_policy",
            "set_receipt_mode",
            "start_receipt_diagnostic",
            "finish_receipt_diagnostic",
            "last_receipt_diagnostic",
            "receipt_system_snapshot",
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
        if self.staff_access is not None:
            self.staff_access.require_admin(telegram_id)
            return
        if int(telegram_id) not in self.admin_ids:
            raise PermissionError("administrator access required")

    def call(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.require_admin(telegram_id)
        if self.commerce is None:
            raise CommerceError("Commerce is not configured.")
        if operation not in self.COMMERCE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        method = getattr(self.commerce, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)

    def call_service(self, telegram_id: int, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.require_admin(telegram_id)
        if self.service is None:
            raise CommerceError("Service is not configured.")
        if operation not in self.SERVICE_OPERATIONS:
            raise CommerceError("That administrator operation is unavailable.")
        method = getattr(self.service, operation, None)
        if not callable(method):
            raise CommerceError("That administrator operation is unavailable.")
        return method(*args, **kwargs)
