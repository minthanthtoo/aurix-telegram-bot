#!/usr/bin/env python3
"""Run the tracked non-Games unittest suite without importing Games tests."""

from pathlib import Path
import subprocess
import sys
import unittest

from scripts.game_store_scope import is_game_test


ROOT = Path(__file__).resolve().parents[1]


def product_test_modules(paths: list[str]) -> list[str]:
    tracked = set(paths)
    modules = []
    for path in paths:
        parts = Path(path).parts
        if not parts or not parts[-1].startswith("test_") or Path(path).suffix != ".py" or is_game_test(path):
            continue
        if any("/".join((*parts[:index], "__init__.py")) not in tracked for index in range(1, len(parts))):
            continue
        modules.append(".".join(Path(path).with_suffix("").parts))
    return sorted(modules)


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    modules = product_test_modules([path for path in paths if path])
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestLoader().loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
