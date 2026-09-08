import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deploy.render_combined as render_combined
from deploy.render_combined import _stop_child


class _Child:
    def __init__(self, status=None):
        self.status = status
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.status

    def terminate(self):
        self.terminated = True
        self.status = 0

    def wait(self, timeout=None):
        return self.status

    def kill(self):
        self.killed = True
        self.status = -9


class RenderCombinedTest(unittest.TestCase):
    def test_entrypoint_imports_when_run_outside_repository(self):
        root = Path(__file__).resolve().parent
        entrypoint = root / "deploy" / "render_combined.py"
        probe = f"import runpy; runpy.run_path({str(entrypoint)!r}, run_name='combined_probe')"
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=root.parent,
            env={},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_child_is_terminated_during_supervisor_shutdown(self):
        child = _Child()
        _stop_child(child)
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)

    def test_already_stopped_child_is_unchanged(self):
        child = _Child(status=1)
        _stop_child(child)
        self.assertFalse(child.terminated)

    def test_schema_composition_finishes_before_bot_process_starts(self):
        events = []
        runtime = SimpleNamespace(commerce_database=SimpleNamespace())

        class _Server:
            timeout = None

            def server_close(self):
                events.append("server_close")

        def build(**_kwargs):
            events.append("build")
            return runtime

        def application(*_args, **_kwargs):
            events.append("application")
            return object()

        def server(*_args, **_kwargs):
            events.append("server")
            return _Server()

        def process(*_args, **_kwargs):
            events.append("process")
            return _Child(status=1)

        with (
            patch.dict("os.environ", {"PORT": "10000"}, clear=True),
            patch.object(render_combined, "build_runtime_services", side_effect=build),
            patch.object(render_combined, "AuriXVpnWebApplication", side_effect=application),
            patch.object(render_combined, "create_server", side_effect=server),
            patch.object(render_combined.subprocess, "Popen", side_effect=process),
            patch.object(render_combined.signal, "signal"),
        ):
            self.assertEqual(render_combined.main(), 1)

        self.assertLess(events.index("build"), events.index("process"))
        self.assertLess(events.index("server"), events.index("process"))


if __name__ == "__main__":
    unittest.main()
