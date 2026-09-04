import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from commerce import CommerceDatabase
from deploy.render_web import HealthHandler
from fleet_enrollment import create_pending_enrollment, generate_token


class _Child:
    pid = 4242
    returncode = None

    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.returncode = exit_code

    def poll(self):
        return self._exit_code


class RenderWebHealthTest(unittest.TestCase):
    def tearDown(self):
        HealthHandler.registration_database = None

    def _request(self, child):
        HealthHandler.child = child
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_is_ok_only_while_bot_child_is_running(self):
        status, payload = self._request(_Child())
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["bot_pid"], 4242)

        status, payload = self._request(_Child(exit_code=1))
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["bot_exit_code"], 1)

    def test_health_does_not_expose_query_values(self):
        HealthHandler.child = _Child()
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/healthz?token=should-not-appear")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            self.assertNotIn("should-not-appear", response.read().decode())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_registration_endpoint_accepts_one_time_enrollment_without_echoing_secrets(self):
        import base64

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "registration.db"
            database = CommerceDatabase(database_path)
            database.initialize()
            now = "2026-09-04T00:00:00+00:00"
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO infrastructure_jobs
                       (id, operation, status, attempts, next_attempt_at,
                        request_fingerprint, created_at)
                       VALUES (?, 'provision', 'running', 1, ?, ?, ?)""",
                    ("job-render-1", now, "fingerprint", now),
                )
            token = generate_token()
            key = Fernet.generate_key().decode()
            create_pending_enrollment(database, job_id="job-render-1", token=token)
            payload = {
                "token": token,
                "job_id": "job-render-1",
                "node_id": "auto-render-1",
                "public_ip": "203.0.113.10",
                "access_txt_b64": base64.b64encode(
                    b"apiUrl:https://203.0.113.10:61603/abcdefghijklmnop\ncertSha256:" + b"a" * 64
                ).decode(),
                "ssh_host_key_b64": base64.b64encode(
                    b"ssh-ed25519 " + b"A" * 44
                ).decode(),
            }
            HealthHandler.child = _Child()
            server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                with patch.dict(
                    os.environ,
                    {
                        "AURIX_FLEET_REGISTRATION_ENABLED": "1",
                        "AURIX_FLEET_ENROLLMENT_KEY": key,
                        "DATABASE_PATH": str(database_path),
                    },
                    clear=False,
                ):
                    connection.request(
                        "POST",
                        "/fleet/register",
                        body=json.dumps(payload),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    body = response.read().decode()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(response.status, 200)
        result = json.loads(body)
        self.assertEqual(result["status"], "accepted")
        self.assertNotIn(token, body)
        self.assertNotIn("certSha256", body)


if __name__ == "__main__":
    unittest.main()
