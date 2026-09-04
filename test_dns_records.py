from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.dns_records import CloudflareClient, FleetError, desired_records, from_env, sync
from deploy.dns_sync_worker import run_once
from deploy.fleet_reconcile import parse_manifest


def env(**overrides: str) -> dict[str, str]:
    values = {
        "AURIX_DNS_PROVIDER": "cloudflare",
        "AURIX_DNS_ZONE_ID": "zone-test",
        "AURIX_DNS_API_TOKEN": "token-test",
        "AURIX_DNS_TTL": "300",
        "AURIX_FLEET_NODES_JSON": json.dumps([{
            "id": "sg-a",
            "label": "Singapore A",
            "host": "192.0.2.10",
            "dns_name": "sg-a.vpn.example.com",
            "api_port": 61603,
            "keys_port": 443,
            "max_keys": 10,
            "reserved_keys": 2,
        }, {
            "id": "bkk-a",
            "label": "Bangkok A",
            "host": "2001:db8::10",
            "dns_name": "bkk-a.vpn.example.com",
            "api_port": 61603,
            "keys_port": 443,
            "max_keys": 10,
            "reserved_keys": 2,
        }]),
    }
    values.update(overrides)
    return values


class DnsRecordTests(unittest.TestCase):
    def test_desired_records_map_ipv4_and_ipv6(self) -> None:
        config = from_env(env())
        records = desired_records(parse_manifest(env()["AURIX_FLEET_NODES_JSON"]), config)

        self.assertEqual(records[0].record_type, "A")
        self.assertEqual(records[0].content, "192.0.2.10")
        self.assertEqual(records[1].record_type, "AAAA")
        self.assertEqual(records[1].content, "2001:db8::10")

    def test_dry_run_reports_sanitized_upserts(self) -> None:
        report = sync(env(), dry_run=True)

        self.assertEqual(report["status"], "dry-run")
        self.assertEqual(report["records"][0]["action"], "would-upsert")
        self.assertNotIn("token-test", json.dumps(report))

    def test_rejects_partial_or_proxy_configuration(self) -> None:
        with self.assertRaises(FleetError):
            from_env(env(AURIX_DNS_ZONE_ID=""))
        with self.assertRaises(FleetError):
            from_env(env(AURIX_DNS_PROXIED="1"))

    def test_rejects_node_without_dns_name(self) -> None:
        values = env()
        manifest = json.loads(values["AURIX_FLEET_NODES_JSON"])
        manifest[0].pop("dns_name")
        values["AURIX_FLEET_NODES_JSON"] = json.dumps(manifest)

        with self.assertRaisesRegex(FleetError, "missing dns_name"):
            sync(values, dry_run=True)

    def test_cloudflare_upsert_creates_updates_and_skips(self) -> None:
        config = from_env(env())
        record = desired_records(parse_manifest(env()["AURIX_FLEET_NODES_JSON"]), config, node_id="sg-a")[0]
        client = CloudflareClient(config)

        with patch.object(client, "request", side_effect=[
            {"success": True, "result": []},
            {"success": True, "result": {"id": "created"}},
        ]):
            self.assertEqual(client.upsert(record), "created")

        with patch.object(client, "request", side_effect=[
            {"success": True, "result": [{"id": "rec-1", "name": record.name, "type": "A",
                                          "content": "198.51.100.7", "ttl": 300, "proxied": False}]},
            {"success": True, "result": {"id": "rec-1"}},
        ]):
            self.assertEqual(client.upsert(record), "updated")

        with patch.object(client, "request", return_value={
            "success": True,
            "result": [{"id": "rec-1", "name": record.name, "type": "A",
                        "content": record.content, "ttl": 300, "proxied": False}],
        }):
            self.assertEqual(client.upsert(record), "unchanged")

    def test_dns_worker_is_noop_until_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "aurix.env"
            env_file.write_text("AURIX_DNS_SYNC_ENABLED=0\n", encoding="utf-8")
            with patch("deploy.dns_sync_worker.sync") as sync_mock:
                self.assertEqual(run_once(env_file), 0)
                sync_mock.assert_not_called()

    def test_dns_worker_writes_only_when_enabled_and_reports_sanitized_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "aurix.env"
            env_file.write_text(
                "AURIX_DNS_SYNC_ENABLED=1\n"
                "AURIX_DNS_PROVIDER=cloudflare\n"
                "AURIX_DNS_ZONE_ID=zone-test\n"
                "AURIX_DNS_API_TOKEN=secret-token\n"
                "AURIX_FLEET_NODES_JSON='" + env()["AURIX_FLEET_NODES_JSON"] + "'\n",
                encoding="utf-8",
            )
            with patch(
                "deploy.dns_sync_worker.sync",
                return_value={"status": "synced", "records": [{"action": "unchanged"}]},
            ) as sync_mock:
                self.assertEqual(run_once(env_file), 0)
                sync_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
