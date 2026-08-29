import contextlib
import io
import os
import time
import unittest
from unittest.mock import patch

from observability import latency_log


class LatencyLogTest(unittest.TestCase):
    def test_disabled_logging_is_silent(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"AURIX_LATENCY_LOG": "0"}, clear=True):
            with contextlib.redirect_stderr(output):
                latency_log("adapter", time.perf_counter(), status="ok")
        self.assertEqual(output.getvalue(), "")

    def test_enabled_logging_is_bounded_and_structured(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"AURIX_LATENCY_LOG": "true"}, clear=True):
            with contextlib.redirect_stderr(output):
                latency_log("adapter", time.perf_counter(), status="ok")
        value = output.getvalue()
        self.assertIn("latency event=adapter duration_ms=", value)
        self.assertIn(" status=ok", value)


if __name__ == "__main__":
    unittest.main()
