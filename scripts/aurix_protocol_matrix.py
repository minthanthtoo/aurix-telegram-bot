#!/usr/bin/env python3
"""Run a bounded local mixed-protocol compatibility matrix.

This command does not contact or mutate a real VPN server.  Use it for
controller/node-agent regression evidence before the separately approved live
canary, speed, and soak gates.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Keep the documented ``python scripts/aurix_protocol_matrix.py`` invocation
# equivalent to ``python -m`` when launched from the repository root or from a
# different working directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from aurix_vpn.protocol_matrix import ProtocolMatrixRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--customers-per-protocol",
        type=int,
        default=8,
        help="bounded customer cohort for each protocol (1-200)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="bounded concurrent worker count (1-64)",
    )
    parser.add_argument(
        "--quota-bytes",
        type=int,
        default=1_000_000,
        help="per-customer quota used by the local contract (positive integer)",
    )
    args = parser.parse_args(argv)
    try:
        report = ProtocolMatrixRunner(
            customers_per_protocol=args.customers_per_protocol,
            workers=args.workers,
            quota_bytes=args.quota_bytes,
        ).run()
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
