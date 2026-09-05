"""Compatibility facade for decomposed wallet and approval workflows."""

from commerce_models import ApprovalResult, CommerceError, UTC, _new_id, _normalize_reference, _now_text
from commerce_service_wallet_approval import approve_order
from commerce_service_wallet_payment import credit_wallet, pay_order_with_wallet
from commerce_service_wallet_read import consistency_report, list_pending_orders, wallet_balance, wallet_history
from commerce_service_wallet_refunds import refund_order, reject_order

__all__ = [
    "ApprovalResult",
    "CommerceError",
    "consistency_report",
    "credit_wallet",
    "list_pending_orders",
    "pay_order_with_wallet",
    "approve_order",
    "refund_order",
    "reject_order",
    "wallet_balance",
    "wallet_history",
]
