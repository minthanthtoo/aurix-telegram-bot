"""Supplier webhook verification and idempotent Games-order reconciliation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
import uuid

from .order_repository import GameOrderRepository, ProviderEventResult
from .order_state import GameOrder, OrderState
from .providers.fazercards_commands import (
    SupplierCommandError,
    SupplierWebhookEvent,
    parse_webhook_event,
    verify_webhook_signature,
)


UTC = timezone.utc


def write_poll_status(
    path: str | Path, *, at: datetime, checked: int, errors: int,
) -> None:
    """Atomically record a private, identifier-free supplier polling heartbeat."""
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("poll status time must be timezone-aware")
    if isinstance(checked, bool) or not isinstance(checked, int) or checked < 0:
        raise ValueError("checked must be a non-negative integer")
    if isinstance(errors, bool) or not isinstance(errors, int) or not 0 <= errors <= checked:
        raise ValueError("errors must be an integer between zero and checked")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({
                "status": "ok" if errors == 0 else "error",
                "checked_at": at.astimezone(UTC).isoformat(),
                "checked": checked,
                "errors": errors,
            }, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def poll_status_is_fresh(
    path: str | Path, *, now: datetime | None = None, max_age: timedelta = timedelta(seconds=180),
) -> bool:
    """Return true only for a successful, recent polling-worker heartbeat."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None or max_age < timedelta(0):
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(payload["checked_at"])
        if checked_at.tzinfo is None or payload.get("status") != "ok" or payload.get("errors") != 0:
            return False
        age = current.astimezone(UTC) - checked_at.astimezone(UTC)
        return timedelta(0) <= age <= max_age
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False


class WebhookAuthenticationError(SupplierCommandError):
    """The webhook signature was missing or invalid."""


class WebhookReconciliationError(SupplierCommandError):
    """The signed webhook could not be safely reconciled."""


@dataclass(frozen=True)
class ReconciliationOutcome:
    event_id: str
    provider_order_id: str
    provider_status: str | None
    order_id: str | None
    state: str | None
    duplicate: bool
    applied: bool
    ignored_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provider_order_id": self.provider_order_id,
            "provider_status": self.provider_status,
            "order_id": self.order_id,
            "state": self.state,
            "duplicate": self.duplicate,
            "applied": self.applied,
            "ignored_reason": self.ignored_reason,
        }


class _EventClient(Protocol):
    def get_order(self, order_id: str) -> dict[str, Any]: ...


def reconcile_order(
    repository: GameOrderRepository, client: _EventClient, order_id: str, *, now: datetime,
) -> ReconciliationOutcome:
    """Recover a missed webhook using an exact, already accepted provider order."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise WebhookReconciliationError("reconciliation time must be timezone-aware")
    local = repository.get(order_id)
    if local.state not in {OrderState.SUBMITTED, OrderState.PROCESSING, OrderState.UNKNOWN,
                           OrderState.DELIVERED, OrderState.FAILED, OrderState.RECONCILIATION} or not local.provider_order_id:
        raise WebhookReconciliationError("order does not have a pending accepted supplier attempt")
    payload = client.get_order(local.provider_order_id)
    remote = payload.get("order") if isinstance(payload, dict) and payload.get("ok") is True else None
    if not isinstance(remote, dict) or remote.get("id") != local.provider_order_id:
        raise WebhookReconciliationError("supplier order read did not match the accepted attempt")
    status = remote.get("status")
    if not isinstance(status, str) or not status or len(status) > 80 or any(ord(c) < 32 for c in status):
        raise WebhookReconciliationError("supplier order status is malformed")
    target = _target_for_status(status)
    event_id = "poll-" + hashlib.sha256(
        f"{local.order_id}:{local.provider_order_id}:{status}:{target.value if target else ''}".encode()
    ).hexdigest()
    result = repository.apply_provider_event(event_id=event_id,
        provider_order_id=local.provider_order_id, provider_status=status, target=target,
        at=now, reason="authenticated supplier order-status reconciliation")
    event = SupplierWebhookEvent(event_id, "order.status_changed", now.isoformat(),
        local.provider_order_id, None, status, None)
    return _outcome(event, result)


class SupplierWebhookReconciler:
    """Verify signed events and apply only known, allowed order states."""

    def __init__(self, repository: GameOrderRepository, *, webhook_secret: str) -> None:
        if not isinstance(webhook_secret, str) or not webhook_secret:
            raise ValueError("webhook_secret is required")
        self.repository = repository
        self.webhook_secret = webhook_secret

    def process(self, raw_body: bytes, signature: str, *, now: datetime) -> ReconciliationOutcome:
        return _apply_event(self.repository, self.receive(raw_body, signature, now=now), now=now)

    def receive(self, raw_body: bytes, signature: str, *, now: datetime) -> SupplierWebhookEvent:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise WebhookReconciliationError("reconciliation time must be timezone-aware")
        if not verify_webhook_signature(raw_body, signature, self.webhook_secret):
            raise WebhookAuthenticationError("invalid webhook signature")
        event = parse_webhook_event(raw_body)
        if event.order_id:
            occurred_at = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if occurred_at.utcoffset() is None or occurred_at > now + timedelta(minutes=5):
                raise WebhookReconciliationError("invalid supplier event time")
            self.repository.store_provider_event(event_id=event.event_id, provider_order_id=event.order_id,
                event_json=json.dumps(asdict(event), sort_keys=True), occurred_at=occurred_at, received_at=now)
        return event

    def apply_event(self, event: SupplierWebhookEvent, *, now: datetime) -> ReconciliationOutcome:
        return _apply_event(self.repository, event, now=now, from_inbox=False)


class Shop2TopupWebhookReconciler:
    """Verify SHOP2TOPUP's raw-body HMAC and queue normalized order events."""

    _SIGNATURE = re.compile(r"^sha256=[a-f0-9]{64}$")
    _EVENTS = {"order.completed", "order.refunded", "webhook.test"}
    _STATUSES = {"pending", "completed", "refunded", "partial"}

    def __init__(self, repository: GameOrderRepository, *, webhook_secret: str) -> None:
        if not isinstance(webhook_secret, str) or not webhook_secret or len(webhook_secret) > 512:
            raise ValueError("webhook_secret is required")
        self.repository = repository
        self.webhook_secret = webhook_secret

    def receive(
        self, raw_body: bytes, signature: str, event_header: str, *, now: datetime,
    ) -> SupplierWebhookEvent:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise WebhookReconciliationError("reconciliation time must be timezone-aware")
        if not isinstance(raw_body, bytes) or not raw_body or len(raw_body) > 4 * 1024 * 1024:
            raise WebhookReconciliationError("webhook body is invalid")
        if not isinstance(signature, str) or not self._SIGNATURE.fullmatch(signature):
            raise WebhookAuthenticationError("invalid supplier webhook signature")
        expected = "sha256=" + hmac.new(
            self.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WebhookAuthenticationError("invalid supplier webhook signature")
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebhookReconciliationError("webhook body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise WebhookReconciliationError("webhook payload must be an object")
        event_name = payload.get("event")
        if (not isinstance(event_name, str) or event_name not in self._EVENTS
                or event_name != event_header):
            raise WebhookReconciliationError("webhook event header does not match its payload")
        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 80:
            raise WebhookReconciliationError("webhook timestamp is invalid")
        try:
            occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise WebhookReconciliationError("webhook timestamp is invalid") from None
        if (occurred_at.tzinfo is None or occurred_at.utcoffset() is None
                or occurred_at > now + timedelta(minutes=5)):
            raise WebhookReconciliationError("webhook timestamp is invalid")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WebhookReconciliationError("webhook data is invalid")
        raw_order_id = data.get("order_id")
        if raw_order_id is None:
            if event_name != "webhook.test":
                raise WebhookReconciliationError("webhook order ID is missing")
            provider_order_id = None
        else:
            if not isinstance(raw_order_id, str):
                raise WebhookReconciliationError("webhook order ID is invalid")
            try:
                parsed_order_id = uuid.UUID(raw_order_id)
            except (ValueError, AttributeError):
                raise WebhookReconciliationError("webhook order ID is invalid") from None
            provider_order_id = "s2t-" + parsed_order_id.hex
        status = data.get("status")
        if provider_order_id is not None and (not isinstance(status, str) or status not in self._STATUSES):
            raise WebhookReconciliationError("webhook order status is invalid")
        event_id = "s2t-webhook-" + hashlib.sha256(raw_body).hexdigest()
        event = SupplierWebhookEvent(
            event_id=event_id,
            event_type="order.status_changed",
            timestamp=timestamp,
            order_id=provider_order_id,
            supplier_kind="shop2topup",
            status=status if isinstance(status, str) else None,
            previous_status=None,
        )
        if provider_order_id is not None:
            self.repository.store_provider_event(
                event_id=event.event_id,
                provider_order_id=provider_order_id,
                event_json=json.dumps(asdict(event), sort_keys=True),
                occurred_at=occurred_at,
                received_at=now,
            )
        return event


def drain_provider_events(repository: GameOrderRepository, *, now: datetime, limit: int) -> tuple[ReconciliationOutcome, ...]:
    return tuple(_apply_event(repository, SupplierWebhookEvent(**json.loads(encoded)), now=now)
                 for encoded in repository.pending_provider_events(limit=limit))


def _apply_event(repository, event, *, now, from_inbox=True):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise WebhookReconciliationError("reconciliation time must be timezone-aware")
    if not isinstance(event, SupplierWebhookEvent):
        raise WebhookReconciliationError("webhook event has unsupported type")
    if not event.order_id:
        return ReconciliationOutcome(
            event.event_id, "", event.status, None, None, False, False, "event_has_no_order",
        )
    target = (
        _target_for_status(event.status)
        if event.event_type == "order.status_changed"
        else None
    )
    result = repository.apply_provider_event(
        event_id=event.event_id,
        provider_order_id=event.order_id,
        provider_status=event.status,
        target=target,
        at=now,
        reason=f"signed supplier webhook {event.event_type}",
        from_inbox=from_inbox,
    )
    if result.order is None:
        return ReconciliationOutcome(
            event.event_id, event.order_id, event.status, None, None, False, False, "provider_order_not_found",
        )
    outcome = _outcome(event, result)
    if target is None and not outcome.duplicate:
        return ReconciliationOutcome(
            outcome.event_id,
            outcome.provider_order_id,
            outcome.provider_status,
            outcome.order_id,
            outcome.state,
            outcome.duplicate,
            outcome.applied,
            (
                "provider_status_semantics_undocumented"
                if event.event_type == "order.status_changed"
                else "event_type_semantics_undocumented"
            ),
        )
    return outcome


def _target_for_status(status: str | None) -> OrderState | None:
    # The official SDK uses refund; retain refunded from the webhook examples.
    # Keep other provider values as audit-only evidence. A supplier
    # refund is not proof that the customer has been refunded, so it goes to
    # operator reconciliation rather than mutating the customer ledger.
    if not isinstance(status, str):
        return None
    return {
        "pending": OrderState.PROCESSING,
        "processing": OrderState.PROCESSING,
        "completed": OrderState.DELIVERED,
        "failed": OrderState.FAILED,
        "refund": OrderState.RECONCILIATION,
        "refunded": OrderState.RECONCILIATION,
        "partial": OrderState.RECONCILIATION,
    }.get(status)

def _outcome(event: SupplierWebhookEvent, result: ProviderEventResult) -> ReconciliationOutcome:
    return ReconciliationOutcome(
        event.event_id,
        event.order_id or "",
        event.status,
        result.order.order_id if result.order else None,
        result.order.state.value if result.order else None,
        result.duplicate,
        result.applied,
        None if result.applied else "state_transition_not_applied",
    )


__all__ = [
    "ReconciliationOutcome",
    "Shop2TopupWebhookReconciler",
    "SupplierWebhookReconciler",
    "WebhookAuthenticationError",
    "WebhookReconciliationError",
    "reconcile_order",
    "drain_provider_events",
]
