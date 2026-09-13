"""Decrypt Android collector envelopes without persisting plaintext."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAX_ENVELOPE_BYTES = 8 * 1024 * 1024


def open_envelope(envelope_bytes: bytes, private_key_path: Path) -> dict[str, Any]:
    if not envelope_bytes or len(envelope_bytes) > MAX_ENVELOPE_BYTES:
        raise ValueError("invalid envelope size")
    try:
        envelope = json.loads(envelope_bytes)
        if int(envelope.get("version")) != 1:
            raise ValueError("unsupported envelope version")
        encrypted_key = base64.b64decode(envelope["key"], validate=True)
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid encrypted envelope") from exc
    if len(nonce) != 12:
        raise ValueError("invalid envelope nonce")
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    content_key = private_key.decrypt(
        encrypted_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    try:
        payload = json.loads(AESGCM(content_key).decrypt(nonce, ciphertext, None))
    except Exception as exc:
        raise ValueError("envelope authentication failed") from exc
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    return payload
