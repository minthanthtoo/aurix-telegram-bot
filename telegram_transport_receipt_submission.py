"""Telegram receipt-order resolution and submission transport steps."""

from __future__ import annotations

from typing import Any

from commerce import CommerceError


def resolve_receipt_order(
    transport: Any, chat_id: int, telegram_id: int, message: dict[str, Any]
) -> str | None:
    """Resolve the order context or explain the ambiguity to the customer."""
    order_id = transport._pending_order_id(telegram_id, str(message.get("caption") or ""))
    if order_id:
        return order_id
    list_open = getattr(transport.commerce, "open_order_ids_for_user", None)
    try:
        open_count = len(list_open(telegram_id, limit=20)) if callable(list_open) else 0
    except Exception:
        open_count = 0
    if open_count > 1:
        transport.send(
            chat_id,
            "I found more than one open order. Open My Orders and tap “Upload Receipt” "
            "on the exact order, or send the screenshot with /paid <order-id> in its caption.",
            transport._customer_keyboard(telegram_id),
        )
    else:
        transport.send(
            chat_id,
            "Create an order with Plans, then send its receipt screenshot. "
            "Use the order’s Upload Receipt button or caption it with /paid <order-id>.",
            transport._customer_keyboard(telegram_id),
        )
    return None


def submit_receipt_payload(
    transport: Any,
    telegram_id: int,
    order_id: str,
    *,
    file_id: str,
    unique_id: str | None,
    media_type: str,
) -> tuple[dict[str, Any], bool]:
    """Download, deduplicate, and submit one Telegram receipt payload."""
    order = transport.commerce.order_detail(order_id, telegram_id)
    provider = str((order or {}).get("payment_method") or "manual")
    image, mime = transport._download_telegram_file(file_id)
    duplicate_status = transport.commerce.receipt_duplicate_status(
        telegram_id,
        order_id,
        image,
        str(unique_id) if unique_id else None,
        provider=provider,
    )
    if duplicate_status == "different_order":
        raise CommerceError(
            "This receipt was already submitted for another order; please send the original "
            "receipt for this order"
        )
    policy = transport.commerce.receipt_policy()
    extraction_configured = bool(
        getattr(transport.receipt_extractor, "base_url", "")
        and getattr(transport.receipt_extractor, "model", "")
        and getattr(transport.receipt_extractor, "api_key", "")
    )
    queue_extraction = (
        str(policy.get("mode") or "manual") == "assisted" and extraction_configured
    )
    result = transport.commerce.submit_receipt(
        telegram_id,
        order_id,
        provider=provider,
        file_id=file_id,
        file_unique_id=str(unique_id) if unique_id else None,
        image_bytes=image,
        mime_type=mime,
        extraction=None,
        telegram_media_type=media_type,
        queue_extraction=queue_extraction,
    )
    return result, queue_extraction
