"""Credential-generation and bounded lease lifecycle handlers."""

from __future__ import annotations

from identity_generations_repository import IdentityGenerationsRepository
from identity_support import IdentityError, _now_text, _parse_time


class IdentityGenerationsMixin:
    generation_repository = IdentityGenerationsRepository()

    def ensure_generation_for_credential(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        """Converge one entitlement onto its current credential generation."""
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        credential_id = str(credential_id or "").strip()
        if not credential_id:
            raise IdentityError("credential id is required")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.generation_repository.ensure_generation_for_credential(
                connection,
                entitlement_id=str(entitlement_id),
                endpoint_id=str(endpoint_id),
                credential_id=credential_id,
                status=status,
                timestamp=timestamp,
            )

    def _active_lease_for_generation(self, generation_id: str) -> str | None:
        with self.database.connect() as connection:
            return self.generation_repository.active_lease_for_generation(
                connection, str(generation_id)
            )

    def ensure_generation_lease(
        self,
        entitlement_id: str,
        generation_id: str,
        endpoint_id: str,
        quota_bytes: int,
        entitlement_expires_at: str,
        *,
        now: str | None = None,
    ) -> str:
        """Reserve a bounded first quota block for a live generation."""
        existing = self._active_lease_for_generation(generation_id)
        if existing is not None:
            return existing
        current = _parse_time(str(now or _now_text()))
        expiry = _parse_time(entitlement_expires_at)
        ttl = max(30, min(2_592_000, int((expiry - current).total_seconds())))
        lease_bytes = min(int(quota_bytes), 10 * 1024 * 1024 * 1024)
        return self.grant_lease(
            entitlement_id,
            endpoint_id,
            lease_bytes=lease_bytes,
            generation_id=generation_id,
            ttl_seconds=ttl,
            now=current.isoformat(),
        )

    def create_generation(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        credential_id: str | None = None,
        route_id: str | None = None,
        status: str = "active",
        now: str | None = None,
    ) -> str:
        if status not in {"pending", "active", "revoked", "failed"}:
            raise IdentityError("credential generation status is invalid")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.generation_repository.create_generation(
                connection,
                entitlement_id=str(entitlement_id),
                endpoint_id=str(endpoint_id),
                credential_id=credential_id,
                route_id=route_id,
                status=status,
                timestamp=timestamp,
            )

    def revoke_generation(self, generation_id: str, *, now: str | None = None) -> bool:
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.generation_repository.revoke_generation(
                connection, str(generation_id), timestamp
            )

    def grant_lease(
        self,
        entitlement_id: str,
        endpoint_id: str,
        *,
        lease_bytes: int,
        generation_id: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> str:
        if int(lease_bytes) <= 0 or int(lease_bytes) > 10 * 1024 * 1024 * 1024:
            raise IdentityError("lease size is invalid")
        if not 30 <= int(ttl_seconds) <= 2_592_000:
            raise IdentityError("lease TTL is outside the allowed range")
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.generation_repository.grant_lease(
                connection,
                entitlement_id=str(entitlement_id),
                endpoint_id=str(endpoint_id),
                lease_bytes=int(lease_bytes),
                generation_id=str(generation_id) if generation_id is not None else None,
                ttl_seconds=int(ttl_seconds),
                timestamp=timestamp,
            )

    def _mark_entitlement_exhausted_locked(
        self,
        connection,
        entitlement_id: str,
        *,
        now: str,
        generation_id: str | None = None,
        endpoint_id: str | None = None,
        epoch_id: str | None = None,
        reason: str = "aggregate_quota",
    ) -> bool:
        """Revoke every route when an entitlement reaches its aggregate cap."""
        return self.generation_repository.mark_entitlement_exhausted(
            connection,
            str(entitlement_id),
            now=str(now),
            generation_id=generation_id,
            endpoint_id=endpoint_id,
            epoch_id=epoch_id,
            reason=reason,
        )

    def release_lease(
        self, lease_id: str, *, now: str | None = None, reason: str = "released"
    ) -> bool:
        """Release unconsumed reservation while retaining its usage history."""
        timestamp = str(now or _now_text())
        with self.database.connect() as connection:
            self.database.begin_write(connection)
            return self.generation_repository.release_lease(
                connection, str(lease_id), timestamp=timestamp, reason=reason
            )
