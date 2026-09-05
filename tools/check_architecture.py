#!/usr/bin/env python3
"""Enforce the monolith-reduction ratchet without third-party dependencies."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "architecture_baseline.json"


def _load_baseline(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    if baseline.get("schema_version") != 1:
        raise ValueError(f"unsupported architecture baseline schema: {baseline.get('schema_version')}")
    return baseline


def _imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def _matches_forbidden(imported: str, patterns: list[str]) -> bool:
    return any(
        imported.startswith(pattern[:-1]) if pattern.endswith("*") else imported == pattern
        for pattern in patterns
    )


def _function_span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return (node.end_lineno or node.lineno) - node.lineno + 1


def _sql_call_count(tree: ast.AST) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "executescript"}
    )


def _first_party_graph(trees: dict[str, ast.AST]) -> dict[str, set[str]]:
    modules = {Path(path).stem for path in trees}
    return {
        Path(path).stem: _imports(tree).intersection(modules)
        for path, tree in trees.items()
    }


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    found: set[tuple[str, ...]] = set()
    visiting: list[str] = []
    active: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            start = visiting.index(node)
            cycle = visiting[start:]
            rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
            found.add(min(rotations))
            return
        if node in visited:
            return
        active.add(node)
        visiting.append(node)
        for dependency in sorted(graph.get(node, ())):
            visit(dependency)
        visiting.pop()
        active.remove(node)
        visited.add(node)

    for module in sorted(graph):
        visit(module)
    return [list(cycle) for cycle in sorted(found)]


def check_architecture(
    root: Path = ROOT,
    baseline_path: Path = BASELINE_PATH,
) -> list[str]:
    """Return human-readable architecture violations for the supplied checkout."""

    baseline = _load_baseline(baseline_path)
    violations: list[str] = []
    trees: dict[str, ast.AST] = {}

    for path in sorted(root.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            trees[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            violations.append(f"{path.name}: cannot inspect invalid syntax: {error}")

    for path_name, limits in baseline["hotspots"].items():
        path = root / path_name
        if not path.is_file():
            violations.append(f"{path_name}: baseline hotspot is missing")
            continue
        source = path.read_text(encoding="utf-8")
        tree = trees.get(path_name)
        if tree is None:
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                continue
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        actual = {
            "max_lines": len(source.splitlines()),
            "max_function_lines": max((_function_span(node) for node in functions), default=0),
            "max_sql_calls": _sql_call_count(tree),
        }
        for metric, ceiling in limits.items():
            if actual[metric] > ceiling:
                violations.append(
                    f"{path_name}: {metric} grew from ceiling {ceiling} to {actual[metric]}; "
                    "extract responsibility or deliberately revise the baseline"
                )

    for rule in baseline["dependency_rules"]:
        forbidden = rule["forbidden_prefixes"]
        for path_name in rule["sources"]:
            tree = trees.get(path_name)
            if tree is None:
                violations.append(f"{rule['name']}: source module {path_name} is missing")
                continue
            for imported in sorted(_imports(tree)):
                if _matches_forbidden(imported, forbidden):
                    violations.append(
                        f"{rule['name']}: {path_name} must not import {imported}"
                    )

    for cycle in _cycles(_first_party_graph(trees)):
        violations.append("first-party import cycle: " + " -> ".join(cycle + [cycle[0]]))

    return violations


def main() -> int:
    violations = check_architecture()
    if violations:
        print("Architecture guard failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Architecture guard passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
