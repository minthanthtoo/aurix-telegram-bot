"""Small, server-side Supabase Storage client for payment evidence.

The bot deliberately uses the Storage HTTP API instead of a heavyweight SDK.
The service-role key is only read by the Render process and is never returned
to Telegram or persisted in the database.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from observability import latency_log as _latency_log


class ReceiptStorageError(RuntimeError):
    """A safe error raised when receipt object storage cannot complete."""


class NullReceiptStorage:
    """Compatibility storage for local tests and legacy staging deployments."""

    configured = False
    bucket: str | None = None

    def upload(self, path: str, data: bytes, mime_type: str) -> str:
        return path

    def signed_url(self, path: str, expires_in: int = 300) -> None:
        return None

    def delete(self, path: str) -> None:
        return None


class SupabaseReceiptStorage:
    """Private Supabase Storage bucket accessed with a server-side key."""

    configured = True

    def __init__(
        self,
        project_url: str,
        service_role_key: str,
        bucket: str = "payment-receipts",
        timeout: float = 20.0,
        max_bytes: int = 20 * 1024 * 1024,
    ):
        parsed = urllib.parse.urlsplit(project_url.rstrip("/"))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("SUPABASE_URL must be an https URL")
        if parsed.query or parsed.fragment:
            raise ValueError("SUPABASE_URL must not contain a query or fragment")
        key = str(service_role_key or "").strip()
        if not key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required")
        normalized_bucket = str(bucket or "").strip()
        if not normalized_bucket or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in normalized_bucket
        ):
            raise ValueError("SUPABASE_RECEIPTS_BUCKET contains invalid characters")
        if timeout <= 0:
            raise ValueError("Supabase Storage timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("Supabase Storage max size must be positive")
        self.project_url = f"{parsed.scheme}://{parsed.netloc}"
        self.service_role_key = key
        self.bucket = normalized_bucket
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)

    def _object_url(self, path: str, action: str = "object") -> str:
        normalized_path = str(path or "").strip().lstrip("/")
        if not normalized_path or ".." in normalized_path.split("/"):
            raise ReceiptStorageError("Receipt storage path is invalid")
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        encoded_path = urllib.parse.quote(normalized_path, safe="/")
        return f"{self.project_url}/storage/v1/{action}/{encoded_bucket}/{encoded_path}"

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not echo the response body: Storage errors can contain paths or
            # provider details that are not suitable for Telegram/user logs.
            if exc.code == 409:
                return {"already_exists": True}
            raise ReceiptStorageError(f"Supabase Storage request failed (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReceiptStorageError("Supabase Storage request failed") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw

    def upload(self, path: str, data: bytes, mime_type: str) -> str:
        started_at = time.perf_counter()
        request_status = "error"
        payload = bytes(data)
        if not payload or len(payload) > self.max_bytes:
            raise ReceiptStorageError("Receipt image is empty or too large")
        normalized_mime = str(mime_type or "application/octet-stream").strip()[:128]
        url = self._object_url(path)
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.service_role_key}",
                "apikey": self.service_role_key,
                "Content-Type": normalized_mime,
                "Cache-Control": "private, no-store",
                # Evidence paths are immutable. A retry after a lost database
                # response receives 409 and is safely treated as success.
                "x-upsert": "false",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
            request_status = "ok"
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise ReceiptStorageError(
                    f"Supabase Storage upload failed (HTTP {exc.code})"
                ) from exc
            request_status = "already_exists"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReceiptStorageError("Supabase Storage upload failed") from exc
        finally:
            _latency_log("receipt_storage_upload", started_at, status=request_status)
        return str(path).lstrip("/")

    def signed_url(self, path: str, expires_in: int = 300) -> str:
        started_at = time.perf_counter()
        ttl = max(30, min(int(expires_in), 3600))
        url = self._object_url(path, action="object/sign")
        payload = json.dumps({"expiresIn": ttl}).encode("utf-8")
        try:
            result = self._request("POST", url, payload, "application/json")
        except Exception:
            _latency_log("receipt_storage_signed_url", started_at, status="error")
            raise
        if not isinstance(result, dict):
            _latency_log("receipt_storage_signed_url", started_at, status="invalid")
            raise ReceiptStorageError("Supabase Storage returned an invalid signed URL")
        signed = result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")
        if not isinstance(signed, str) or not signed:
            _latency_log("receipt_storage_signed_url", started_at, status="invalid")
            raise ReceiptStorageError("Supabase Storage did not return a signed URL")
        _latency_log("receipt_storage_signed_url", started_at, status="ok")
        if signed.startswith("https://"):
            return signed
        if signed.startswith("/"):
            return f"{self.project_url}{signed}"
        return f"{self.project_url}/{signed}"

    def delete(self, path: str) -> None:
        normalized_path = str(path or "").strip().lstrip("/")
        if not normalized_path:
            return
        url = f"{self.project_url}/storage/v1/object/{urllib.parse.quote(self.bucket, safe='')}/remove"
        payload = json.dumps({"prefixes": [normalized_path]}).encode("utf-8")
        try:
            self._request("POST", url, payload, "application/json")
        except ReceiptStorageError:
            # Cleanup is best effort. The evidence row remains the source of
            # truth and an operator can remove an orphaned object later.
            return
