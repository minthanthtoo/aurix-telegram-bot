import json
import unittest
from unittest.mock import patch

from supabase_storage import ReceiptStorageError, SupabaseReceiptStorage


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class SupabaseReceiptStorageTest(unittest.TestCase):
    def test_upload_uses_private_immutable_object_request(self):
        storage = SupabaseReceiptStorage(
            "https://project.supabase.co", "service-role-secret"
        )
        with patch("supabase_storage.urllib.request.urlopen", return_value=_Response()) as opener:
            result = storage.upload(
                "orders/order-1/evidence-1.jpg", b"receipt", "image/jpeg"
            )
        self.assertEqual(result, "orders/order-1/evidence-1.jpg")
        request = opener.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"receipt")
        self.assertEqual(request.headers["X-upsert"], "false")
        self.assertIn("Bearer service-role-secret", request.headers["Authorization"])
        self.assertNotIn("service-role-secret", request.full_url)

    def test_signed_url_is_short_lived_and_never_contains_service_key(self):
        storage = SupabaseReceiptStorage(
            "https://project.supabase.co", "service-role-secret"
        )
        payload = json.dumps({"signedURL": "/storage/v1/object/sign/payment-receipts/orders/x.jpg?token=abc"}).encode()
        with patch("supabase_storage.urllib.request.urlopen", return_value=_Response(payload)) as opener:
            signed = storage.signed_url("orders/x.jpg", expires_in=5)
        self.assertEqual(
            signed,
            "https://project.supabase.co/storage/v1/object/sign/payment-receipts/orders/x.jpg?token=abc",
        )
        request = opener.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"expiresIn": 30})
        self.assertNotIn("service-role-secret", signed)

    def test_invalid_paths_fail_closed(self):
        storage = SupabaseReceiptStorage("https://project.supabase.co", "secret")
        with self.assertRaises(ReceiptStorageError):
            storage.signed_url("../private.jpg")


if __name__ == "__main__":
    unittest.main()
