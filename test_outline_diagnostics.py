import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.outline_diagnostics import probe_server, run


class _Socket:
    def close(self):
        return None


class _Client:
    def __init__(self, *_args, **_kwargs):
        pass

    def server_info(self):
        return {"version": "1.12.3"}

    def list_keys(self):
        return {"accessKeys": [{"id": "1", "accessUrl": "ss://opaque@198.51.100.10:45524"}]}

    def transfer_metrics(self):
        return {"bytesTransferredByUserId": {"1": 12}}


class OutlineDiagnosticsTest(unittest.TestCase):
    def test_probe_reports_management_and_data_health_without_access_secret(self):
        calls = []

        def connector(address, timeout):
            calls.append((address, timeout))
            return _Socket()

        result = probe_server(
            {
                "id": "sg-a",
                "label": "Singapore A",
                "api_url": "https://198.51.100.10:61603/private-management-path",
                "cert_sha256": "a" * 64,
            },
            request_timeout=2,
            data_timeout=1,
            client_factory=_Client,
            connector=connector,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["outline_version"], "1.12.3")
        self.assertEqual(result["key_count"], 1)
        self.assertEqual(result["metrics_key_count"], 1)
        self.assertEqual(result["data_ports"][0]["port"], 45524)
        self.assertNotIn("private-management-path", json.dumps(result))
        self.assertNotIn("opaque", json.dumps(result))
        self.assertEqual(len(calls), 2)

    def test_probe_marks_data_plane_failure_as_degraded(self):
        def connector(address, timeout):
            if address[1] == 45524:
                raise TimeoutError
            return _Socket()

        result = probe_server(
            {
                "id": "sg-a",
                "api_url": "https://198.51.100.10:61603/private",
                "cert_sha256": "a" * 64,
            },
            request_timeout=2,
            data_timeout=1,
            client_factory=_Client,
            connector=connector,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["data_ports"][0]["status"], "timeout")

    def test_probe_still_checks_configured_data_port_when_management_is_down(self):
        calls = []

        def connector(address, timeout):
            calls.append((address, timeout))
            if address[1] == 61603:
                raise TimeoutError
            raise ConnectionRefusedError

        result = probe_server(
            {
                "id": "bkk-a",
                "label": "Bangkok A",
                "api_url": "https://198.51.100.11:61603/private",
                "cert_sha256": "b" * 64,
                "keys_port": 443,
            },
            request_timeout=2,
            data_timeout=1,
            client_factory=_Client,
            connector=connector,
        )

        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["error"], "management_tcp_timeout")
        self.assertEqual(result["data_ports"], [{
            "host": "198.51.100.11",
            "port": 443,
            "status": "refused",
            "latency_ms": result["data_ports"][0]["latency_ms"],
        }])
        self.assertEqual(len(calls), 2)

    def test_run_loads_explicit_env_file_and_reports_partial_fleet(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "aurix.env"
            env_file.write_text(
                "OUTLINE_SERVERS_JSON='[{\"id\":\"sg-a\",\"api_url\":\"https://198.51.100.10:61603/private\",\"cert_sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}]'\n",
                encoding="utf-8",
            )
            # Patch the transport probe so this contract test never contacts
            # an external host.
            with patch(
                "deploy.outline_diagnostics.probe_server",
                return_value={"status": "healthy"},
            ) as probe:
                report = run(env_file)
            probe.assert_called_once()
            self.assertEqual(report["server_count"], 1)
            self.assertEqual(report["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
