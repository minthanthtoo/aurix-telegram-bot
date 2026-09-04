"""One-time, encrypted enrollment for automatically created VPN nodes.

The enrollment token is short lived and single use.  It is the only secret
allowed in provider bootstrap data; Outline management credentials are posted
over HTTPS and encrypted before entering the commerce database.  Enrollment
never activates an endpoint by itself: the worker still binds the job to the
provider-observed IP and runs the pinned-SSH fleet reconciler.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import shlex
import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken


UTC = timezone.utc
TOKEN_TTL = timedelta(minutes=20)
TOKEN_MIN_LENGTH = 32
TOKEN_MAX_LENGTH = 256
MAX_PAYLOAD_BYTES = 32 * 1024
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
NODE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}\Z")
SSH_KEY_RE = re.compile(
    r"(?:ssh|ecdsa)-[A-Za-z0-9+_.-]+\s+[A-Za-z0-9+/=]+(?:\s+[^\s]+)?\Z"
)


class EnrollmentError(RuntimeError):
    """Raised when a node enrollment request cannot be trusted or recovered."""


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(str(key).encode())
    except (TypeError, ValueError) as exc:
        raise EnrollmentError("fleet enrollment encryption key is invalid") from exc


def validate_enrollment_key(key: str) -> None:
    """Fail closed before a provider VM is created with unusable state."""
    _fernet(key)


def generate_token() -> str:
    """Generate a URL-safe token for one-time bootstrap enrollment."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    normalized = str(token or "").strip()
    if not TOKEN_MIN_LENGTH <= len(normalized) <= TOKEN_MAX_LENGTH:
        raise EnrollmentError("fleet enrollment token is invalid")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _payload_copy(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise EnrollmentError("fleet enrollment payload must be an object")
    allowed = {"job_id", "node_id", "public_ip", "access_txt", "ssh_host_key"}
    if set(payload) - allowed:
        raise EnrollmentError("fleet enrollment payload contains unknown fields")
    values = {name: str(payload.get(name) or "").strip() for name in allowed}
    if not JOB_ID_RE.fullmatch(values["job_id"]):
        raise EnrollmentError("fleet enrollment job id is invalid")
    if not NODE_ID_RE.fullmatch(values["node_id"]):
        raise EnrollmentError("fleet enrollment node id is invalid")
    try:
        ipaddress.ip_address(values["public_ip"])
    except ValueError as exc:
        raise EnrollmentError("fleet enrollment public IP is invalid") from exc
    if not values["access_txt"] or len(values["access_txt"].encode()) > 8192:
        raise EnrollmentError("fleet enrollment Outline identity is missing or too large")
    if not SSH_KEY_RE.fullmatch(values["ssh_host_key"]):
        raise EnrollmentError("fleet enrollment SSH host key is invalid")
    return values


def _encrypted_payload(key: str, payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise EnrollmentError("fleet enrollment payload is too large")
    return _fernet(key).encrypt(encoded).decode()


def _decrypted_payload(key: str, ciphertext: str) -> dict[str, str]:
    try:
        raw = _fernet(key).decrypt(str(ciphertext).encode(), ttl=None)
        payload = json.loads(raw)
    except (InvalidToken, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrollmentError("fleet enrollment payload cannot be decrypted") from exc
    return _payload_copy(payload)


def create_pending_enrollment(
    database: Any,
    *,
    job_id: str,
    token: str,
    expires_at: datetime | None = None,
    now: datetime | None = None,
    connection: Any | None = None,
) -> dict[str, str]:
    """Persist only a token hash before provider creation.

    ``connection`` lets a provider job insert its enrollment row in the same
    transaction that moves the job to ``running``. That closes the crash window
    where a VM could be created with no callback state to activate it.
    """
    normalized_job = str(job_id).strip()
    if not JOB_ID_RE.fullmatch(normalized_job):
        raise EnrollmentError("fleet enrollment job id is invalid")
    digest = token_hash(token)
    current = _now(now)
    expiry = (expires_at or current + TOKEN_TTL).astimezone(UTC)
    if expiry <= current:
        raise EnrollmentError("fleet enrollment expiry must be in the future")
    now_text = current.isoformat()
    expiry_text = expiry.isoformat()

    def insert_row(active_connection: Any) -> dict[str, str]:
        existing = active_connection.execute(
            "SELECT job_id, token_hash, expires_at FROM infrastructure_enrollments WHERE job_id = ?",
            (normalized_job,),
        ).fetchone()
        if existing is not None:
            if str(existing["token_hash"]) != digest:
                raise EnrollmentError("fleet enrollment already exists for this job")
            return {"job_id": normalized_job, "expires_at": str(existing["expires_at"])}
        job = active_connection.execute(
            "SELECT id, operation FROM infrastructure_jobs WHERE id = ?",
            (normalized_job,),
        ).fetchone()
        if job is None or str(job["operation"]) != "provision":
            raise EnrollmentError("fleet enrollment job does not exist")
        active_connection.execute(
            """INSERT INTO infrastructure_enrollments
               (job_id, token_hash, expires_at, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (normalized_job, digest, expiry_text, now_text),
        )
        active_connection.execute(
            """INSERT INTO infrastructure_events
               (id, infrastructure_job_id, event_type, metadata_json, created_at)
               VALUES (?, ?, 'enrollment_created', ?, ?)""",
            (uuid.uuid4().hex, normalized_job, "{}", now_text),
        )
        return {"job_id": normalized_job, "expires_at": expiry_text}

    if connection is not None:
        return insert_row(connection)
    with database.connect() as active_connection:
        database.begin_write(active_connection)
        return insert_row(active_connection)


def receive_enrollment(
    database: Any,
    *,
    token: str,
    payload: dict[str, Any],
    encryption_key: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Validate and encrypt a node callback without activating the endpoint."""
    digest = token_hash(token)
    values = _payload_copy(payload)
    ciphertext = _encrypted_payload(encryption_key, values)
    current = _now(now)
    now_text = current.isoformat()
    with database.connect() as connection:
        database.begin_write(connection)
        lock_clause = " FOR UPDATE" if connection.__class__.__name__ == "_PostgresConnection" else ""
        row = connection.execute(
            "SELECT * FROM infrastructure_enrollments WHERE token_hash = ?" + lock_clause,
            (digest,),
        ).fetchone()
        if row is None:
            raise EnrollmentError("fleet enrollment token is unknown")
        if str(row["status"]) == "consumed":
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'enrollment_replay', ?, ?)""",
                (uuid.uuid4().hex, row["job_id"], json.dumps({"status": "consumed"}), now_text),
            )
            return {"status": "already_consumed", "job_id": str(row["job_id"])}
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"])).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise EnrollmentError("fleet enrollment expiry is invalid") from exc
        if expires_at <= current:
            connection.execute(
                "UPDATE infrastructure_enrollments SET status = 'expired', last_error = ? WHERE job_id = ?",
                ("enrollment token expired", row["job_id"]),
            )
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'enrollment_expired', ?, ?)""",
                (uuid.uuid4().hex, row["job_id"], "{}", now_text),
            )
            raise EnrollmentError("fleet enrollment token has expired")
        if values["job_id"] != str(row["job_id"]):
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'enrollment_rejected', ?, ?)""",
                (
                    uuid.uuid4().hex,
                    row["job_id"],
                    json.dumps({"reason": "job_binding_mismatch"}),
                    now_text,
                ),
            )
            raise EnrollmentError("fleet enrollment job binding does not match")
        if row["payload_ciphertext"]:
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'enrollment_replay', ?, ?)""",
                (uuid.uuid4().hex, row["job_id"], "{}", now_text),
            )
            return {"status": "already_received", "job_id": str(row["job_id"])}
        connection.execute(
            """UPDATE infrastructure_enrollments
               SET payload_ciphertext = ?, received_at = ?, last_error = NULL
               WHERE job_id = ? AND status = 'pending'""",
            (ciphertext, now_text, row["job_id"]),
        )
        connection.execute(
            """INSERT INTO infrastructure_events
               (id, infrastructure_job_id, event_type, metadata_json, created_at)
               VALUES (?, ?, 'enrollment_received', ?, ?)""",
            (uuid.uuid4().hex, row["job_id"], "{}", now_text),
        )
    return {"status": "accepted", "job_id": str(values["job_id"])}


def read_enrollment(
    database: Any,
    *,
    job_id: str,
    encryption_key: str,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Read a pending payload without consuming it, allowing retries."""
    normalized_job = str(job_id).strip()
    current = _now(now)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM infrastructure_enrollments WHERE job_id = ?",
            (normalized_job,),
        ).fetchone()
    if row is None or str(row["status"]) != "pending" or not row["payload_ciphertext"]:
        return None
    try:
        expires_at = datetime.fromisoformat(str(row["expires_at"])).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise EnrollmentError("fleet enrollment expiry is invalid") from exc
    if expires_at <= current:
        with database.connect() as connection:
            database.begin_write(connection)
            connection.execute(
                """UPDATE infrastructure_enrollments
                   SET status = 'expired', last_error = ?
                   WHERE job_id = ? AND status = 'pending'""",
                ("enrollment token expired before activation", normalized_job),
            )
        return None
    return _decrypted_payload(encryption_key, str(row["payload_ciphertext"]))


def mark_consumed(
    database: Any,
    *,
    job_id: str,
    now: datetime | None = None,
) -> bool:
    """Commit one-time enrollment only after endpoint activation succeeds."""
    normalized_job = str(job_id).strip()
    now_text = _now(now).isoformat()
    with database.connect() as connection:
        database.begin_write(connection)
        updated = connection.execute(
            """UPDATE infrastructure_enrollments
               SET status = 'consumed', consumed_at = ?, last_error = NULL
               WHERE job_id = ? AND status = 'pending' AND payload_ciphertext IS NOT NULL""",
            (now_text, normalized_job),
        ).rowcount
    if updated:
        return True
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM infrastructure_enrollments WHERE job_id = ?",
            (normalized_job,),
        ).fetchone()
    return row is not None and str(row["status"]) == "consumed"


def expire_pending_enrollments(database: Any, now: datetime | None = None) -> int:
    """Converge stale one-time enrollment rows without touching provider state."""
    now_text = _now(now).isoformat()
    with database.connect() as connection:
        database.begin_write(connection)
        rows = connection.execute(
            "SELECT job_id FROM infrastructure_enrollments WHERE status = 'pending' AND expires_at <= ?",
            (now_text,),
        ).fetchall()
        updated = connection.execute(
            """UPDATE infrastructure_enrollments
               SET status = 'expired', last_error = COALESCE(last_error, ?)
               WHERE status = 'pending' AND expires_at <= ?""",
            ("enrollment token expired", now_text),
        ).rowcount
        for row in rows:
            connection.execute(
                """INSERT INTO infrastructure_events
                   (id, infrastructure_job_id, event_type, metadata_json, created_at)
                   VALUES (?, ?, 'enrollment_expired', ?, ?)""",
                (uuid.uuid4().hex, row["job_id"], json.dumps({"source": "expiry_pass"}), now_text),
            )
        return updated


def render_user_data(
    *,
    bootstrap_script: bytes,
    registration_url: str,
    token: str,
    job_id: str,
    node_id: str,
    control_plane_source: str,
    api_port: int,
    keys_port: int,
    ssh_port: int = 22,
    swap_mb: int = 1024,
    installer_url: str = "",
    installer_sha256: str = "",
) -> str:
    """Render cloud-init enrollment without long-lived control-plane secrets."""
    url = str(registration_url or "").strip()
    try:
        parsed_registration = urlsplit(url)
    except ValueError as exc:
        raise EnrollmentError("fleet registration URL is invalid") from exc
    if (
        parsed_registration.scheme != "https"
        or not parsed_registration.hostname
        or parsed_registration.path != "/fleet/register"
        or parsed_registration.fragment
        or parsed_registration.username
        or parsed_registration.password
        or len(url) > 512
    ):
        raise EnrollmentError("fleet registration URL must be the credential-free HTTPS /fleet/register endpoint")
    normalized_job = str(job_id).strip()
    normalized_node = str(node_id).strip()
    if not JOB_ID_RE.fullmatch(normalized_job) or not NODE_ID_RE.fullmatch(normalized_node):
        raise EnrollmentError("fleet enrollment identity is invalid")
    token_hash(token)
    try:
        source = str(control_plane_source).strip()
        ipaddress.ip_network(source, strict=False)
        ports = (int(api_port), int(keys_port), int(ssh_port))
        if any(port < 1 or port > 65535 for port in ports) or ports[0] == ports[1]:
            raise ValueError
        swap = max(0, int(swap_mb))
    except (TypeError, ValueError) as exc:
        raise EnrollmentError("fleet bootstrap network settings are invalid") from exc
    bootstrap_b64 = base64.b64encode(bytes(bootstrap_script)).decode("ascii")
    if len(bootstrap_b64) > 128 * 1024:
        raise EnrollmentError("fleet bootstrap script is too large")

    registration_client = """import base64
import json
import os
import subprocess
import urllib.request

access_path = os.environ["AURIX_ENROLLMENT_ACCESS_FILE"]
if not os.path.isfile(access_path):
    access_path = "/root/shadowbox/access.txt"
with open(access_path, encoding="utf-8") as stream:
    access = stream.read()
with open(os.environ["AURIX_ENROLLMENT_SSH_HOST_KEY_FILE"], encoding="utf-8") as stream:
    host_key = stream.read().strip()
public_ip = urllib.request.urlopen(
    "http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address",
    timeout=5,
).read().decode().strip()
payload = {
    "token": os.environ["AURIX_ENROLLMENT_TOKEN"],
    "job_id": os.environ["AURIX_ENROLLMENT_JOB_ID"],
    "node_id": os.environ["AURIX_ENROLLMENT_NODE_ID"],
    "public_ip": public_ip,
    "access_txt_b64": base64.b64encode(access.encode()).decode(),
    "ssh_host_key_b64": base64.b64encode(host_key.encode()).decode(),
}
request = urllib.request.Request(
    os.environ["AURIX_ENROLLMENT_URL"],
    data=json.dumps(payload, separators=(",", ":")).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    result = json.loads(response.read() or b"{}")
if result.get("status") not in {"accepted", "already_received", "already_consumed"}:
    raise SystemExit(1)
subprocess.run(
    ["systemctl", "disable", "--now", "aurix-node-enrollment.service"],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
for path in (
    "/etc/aurix-node/enrollment.env",
    "/usr/local/libexec/aurix-node-enrollment.py",
    "/etc/systemd/system/aurix-node-enrollment.service",
):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
subprocess.run(["systemctl", "daemon-reload"], check=False)
    """
    client_b64 = base64.b64encode(registration_client.encode()).decode("ascii")
    q = {
        "url": shlex.quote(url),
        "token": shlex.quote(str(token).strip()),
        "job": shlex.quote(normalized_job),
        "node": shlex.quote(normalized_node),
        "source": shlex.quote(source),
        "api": shlex.quote(str(ports[0])),
        "keys": shlex.quote(str(ports[1])),
        "ssh": shlex.quote(str(ports[2])),
        "swap": shlex.quote(str(swap)),
        "installer_url": shlex.quote(str(installer_url or "").strip()),
        "installer_sha": shlex.quote(str(installer_sha256 or "").strip()),
    }
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "install -d -m 0750 /etc/aurix-node /usr/local/libexec",
        'public_ip=""',
        "for _attempt in $(seq 1 30); do",
        '  public_ip="$(python3 -c \'import urllib.request; print(urllib.request.urlopen("http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address", timeout=5).read().decode().strip())\' 2>/dev/null || true)"',
        '  if [[ "$public_ip" =~ ^[0-9A-Fa-f:.]+$ ]]; then break; fi',
        "  sleep 2",
        "done",
        '[[ "$public_ip" =~ ^[0-9A-Fa-f:.]+$ ]] || exit 1',
        f"bootstrap_b64={shlex.quote(bootstrap_b64)}",
        "printf '%s' \"$bootstrap_b64\" | base64 -d | \\",
        f"  AURIX_NODE_ID={q['node']} AURIX_NODE_HOST=\"$public_ip\" \\",
        f"  AURIX_NODE_API_PORT={q['api']} AURIX_NODE_KEYS_PORT={q['keys']} \\",
        f"  AURIX_NODE_SSH_PORT={q['ssh']} AURIX_NODE_SWAP_MB={q['swap']} \\",
        f"  AURIX_CONTROL_PLANE_SOURCE={q['source']} \\",
        f"  AURIX_OUTLINE_INSTALLER_URL={q['installer_url']} \\",
        f"  AURIX_OUTLINE_INSTALLER_SHA256={q['installer_sha']} \\",
        "  bash -s",
        "cat > /etc/aurix-node/enrollment.env <<'AURIX_ENROLLMENT_ENV'",
        f"AURIX_ENROLLMENT_URL={q['url']}",
        f"AURIX_ENROLLMENT_TOKEN={q['token']}",
        f"AURIX_ENROLLMENT_JOB_ID={q['job']}",
        f"AURIX_ENROLLMENT_NODE_ID={q['node']}",
        "AURIX_ENROLLMENT_ACCESS_FILE=/opt/outline/access.txt",
        "AURIX_ENROLLMENT_SSH_HOST_KEY_FILE=/etc/ssh/ssh_host_ed25519_key.pub",
        "AURIX_ENROLLMENT_ENV",
        "chmod 0600 /etc/aurix-node/enrollment.env",
        f"client_b64={shlex.quote(client_b64)}",
        "printf '%s' \"$client_b64\" | base64 -d > /usr/local/libexec/aurix-node-enrollment.py",
        "chmod 0700 /usr/local/libexec/aurix-node-enrollment.py",
        "cat > /etc/systemd/system/aurix-node-enrollment.service <<'AURIX_ENROLLMENT_UNIT'",
        "[Unit]",
        "Description=AuriX one-time VPN node enrollment",
        "After=network-online.target docker.service",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "EnvironmentFile=/etc/aurix-node/enrollment.env",
        "ExecStart=/usr/bin/python3 /usr/local/libexec/aurix-node-enrollment.py",
        "User=root",
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ReadWritePaths=/etc/aurix-node /usr/local/libexec /etc/systemd/system",
        "TimeoutStartSec=90s",
        "Restart=on-failure",
        "RestartSec=15s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "AURIX_ENROLLMENT_UNIT",
        "systemctl daemon-reload",
        "systemctl enable --now aurix-node-enrollment.service",
    ]
    return "\n".join(lines) + "\n"
