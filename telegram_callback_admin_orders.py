"""Order and receipt actions from the administrator callback surface."""

from __future__ import annotations

from typing import Any


def _show_order(
    host: Any,
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    entity_id: str,
) -> None:
    host._send_order_detail(
        chat_id,
        telegram_id,
        entity_id,
        admin_view=True,
        message_id=message_id if can_edit_text else None,
    )


def _queue_repair(
    host: Any, chat_id: int, telegram_id: int, entity_id: str
) -> None:
    if not host._is_owner(telegram_id):
        host._send_customer_fallback(chat_id, telegram_id)
        return
    try:
        repair_id, approval_mode = entity_id.split(":", 1)
    except ValueError:
        host.send(chat_id, "That repair action is no longer valid.")
        return
    if approval_mode not in {"safe", "full"} or not repair_id:
        host.send(chat_id, "That repair action is no longer valid.")
        return
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        "/approverepair",
        [repair_id, *(["full"] if approval_mode == "full" else [])],
        (
            f"Approve managed-key repair {repair_id[:16]} while preserving observed usage?"
            if approval_mode == "safe"
            else f"Approve managed-key repair {repair_id[:16]} with explicit full-quota restoration?"
        ),
        "✅ Confirm Repair" if approval_mode == "safe" else "⚠️ Confirm Full Quota",
        cancel_data="a:n:repairs",
    )


def _handle_promo_or_revocation(
    host: Any, chat_id: int, telegram_id: int, entity_id: str
) -> None:
    if ":" not in entity_id:
        host._queue_admin_confirmation(
            chat_id,
            telegram_id,
            "/retry",
            [entity_id, "revoke"],
            f"Retry the failed revocation job for order {entity_id}?",
            "Confirm Retry",
        )
        return
    promo_action, promo_code = entity_id.split(":", 1)
    if promo_action not in {"stop", "resume"} or not promo_code:
        host.send(chat_id, "This promo action is no longer valid.")
        return
    command = "/stoppromo" if promo_action == "stop" else "/resumepromo"
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        command,
        [promo_code],
        f"{promo_action.title()} promo {promo_code}?",
        "Confirm Promo Change",
        cancel_data="a:n:promo",
    )


def _queue_order_retry(
    host: Any, chat_id: int, telegram_id: int, entity_id: str, job_type: str
) -> None:
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        "/retry",
        [entity_id, job_type],
        f"Retry the failed {job_type} job for order {entity_id}?",
        "Confirm Retry",
    )


def _verify_receipt(
    host: Any,
    chat_id: int,
    telegram_id: int,
    entity_id: str,
) -> None:
    receipt = host._admin_call(telegram_id, "get_receipt", entity_id)
    if receipt is None or receipt.get("review_status") != "pending":
        host.send(chat_id, "This receipt is no longer awaiting verification.")
        return
    extracted = receipt.get("extraction") or {}
    reference = str(extracted.get("transaction_id") or "").strip()
    amount_value = extracted.get("amount_minor", extracted.get("amount"))
    try:
        amount = int(str(amount_value).replace(",", ""))
    except (TypeError, ValueError):
        amount = 0
    if reference and amount > 0:
        host._queue_admin_confirmation(
            chat_id,
            telegram_id,
            "/verify",
            [entity_id, reference, str(amount)],
            "Confirm that these extracted details match the actual receiving account.",
            "✅ I Checked · Verify",
            f"a:r:{entity_id}",
        )
        return
    host._receipt_verify_inputs[telegram_id] = entity_id
    host._save_interaction_state(
        telegram_id, "receipt_verify", {"evidence_id": entity_id}
    )
    host.send(
        chat_id,
        "🔎 Check the actual receiving account, then reply with only:\n"
        "transaction-ID amount\n\nExample: 123456789 3000\n"
        "The receipt/order ID is already selected for you.",
        host._inline_keyboard([[('Cancel', f"a:r:{entity_id}")]]),
    )


def _queue_confirmation(
    host: Any,
    chat_id: int,
    telegram_id: int,
    command: str,
    entity_id: str,
    prompt: str,
    title: str,
    cancel_data: str | None = None,
) -> None:
    args = [entity_id]
    host._queue_admin_confirmation(
        chat_id,
        telegram_id,
        command,
        args,
        prompt,
        title,
        cancel_data,
    )


def dispatch_order_action(
    host: Any,
    query: dict[str, Any],
    chat_id: int,
    telegram_id: int,
    message_id: Any,
    can_edit_text: bool,
    synthetic: dict[str, Any],
    action: str,
    entity_id: str,
) -> None:
    if action == "o":
        _show_order(host, chat_id, telegram_id, message_id, can_edit_text, entity_id)
    elif action == "j":
        _queue_repair(host, chat_id, telegram_id, entity_id)
    elif action == "p":
        _queue_confirmation(
            host, chat_id, telegram_id, "/retryjob", entity_id,
            f"Retry worker job {entity_id}?", "Confirm Retry"
        )
    elif action == "g":
        _handle_promo_or_revocation(host, chat_id, telegram_id, entity_id)
    elif action == "h":
        _queue_order_retry(host, chat_id, telegram_id, entity_id, "provision")
    elif action == "l":
        synthetic["text"] = f"/ledger {entity_id}"
        host.handle(synthetic)
    elif action in {"f", "z"}:
        _queue_confirmation(
            host, chat_id, telegram_id, "/refund", entity_id,
            f"Refund order {entity_id} to the customer wallet and revoke paid access?",
            "Confirm Refund", f"a:o:{entity_id}"
        )
    elif action == "r":
        host._receipt_verify_inputs.pop(telegram_id, None)
        host._clear_interaction_state(telegram_id, "receipt_verify")
        synthetic["text"] = f"/receipt {entity_id}"
        host.handle(synthetic)
    elif action == "v":
        _verify_receipt(host, chat_id, telegram_id, entity_id)
    elif action == "a":
        _queue_confirmation(
            host, chat_id, telegram_id, "/approve", entity_id,
            f"Approve order {entity_id} and queue VPN provisioning?",
            "Confirm Approve", f"a:o:{entity_id}"
        )
    elif action in {"x", "c"}:
        _queue_confirmation(
            host, chat_id, telegram_id, "/reject", entity_id,
            f"Reject order {entity_id} and notify the customer?",
            "Confirm Reject", f"a:o:{entity_id}"
        )
    elif action in {"q", "y"}:
        _queue_confirmation(
            host, chat_id, telegram_id, "/rejectreceipt", entity_id,
            f"Reject receipt {entity_id} and request a replacement screenshot?",
            "Confirm Reject Receipt", f"a:r:{entity_id}"
        )
    else:
        host.send(chat_id, "This admin action is no longer valid.")
