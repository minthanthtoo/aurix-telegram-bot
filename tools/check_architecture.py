#!/usr/bin/env python3
"""Enforce the monolith-reduction ratchet without third-party dependencies."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

try:
    from tools.source_inventory import module_name, production_sources
except ModuleNotFoundError:  # Direct execution: python tools/check_architecture.py
    from source_inventory import module_name, production_sources


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "architecture_baseline.json"


def _load_baseline(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    if baseline.get("schema_version") != 1:
        raise ValueError(
            f"unsupported architecture baseline schema: {baseline.get('schema_version')}"
        )
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
    modules = {module_name(path) for path in trees}
    graph: dict[str, set[str]] = {}
    for path, tree in trees.items():
        current = module_name(path)
        package = current if Path(path).name == "__init__.py" else current.rpartition(".")[0]
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    if node.level > len(parts):
                        continue
                    prefix = ".".join(parts[: len(parts) - node.level + 1])
                    base = ".".join(part for part in (prefix, base) if part)
                candidates = [base]
                candidates.extend(f"{base}.{alias.name}" for alias in node.names)
            else:
                continue
            dependencies.update(name for name in candidates if name in modules and name != current)
        graph[current] = dependencies
    return graph


def _layer_violations(root: Path, trees: dict[str, ast.AST]) -> list[str]:
    """Explicit path classification prevents endpoint migrations escaping the SQL rule."""
    manifest = root / "architecture_layers.json"
    if not manifest.exists():
        return []  # Standalone legacy metric fixtures need no layer manifest.
    policy = json.loads(manifest.read_text(encoding="utf-8"))
    layers = policy["modules"]
    debt = policy.get("sql_debt", {})
    operational = policy.get("operational_persistence", {})
    violations = []
    for path in sorted(set(trees) - set(layers)):
        violations.append(f"unclassified production module: {path}")
    for path in sorted(set(layers) - set(trees)):
        violations.append(f"classified production module is missing: {path}")
    for path, tree in trees.items():
        layer = layers.get(path)
        if layer is None:
            continue
        if layer not in policy["allowed_layers"]:
            violations.append(f"{path}: unknown layer {layer}")
            continue
        calls = _sql_call_count(tree)
        if layer not in {"persistence", "schema"}:
            exception = operational.get(path, {}) if layer == "operations" else {}
            ceiling = exception.get("max_calls", debt.get(path, {}).get("max_calls", 0))
            if calls > ceiling:
                violations.append(
                    f"{path}: SQL outside persistence ({calls} calls; ceiling {ceiling})"
                )
            for imported in _imports(tree):
                if not exception and imported in {"sqlite3", "psycopg", "psycopg2", "psycopg_pool"}:
                    violations.append(
                        f"{path}: database driver import outside persistence: {imported}"
                    )
    by_module = {module_name(path): layer for path, layer in layers.items()}
    rules = policy.get("forbidden_layer_dependencies", {})
    for source, imports in _first_party_graph(trees).items():
        forbidden = rules.get(by_module.get(source), [])
        for imported in sorted(imports):
            if by_module.get(imported) in forbidden:
                violations.append(
                    f"forbidden layer dependency: {source} ({by_module[source]}) -> "
                    f"{imported} ({by_module[imported]})"
                )
    return violations


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

    for path in production_sources(root):
        relative = path.relative_to(root).as_posix()
        try:
            trees[relative] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
                    violations.append(f"{rule['name']}: {path_name} must not import {imported}")

    violations.extend(_layer_violations(root, trees))
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
