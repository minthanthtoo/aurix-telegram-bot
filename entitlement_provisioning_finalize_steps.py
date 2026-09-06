"""Persistence phases for completing a durable free entitlement intent."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

from commerce_models import CommerceError
from connectivity_registry import ConnectivityRegistry
from entitlement_models import OutlineError
from entitlement_support import UTC


def _kind_for_key(intent: dict[str, Any]) -> str:
    if intent["kind"] == "promo":
        return "promo"
    if intent["kind"] == "trial":
        return "trial"
    return "free"


def persist_local_key(
    service: Any,
    repository: Any,
    connection: Any,
    *,
    intent: dict[str, Any],
    remote_id: str,
) -> int:
    """Create or recover the local free-key row for the remote credential."""
    existing = repository.existing_key(connection, str(intent["server_id"]), remote_id)
    if existing is not None:
        if int(existing["telegram_id"]) != int(intent["telegram_id"]):
            raise CommerceError("Outline key is already mapped to another account")
        return int(existing["id"])
    local_key_id = repository.insert_key(
        connection,
        telegram_id=int(intent["telegram_id"]),
        server_id=str(intent["server_id"]),
        outline_key_id=remote_id,
        key_type="monthly_trial" if intent["kind"] in {"trial", "promo"} else "daily_free",
        created_at=str(intent["claim_started_at"]),
        expires_at=(
            datetime.fromisoformat(str(intent["claim_started_at"])).astimezone(UTC)
            + timedelta(days=int(intent["duration_days"]))
        ).isoformat(),
        quota_bytes=int(intent["quota_bytes"]),
    )
    service._adjust_remote_key_count(connection, str(intent["server_id"]), 1)
    return int(local_key_id)


def record_intent_claim(
    repository: Any,
    connection: Any,
    *,
    intent: dict[str, Any],
    local_key_id: int,
    now_text: str,
) -> None:
    """Update the appropriate daily/trial marker or promo campaign claim."""
    if intent["kind"] in {"daily", "trial"}:
        repository.update_user_claim(
            connection,
            kind=str(intent["kind"]),
            telegram_id=int(intent["telegram_id"]),
            claim_started_at=str(intent["claim_started_at"]),
        )
        return
    campaign_code = str(intent["campaign_code"])
    claim = repository.giveaway_claim(
        connection, campaign_code, int(intent["telegram_id"])
    )
    if claim is None:
        repository.insert_giveaway_claim(
            connection,
            campaign_code=campaign_code,
            telegram_id=int(intent["telegram_id"]),
            key_id=local_key_id,
            winner_number=int(intent["winner_number"]),
            claimed_at=str(intent["claim_started_at"]),
        )
        window = repository.giveaway_window(
            connection, campaign_code, str(intent["window_start"])
        )
        if window is None:
            repository.insert_giveaway_window(
                connection, campaign_code, str(intent["window_start"])
            )
        else:
            repository.increment_giveaway_window(
                connection, campaign_code, str(intent["window_start"])
            )
        repository.increment_campaign(connection, campaign_code, now_text)


def bind_and_complete(
    repository: Any,
    connection: Any,
    *,
    intent: dict[str, Any],
    local_key_id: int,
    remote_id: str,
    encrypted_access_url: str,
    now_text: str,
) -> None:
    """Bind the credential and mark the intent done in the same transaction."""
    ConnectivityRegistry.bind_credential(
        connection,
        telegram_id=int(intent["telegram_id"]),
        server_id=str(intent["server_id"]),
        external_id=remote_id,
        secret_ciphertext=encrypted_access_url,
        now_text=now_text,
        profile_kind=_kind_for_key(intent),
    )
    repository.mark_done(
        connection,
        intent_id=str(intent["id"]),
        key_id=local_key_id,
        completed_at=now_text,
    )


def sync_identity_after_finalize(
    service: Any,
    *,
    intent: dict[str, Any],
    local_key_id: int,
    remote_id: str,
    now_text: str,
) -> None:
    """Converge the additive identity view after the legacy commit succeeds."""
    try:
        service._sync_identity_key(
            telegram_id=int(intent["telegram_id"]),
            local_key_id=local_key_id,
            kind=_kind_for_key(intent),
            quota_bytes=int(intent["quota_bytes"]),
            expires_at=(
                datetime.fromisoformat(str(intent["claim_started_at"])).astimezone(UTC)
                + timedelta(days=int(intent["duration_days"]))
            ).isoformat(),
            server_id=str(intent["server_id"]),
            external_id=remote_id,
            now=now_text,
        )
    except Exception as exc:
        print(f"identity free-key sync error: {type(exc).__name__}", file=sys.stderr)
