import json
import tempfile
import unittest
from pathlib import Path

from tools.check_architecture import check_architecture


class ArchitectureGuardTest(unittest.TestCase):
    def test_monolith_reduction_ratchet(self):
        violations = check_architecture()
        self.assertEqual([], violations, "\n" + "\n".join(violations))

    def test_guard_reports_growth_for_each_metric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.py").write_text(
                "def oversized():\n"
                "    connection.execute('SELECT 1')\n"
                "    return True\n",
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
            self.assertTrue(any(item.startswith("first-party import cycle:") for item in violations))

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
