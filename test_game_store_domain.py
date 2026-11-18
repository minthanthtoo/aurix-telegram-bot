from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError

from scripts.verify_game_store_domain import DomainCheckError, verify_domain


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class GameStoreDomainTests(unittest.TestCase):
    def test_read_only_domain_check_validates_health_and_storefront(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.method, timeout))
            if request.full_url.endswith("/api/healthz"):
                return _Response(200, json.dumps({
                    "status": "ok", "service": "aurix-games", "checkout_enabled": False,
                }).encode())
            return _Response(200, b"<!doctype html>")

        result = verify_domain(
            "store.aurix-mart.online",
            opener=opener,
            resolver=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
        )
        self.assertEqual(result["health_status"], 200)
        self.assertEqual(result["storefront_status"], 200)
        self.assertEqual([item[1] for item in calls], ["GET", "GET"])

    def test_domain_check_rejects_wildcards_and_bad_health(self):
        with self.assertRaises(DomainCheckError):
            verify_domain("*.aurix-mart.online", resolver=lambda *args, **kwargs: [])

        def bad_opener(request, timeout):
            return _Response(200, b'{"status":"ok","service":"other","checkout_enabled":false}')

        with self.assertRaises(DomainCheckError):
            verify_domain(
                "store.aurix-mart.online",
                opener=bad_opener,
                resolver=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
            )


if __name__ == "__main__":
    unittest.main()
