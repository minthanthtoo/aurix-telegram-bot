"""Public UTC serialization shared by application contracts."""

from datetime import datetime, timezone


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
