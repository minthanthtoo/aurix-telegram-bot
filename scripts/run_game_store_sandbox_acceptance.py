#!/usr/bin/env python3
"""Run the provider-free AuriX Game Store acceptance workflow.

This command is intentionally safe to run during supplier-key rotation.  It
uses temporary local stores and a synthetic supplier; it never calls the
FazerCards API, sends money, or enables checkout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.sandbox_acceptance import run_sandbox_acceptance  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="optional directory for local SQLite evidence (defaults to a temporary directory)",
    )
    args = parser.parse_args()
    report = run_sandbox_acceptance(root=args.root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
