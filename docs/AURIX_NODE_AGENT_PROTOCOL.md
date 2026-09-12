# AuriX node-agent contract

The commerce service remains the source of truth for accounts, entitlements,
credential generations, quotas, failover, and device delivery. A node agent is
an intentionally small provider boundary on each VPN server. It owns only the
provider-specific user/config lifecycle and reports observations back through
the adapter contract.

## Required endpoints

The current client contract uses an authenticated local or mTLS-protected HTTP
surface:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/server` | version/capability and management health |
| `GET` | `/v1/users` | inventory of provider users |
| `GET` | `/v1/users/{id}` | deterministic read-back |
| `POST` | `/v1/users` | create or reconcile a user |
| `DELETE` | `/v1/users/{id}` | revoke authentication |
| `PATCH` | `/v1/users/{id}/quota` | apply an absolute quota cap |
| `GET` | `/v1/users/{id}/usage` | cumulative/reset-aware per-user usage |
| `POST` | `/v1/users/{id}/sessions/terminate` | optional force-disconnect |
| `POST` | `/v1/probe` | authenticated data-plane probe |

The repository now also contains `aurix_vpn.node_agent_app.NodeAgentService` and
`create_node_agent_wsgi_app`. They implement this HTTP boundary without binding
the project to a particular Xray or Hysteria2 SDK: the provider is injected and
must implement the lifecycle methods listed above. The service requires an
exact Bearer token, rejects oversized/non-object bodies, validates path IDs,
returns bounded JSON errors, and never logs request headers or payloads.

Inventory and server-info responses redact access URLs, tokens, certificates,
fingerprints, private keys, and provider secrets. Authenticated `POST /v1/users`
and `GET /v1/users/{id}` may return the customer-scoped `secret` required by the
controller to render a config; this value is returned only over the protected
agent channel and is never written to logs or inventory responses. Provider
implementations remain responsible for not returning unrelated secrets.

Every mutating operation must be idempotent by external user ID. A timeout is
ambiguous: the worker reads the user back before deciding whether a create
committed. Unknown provider users are reported and preserved; they are never
deleted by reconciliation.

Deleting provider authentication is not proof that an already-established
session has disconnected. If `/v1/users/{id}/sessions/terminate` is unsupported
or cannot prove termination, AuriX records verified auth revocation but keeps
the generation in accounting/lease state. A later session observation must call
the identity finalization path before that lease is released.

## Local Xray config fallback

`XrayConfigWriter` updates only an inbound tagged `aurix-managed`, preserves
all other inbounds and users, and commits through a same-directory atomic
replace. It does not restart Xray or execute shell commands. A supervisor must
validate the generated JSON and perform a separately controlled reload.

The writer is suitable for a lab or a deliberately minimal node agent. Before
production use, validate Xray's actual statistics behavior, quota semantics,
restart persistence, and force-disconnect behavior on an isolated server.

The Xray writer serializes each read-modify-write mutation with a mode-0600
sidecar lock while retaining same-directory atomic replacement. The Hysteria2
encrypted user store uses the same mutation-lock pattern, so concurrent agent
requests cannot silently discard another customer update.

`XrayConfigProvider` is the concrete implementation of this conservative
fallback. It combines the tagged writer with an injected, supervised reload
callback and an injected StatsService query. It preserves unknown users and
fails closed when a reload or statistics response is invalid. If a changed
config cannot be reloaded, it restores and reloads the exact pre-mutation
config before returning an error, so file read-back is never mistaken for a
usable credential. It intentionally does not advertise hard-quota enforcement
or force-disconnect: the statistics interface is an accounting source, not
proof of either behavior.

`Hysteria2UserStore` and `Hysteria2Provider` provide the corresponding
Hysteria2 lab boundary. The user store keeps a keyed digest for authentication
and a Fernet-encrypted copy for customer delivery; the auth callback accepts
the documented HTTP-auth request shape. `Hysteria2TrafficStatsClient` is
bounded, accepts only a loopback-local HTTP(S) URL without embedded
credentials, query, or fragment, and sends the Traffic Stats API secret
explicitly. `/traffic` supplies usage and `/kick` is treated only as a
disconnect request because a kick does not by itself prevent a reconnect.
Hysteria2 quota enforcement is therefore not advertised by this backend.

The current managed Hysteria2 adapter and binding require the explicit route
field `auth_mode: "http"`. Hysteria2's HTTP callback returns the unique client
ID that its Traffic Stats API then reports and kicks, so this is the only
implemented customer-isolation/accounting mode. Missing, `password`, and
`userpass` modes fail before a local or remote user is created. Adding another
auth mode requires a separate adapter/provider implementation plus the same
per-customer lifecycle evidence; the existing shared-password deployment
cannot be bound by configuration mistake.

The encrypted HTTP-auth store derives and persists a one-to-one keyed digest
index from customer authentication secret to Hysteria client ID. It rejects a
second customer ID using the same secret, preserves compatible version-1 store
files by rebuilding the index in memory, and writes version 2 on the next user
mutation. This keeps traffic, kick, and accounting identity unambiguous without
storing plaintext customer secrets outside their encrypted record.

The WSGI service and these concrete backends are contract boundaries, not proof
that a provider is safe for commercial traffic. A real deployment still needs
local/mTLS transport policy, service supervision, restart reconciliation,
encrypted secret-key operations, and the protocol-specific evidence gates in
the fleet feasibility record. The default route registry remains Outline-only
until those gates are passed.

## Explicit controller bindings

The controller can opt into reviewed node-agent routes with the
`AURIX_MANAGED_NODE_AGENTS_JSON` environment variable. Its value is a bounded
JSON list; each entry contains `endpoint_id`, `protocol`, `base_url`, `token`,
and a scalar-only `route` object. Xray and Hysteria2 are currently the only
accepted managed protocols. Example shape (with non-production placeholders):

```json
[
  {
    "endpoint_id": "sg-a",
    "protocol": "xray",
    "base_url": "https://127.0.0.1:18001",
    "token": "<node-agent-token>",
    "route": {
      "public_address": "<canary-address>",
      "port": 18443,
      "public_key": "<reality-public-key>",
      "server_name": "<sni>",
      "short_id": "<short-id>"
    }
  }
]
```

Runtime construction registers configured adapters as candidates and wires the
route/adapter callbacks into managed quota, reconciliation, revocation, and
protocol-aware failover. It does **not** promote an endpoint protocol profile;
fresh evidence and the existing explicit operator promotion boundary remain
required. An absent variable leaves the runtime unchanged, and malformed or
unsafe bindings stop startup before a managed route can be used. Tokens are
held only by the node-agent client and are never included in route metadata or
operator browser payloads. Production use still requires a separately
reviewed canary-only binding, protected transport, restart/reconciliation
evidence, and approval for any live mutation.

Binding validation now requires HTTPS for remote node agents and permits plain
HTTP only for loopback-local agents (127.0.0.1, ::1, or localhost). URL
userinfo, query strings, and fragments are rejected so bearer tokens cannot
be sent alongside ambiguous or embedded endpoint credentials.

Managed Xray and Hysteria2 adapters additionally validate the route's public
host and port before calling the provider: the host must be a plain DNS name or
IP address without URI delimiters or whitespace, and the port must be an
integer from 1 through 65535. This is a customer-export safety boundary, not a
live data-plane compatibility claim.

## Protocol scope

The same lifecycle contract can back Xray/VLESS, VMess, Trojan, Shadowsocks,
and Hysteria2. Protocol-specific URI rendering and accounting remain in the
connectivity adapters. A protocol is not enabled in the default registry until
its management, usage, quota, restart, and data-plane evidence is recorded.
