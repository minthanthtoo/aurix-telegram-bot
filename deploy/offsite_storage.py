#!/usr/bin/env python3
"""Small S3-compatible backup object-store client for encrypted artifacts."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

try:
    from deploy.fleet_reconcile import FleetError
except ModuleNotFoundError:  # Direct execution from deploy/ sets deploy/ as sys.path[0].
    from fleet_reconcile import FleetError


@dataclass(frozen=True)
class ObjectStore:
    bucket: str
    prefix: str
    endpoint: str
    region: str
    access_key: str
    secret_key: str


def configured(env: dict[str, str]) -> bool:
    return bool(env.get("AURIX_BACKUP_OBJECT_STORE_URL", "").strip())


def from_env(env: dict[str, str]) -> ObjectStore:
    raw_url = env.get("AURIX_BACKUP_OBJECT_STORE_URL", "").strip()
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise FleetError("AURIX_BACKUP_OBJECT_STORE_URL must look like s3://bucket/prefix")
    endpoint = env.get("AURIX_BACKUP_OBJECT_STORE_ENDPOINT", "").strip().rstrip("/")
    if not endpoint.startswith("https://"):
        raise FleetError("AURIX_BACKUP_OBJECT_STORE_ENDPOINT must be an HTTPS URL")
    access_key = env.get("AURIX_BACKUP_OBJECT_STORE_ACCESS_KEY_ID", "").strip()
    secret_key = env.get("AURIX_BACKUP_OBJECT_STORE_SECRET_ACCESS_KEY", "").strip()
    if not access_key or not secret_key:
        raise FleetError("object-store access key and secret key are required")
    return ObjectStore(
        bucket=parsed.netloc,
        prefix=parsed.path.strip("/"),
        endpoint=endpoint,
        region=env.get("AURIX_BACKUP_OBJECT_STORE_REGION", "auto").strip() or "auto",
        access_key=access_key,
        secret_key=secret_key,
    )


def join_key(store: ObjectStore, key: str) -> str:
    clean = key.strip("/")
    return f"{store.prefix}/{clean}" if store.prefix else clean


def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = ("AWS4" + secret).encode()
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _request(
    store: ObjectStore,
    method: str,
    key: str = "",
    *,
    body: bytes = b"",
    query: dict[str, str] | None = None,
) -> bytes:
    query = query or {}
    encoded_key = urllib.parse.quote(key.strip("/"), safe="/")
    path = f"/{store.bucket}" + (f"/{encoded_key}" if encoded_key else "")
    query_string = urllib.parse.urlencode(sorted(query.items()), quote_via=urllib.parse.quote)
    url = f"{store.endpoint}{path}" + (f"?{query_string}" if query_string else "")
    parsed_endpoint = urllib.parse.urlsplit(store.endpoint)
    host = parsed_endpoint.netloc
    now = dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join((
        method,
        path,
        query_string,
        canonical_headers,
        signed_headers,
        payload_hash,
    ))
    credential_scope = f"{date}/{store.region}/s3/aws4_request"
    string_to_sign = "\n".join((
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical.encode()).hexdigest(),
    ))
    signature = hmac.new(
        _signing_key(store.secret_key, date, store.region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={store.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = urllib.request.Request(
        url,
        data=body if method in {"PUT", "POST"} else None,
        method=method,
        headers={
            "Authorization": authorization,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
            "User-Agent": "aurix-backup-object-store/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise FleetError(f"object-store request failed: {method} {key or store.bucket}") from exc


def put(env: dict[str, str], key: str, content: bytes) -> str:
    store = from_env(env)
    object_key = join_key(store, key)
    _request(store, "PUT", object_key, body=content)
    return f"s3://{store.bucket}/{object_key}"


def get(env: dict[str, str], key: str) -> bytes:
    store = from_env(env)
    return _request(store, "GET", join_key(store, key))


def delete(env: dict[str, str], key: str) -> None:
    store = from_env(env)
    _request(store, "DELETE", join_key(store, key))


def list_keys(env: dict[str, str], prefix: str) -> list[str]:
    store = from_env(env)
    full_prefix = join_key(store, prefix)
    raw = _request(store, "GET", "", query={"list-type": "2", "prefix": full_prefix})
    root = ET.fromstring(raw)
    keys = []
    for item in root.findall(".//{*}Contents/{*}Key"):
        if item.text:
            keys.append(item.text)
    if store.prefix:
        base = store.prefix.rstrip("/") + "/"
        keys = [key[len(base):] for key in keys if key.startswith(base)]
    return sorted(keys)


def latest_key(env: dict[str, str], prefix: str, suffix: str) -> str:
    matches = [key for key in list_keys(env, prefix) if key.endswith(suffix)]
    if not matches:
        raise FleetError(f"no object-store backups found below {prefix}")
    return sorted(matches, reverse=True)[0]
