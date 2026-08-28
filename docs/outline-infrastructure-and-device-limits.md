# Outline Infrastructure and Device-Limit Decision

- Status: accepted for the owner-only staging prototype; not approval for a public launch
- Fact-checked: 2026-08-27
- Evidence standard: current first-party DigitalOcean and Outline documentation, plus upstream 3x-ui documentation and source. Network-behavior conclusions are labeled as inferences.

## Decision

Keep the official Outline server and its Management API for the current AuriX prototype.

- DigitalOcean provides the virtual machine, firewall, public IP, and billing boundary.
- Outline provides proxy access keys, per-key transfer limits, usage accounting, and revocation.
- The Telegram bot is a control-plane client. Customer traffic flows directly between the Outline client and server; it never passes through the bot.
- One key's allowance is pooled across every device that imports that key.
- Do not migrate to 3x-ui or automate device-based revocation from Prometheus during staging.
- Keep the DigitalOcean provisioning token out of the bot's normal runtime. Provisioning and VPN-key management are separate trust domains.

The first real-server test should prove only this owner-allowlisted flow:

```text
Telegram /claim
  -> persist a provisioning operation
  -> create one 100 MiB Outline key
  -> durably deliver it to the owner
  -> connect through an Outline client
  -> observe transfer accounting
  -> revoke after 24 hours
  -> reconcile and clean up every staging key
```

## Fact-check summary

| Claim | Verdict | Correction or qualification |
|---|---|---|
| DigitalOcean creates Droplets with `POST /v2/droplets` | Confirmed | The full endpoint is `https://api.digitalocean.com/v2/droplets`, not `https://digitalocean.com`. |
| A Personal Access Token is sent as a Bearer token | Confirmed | Use least-privilege scopes and do not give the runtime bot this token. |
| `user_data` can automate first boot | Confirmed | Cloud-init consumes it on first boot. It runs privileged code and can leave output in logs, so it must not contain long-lived secrets. |
| Outline emits `apiUrl` and `certSha256` | Confirmed | `apiUrl` contains the secret management path. The fingerprint is an integrity pin rather than an authentication secret, but protect the configuration bundle. |
| Keys are created at `POST /access-keys` | Confirmed | Current API also supports caller-selected IDs with `PUT /access-keys/{id}`, which is useful for recovery after ambiguous network failures. |
| The default limit endpoint is `/experimental/access-key-data-limit` | Outdated | Use `/server/access-key-data-limit`; the experimental path is explicitly deprecated. |
| `verify=False` is standard for remote Outline management | Rejected | It disables server authentication. Pin the installer-provided certificate fingerprint or explicitly trust the exact certificate. |
| A key limit is a monthly or lifetime quota | Rejected | Outline enforces a trailing 30-day per-key limit based on server egress. It is not a calendar-month or lifetime counter. |
| Outline has a whole-server transfer cap | Rejected | The server setting is a default applied to each key. Outline does not expose an aggregate server budget such as 1 TB per 30 days. |
| Outline can hard-limit physical devices per key | Rejected | The same key can be reused on any number of devices. All their traffic shares the key's quota. |
| Prometheus exposes a stable `remote_ip` device identity | Rejected | Official examples expose counters such as `shadowsocks_data_bytes` grouped by `access_key`; they do not define a stable physical-device identity. |
| 3x-ui `Limit IP` is a device limit | Partly true | It caps simultaneous source IPs through Fail2ban. It is a network-observation control, not proof of a physical device. |

Do not encode a claim that Outline requires exactly 1 vCPU and 1 GiB as an API invariant. Select a current image and size supported in the target region, validate the latest installer requirements, and record the tested combination.

## API and trust boundaries

### DigitalOcean infrastructure API

The API base URL is:

```text
https://api.digitalocean.com
```

Create a Droplet with:

```text
POST /v2/droplets
```

The request requires `region`, `size`, and `image`. Query `/v2/regions`, `/v2/sizes`, and `/v2/images` at provisioning time because slugs and regional capacity change. Authenticate with:

```http
Authorization: Bearer DIGITALOCEAN_TOKEN
```

Use an SSH key, attach a default-deny firewall, tag the Droplet as staging, and use the narrowest available token scopes. If `user_data` is used, treat it as privileged first-boot code: review it, pin downloaded artifacts where practical, and never embed the Telegram token, Outline management URL, or another long-lived secret in it.

Creating a Droplet is a billable external action. Automation must present the selected region, image, size, current price, firewall plan, and teardown procedure before creation. The runtime Telegram bot does not need permission to create or destroy Droplets.

### Outline installation output

The installer emits management configuration shaped like:

```json
{
  "apiUrl": "https://SERVER:MANAGEMENT_PORT/SECRET_PATH",
  "certSha256": "LEAF_CERTIFICATE_SHA256"
}
```

The secret path in `apiUrl` authorizes management operations. `certSha256` is the SHA-256 fingerprint of the self-signed leaf certificate and authenticates the endpoint when correctly pinned. Store the bundle in a secret store or ignored local environment file with restrictive permissions.

On first connection, call `GET /server` and record the server version as non-secret operational metadata. The bot must be integration-tested against that exact version; `master` documentation alone is not a deployed-version guarantee.

### Outline Management API contract

Paths below are relative to the complete secret `apiUrl` prefix.

| Action | Method and path | Success |
|---|---|---:|
| Read server information and version | `GET /server` | `200` |
| Create a server-selected key ID | `POST /access-keys` | `201` |
| Create a caller-selected key ID | `PUT /access-keys/{id}` | `201` |
| List keys | `GET /access-keys` | `200` |
| Get one key | `GET /access-keys/{id}` | `200` |
| Rename a key | `PUT /access-keys/{id}/name` | `204` |
| Set or remove one key's limit | `PUT` or `DELETE /access-keys/{id}/data-limit` | `204` |
| Delete/revoke a key | `DELETE /access-keys/{id}` | `204` |
| Read transfer usage by key ID | `GET /metrics/transfer` | `200` |
| Set or remove the default per-key limit | `PUT` or `DELETE /server/access-key-data-limit` | `204` |
| Read richer, unstable metrics | `GET /experimental/server/metrics?since=...` | `200` |

`POST /access-keys` currently accepts optional `name`, `method`, `password`, `port`, and `limit: {"bytes": ...}` fields. Do not assume every historical Outline deployment supports that request shape; verify the deployed version and response.

`PUT /metrics/enabled` controls anonymous metrics sharing. It does not turn local transfer accounting on or off and should not be conflated with `GET /metrics/transfer`.

### Limit semantics

A `100 MiB` key limit means `104857600` bytes of counted server egress during the trailing 30 days.

- The window is rolling, not aligned to a calendar month.
- Available capacity returns as traffic becomes older than 30 days.
- Usage cannot be reset on an existing key; the operator can raise the limit or create a new key.
- A per-key limit overrides the server's default per-key limit.
- The default server setting is not one aggregate quota shared by all keys.
- Outline does not notify users when they approach or reach the limit.
- The planned 24-hour expiry is an AuriX policy enforced by deleting the key. Outline's byte limit does not provide a 24-hour TTL.

Creating a fresh key for every claim also creates a fresh trailing-window allowance. Account, global issuance, and cost controls must therefore exist before any public test.

## TLS, secrets, and network exposure

Do not use `verify=False` or `curl -k` for remote bot-to-server requests. The bot's existing certificate-pin design is the correct model:

1. complete the TLS handshake without sending the HTTP request;
2. hash the peer leaf certificate's DER bytes with SHA-256;
3. compare it exactly with the normalized `OUTLINE_CERT_SHA256` value;
4. send the management request only when the pin matches;
5. fail closed on mismatch.

Alternatively, configure the HTTP client to trust the exact installer certificate. Do not combine a disabled verifier with an unchecked fingerprint.

Never place these values in Git, ordinary logs, analytics, crash reports, or support screenshots:

- DigitalOcean Personal Access Tokens;
- the full Outline `apiUrl`;
- Telegram bot tokens;
- Outline `accessUrl` values, passwords, and QR codes;
- database files containing Telegram IDs or credential-delivery state.

The fingerprint alone is not equivalent to the secret `apiUrl`, but there is no operational benefit in publishing the configuration bundle.

Firewall rules must distinguish management and proxy traffic:

- restrict SSH TCP to the operator's stable source IP;
- restrict the Outline management TCP port to the bot host's stable egress IP or a private tunnel;
- allow every assigned access-key port over both TCP and UDP from clients;
- verify that no second DigitalOcean firewall adds a broader management rule.

The secret management path is defense in depth, not a substitute for restricting the management port. A bot running from a changing residential IP needs a tunnel or stable host before IP allowlisting will be dependable.

## Required recovery semantics

These are requirements for the real-server integration, not claims that the current prototype already implements them.

1. Persist a unique provisioning-operation ID before calling Outline.
2. Derive or reserve a caller-selected key ID for that operation where the deployed server supports it.
3. Perform remote network I/O outside long-lived SQLite write transactions.
4. If a create response is lost, use `GET /access-keys/{id}` to reconcile before retrying; do not blindly create a second key.
5. Persist delivery state before advancing past a Telegram update. A sent-message timeout must be reconcilable without consuming the user's one claim.
6. Never log an `accessUrl`. If reliable redelivery requires temporary persistence, encrypt it at rest, restrict access, set a short retention period, and delete it after acknowledgement or expiry.
7. Treat `DELETE 404` as converged only while reconciling a known key that was already scheduled for deletion.
8. Retry revocation with bounded batches, timeouts, backoff, and a next-attempt timestamp so an Outline outage cannot block Telegram polling.
9. Reconcile the remote key inventory at startup and after every staging failure so a database commit failure cannot leave an untracked active key.

Until this state machine is implemented, an owner-only live trial must take before/after key inventories and manually revoke any orphan. It is not safe for public users.

## Device-limit assessment

### Official Outline

Outline explicitly permits one access key to be used on any number of devices. Its Management API does not expose a hard physical-device limit. All devices using the same `ss://` credential contribute to the same key allowance.

For the 100 MiB staging trial, the pooled byte allowance bounds transfer per issued key. It does not bound the number of keys an account farm could cause the service to issue.

### 3x-ui / Xray

3x-ui is a third-party management stack, not an Outline component. Its `Limit IP` setting caps simultaneous source IPs and enforces the limit through Fail2ban. Upstream documents a default 30-minute ban; the `3x-ipl` jail applies temporary firewall bans to TCP and UDP while excluding configured SSH and panel ports.

Because the control counts observable source IPs rather than physical devices, the following are network-behavior inferences that must be tested in the intended topology:

- several devices behind one NAT or CGNAT may be undercounted;
- one device changing mobile or Wi-Fi networks may be overcounted;
- upstream warns that IP-limit behavior may not work correctly through IP tunnels;
- VPN chaining and relays can produce false matches.

For Docker deployments, `iptables` enforcement needs `NET_ADMIN`; upstream also configures `NET_RAW`. Without the necessary capabilities, bans can be logged but not applied. Host networking exposes all panel and inbound ports, so explicit port mappings are safer when host mode is unnecessary. The panel needs TLS, unique credentials, TOTP, a non-default port, a random base path, and strict firewalling.

The current 3x-ui API source also exposes a `limitHwid` client field. This is not evidence of a secure, universal hardware identity. It must be assessed separately with compatible clients and a defined trust and spoofing model before it can support a product promise.

If source-IP deterrence later becomes a firm requirement, evaluate 3x-ui as a separate server/control-plane migration with a version-pinned API. Current API work should target `/panel/api/clients/...`; pasted legacy `/panel/api/inbounds/addClient` examples are not a safe integration contract.

### Metrics and Prometheus

The stable Management API endpoint `GET /metrics/transfer` returns transfer bytes keyed by access-key ID. Official Prometheus examples use metrics such as:

```promql
sum(increase(shadowsocks_data_bytes[1d])) by (access_key)
```

Do not invent or depend on undocumented labels such as:

```text
shadowsocks_data_bytes{user="...", remote_ip="..."}
```

The experimental REST metrics endpoint currently includes values such as `lastTrafficSeen` and `peakDeviceCount`, but it is explicitly unstable and may disappear. Neither it nor connection counters establish a trustworthy physical-device identity.

Keep Prometheus bound privately and access it through SSH forwarding or another authenticated private path. Metrics may support capacity planning, anomaly scoring, or operator review; they must not trigger irreversible credential deletion without a documented, tested policy and recovery path.

## Owner-only staging runbook

### Preflight

- [ ] Use a disposable staging Droplet with an explicit spending bound and teardown time.
- [ ] Query current region, size, and image availability instead of copying old slugs.
- [ ] Use SSH keys, disable password login, and restrict SSH to the operator.
- [ ] Apply firewall rules before exposing Outline: management TCP restricted; access-key TCP and UDP public.
- [ ] Install from an official, reviewed release or record the exact installer commit and digest used.
- [ ] Store `apiUrl`, `certSha256`, and the bot token outside Git with restrictive permissions.
- [ ] Call pinned `GET /server`; record the version and verify the hostname and access-key port.
- [ ] Add an owner Telegram-ID allowlist before real `/claim` provisioning.
- [ ] Use a separate staging bot token, database, and test key namespace.
- [ ] Capture `GET /access-keys` as the pre-test inventory and remove or label installer-created keys.

### Test

- [ ] Persist one test operation and create one `104857600`-byte key with a deterministic ID where supported.
- [ ] Verify the returned key ID with `GET /access-keys/{id}` and confirm the limit in the listed key representation or with a bounded transfer test.
- [ ] Deliver the access URL only to the allowlisted owner; do not print or screenshot it.
- [ ] Import it into an official Outline client from an independent network.
- [ ] Confirm traffic works and observe the key's transfer counter increase.
- [ ] Confirm manual revocation prevents subsequent traffic or reconnection, and record any delay for an already-open connection.
- [ ] Confirm a repeated delete returning `404` is reconciled as already absent.
- [ ] Test bot restart/replay behavior before waiting for the 24-hour expiry path.
- [ ] Verify the scheduled expiry eventually deletes the correct remote key.

### Cleanup

- [ ] Compare the final remote inventory with the pre-test inventory.
- [ ] Revoke every staging key, including any key not represented in SQLite.
- [ ] Remove temporary credential-delivery records and rotate any secret exposed during debugging.
- [ ] Destroy the Droplet, or explicitly document the owner, purpose, monthly cost, and next review date.

## Revisit triggers

Reconsider device/IP enforcement only when at least one of these is measured:

- key sharing materially increases support or bandwidth cost;
- paid terms explicitly promise a concurrent-IP or compatible-client device limit;
- quotas, account limits, issuance limits, and operator review do not control abuse;
- users accept the false-positive behavior of source-IP enforcement;
- the team is prepared to own another proxy stack, panel, API, and security lifecycle.

Until then, prioritize reliable provisioning, delivery, reconciliation, revocation, usage measurement, global issuance controls, and unit economics.

## Primary references

DigitalOcean:

- [Create a Droplet](https://docs.digitalocean.com/products/droplets/how-to/create/)
- [Droplet API reference](https://docs.digitalocean.com/products/droplets/reference/api/droplets/)
- [User data and cloud-init](https://docs.digitalocean.com/products/droplets/how-to/provide-user-data/)
- [Personal Access Tokens](https://docs.digitalocean.com/reference/api/create-personal-access-token/)
- [`droplet:create` scope](https://docs.digitalocean.com/reference/api/scopes/droplet/create/)
- [Cloud firewall behavior](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/)

Outline:

- [Advanced server installation](https://developer.getoutline.org/vpn/getting-started/server-setup-advanced/)
- [Management API OpenAPI specification](https://github.com/OutlineFoundation/outline-server/blob/master/src/shadowbox/server/api.yml)
- [Share management access](https://developer.getoutline.org/vpn/management/share-management-access/)
- [Data-limit semantics](https://support.getoutline.org/manager/server-management/data-limits/)
- [Access-key reuse across devices](https://support.getoutline.org/client/getting-started/access-key-reuse/)
- [Prometheus performance metrics](https://developer.getoutline.org/vpn/management/metrics/)

3x-ui / Xray (third-party alternative):

- [3x-ui client and IP-limit settings](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/config/clients.mdx)
- [3x-ui security and Fail2ban behavior](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/operations/security.mdx)
- [3x-ui installation and Docker networking](https://github.com/MHSanaei/3x-ui/wiki/Installation)
- [3x-ui current API source](https://github.com/MHSanaei/3x-ui/blob/main/frontend/src/pages/api-docs/endpoints.ts)
- [Xray traffic statistics](https://xtls.github.io/en/config/stats.html)
