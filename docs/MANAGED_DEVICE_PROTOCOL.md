# AuriX managed-device protocol

The managed-device API is an optional client integration. It is not required
for VPN traffic, manual Outline clients, server-side health probes, or the
Telegram bot.

## Topology

```text
Telegram bot ── creates one-time pairing token ──> user device
user device ── signed HTTPS control request ──> AuriX control API
user device ── VPN traffic directly ──> selected Outline endpoint
```

The control API only distributes state and credentials. It is never a VPN
traffic tunnel. Node agents independently run server-to-server and
server-to-public-target probes; their signed results go to the same control
plane and are visible to Telegram administrators.

## Pairing

1. The user sends `/pair` to the bot.
2. The bot returns `AURIX_DEVICE_API_URL` and a single-use token valid for five
   minutes. The token is never stored in plaintext.
3. The client generates an Ed25519 key pair locally and POSTs the token plus
   its public key to `/v1/devices/pair`.
4. The server returns an opaque `device_id` and the public key used to verify
   signed manifests.

The Telegram ID is not exposed as the device identity. A user may enroll
multiple devices; revoking one device increments the account revocation epoch
and invalidates its future requests.

## Signed requests

Every request after pairing carries:

```text
X-AuriX-Device-ID
X-AuriX-Request-Timestamp
X-AuriX-Request-Signature
```

The Ed25519 signature covers:

```text
METHOD\nPATH[?QUERY]\nTIMESTAMP\nSHA256(BODY)
```

The server accepts only a bounded five-minute clock window. HTTPS termination
must be provided by a trusted edge; the local WSGI listener binds to loopback.

## Manifest and route configuration

`GET /v1/devices/manifest` returns an Ed25519-signed, fifteen-minute manifest
containing only opaque account/device IDs, entitlement/generation references,
logical region, protocol, and transport metadata. It contains no management
URL, management secret, or raw `ss://` access URL.

The client chooses a route from the manifest, then requests:

```text
GET /v1/devices/config?route_id=<generation-id>
```

That response is authenticated by the device signature and contains one
account-owned, currently active route configuration. A changed credential is a
new generation; the previous generation is revoked. A client must discard
expired manifests and stop using a revoked generation.

`device_client.py` is a reference implementation for native-client authors;
it does not attempt to implement a VPN engine.

## Availability and dependency matrix

| Feature | Requires managed client | Requires VPN client | Requires probe agent |
|---|---:|---:|---:|
| Telegram purchase, pairing, and manual key delivery | No | No | No |
| Existing Outline client VPN session | No | Yes | No |
| Server-to-server ping/throughput auto-discovery | No | No | Yes |
| Telegram probe summary and route recommendation | No | No | Results from agents |
| Signed route manifest and automatic refresh | Yes | No | No |
| Authenticated route config delivery | Yes | No | No |

Probe evidence is therefore a control-plane input to route admission and
selection, not a dependency on the customer device or its VPN client.
