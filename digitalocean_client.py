"""Minimal DigitalOcean provider client."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from infrastructure_support import InfrastructureError

class DigitalOceanClient:
    """Minimal DigitalOcean API client that never logs its bearer token."""

    BASE_URL = "https://api.digitalocean.com/v2"

    def __init__(self, token: str, timeout_seconds: int = 20):
        if not token:
            raise ValueError("DIGITALOCEAN_API_TOKEN is required")
        self._token = token
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.BASE_URL + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise InfrastructureError(f"DigitalOcean returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise InfrastructureError("DigitalOcean request failed") from exc
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InfrastructureError("DigitalOcean returned invalid JSON") from exc

    def create_droplet(self, specification: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("POST", "/droplets", specification)
        droplet = payload.get("droplet") if isinstance(payload, dict) else None
        if not isinstance(droplet, dict) or not droplet.get("id"):
            raise InfrastructureError("DigitalOcean create response lacks a Droplet ID")
        return droplet

    def list_droplets(self, *, tag_name: str | None = None) -> list[dict[str, Any]]:
        """Return all Droplets, following provider pagination safely."""
        droplets: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        while page <= 100:
            query = urllib.parse.urlencode({"page": page, "per_page": per_page})
            if tag_name:
                query += "&" + urllib.parse.urlencode({"tag_name": tag_name})
            payload = self._request("GET", f"/droplets?{query}")
            batch = payload.get("droplets") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise InfrastructureError("DigitalOcean response lacks Droplets")
            droplets.extend(item for item in batch if isinstance(item, dict) and item.get("id"))
            meta = payload.get("meta") if isinstance(payload, dict) else None
            total = meta.get("total") if isinstance(meta, dict) else None
            if not batch or (isinstance(total, int) and len(droplets) >= total) or len(batch) < per_page:
                break
            page += 1
        if page > 100:
            raise InfrastructureError("DigitalOcean pagination exceeded safety limit")
        return droplets

    def droplet(self, droplet_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/droplets/{urllib.parse.quote(droplet_id, safe='')}")
        droplet = payload.get("droplet") if isinstance(payload, dict) else None
        if not isinstance(droplet, dict):
            raise InfrastructureError("DigitalOcean response lacks a Droplet")
        return droplet

    def delete_droplet(self, droplet_id: str) -> None:
        """Delete one Droplet after the fleet controller's safety gates pass."""
        normalized = str(droplet_id).strip()
        if not normalized:
            raise InfrastructureError("DigitalOcean Droplet ID is required")
        self._request("DELETE", f"/droplets/{urllib.parse.quote(normalized, safe='')}")

    def action(self, action_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/actions/{urllib.parse.quote(action_id, safe='')}")
        action = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(action, dict):
            raise InfrastructureError("DigitalOcean response lacks an action")
        return action

    def billing_balance(self) -> dict[str, Any]:
        payload = self._request("GET", "/customers/my/balance")
        if not isinstance(payload, dict):
            raise InfrastructureError("DigitalOcean response lacks billing data")
        return payload
