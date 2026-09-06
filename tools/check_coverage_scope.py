"""Require the coverage report to account for every owned production source."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from tools.source_inventory import production_sources
except ModuleNotFoundError:
    from source_inventory import production_sources


def missing_sources(root: Path, report: dict) -> list[str]:
    measured = set()
    for name in report["files"]:
        path = Path(name)
        if path.is_absolute():
            try:
                path = path.relative_to(root.resolve())
            except ValueError:
                continue
        measured.add(path.as_posix())
    expected = {p.relative_to(root).as_posix() for p in production_sources(root)}
    return sorted(expected - measured)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    missing = missing_sources(root, report)
    if missing:
        print("Coverage omitted production source:\n" + "\n".join(missing))
        return 1
    print("Coverage includes every owned production module.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
