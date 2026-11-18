#!/usr/bin/env python3
"""Audit private supplier-to-AuriX mappings without publishing a catalog.

The command reads only a local FazerCards snapshot and an optional local
mapping file. It never contacts the supplier, writes a public artifact, or
changes checkout state. A non-zero exit is available through ``--strict``
when no offer is ready for publication.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aurix_games.mapping_review import review_mapping  # noqa: E402


DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_CONFIG = Path(".local/fazercards/public-catalog-config.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-age-minutes", type=int, default=15)
    parser.add_argument("--strict", action="store_true", help="exit 1 unless at least one offer is ready")
    args = parser.parse_args(argv)
    try:
        report = review_mapping(
            snapshot_path=args.snapshot,
            config_path=args.config,
            max_age_minutes=args.max_age_minutes,
        )
    except (OSError, ValueError, SystemExit) as exc:
        print(json.dumps({"status": "invalid", "writes": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report.get("status") != "ready":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
