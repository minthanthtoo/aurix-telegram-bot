#!/usr/bin/env python3
"""Render entrypoint for the authenticated AuriX VPN portal."""

import sys
from pathlib import Path


# Render invokes this file by path, which otherwise places only ``deploy/`` on
# ``sys.path`` and makes repository-root modules unavailable.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vpn_web_api import main


if __name__ == "__main__":
    raise SystemExit(main())
