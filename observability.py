"""Small, secret-safe latency logging shared by external adapters."""

from __future__ import annotations

import os
import sys
import time
from typing import Any


def latency_log(event: str, started_at: float, **fields: Any) -> None:
    """Emit a bounded timing record only when explicitly enabled."""
    if os.environ.get("AURIX_LATENCY_LOG", "0").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"latency event={event} duration_ms={duration_ms:.1f}{suffix}", file=sys.stderr)
