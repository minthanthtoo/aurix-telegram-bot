"""Small, server-side Supabase Storage client for payment evidence.

The bot deliberately uses the Storage HTTP API instead of a heavyweight SDK.
The service-role key is only read by the Render process and is never returned
to Telegram or persisted in the database.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from observability import latency_log as _latency_log


class ReceiptStorageError(RuntimeError):
    """A safe error raised when receipt object storage cannot complete."""


class SupabaseObjectStore:
    """Private Supabase Storage object store for encrypted recovery artifacts.

    This is intentionally separate from :class:`SupabaseReceiptStorage` at the
    configuration level.  Recovery archives and payment evidence must not share
    a bucket: a service-role credential can access both, but a bucket boundary
    makes accidental retention/deletion and operational review much safer.
    """

    configured = True

    def __init__(
        self,
        project_url: str,
        service_role_key: str,
        bucket: str,
        prefix: str = "",
        timeout: float = 45.0,
        max_bytes: int = 512 * 1024 * 1024,
    ):
        parsed = urllib.parse.urlsplit(str(project_url or "").rstrip("/"))
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
            raise ValueError("AURIX_BACKUP_SUPABASE_BUCKET contains invalid characters")
        normalized_prefix = str(prefix or "").strip().strip("/")
        if ".." in normalized_prefix.split("/"):
            raise ValueError("AURIX_BACKUP_SUPABASE_PREFIX contains an invalid path")
        if not math.isfinite(float(timeout)) or not 1 <= float(timeout) <= 120:
            raise ValueError("Supabase Storage timeout must be between 1 and 120 seconds")
        if not 1 <= int(max_bytes) <= 2 * 1024 * 1024 * 1024:
            raise ValueError("Supabase Storage max size must be between 1 byte and 2 GiB")
        self.project_url = f"{parsed.scheme}://{parsed.netloc}"
        self.service_role_key = key
        self.bucket = normalized_bucket
        self.prefix = normalized_prefix
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)

    def _full_path(self, path: str) -> str:
        clean = str(path or "").strip().strip("/")
        if not clean or ".." in clean.split("/"):
            raise ReceiptStorageError("Supabase backup object path is invalid")
        return f"{self.prefix}/{clean}" if self.prefix else clean

    def _object_url(self, path: str, action: str = "object") -> str:
        normalized_path = self._full_path(path)
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        encoded_path = urllib.parse.quote(normalized_path, safe="/")
        return f"{self.project_url}/storage/v1/{action}/{encoded_bucket}/{encoded_path}"

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        content_type: str | None = None,
        *,
        raw_response: bool = False,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
            "Cache-Control": "private, no-store",
            "User-Agent": "aurix-recovery-storage/1",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return {"already_exists": True}
            raise ReceiptStorageError(
                f"Supabase recovery storage request failed (HTTP {exc.code})"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReceiptStorageError("Supabase recovery storage request failed") from exc
        if len(raw) > self.max_bytes:
            raise ReceiptStorageError("Supabase recovery object exceeds the configured size limit")
        if not raw:
            return None
        if raw_response:
            return raw
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw

    def put(self, path: str, data: bytes) -> str:
        payload = bytes(data)
        if not payload or len(payload) > self.max_bytes:
            raise ReceiptStorageError("Supabase recovery object is empty or too large")
        full_path = self._full_path(path)
        url = self._object_url(path)
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.service_role_key}",
                "apikey": self.service_role_key,
                "Content-Type": "application/octet-stream",
                "Cache-Control": "private, no-store",
                # Archives are content-addressed by timestamp and are immutable.
                # A retry after a lost response must never overwrite an archive.
                "x-upsert": "false",
                "User-Agent": "aurix-recovery-storage/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise ReceiptStorageError(
                    f"Supabase recovery upload failed (HTTP {exc.code})"
                ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReceiptStorageError("Supabase recovery upload failed") from exc
        return f"supabase://{self.bucket}/{full_path}"

    def ensure_private_bucket(self) -> bool:
        """Ensure this recovery bucket exists and is not publicly readable.

        Returns ``True`` when a bucket was created and ``False`` when an
        existing private bucket was reused. This is intentionally an explicit
        operator action (the backup path never creates buckets implicitly).
        """
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        bucket_url = f"{self.project_url}/storage/v1/bucket/{encoded_bucket}"
        try:
            existing = self._request("GET", bucket_url)
        except ReceiptStorageError as exc:
            # Storage returns HTTP 404 for a missing bucket. Keep the check
            # narrow: every other error is a real configuration/availability
            # failure and must not be turned into a create attempt.
            cause = exc.__cause__
            if not isinstance(cause, urllib.error.HTTPError) or cause.code != 404:
                raise
            existing = None
        if existing is not None:
            if isinstance(existing, dict) and existing.get("public") is True:
                raise ReceiptStorageError("Supabase recovery bucket must be private")
            return False
        payload = json.dumps({
            "id": self.bucket,
            "name": self.bucket,
            "public": False,
        }).encode("utf-8")
        try:
            created = self._request(
                "POST",
                f"{self.project_url}/storage/v1/bucket",
                payload,
                "application/json",
            )
        except ReceiptStorageError as exc:
            # A concurrent bootstrap may win the race. Re-check that the
            # resulting bucket is private before reporting success.
            cause = exc.__cause__
            if not isinstance(cause, urllib.error.HTTPError) or cause.code != 409:
                raise
            existing = self._request("GET", bucket_url)
            if isinstance(existing, dict) and existing.get("public") is True:
                raise ReceiptStorageError("Supabase recovery bucket must be private")
            return False
        if isinstance(created, dict) and created.get("already_exists"):
            existing = self._request("GET", bucket_url)
            if isinstance(existing, dict) and existing.get("public") is True:
                raise ReceiptStorageError("Supabase recovery bucket must be private")
            return False
        return True

    def get(self, path: str) -> bytes:
        result = self._request("GET", self._object_url(path), raw_response=True)
        if not isinstance(result, bytes) or not result or len(result) > self.max_bytes:
            raise ReceiptStorageError("Supabase recovery storage returned invalid object data")
        return result

    def delete(self, path: str) -> None:
        normalized_path = self._full_path(path)
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        url = f"{self.project_url}/storage/v1/object/{encoded_bucket}"
        payload = json.dumps({"prefixes": [normalized_path]}).encode("utf-8")
        self._request("DELETE", url, payload, "application/json")

    def list_keys(self, prefix: str, *, page_size: int = 1000) -> list[str]:
        """List object names below ``prefix`` with bounded pagination."""
        clean_prefix = str(prefix or "").strip().strip("/")
        if ".." in clean_prefix.split("/"):
            raise ReceiptStorageError("Supabase backup list prefix is invalid")
        full_prefix = f"{self.prefix}/{clean_prefix}" if self.prefix and clean_prefix else (
            self.prefix or clean_prefix
        )
        encoded_bucket = urllib.parse.quote(self.bucket, safe="")
        url = f"{self.project_url}/storage/v1/object/list/{encoded_bucket}"
        safe_page_size = max(1, min(int(page_size), 1000))
        offset = 0
        pages = 0
        names: list[str] = []
        while True:
            pages += 1
            if pages > 1000:
                raise ReceiptStorageError("Supabase recovery list exceeded the pagination limit")
            payload = json.dumps({
                "prefix": full_prefix,
                "limit": safe_page_size,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            }).encode("utf-8")
            result = self._request("POST", url, payload, "application/json")
            if not isinstance(result, list):
                raise ReceiptStorageError("Supabase recovery list response is invalid")
            page_names = [
                str(item.get("name"))
                for item in result
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            names.extend(page_names)
            if len(result) < safe_page_size:
                break
            offset += len(result)
        base = self.prefix.rstrip("/") + "/" if self.prefix else ""
        full_base = full_prefix.rstrip("/") + "/" if full_prefix else ""
        logical_base = clean_prefix.rstrip("/") + "/" if clean_prefix else ""
        normalized: list[str] = []
        for name in names:
            if base and name.startswith(base):
                normalized.append(name[len(base):])
            elif full_base and name.startswith(full_base):
                normalized.append(logical_base + name[len(full_base):])
            elif clean_prefix:
                # Supabase may return names relative to the requested prefix.
                normalized.append(logical_base + name)
            else:
                normalized.append(name)
        return sorted(
            [name for name in normalized if name],
        )


class NullReceiptStorage:
    """Compatibility storage for local tests and legacy staging deployments."""

    configured = False
    bucket: str | None = None

    def upload(self, path: str, data: bytes, mime_type: str) -> str:
        return path

    def signed_url(self, path: str, expires_in: int = 300) -> None:
        return None

    def download(self, path: str) -> None:
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

    def download(self, path: str) -> bytes:
        """Read one private evidence object for background-assisted extraction."""
        result = self._request("GET", self._object_url(path))
        if not isinstance(result, bytes) or not result or len(result) > self.max_bytes:
            raise ReceiptStorageError("Supabase Storage returned invalid receipt evidence")
        return result

    def delete(self, path: str) -> None:
        normalized_path = str(path or "").strip().lstrip("/")
        if not normalized_path:
            return
        url = f"{self.project_url}/storage/v1/object/{urllib.parse.quote(self.bucket, safe='')}"
        payload = json.dumps({"prefixes": [normalized_path]}).encode("utf-8")
        try:
            self._request("DELETE", url, payload, "application/json")
        except ReceiptStorageError:
            # Cleanup is best effort. The evidence row remains the source of
            # truth and an operator can remove an orphaned object later.
            return
