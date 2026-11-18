#!/usr/bin/env python3
"""Import a private FazerCards inventory snapshot into a blocked Games catalog.

This command is offline-only. It does not contact FazerCards and prints only
aggregate counts; supplier IDs, names, recipient schemas, and wholesale prices
remain in a mode-0600 SQLite database under a mode-0700 parent directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aurix_games.catalog_repository import CatalogImportError, GameCatalogSnapshotDatabase


DEFAULT_SNAPSHOT = Path(".local/fazercards/catalog-20260921.json")
DEFAULT_DATABASE = Path(".local/fazercards/aurix-game-catalog.sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        summary = GameCatalogSnapshotDatabase(args.database).import_snapshot(args.snapshot)
    except CatalogImportError as error:
        # CatalogImportError intentionally contains no upstream body, identifiers,
        # prices, or paths. Do not print exception chains for local private data.
        parser.error(str(error))
    print(json.dumps(summary.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
