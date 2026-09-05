# AuriX node-two installation and canary runbook

This runbook is for the existing DigitalOcean Droplet `139.59.122.170`
(`595616487`, `sgp1`, `s-1vcpu-1gb`). It is currently an active, registered,
but empty capacity node. The primary node is `157.245.63.95`
(`595626749`). The two Droplets are peers, not replicas: an Outline access key
belongs to the Management API endpoint that created it.

Current evidence (5 September 2026): Singapore-B's pinned Management API is
healthy and reports Outline 1.12.3 with zero keys. A direct SSH probe from the
control-plane host timed out, so provider-side enrollment/rebuild automation
must remain disabled until the SSH path is restored and re-pinned; this does
not invalidate the read-only Management API health result. The DigitalOcean
read-only Droplet API confirms the VM is active and unlocked; the worker token
receives HTTP 403 for DigitalOcean firewall reads, so inspect the console's
cloud-firewall/network rules before changing anything. Do not open SSH broadly
as a workaround.

Do not perform the installation while the AuriX bot is serving a live customer
rollout unless the owner has explicitly approved the maintenance window. Keep
provider mutations disabled throughout installation and canary.

## 1. Access and identity preflight

1. In the DigitalOcean console, open Droplet `595616487` and use the web
   console if SSH from the workstation is unavailable. Do not reset the root
   password in chat or place it in a repository, shell history, or Telegram.
2. Verify the host identity from the console and record the output privately:

   ```sh
   hostnamectl
   cat /etc/os-release
   ip -br address
   ss -lntup
   ```

3. From the bot host, confirm the private/public SSH path and the intended
   management exposure. A reachable port is not proof that the host is ready.
4. Confirm the machine is Ubuntu 24.04, has correct time synchronization, at
   least 2 GB free disk, and no unrelated Shadowsocks/Xray service owns the
   ports that Outline will choose.

## 2. Install Outline using the official flow

Use Outline Manager's **Set up another server** flow and execute the exact
installer command it displays. Do not substitute a copied Management URL or
assume a fixed `/opt/outline/access.txt` path; installer locations vary by
version. The official server and Management API behavior are documented in the
[Outline server repository](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/README.md).

Save the following values in the password manager or root-only environment
file, never in Git, issue text, or ordinary logs:

- complete Management API URL, including its secret path;
- SHA-256 certificate fingerprint;
- Outline server version;
- selected Shadowsocks data port.

Verify locally on node two:

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
curl --fail --silent --show-error https://127.0.0.1:<management-port>/server
```

The bot must use TLS fingerprint pinning. Never disable certificate verification
just to make a canary pass.

## 3. Firewall and service boundary

- Allow SSH only from the operator's trusted source or DigitalOcean console.
- Allow the Shadowsocks data port from the Internet.
- Allow the Outline Management API port only from `157.245.63.95` (and a
  temporary operator source during setup, removed afterward).
- Keep Supabase, PostgreSQL, and Telegram credentials off node two unless a
  separately approved service requires them.
- Confirm the final rules with `nft list ruleset` or the installed firewall
  tool, and record a redacted copy in the change log.

## 4. Register the endpoint

On the bot host, update the root-only environment with a server-scoped entry.
Use placeholders while editing; insert the real URL and fingerprint only in
the protected environment:

```dotenv
OUTLINE_SERVERS_JSON=[{"id":"primary","label":"Singapore A","provider_resource_id":"595626749","api_url":"https://<primary-management-url>","cert_sha256":"<64-hex>"},{"id":"sg-b","label":"Singapore B","provider_resource_id":"595616487","api_url":"https://<node-two-management-url>","cert_sha256":"<64-hex>"}]
OUTLINE_DEFAULT_SERVER_ID=primary
AURIX_SERVER_HEALTH_MAX_AGE_SECONDS=900
```

Server IDs are durable database identities. Preserve the established
`primary` ID when converting an existing one-server deployment to a fleet;
changing only its label is safe. Reusing the same Droplet ID under a new
server ID is rejected before any partial registration can be committed.

Run the repository preflight from the deployed release, restart exactly one bot
process, and inspect startup without printing the environment:

```sh
python deploy/digitalocean_preflight.py
systemctl restart aurix-bot
journalctl -u aurix-bot -n 80 --no-pager -o cat
```

Expected state for the two DigitalOcean endpoints: both pass `/server`,
`/access-keys`, and metrics. The current full fleet (including BKK/Nube) is
reported as `3/3 server(s) healthy`; no Management URL or access key is
printed.

Run the separate worker once and expect `inventory=2 managed/2 registered`:

```sh
systemctl start aurix-infrastructure-worker.service
journalctl -u aurix-infrastructure-worker.service -n 20 --no-pager -o cat
```

## 5. Capacity declaration

Keep `AURIX_INFRASTRUCTURE_QUEUE_ENABLED=0` and
`AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=0`. In the owner/admin Capacity panel:

1. set `max_keys` and reserved headroom for each endpoint;
2. set monthly traffic commitment conservatively;
3. allocate Daily 300 MB, Monthly 3 GB, Promo, 50 GB, and 100 GB slots;
4. leave a tier at zero until its canary passes.

Allocation changes affect new issuance only. They do not migrate existing keys.

## 6. Two-node canary

Run one controlled test per endpoint and retain only redacted evidence:

1. create one daily-free key and one paid test key;
2. verify the key name, `(server_id, outline_key_id)` identity, one-click copy,
   Outline connection, and measured traffic;
3. exercise `/myvpn`, usage, order history, pagination, and refresh-in-place;
4. submit a receipt through each payment method, test AI extraction, then have
   an admin approve and reject separate test orders;
5. upload the same receipt twice and verify checksum/transaction-ID dedupe;
6. force a test key to its exact quota and verify durable termination event,
   hard deletion, delete verification, and user/admin notifications;
7. expire a test key and verify the same cleanup path;
8. confirm a node outage stops new assignment to that node while the other node
   remains usable.

Never use a real customer's receipt or production key as a forced-quota test.

## 7. Enable assisted scaling only after acceptance

After two consecutive fresh healthy observations and a successful canary,
owner approval may set `AURIX_INFRASTRUCTURE_QUEUE_ENABLED=1`. This exposes the
admin **Prepare next node** intent button only when the fleet is Prepare/Urgent.
The Telegram process never calls DigitalOcean; the worker alone enforces
allowlists, budget, cooldown, inventory, and node-count limits. Leave provider
mutations disabled until a separately approved provisioning window.

## 8. Rollback

If node two fails any canary:

1. set its tier/plan allocations to zero;
2. disable the endpoint in the protected environment and restart the bot;
3. preserve its audit/inventory evidence;
4. do not migrate or revoke customer keys automatically;
5. restore the last known-good release/configuration and rerun the primary-node
   smoke test.

The 100% acceptance criteria are listed in
[`AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md`](AUTOSCALE_ARCHITECTURE_AND_RUNBOOK.md).
