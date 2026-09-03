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

    def test_long_poll_telemetry_can_be_distinguished_from_command_calls(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"AURIX_LATENCY_LOG": "true"}, clear=True):
            with contextlib.redirect_stderr(output):
                latency_log(
                    "telegram_request",
                    time.perf_counter(),
                    method="getUpdates",
                    request_kind="long_poll",
                )
        self.assertIn("method=getUpdates", output.getvalue())
        self.assertIn("request_kind=long_poll", output.getvalue())


if __name__ == "__main__":
    unittest.main()
