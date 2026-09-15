import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from aurix_ai.capabilities import build_capability_report
from aurix_ai.web_api import AuriXAIApplication, make_handler


class _Router:
    def list_models(self, category=None):
        if category == "stt":
            raise RuntimeError("provider unavailable")
        if category is None:
            return [{"id": "ag/gemini-3.7-flash-high"}]
        return [{"id": f"{category}/demo", "owned_by": "test"}]

    def request_json(self, path, payload, *, method="POST"):
        if path == "/api/quota":
            return {
                "providers": [{"id": "ag", "quota": {"unit": "requests"}}],
                "access_token": "must-not-leak",
            }
        return {"summary": {"requests": 1}}


class CapabilityReportTest(unittest.TestCase):
    def test_report_separates_discovery_errors_and_quota(self):
        report = build_capability_report(_Router())

        self.assertEqual(report["schema"], "aurix.capabilityReport.v1")
        self.assertEqual(report["categories"]["chat"]["status"], "ok")
        self.assertEqual(report["categories"]["speech_to_text"]["status"], "error")
        self.assertEqual(report["account"]["quota"]["status"], "ok")
        self.assertEqual(report["summary"]["models_discovered"], 5)
        self.assertNotIn("Authorization", str(report))
        self.assertNotIn("must-not-leak", str(report))
        self.assertEqual(report["account"]["quota"]["data"]["access_token"], "[redacted]")

    def test_admin_http_route_requires_admin_token_and_returns_report(self):
        application = AuriXAIApplication(
            _Router(),
            access_token="legacy",
            admin_token="admin-secret",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            connection.request("GET", "/api/admin/capabilities")
            unauthorized = connection.getresponse()
            unauthorized.read()
            connection.close()
            self.assertEqual(unauthorized.status, 401)

            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
            connection.request(
                "GET",
                "/api/admin/capabilities",
                headers={"Authorization": "Bearer admin-secret"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["schema"], "aurix.capabilityReport.v1")
        finally:
            server.shutdown()
            server.server_close()
            application.close()


if __name__ == "__main__":
    unittest.main()
