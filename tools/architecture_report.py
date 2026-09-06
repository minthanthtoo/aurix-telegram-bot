"""Emit revision-labelled architecture evidence using the same inventory as CI."""

from __future__ import annotations

import argparse
import ast
import json
import platform
import subprocess
from pathlib import Path

try:
    from tools.source_inventory import module_name, production_sources
    from tools.check_architecture import _first_party_graph, _sql_call_count, check_architecture
except ModuleNotFoundError:
    from source_inventory import module_name, production_sources
    from check_architecture import _first_party_graph, _sql_call_count, check_architecture


def report(root: Path) -> dict:
    layers = json.loads((root / "architecture_layers.json").read_text())["modules"]
    trees = {}
    metrics = {}
    for path in production_sources(root):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        trees[relative] = tree
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        longest = max(functions, key=lambda n: n.end_lineno - n.lineno, default=None)
        metrics[relative] = {
            "layer": layers.get(relative, "unclassified"),
            "lines": len(source.splitlines()),
            "longest_function": None if longest is None else longest.name,
            "longest_function_lines": 0 if longest is None else longest.end_lineno - longest.lineno + 1,
            "sql_calls": _sql_call_count(tree),
            "attribute_forwarders": [n.name for n in functions if n.name in {"__getattr__", "__setattr__"}],
        }
    graph = _first_party_graph(trees)
    for path, values in metrics.items():
        values["imports"] = sorted(graph[module_name(path)])
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    changes = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).splitlines()
    return {
        "schema_version": 1,
        "revision": revision,
        "working_tree_changed": bool(changes),
        "python": platform.python_version(),
        "source_count": len(metrics),
        "application_sql_calls": sum(v["sql_calls"] for v in metrics.values()
                                     if v["layer"] in {"application", "worker", "transport", "domain", "contract"}),
        "violations": check_architecture(root),
        "modules": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = report(Path(__file__).resolve().parents[1])
    encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 1 if data["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
