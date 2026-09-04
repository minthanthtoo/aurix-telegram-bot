from __future__ import annotations

import unittest
from pathlib import Path


class NodeBootstrapSecurityTests(unittest.TestCase):
    def test_management_canaries_pin_the_installer_certificate(self) -> None:
        source = (Path(__file__).parent / "deploy" / "node_bootstrap.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("_create_unverified_context", source)
        self.assertGreaterEqual(source.count("getpeercert(binary_form=True)"), 2)
        self.assertGreaterEqual(source.count("Outline certificate pin mismatch"), 2)
        self.assertGreaterEqual(source.count("CERT_NONE"), 2)


if __name__ == "__main__":
    unittest.main()
