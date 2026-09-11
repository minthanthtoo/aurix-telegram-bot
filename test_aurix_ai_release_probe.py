import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.aurix_ai_release_probe import REQUIRED_ASSETS, REQUIRED_DOCUMENTS, run_probe


class _ProbeHandler(BaseHTTPRequestHandler):
    local_root = Path(__file__).resolve().parent / "web" / "ai-app"
    drift = False

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/api/healthz":
            self._json(200, {"ok": True, "service": "aurix-ai"})
            return
        if self.path == "/api/modes":
            self._json(
                200,
                {"modes": [{"id": value} for value in ("english", "translate", "lisu_assistant")]},
            )
            return
        if self.path in {"/api/conversations", "/api/admin/usage"}:
            self._json(404 if self.drift else 401, {"error": "authentication required"})
            return
        if self.path == "/":
            body = self.local_root.joinpath("index.html").read_bytes()
            self._send(200, "text/html", body)
            return
        if self.path in {f"/{document}" for document in REQUIRED_DOCUMENTS}:
            body = self.local_root.parent.parent.joinpath("docs", self.path.lstrip("/")).read_bytes()
            if self.drift:
                body += b"\n<!-- legacy guide marker -->\n"
            self._send(200, "text/markdown", body)
            return
        asset = self.path.lstrip("/")
        if asset in REQUIRED_ASSETS:
            body = self.local_root.joinpath(asset).read_bytes()
            if self.drift and asset == "app.js":
                body += b"\n// legacy deployment marker\n"
            content_type = "text/html" if asset.endswith(".html") else "text/javascript"
            self._send(200, content_type, body)
            return
        self._json(404, {"error": "not found"})

    def _json(self, status, payload):
        self._send(status, "application/json", json.dumps(payload).encode())

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class AuriXAIReleaseProbeTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_probe_passes_for_matching_staging_contract(self):
        _ProbeHandler.drift = False
        result = run_probe(self.base_url, local_root=_ProbeHandler.local_root)
        self.assertTrue(result["ok"], result)

    def test_probe_fails_for_legacy_asset_or_route_drift(self):
        _ProbeHandler.drift = True
        result = run_probe(self.base_url, local_root=_ProbeHandler.local_root)
        self.assertFalse(result["ok"])
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["asset_parity"]["ok"])
        self.assertFalse(checks["document_parity"]["ok"])
        self.assertFalse(checks["protected_route_presence"]["ok"])


if __name__ == "__main__":
    unittest.main()
