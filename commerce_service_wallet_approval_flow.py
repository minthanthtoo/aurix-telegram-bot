"""Wallet-funded order approval workflow."""

from __future__ import annotations

from datetime import datetime

from commerce_models import ApprovalResult, _now_text
from commerce_service_wallet_approval_stages import (
    approve_paid_order,
    approve_wallet_topup,
    load_approval_context,
)


def approve_order(
    self,
    order_id: str,
    admin_id: int,
    now: datetime | None = None,
) -> ApprovalResult:
    starts_at = _now_text(now)
    with self.database.connect() as connection:
        self.database.begin_write(connection)
        self._lock_order(connection, order_id)
        context = load_approval_context(self, connection, order_id)
        if isinstance(context, ApprovalResult):
            return context
        if context.is_wallet_topup:
            return approve_wallet_topup(
                self,
                connection,
                context,
                order_id=order_id,
                admin_id=admin_id,
                starts_at=starts_at,
            )
        subscription_id = approve_paid_order(
            self,
            connection,
            context,
            order_id=order_id,
            admin_id=admin_id,
            starts_at=starts_at,
        )
    return ApprovalResult(order_id, subscription_id, "approved")
