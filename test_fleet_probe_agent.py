import subprocess
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fleet_probe import FleetProbeError, verify_probe_result
from fleet_probe_agent import main, run_instruction, signed_result


class _Socket:
    def close(self):
        return None


class FleetProbeAgentTest(unittest.TestCase):
    def test_server_agent_runs_tcp_without_shell_and_returns_signed_result(self):
        calls = []

        def connector(address, timeout):
            calls.append((address, timeout))
            return _Socket()

        job = {
            "job_id": "probe-1",
            "source_server_id": "sg-a",
            "probe_type": "tcp",
            "host": "198.51.100.10",
            "port": 443,
            "timeout_ms": 1000,
        }
        with patch("fleet_probe_agent.run_instruction", lambda instruction: run_instruction(instruction, connector=connector)):
            result = signed_result(job, agent_id="sg-a", secret="node-secret")
        self.assertEqual(calls, [(('198.51.100.10', 443), 1.0)])
        self.assertTrue(
            verify_probe_result(result["job_id"], result["payload"], result["signature"], "node-secret")
        )
        self.assertEqual(result["payload"]["status"], "success")

    def test_unsupported_or_malformed_instruction_fails_closed(self):
        with self.assertRaisesRegex(FleetProbeError, "unsupported"):
            run_instruction({"probe_type": "shell", "host": "example.invalid"})
        result = run_instruction({"probe_type": "tcp", "host": "bad\nname", "port": 443})
        self.assertIn(result["status"], {"error", "unavailable"})

    def test_icmp_command_uses_argument_list(self):
        completed = SimpleNamespace(returncode=0, stdout="3 packets transmitted, 3 received, 0% packet loss")
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return completed

        with patch("fleet_probe_agent.shutil.which", return_value="/bin/ping"):
            result = run_instruction(
                {"probe_type": "icmp", "host": "198.51.100.10", "timeout_ms": 1000},
                command_runner=runner,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls[0][0][0], ["/bin/ping", "-c", "3", "-W", "1", "198.51.100.10"])
        self.assertFalse(calls[0][1].get("shell", False))

    def test_cli_can_read_agent_identity_and_jobs_mode_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs_path = os.path.join(directory, "jobs.json")
            output_path = os.path.join(directory, "results.json")
            with open(jobs_path, "w", encoding="utf-8") as handle:
                json.dump([{
                    "job_id": "probe-env-1",
                    "source_server_id": "sg-a",
                    "probe_type": "dns",
                    "host": "localhost",
                }], handle)
            with patch.dict(os.environ, {"AURIX_PROBE_AGENT_ID": "sg-a", "AURIX_PROBE_AGENT_SECRET": "node-secret"}, clear=False):
                with patch("fleet_probe_agent.run_instruction", return_value={"status": "success"}):
                    self.assertEqual(main(["--jobs", jobs_path, "--output", output_path]), 0)
            with open(output_path, encoding="utf-8") as handle:
                results = json.load(handle)
            self.assertEqual(results[0]["agent_id"], "sg-a")


if __name__ == "__main__":
    unittest.main()
