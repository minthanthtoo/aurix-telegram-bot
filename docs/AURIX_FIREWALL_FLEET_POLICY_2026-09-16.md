# AuriX VPN fleet firewall policy

Date: 2026-09-16 (Asia/Rangoon)

This is the current least-privilege ingress policy for the three VPN nodes.
It deliberately treats `bkk-a` as a first-class fleet node even though it is a
Nube Cloud host and therefore cannot be attached to a DigitalOcean Cloud
Firewall.

## Fleet boundary

| Node | Address | Provider | Firewall boundary | Role |
|---|---|---|---|---|
| `sg-a` / primary | `157.245.63.95` | DigitalOcean | DO Cloud Firewall `sg-a-core` plus host UFW | AuriX control plane, Outline, Xray, Hysteria2, and relays |
| `sg-b` | `139.59.122.170` | DigitalOcean | DO Cloud Firewall `sg-b-outline` plus host UFW | Standalone Outline data node |
| `bkk-a` | `191.40.15.51` | Nube Cloud | Host UFW only | Independent Bangkok Outline data node |

`bkk-a` must not be represented as a second copy of sg-a or as a DigitalOcean
droplet. Its direct data endpoint and its sg-a management relay are two paths
to the same Bangkok Outline server.

## Effective ingress policy

### sg-a

The attached DO Cloud Firewall permits:

- TCP `22` only from the current operator address `45.41.106.60/32`;
- TCP `80` and `443` publicly for web ingress;
- UDP `443` publicly for the existing Hysteria2 listener;
- TCP `8443` publicly for the existing Xray listener;
- TCP/UDP `45524` publicly for sg-a Outline data;
- TCP/UDP `45525` publicly for the sg-b Outline data relay;
- TCP `61603-61605` only from `45.41.106.60/32` for operator access to sg-a,
  the sg-b management relay, and the BKK management relay.

Host UFW also preserves the relay/data listeners and the current operator
rules. The BKK relay is `157.245.63.95:61605` forwarding to BKK's management
listener at `191.40.15.51:61603`.

### sg-b

The attached DO Cloud Firewall permits:

- TCP `22` only from `45.41.106.60/32`;
- TCP/UDP `45525` publicly for the standalone Outline data service;
- TCP `61604` only from `157.245.63.95/32` for the sg-a management relay.

The host UFW remains an independent enforcement layer. Direct sg-b TCP
`45525` and SSH are currently blocked by host policy, while sg-a can reach the
Outline data and management relay targets. Do not advertise sg-b as a new
customer route until its canonical direct client path passes acceptance tests.

### bkk-a

The Nube host UFW permits:

- TCP/UDP `443` publicly for the existing Outline data service;
- TCP `61603` from `157.245.63.95` for the sg-a management relay;
- TCP `61603` from the current operator `45.41.106.60/32` for direct Outline
  Manager/API administration;
- SSH `22/tcp` under the existing rate-limited host rule.

No Xray, Hysteria2, or other protocol port is opened on BKK. That remains
intentional until BKK receives a capacity upgrade and a separate protocol
canary passes lifecycle, quota, usage, restart, and client-path gates.

## Verification recorded on 2026-09-16

- BKK hostname: `VM-BKK1-H3P8W5SFMA`.
- BKK direct TCP `443`: reachable.
- BKK direct TCP `61603`: reachable after adding the current operator `/32`.
- sg-a BKK relay TCP `61605`: reachable.
- BKK management API `/server`: HTTP `200` through both the direct and relay
  management paths.
- BKK continues to expose Outline on TCP/UDP `443`.

The operator address is dynamic. If it changes, update the narrow management
allow rules on sg-a and BKK before removing any older fallback entries. Do not
open BKK management publicly and do not delete old access rules until the
replacement path has been verified from the new operator network.

## Operational rule

The customer-facing inventory should contain exactly these active nodes:
`sg-a`, `sg-b`, and `bkk-a`. The retired `sg-c` record is not a firewall target
or allocation candidate. The portal/admin directory may expose BKK's code,
region, health, capacity, and protocol profile, but must never expose its
management URL, certificate, or provider identifiers.
