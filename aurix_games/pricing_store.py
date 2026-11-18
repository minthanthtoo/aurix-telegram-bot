"""Private, atomic pricing policy inside the shared catalog configuration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import os
import re
import stat
import tempfile
from typing import Any

from .cbm_fx import FXRateUnavailable, MMT, SOURCE_ID, SOURCE_LABEL, SOURCE_URL, fetch_market_rate
from .pricing import PricingPolicy


class PricingPolicyStoreError(ValueError):
    """The private pricing-policy artifact is unsafe, unapproved or malformed."""


UTC = timezone.utc
_ACTOR = re.compile(r"staff:[A-Za-z0-9._-]{1,64}\Z")
AUTO_FX_MAX_AGE = timedelta(hours=96)
AUTO_FX_RECHECK = timedelta(hours=6)
AUTO_FX_RETRY = timedelta(minutes=15)


def catalog_config_revision(config: dict[str, Any]) -> str:
    """Bind prices and routes to the exact private configuration used to build them."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _instant(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise PricingPolicyStoreError(f"{label} is required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingPolicyStoreError(f"{label} is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise PricingPolicyStoreError(f"{label} must include a timezone")
    return result.astimezone(UTC)


class PricingPolicyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_config(self, *, create: bool = False) -> dict[str, Any]:
        if self.path.is_symlink():
            raise PricingPolicyStoreError("pricing configuration must not be a symlink")
        if not self.path.exists():
            if create:
                return {"schema_version": 1, "mappings": []}
            raise PricingPolicyStoreError("pricing policy is not configured")
        try:
            if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise PricingPolicyStoreError("pricing configuration directory must be private")
            if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise PricingPolicyStoreError("pricing configuration must be private")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PricingPolicyStoreError("pricing configuration is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("mappings", []), list):
            raise PricingPolicyStoreError("pricing configuration is invalid")
        return payload

    def _fx_state_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.fx-state.json")

    def _read_fx_state(self) -> dict[str, Any]:
        path = self._fx_state_path()
        if path.is_symlink():
            raise PricingPolicyStoreError("FX observation state must not be a symlink")
        if not path.exists():
            return {}
        try:
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise PricingPolicyStoreError("FX observation state must be private")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PricingPolicyStoreError("FX observation state is invalid") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("source_id") != SOURCE_ID:
            raise PricingPolicyStoreError("FX observation state is invalid")
        return value

    def _write_fx_state(self, state: dict[str, Any]) -> None:
        path = self._fx_state_path()
        parent = self.path.parent
        if path.is_symlink() or stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise PricingPolicyStoreError("FX observation storage is not private")
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
            temporary = Path(name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(state, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise PricingPolicyStoreError("FX observation could not be saved") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _write_config(self, payload: dict[str, Any]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise PricingPolicyStoreError("pricing configuration directory must be private")
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
            temporary = Path(name)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise PricingPolicyStoreError("pricing policy could not be saved") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _automatic_settings(payload: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
        raw = payload.get("automatic_fx")
        if raw is None:
            return None
        expected = {"enabled", "source_id", "floor_rate", "max_age_hours", "approved_by", "approved_at", "owner_approved"}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise PricingPolicyStoreError("automatic FX settings are invalid")
        if raw.get("enabled") is not True or raw.get("source_id") != SOURCE_ID or raw.get("owner_approved") is not True:
            raise PricingPolicyStoreError("automatic FX approval is invalid")
        actor = raw.get("approved_by")
        if not isinstance(actor, str) or not _ACTOR.fullmatch(actor):
            raise PricingPolicyStoreError("automatic FX reviewer is invalid")
        approved_at = _instant(raw.get("approved_at"), "automatic FX approval time")
        if approved_at > now:
            raise PricingPolicyStoreError("automatic FX approval time is in the future")
        if raw.get("max_age_hours") != int(AUTO_FX_MAX_AGE.total_seconds() // 3600):
            raise PricingPolicyStoreError("automatic FX maximum age is invalid")
        try:
            floor_rate = Decimal(str(raw.get("floor_rate")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PricingPolicyStoreError("automatic FX floor is invalid") from exc
        if not floor_rate.is_finite() or floor_rate < Decimal("100") or floor_rate > Decimal("100000"):
            raise PricingPolicyStoreError("automatic FX floor is outside the supported range")
        return {**raw, "approved_at": approved_at.isoformat(), "floor_rate": format(floor_rate, "f")}

    def _automatic_status(self, config: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
        settings = self._automatic_settings(config, now=now)
        if settings is None:
            return None
        state = self._read_fx_state()
        observation = state.get("observation")
        base_policy = PricingPolicy.from_complete_mapping(config.get("policy"))
        result: dict[str, Any] = {
            "enabled": True,
            "source_id": SOURCE_ID,
            "source": SOURCE_LABEL,
            "floor_rate": settings["floor_rate"],
            "max_age_hours": settings["max_age_hours"],
            "last_attempt_at": state.get("last_attempt_at"),
            "last_error": state.get("last_error"),
            "status": "awaiting_rate",
        }
        if not isinstance(observation, dict):
            return {"state": "awaiting_rate", "settings": result, "policy": base_policy, "approval": None, "expiry": None}
        expected = {"source_id", "source_url", "rate", "market_date", "observed_at", "fetched_at"}
        if set(observation) != expected or observation.get("source_id") != SOURCE_ID or observation.get("source_url") != SOURCE_URL:
            raise PricingPolicyStoreError("automatic FX observation is invalid")
        try:
            rate = Decimal(str(observation["rate"]))
            observed_at = _instant(observation["observed_at"], "FX observation time")
            fetched_at = _instant(observation["fetched_at"], "FX fetch time")
            market_date = date.fromisoformat(str(observation["market_date"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PricingPolicyStoreError("automatic FX observation is invalid") from exc
        expected_observation = datetime.combine(market_date, time.min, tzinfo=MMT).astimezone(timezone.utc)
        if (not rate.is_finite() or rate < Decimal("100") or rate > Decimal("100000")
                or observed_at > now or fetched_at > now or fetched_at < observed_at
                or observed_at != expected_observation):
            raise PricingPolicyStoreError("automatic FX observation is outside its valid range")
        expiry = observed_at + AUTO_FX_MAX_AGE
        effective_rate = max(Decimal(settings["floor_rate"]), rate)
        effective_policy = replace(base_policy, fx_rate=effective_rate)
        approval = {
            "approved_by": settings["approved_by"],
            "approved_at": settings["approved_at"],
            "fx_source": SOURCE_LABEL,
            "fx_observed_at": observed_at.isoformat(),
            "valid_until": expiry.isoformat(),
        }
        result.update({
            "market_rate": format(rate, "f"),
            "market_date": market_date.isoformat(),
            "observed_at": observed_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "effective_rate": format(effective_rate, "f"),
            "valid_until": expiry.isoformat(),
            "status": "approved" if expiry > now else "expired",
        })
        return {
            "state": "approved" if expiry > now else "expired",
            "settings": result,
            "policy": effective_policy,
            "approval": approval,
            "expiry": expiry,
            "observation": observation,
        }

    @staticmethod
    def _approval(payload: dict[str, Any], *, now: datetime) -> tuple[str, dict[str, Any] | None]:
        raw = payload.get("pricing_policy_approval")
        policy = payload.get("policy")
        if not isinstance(raw, dict) or not isinstance(policy, dict):
            return "unconfigured", None
        actor = raw.get("approved_by")
        source = raw.get("fx_source")
        if (
            raw.get("status") != "approved"
            or not isinstance(actor, str)
            or not _ACTOR.fullmatch(actor)
            or not isinstance(source, str)
            or not source.strip()
            or len(source.strip()) > 240
            or any(ord(ch) < 32 for ch in source)
        ):
            raise PricingPolicyStoreError("pricing approval record is invalid")
        approved_at = _instant(raw.get("approved_at"), "approval time")
        observed_at = _instant(raw.get("fx_observed_at"), "FX observation time")
        valid_until = _instant(raw.get("valid_until"), "pricing expiry")
        if approved_at > now or observed_at > now:
            raise PricingPolicyStoreError("pricing approval contains a future timestamp")
        if valid_until <= observed_at:
            raise PricingPolicyStoreError("pricing expiry must follow the FX observation")
        approval = {
            "approved_by": actor,
            "approved_at": approved_at.isoformat(),
            "fx_source": source.strip(),
            "fx_observed_at": observed_at.isoformat(),
            "valid_until": valid_until.isoformat(),
        }
        if valid_until <= now:
            return "expired", approval
        return "approved", approval

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return the operator-safe state without turning missing policy into defaults."""

        try:
            config = self._read_config()
        except PricingPolicyStoreError as exc:
            if str(exc) == "pricing policy is not configured":
                return {"policy_status": "unconfigured", "policy": None, "approval": None}
            raise
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PricingPolicyStoreError("current time must include a timezone")
        current = current.astimezone(UTC)
        automatic = self._automatic_status(config, now=current)
        if automatic is not None:
            return {
                "policy_status": automatic["state"],
                "policy": automatic["policy"].as_dict(),
                "approval": automatic["approval"],
                "automatic_fx": automatic["settings"],
            }
        policy_payload = config.get("policy")
        approval_state, approval = self._approval(config, now=current)
        if approval_state == "unconfigured":
            return {"policy_status": approval_state, "policy": None, "approval": None, "automatic_fx": {"enabled": False}}
        try:
            policy = PricingPolicy.from_complete_mapping(policy_payload)
        except (TypeError, ValueError) as exc:
            raise PricingPolicyStoreError("approved pricing policy is incomplete or invalid") from exc
        return {
            "policy_status": approval_state,
            "policy": policy.as_dict(),
            "approval": approval,
            "automatic_fx": {"enabled": False},
        }

    def load(self, *, now: datetime | None = None) -> PricingPolicy:
        status = self.status(now=now)
        if status["policy_status"] != "approved":
            raise PricingPolicyStoreError("a current approved pricing policy is required")
        return PricingPolicy.from_complete_mapping(status["policy"])

    def approved_snapshot(
        self,
        *,
        now: datetime | None = None,
        allow_expired: bool = False,
    ) -> tuple[dict[str, Any], PricingPolicy, datetime]:
        """Read one approved configuration version; catalog callers may retain its last rate."""
        if type(allow_expired) is not bool:
            raise PricingPolicyStoreError("allow_expired must be boolean")
        config = self._read_config()
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PricingPolicyStoreError("current time must include a timezone")
        current = current.astimezone(UTC)
        automatic = self._automatic_status(config, now=current)
        if automatic is not None:
            if automatic["state"] != "approved" and not (
                allow_expired and automatic["state"] == "expired"
            ):
                raise PricingPolicyStoreError("automatic FX quote is missing or expired")
            policy = automatic["policy"]
            revision_config = dict(config)
            revision_config["policy"] = policy.as_dict()
            revision_config["pricing_policy_approval"] = automatic["approval"]
            revision_config["automatic_fx_observation"] = automatic["observation"]
            return revision_config, policy, automatic["expiry"]
        state, approval = self._approval(config, now=current)
        if state != "approved" and not (allow_expired and state == "expired"):
            raise PricingPolicyStoreError("a current approved pricing policy is required")
        policy = PricingPolicy.from_complete_mapping(config["policy"])
        return config, policy, _instant(approval["valid_until"], "pricing expiry")

    def refresh_auto_fx_if_due(self, *, now: datetime | None = None, fetcher=None) -> dict[str, Any]:
        """Refresh an approved auto-FX observation at most every six hours."""

        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PricingPolicyStoreError("current time must include a timezone")
        current = current.astimezone(UTC)
        config = self._read_config()
        settings = self._automatic_settings(config, now=current)
        if settings is None:
            return {"status": "disabled"}
        state = self._read_fx_state()
        auto = self._automatic_status(config, now=current)
        if auto and auto["state"] == "approved":
            fetched = _instant(auto["settings"]["fetched_at"], "FX fetch time")
            if current - fetched < AUTO_FX_RECHECK:
                return {"status": "fresh", "market_date": auto["settings"]["market_date"],
                        "effective_rate": auto["settings"]["effective_rate"]}
        last_attempt = state.get("last_attempt_at")
        if isinstance(last_attempt, str) and current - _instant(last_attempt, "FX attempt time") < AUTO_FX_RETRY:
            return {"status": "retry_deferred"}
        state = {"schema_version": 1, "source_id": SOURCE_ID,
                 "last_attempt_at": current.isoformat(), "observation": state.get("observation")}
        self._write_fx_state(state)
        try:
            quote = (fetcher or fetch_market_rate)(now=current)
        except FXRateUnavailable as exc:
            state["last_error"] = "source_unavailable"
            self._write_fx_state(state)
            if auto and auto["state"] == "approved":
                return {"status": "using_cached_rate"}
            raise PricingPolicyStoreError("automatic FX source is unavailable") from exc
        except Exception as exc:
            state["last_error"] = "source_unavailable"
            self._write_fx_state(state)
            if auto and auto["state"] == "approved":
                return {"status": "using_cached_rate"}
            raise PricingPolicyStoreError("automatic FX source is unavailable") from exc
        if getattr(quote, "source_id", None) != SOURCE_ID or getattr(quote, "source_url", None) != SOURCE_URL:
            state["last_error"] = "source_identity_invalid"
            self._write_fx_state(state)
            raise PricingPolicyStoreError("automatic FX source identity is invalid")
        rate = getattr(quote, "rate", None)
        observed_at = getattr(quote, "observed_at", None)
        fetched_at = getattr(quote, "fetched_at", None)
        market_date = getattr(quote, "market_date", None)
        if (not isinstance(rate, Decimal) or not rate.is_finite() or rate < Decimal("100") or rate > Decimal("100000")
                or not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None
                or not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None or fetched_at.utcoffset() is None
                or not isinstance(market_date, date) or observed_at.astimezone(UTC) > current
                or current - observed_at.astimezone(UTC) > AUTO_FX_MAX_AGE
                or fetched_at.astimezone(UTC) > current or fetched_at.astimezone(UTC) < observed_at.astimezone(UTC)
                or observed_at.astimezone(UTC) != datetime.combine(market_date, time.min, tzinfo=MMT).astimezone(UTC)):
            state["last_error"] = "source_quote_invalid_or_stale"
            self._write_fx_state(state)
            if auto and auto["state"] == "approved":
                return {"status": "using_cached_rate"}
            raise PricingPolicyStoreError("automatic FX quote is invalid or older than 96 hours")
        observation = {
            "source_id": SOURCE_ID,
            "source_url": SOURCE_URL,
            "rate": format(rate, "f"),
            "market_date": market_date.isoformat(),
            "observed_at": observed_at.astimezone(UTC).isoformat(),
            "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        }
        self._write_fx_state({"schema_version": 1, "source_id": SOURCE_ID,
                              "last_attempt_at": current.isoformat(), "last_error": None,
                              "observation": observation})
        refreshed = self.status(now=current)
        return {"status": refreshed["policy_status"], "automatic_fx": refreshed["automatic_fx"]}

    def save(
        self,
        policy: PricingPolicy,
        *,
        approved_by: str,
        fx_source: str,
        fx_observed_at: str,
        valid_until: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(policy, PricingPolicy):
            raise PricingPolicyStoreError("pricing policy has unsupported type")
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PricingPolicyStoreError("current time must include a timezone")
        current = current.astimezone(UTC)
        if not isinstance(approved_by, str) or not _ACTOR.fullmatch(approved_by):
            raise PricingPolicyStoreError("named pricing reviewer is invalid")
        if not isinstance(fx_source, str) or not fx_source.strip() or len(fx_source.strip()) > 240:
            raise PricingPolicyStoreError("FX source is required")
        if any(ord(ch) < 32 for ch in fx_source):
            raise PricingPolicyStoreError("FX source is invalid")
        observed_at = _instant(fx_observed_at, "FX observation time")
        expiry = _instant(valid_until, "pricing expiry")
        if observed_at > current or expiry <= observed_at or expiry <= current:
            raise PricingPolicyStoreError("FX observation or pricing expiry is outside its valid window")

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise PricingPolicyStoreError("pricing configuration directory must be private")
        payload = self._read_config(create=True)
        payload["policy"] = policy.as_dict()
        payload.pop("automatic_fx", None)
        payload["pricing_policy_approval"] = {
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": current.isoformat(),
            "fx_source": fx_source.strip(),
            "fx_observed_at": observed_at.isoformat(),
            "valid_until": expiry.isoformat(),
        }
        self._write_config(payload)
        return self.status(now=current)

    def save_automatic(
        self,
        policy: PricingPolicy,
        *,
        approved_by: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Approve a CBM market-rate rule once; subsequent observations are automatic."""

        if not isinstance(policy, PricingPolicy):
            raise PricingPolicyStoreError("pricing policy has unsupported type")
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise PricingPolicyStoreError("current time must include a timezone")
        current = current.astimezone(UTC)
        if not isinstance(approved_by, str) or not _ACTOR.fullmatch(approved_by):
            raise PricingPolicyStoreError("named pricing reviewer is invalid")
        floor = Decimal(str(policy.fx_rate))
        if not floor.is_finite() or floor < Decimal("100") or floor > Decimal("100000"):
            raise PricingPolicyStoreError("automatic FX floor is outside the supported range")
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise PricingPolicyStoreError("pricing configuration directory must be private")
        payload = self._read_config(create=True)
        payload["policy"] = policy.as_dict()
        payload["automatic_fx"] = {
            "enabled": True,
            "source_id": SOURCE_ID,
            "floor_rate": format(floor, "f"),
            "max_age_hours": int(AUTO_FX_MAX_AGE.total_seconds() // 3600),
            "approved_by": approved_by,
            "approved_at": current.isoformat(),
            "owner_approved": True,
        }
        self._write_config(payload)
        return self.status(now=current)


__all__ = ["PricingPolicyStore", "PricingPolicyStoreError", "catalog_config_revision"]
