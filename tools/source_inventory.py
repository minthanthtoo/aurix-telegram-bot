"""Discover owned Python source without traversing environments or generated data."""

from __future__ import annotations

import os
from pathlib import Path


EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "project-venv",
        "__pycache__",
        "node_modules",
        "graphify-out",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
    }
)


def production_sources(root: Path) -> list[Path]:
    """Include unimported production modules and deploy tools, excluding tests."""
    sources = []
    for directory, children, filenames in os.walk(root, followlinks=False):
        children[:] = sorted(
            name
            for name in children
            if name not in EXCLUDED_DIRECTORIES
            and name not in {"tests", "fixtures"}
            and not name.startswith(".")
            and not (Path(directory) / name).is_symlink()
            and not (Path(directory) / name / "pyvenv.cfg").exists()
        )
        for name in sorted(filenames):
            path = Path(directory) / name
            if name.endswith(".py") and not name.startswith("test_") and not path.is_symlink():
                sources.append(path)
    return sorted(sources)


def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)
