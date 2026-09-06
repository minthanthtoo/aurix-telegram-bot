import tempfile
import unittest
from pathlib import Path

from tools.check_coverage_scope import missing_sources


class CoverageScopeTest(unittest.TestCase):
    def test_unimported_nested_production_code_must_be_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "feature").mkdir()
            (root / "feature" / "unused.py").write_text("VALUE = 1\n")
            (root / "test_feature.py").write_text("VALUE = 2\n")
            self.assertEqual(missing_sources(root, {"files": {}}), ["feature/unused.py"])
            self.assertEqual(missing_sources(root, {"files": {"feature/unused.py": {}}}), [])

    def test_environments_and_generated_sources_are_not_owned_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ["graphify-out", "custom-environment"]:
                path = root / folder
                path.mkdir()
                (path / "generated.py").write_text("VALUE = 1\n")
            (root / "custom-environment" / "pyvenv.cfg").write_text("home = unused\n")
            self.assertEqual(missing_sources(root, {"files": {}}), [])
