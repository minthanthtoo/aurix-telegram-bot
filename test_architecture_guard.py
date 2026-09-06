import json
import tempfile
import unittest
from pathlib import Path

from tools.check_architecture import check_architecture


class ArchitectureGuardTest(unittest.TestCase):
    def test_layer_rule_catches_nested_aliased_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "domain").mkdir()
            (root / "adapters").mkdir()
            (root / "domain" / "policy.py").write_text("from adapters import store as backend\n")
            (root / "adapters" / "store.py").write_text("VALUE = 1\n")
            (root / "architecture_layers.json").write_text(json.dumps({
                "allowed_layers": ["domain", "persistence"],
                "modules": {"domain/policy.py": "domain", "adapters/store.py": "persistence"},
                "forbidden_layer_dependencies": {"domain": ["persistence"]},
            }))
            self.assertIn(
                "forbidden layer dependency: domain.policy (domain) -> adapters.store (persistence)",
                check_architecture(root, self._write_baseline(root)),
            )

    def test_nested_relative_import_cycles_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "feature"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "a.py").write_text("from . import b\n")
            (package / "b.py").write_text("from .a import operation\n")
            violations = check_architecture(root, self._write_baseline(root))
            self.assertTrue(any("feature.a -> feature.b -> feature.a" in v for v in violations))

    def test_endpoint_migration_name_does_not_exempt_application_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "application"
            package.mkdir()
            (package / "endpoint_migration.py").write_text(
                "def move(connection):\n    connection.execute('SELECT 1')\n"
            )
            (root / "schema_migration.py").write_text(
                "def upgrade(connection):\n    connection.execute('CREATE TABLE example(id INTEGER)')\n"
            )
            (root / "architecture_layers.json").write_text(
                json.dumps(
                    {
                        "allowed_layers": ["application", "schema"],
                        "modules": {
                            "application/endpoint_migration.py": "application",
                            "schema_migration.py": "schema",
                        },
                    }
                )
            )
            violations = check_architecture(root, self._write_baseline(root))
            self.assertEqual(
                ["application/endpoint_migration.py: SQL outside persistence (1 calls; ceiling 0)"],
                violations,
            )

    def test_new_production_module_requires_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new_feature.py").write_text("VALUE = 1\n")
            (root / "architecture_layers.json").write_text(
                json.dumps(
                    {
                        "allowed_layers": ["application"],
                        "modules": {},
                    }
                )
            )
            self.assertIn(
                "unclassified production module: new_feature.py",
                check_architecture(root, self._write_baseline(root)),
            )

    def test_monolith_reduction_ratchet(self):
        violations = check_architecture()
        self.assertEqual([], violations, "\n" + "\n".join(violations))

    def test_guard_reports_growth_for_each_metric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.py").write_text(
                "def oversized():\n    connection.execute('SELECT 1')\n    return True\n",
                encoding="utf-8",
            )
            baseline_path = self._write_baseline(
                root,
                hotspots={
                    "large.py": {
                        "max_lines": 2,
                        "max_function_lines": 2,
                        "max_sql_calls": 0,
                    }
                },
            )
            violations = check_architecture(root, baseline_path)
            self.assertTrue(any("max_lines grew" in item for item in violations))
            self.assertTrue(any("max_function_lines grew" in item for item in violations))
            self.assertTrue(any("max_sql_calls grew" in item for item in violations))

    def test_guard_reports_forbidden_import_and_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "domain.py").write_text(
                "import telegram_transport\nimport helper\n",
                encoding="utf-8",
            )
            (root / "helper.py").write_text("import domain\n", encoding="utf-8")
            baseline_path = self._write_baseline(
                root,
                dependency_rules=[
                    {
                        "name": "domain-isolation",
                        "sources": ["domain.py"],
                        "forbidden_prefixes": ["telegram_*"],
                    }
                ],
            )
            violations = check_architecture(root, baseline_path)
            self.assertIn(
                "domain-isolation: domain.py must not import telegram_transport",
                violations,
            )
            self.assertTrue(
                any(item.startswith("first-party import cycle:") for item in violations)
            )

    @staticmethod
    def _write_baseline(
        root: Path,
        *,
        hotspots: dict | None = None,
        dependency_rules: list[dict] | None = None,
    ) -> Path:
        path = root / "architecture_baseline.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hotspots": hotspots or {},
                    "dependency_rules": dependency_rules or [],
                }
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
