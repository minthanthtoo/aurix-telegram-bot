import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from deploy.render_web import HealthHandler


class _Child:
    pid = 4242
    returncode = None

    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.returncode = exit_code

    def poll(self):
        return self._exit_code


class RenderWebHealthTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
