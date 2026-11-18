#!/usr/bin/env python3
"""Run the scoped Games unit suites with local fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__:
    from .game_store_scope import is_game_test
else:
    from game_store_scope import is_game_test


ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_PYTHON = Path(
    "/Users/min/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)

def _python() -> str:
    override = os.environ.get("AURIX_GAME_PYTHON", "").strip()
    if override:
        return override
    if _BUNDLED_PYTHON.is_file():
        return str(_BUNDLED_PYTHON)
    return sys.executable


def _run(command: list[str]) -> int:
    print("$ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode


def main() -> int:
    python = _python()
    node = os.environ.get("AURIX_GAME_NODE", "").strip() or shutil.which("node")
    if not node:
        print("node executable not found; set AURIX_GAME_NODE", file=sys.stderr)
        return 2
    tests = sorted(path for path in ROOT.glob("test_*") if path.is_file() and is_game_test(path.name))
    python_tests = [path.stem for path in tests if path.suffix == ".py"]
    node_tests = [path.name for path in tests if path.suffix == ".mjs"]
    if not python_tests or not node_tests:
        print("Missing Games Python or Node test suite", file=sys.stderr)
        return 2
    result = _run([python, "-m", "unittest", "-q", *python_tests])
    if result:
        return result
    result = _run([node, "--test", *node_tests])
    if result:
        return result
    return _run([python, "-m", "compileall", "-q", "aurix_games"])


if __name__ == "__main__":
    raise SystemExit(main())
