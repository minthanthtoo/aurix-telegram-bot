# BKK/Nube node (`bkk-a`) runbook

This runbook records the third AuriX endpoint on Nube Cloud and the safe
procedure for future rebuilds. The node is a data-plane peer, not a replica of
the Singapore Droplets. Its Outline key IDs and transfer counters are local to
the BKK endpoint and must always be joined as `(server_id, outline_key_id)`.

## Current verified identity

| Field | Value |
| --- | --- |
| AuriX server ID | `bkk-a` |
| Provider/zone | Nube Cloud / BKK1 |
| Public host | `191.40.15.51` |
| OS | Ubuntu 24.04 |
| Outline | 1.12.3 |
| Management API | TCP 61603, restricted to the control-plane source |
| New access-key port | TCP/UDP 443 |
| Control-plane policy | max 10 keys, reserve 3, 200 GB committed traffic |
| Admission policy | all plan/tier slots currently zero until owner approval |

The complete Management API URL and certificate fingerprint are credentials.
Keep them only in `/etc/aurix-bot/aurix.env` (or the recovery environment),
never in this document, Git, Telegram, or ordinary logs.

## Provider boundary and cost safety

Nube has no provider adapter in AuriX and no documented public VM-management API
is assumed. The control plane therefore performs only pinned-SSH bootstrap,
read-only inspection, and Outline management calls. It does not create, resize,
rebuild, pause, resume, or destroy a Nube VM. Those are billable/provider
mutations and require an owner-approved provider-console action. Record the
monthly price, billing status, renewal date, and owner in the private operator
inventory before opening customer admission.

## Rebuild procedure

1. Confirm the VM is funded and running in Nube; record the new public IP and
   SSH host fingerprint out of band.
2. Allow SSH only from the operator/control-plane source. Allow TCP/UDP 443
   from customers and TCP 61603 only from the control plane.
3. Install the pinned Outline release through Outline Manager's **Set up
   another server** flow. Capture `apiUrl` and `certSha256` in the protected
   environment; never assume `/opt/outline/access.txt` exists.
4. Update the private `AURIX_FLEET_NODES_JSON` entry while preserving `id=bkk-a`
   and `provider=nube`. Change `host`, `api_port`, and `keys_port` only to the
   values verified on the replacement VM.
5. Run the control-plane reconciliation. It discovers/validates the management
   identity over pinned SSH, refreshes inventory, and applies the declared
   zero-admission policy. It must not enable sales merely because SSH succeeds.
6. Run the reversible canary below. Keep plan/tier slots at zero until every
   result is recorded and the owner opens a conservative tranche.

## Reversible data-plane canary

Run from the control plane using the deployed virtual environment and the
protected env file. The script should create a 1 MB canary key, verify
`portForNewAccessKeys`, TCP connectivity to the access URL, a server-scoped
`GET /access-keys/{id}`, and transfer-metrics shape. Always delete the key in a
`finally` block and verify a 404 afterward. Print only the key ID and host/port;
never print the `ss://` value or management URL.

Expected evidence for the current node is:

```text
server_version 1.12.3
advertised_data_port 443
data_port_tcp reachable
server_scoped_lookup present
transfer_metrics_shape dict
deleted <id>
post_delete_lookup absent
```

If TCP/443 is closed before the canary key exists, that is normal for this
Outline installation: the Shadowsocks listener is created when the first key
is present. If it remains closed after creation, stop admission and inspect
Outline state, UFW/nftables, and the Nube network policy.

## Admission and failure behavior

- Keep `AURIX_INFRASTRUCTURE_MUTATIONS_ENABLED=0`; Nube is not an automated
  provider target.
- Keep BKK plan/tier slots at zero during installation and outage recovery.
- Open only a bounded owner-approved tranche after two fresh healthy inventory
  observations and the canary.
- A BKK management or metrics outage stops new assignment there but does not
  stop Telegram/admin recovery or affect keys already assigned to other nodes.
- Do not migrate or recreate existing customer keys automatically when the
  public IP changes. Reissue only through the audited replacement workflow.
- Back up the Outline state and verify encrypted off-site recovery before any
  provider-console rebuild or shutdown.

