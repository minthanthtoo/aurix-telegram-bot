#!/usr/bin/env python3
"""Synchronize stable fleet DNS names without exposing provider secrets."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from deploy.fleet_reconcile import FleetError, FleetNode, environment, parse_manifest
except ModuleNotFoundError:  # Direct execution from deploy/ sets deploy/ as sys.path[0].
    from fleet_reconcile import FleetError, FleetNode, environment, parse_manifest


TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DnsConfig:
    provider: str
    zone_id: str
    token: str
    ttl: int
    proxied: bool


@dataclass(frozen=True)
class DesiredRecord:
    node_id: str
    name: str
    record_type: str
    content: str
    ttl: int
    proxied: bool


def configured(env: dict[str, str]) -> bool:
    return any(
        (env.get("AURIX_DNS_PROVIDER", "").strip(),
         env.get("AURIX_DNS_ZONE_ID", "").strip(),
         env.get("AURIX_DNS_API_TOKEN", "").strip(),
         env.get("AURIX_DNS_ZONE", "").strip())
    )


def from_env(env: dict[str, str]) -> DnsConfig:
    provider = env.get("AURIX_DNS_PROVIDER", "").strip().lower()
    if provider != "cloudflare":
        raise FleetError("AURIX_DNS_PROVIDER must be cloudflare")
    zone_id = env.get("AURIX_DNS_ZONE_ID", "").strip()
    if not zone_id:
        raise FleetError("missing AURIX_DNS_ZONE_ID")
    token = env.get("AURIX_DNS_API_TOKEN", "").strip()
    if not token:
        raise FleetError("missing AURIX_DNS_API_TOKEN")
    try:
        ttl = int(env.get("AURIX_DNS_TTL", "300").strip() or "300")
    except ValueError as exc:
        raise FleetError("AURIX_DNS_TTL must be an integer") from exc
    if ttl != 1 and not 60 <= ttl <= 86400:
        raise FleetError("AURIX_DNS_TTL must be 1 or between 60 and 86400")
    proxied = env.get("AURIX_DNS_PROXIED", "0").strip().lower() in TRUTHY
    if proxied:
        raise FleetError("AURIX_DNS_PROXIED must stay disabled for Outline VPN endpoints")
    return DnsConfig(provider=provider, zone_id=zone_id, token=token, ttl=ttl, proxied=proxied)


def desired_records(nodes: list[FleetNode], config: DnsConfig, *, node_id: str = "all") -> list[DesiredRecord]:
    records: list[DesiredRecord] = []
    selected = [node for node in nodes if node_id == "all" or node.node_id == node_id]
    if not selected:
        raise FleetError(f"fleet node does not exist: {node_id}")
    for node in selected:
        if not node.dns_name:
            raise FleetError(f"node {node.node_id} is missing dns_name")
        address = ipaddress.ip_address(node.host)
        records.append(DesiredRecord(
            node_id=node.node_id,
            name=node.dns_name,
            record_type="AAAA" if address.version == 6 else "A",
            content=node.host,
            ttl=config.ttl,
            proxied=config.proxied,
        ))
    return records


class CloudflareClient:
    def __init__(self, config: DnsConfig) -> None:
        self.config = config
        self.base_url = f"https://api.cloudflare.com/client/v4/zones/{config.zone_id}/dns_records"

    def request(self, method: str, path: str = "", *, body: dict[str, Any] | None = None,
                query: dict[str, str] | None = None) -> dict[str, Any]:
        query_string = urllib.parse.urlencode(query or {})
        url = self.base_url + path + (f"?{query_string}" if query_string else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "User-Agent": "aurix-dns-sync/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            raise FleetError(f"Cloudflare DNS request failed: {method}") from exc
        if not isinstance(payload, dict) or not payload.get("success"):
            raise FleetError(f"Cloudflare DNS request was rejected: {method}")
        return payload

    def find_record(self, record: DesiredRecord) -> dict[str, Any] | None:
        payload = self.request("GET", query={"type": record.record_type, "name": record.name})
        matches = payload.get("result")
        if not isinstance(matches, list):
            raise FleetError("Cloudflare DNS list response is malformed")
        exact = [
            item for item in matches
            if isinstance(item, dict)
            and item.get("type") == record.record_type
            and str(item.get("name") or "").rstrip(".").lower() == record.name
        ]
        if len(exact) > 1:
            raise FleetError(f"multiple DNS records exist for {record.name} {record.record_type}")
        return exact[0] if exact else None

    def upsert(self, record: DesiredRecord) -> str:
        body = {
            "type": record.record_type,
            "name": record.name,
            "content": record.content,
            "ttl": record.ttl,
            "proxied": record.proxied,
        }
        existing = self.find_record(record)
        if existing is None:
            self.request("POST", body=body)
            return "created"
        record_id = str(existing.get("id") or "")
        if not record_id:
            raise FleetError("Cloudflare DNS record is missing id")
        unchanged = (
            existing.get("content") == record.content
            and int(existing.get("ttl", 0)) == record.ttl
            and bool(existing.get("proxied")) == record.proxied
        )
        if unchanged:
            return "unchanged"
        self.request("PATCH", f"/{urllib.parse.quote(record_id, safe='')}", body=body)
        return "updated"


def sync(env: dict[str, str], *, node_id: str = "all", dry_run: bool = False) -> dict[str, Any]:
    config = from_env(env)
    nodes = parse_manifest(env.get("AURIX_FLEET_NODES_JSON", ""))
    records = desired_records(nodes, config, node_id=node_id)
    if dry_run:
        return {
            "status": "dry-run",
            "records": [
                {
                    "node": record.node_id,
                    "name": record.name,
                    "type": record.record_type,
                    "content": record.content,
                    "ttl": record.ttl,
                    "proxied": record.proxied,
                    "action": "would-upsert",
                }
                for record in records
            ],
        }
    client = CloudflareClient(config)
    return {
        "status": "synced",
        "records": [
            {
                "node": record.node_id,
                "name": record.name,
                "type": record.record_type,
                "content": record.content,
                "ttl": record.ttl,
                "proxied": record.proxied,
                "action": client.upsert(record),
            }
            for record in records
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "sync"))
    parser.add_argument("--env-file", default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"))
    parser.add_argument("--node", default="all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        env = environment(Path(args.env_file))
        if args.command == "validate":
            config = from_env(env)
            nodes = parse_manifest(env.get("AURIX_FLEET_NODES_JSON", ""))
            records = desired_records(nodes, config, node_id=args.node)
            print(json.dumps({"status": "valid", "records": [record.name for record in records]}))
            return
        print(json.dumps(sync(env, node_id=args.node, dry_run=args.dry_run), indent=2))
    except (FleetError, OSError, ValueError) as exc:
        print(f"dns sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
