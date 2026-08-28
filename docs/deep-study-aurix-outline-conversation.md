# Deep Study — AuriX Outline/VPN Conversation

Source archive: [`chatgpt_conversation_6a8f6e31.md`](../chatgpt_conversation_6a8f6e31.md)  
Source JSON: [`chatgpt_conversation_6a8f6e31.json`](../chatgpt_conversation_6a8f6e31.json)  
Shared conversation: `6a8f6e31-7808-83ec-8538-13a59c241d43`  
Conversation title: `*** Outline on Render`  
Archive: 244 decoded nodes, 74 visible turns, 37 user turns and 37 assistant turns.

This document is interpretation and critique. The archive files remain the faithful source record.

## Executive conclusion

The conversation began as a hosting question—whether Outline VPN could run on Render—and evolved into a business-system design:

```text
acquisition
  → Telegram customer experience
  → payment and subscription control plane
  → Outline access-key provisioning
  → VPS bandwidth/data plane
  → usage, renewal, referral, affiliate, reseller
```

The strongest conclusion is sound:

> Outline should be the VPN execution layer; AuriX should own commercial rules, customer records, payments, entitlements, distribution, and operations.

The second strong conclusion is economic:

> Bandwidth consumed per customer—not RAM, CPU, Telegram members, or registered resellers—is the primary variable that determines whether the business works.

The conversation is strategically useful, but its repeated `8.5–9.4/10` ratings are conceptual ratings, not evidence of product-market fit or profitability. Confidence should remain limited until these facts are measured:

1. Myanmar-to-server latency, reachability, packet loss, and sustained throughput.
2. Real monthly GB consumed per customer, including the heavy-user tail.
3. Payment acceptance, verification, refunds, and provider policy for the target market and business.
4. Renewal, support burden, abuse rate, and qualified referral conversion.
5. Reliable, idempotent Outline provisioning and revocation against the installed server version.

The current repository is a technical spike, not the full system described in the conversation. It implements a free daily-claim bot; it does not yet implement paid plans, payments, referrals, affiliate tracking, resellers, PostgreSQL, a server pool, or the final production control plane.

## What the conversation decided

### Hosting and placement

The conversation converged on this boundary:

```text
Render or similar control plane
  ├── Telegram bot/API
  ├── database
  ├── payment and subscription logic
  ├── worker and notifications
  └── Outline management API calls

Public Linux VPS data plane
  └── Outline Server and VPN traffic
```

Render was rejected as the normal home for the Outline VPN data plane because the discussion identified the need for a real VM, public networking, inbound TCP/UDP reachability, Docker, and predictable persistence. A VPS was preferred for the actual VPN; Render remained a possible home for the control plane.

For the first test, the conversation selected DigitalOcean SGP1 with Ubuntu 24.04 LTS x64, public IPv4, SSH key authentication, monitoring, and the small Basic plan shown as `$6/month`, `1 vCPU`, `1 GB RAM`, `25 GB SSD`, and `1 TB transfer`. This was a test recommendation, not a demonstrated production capacity.

The alternatives discussion considered Hostinger, DigitalOcean, Vultr, Akamai/Linode, AWS, GCP, Oracle, OVHcloud, Scaleway, UpCloud, Hetzner, and Kamatera. It eventually separated three questions:

- easiest/native installation path;
- cheapest included transfer;
- best user experience for Myanmar customers.

The conversation generally preferred Singapore or nearby Southeast Asia for latency, while recognizing that Hetzner EU could offer much more included traffic at lower raw bandwidth cost. The correct unresolved decision is therefore not “which provider is best?” but:

```text
provider and region
  → measured reachability and quality
  → measured GB/user
  → usable cost per TB
  → contribution margin
```

### Capacity model

The most valuable correction was distinguishing:

1. **Subscription capacity:** how many customers have access.
2. **Monthly transfer capacity:** how much the provider/account can transfer.
3. **Concurrent capacity:** how many users are actively transferring at once.

The transcript repeatedly used a 70–80% operational ceiling. That is sensible planning discipline, not a provider guarantee. Monthly transfer arithmetic cannot prove CPU, packet-processing, connection, or peak-throughput capacity.

The useful planning table for the selected DigitalOcean test unit was:

| Actual average use per customer/month | Theoretical users at 1 TB | 80% planning users |
|---:|---:|---:|
| 5 GB | 200 | 160 |
| 10 GB | 100 | 80 |
| 20 GB | 50 | 40 |
| 30 GB | 33 | 26 |
| 50 GB | 20 | 16 |
| 100 GB | 10 | 8 |
| 200 GB | 5 | 4 |

These are bandwidth-equivalent scenarios only. They are not simultaneous-user or quality guarantees.

### Product and pricing

The candidate price ladder was:

| Product | Candidate price | Candidate entitlement |
|---|---:|---|
| Trial | Free | 3–5 GB |
| Basic | 3,000 MMK | 50 GB |
| Standard | 6,000 MMK | 100 GB |
| Premium | 7,000 MMK | one device, described as “unlimited” |
| Family | 12,000 MMK | three devices, described as “unlimited” |

At the conversation’s assumed exchange rate of `1 USD = 4,500 MMK`, the paid tiers are approximately `$0.67`, `$1.33`, `$1.56`, and `$2.67`.

The best product correction was to stop treating “unlimited” as literally unlimited. A safer definition is:

```text
high-use or fair-use service
  = no small advertised quota
  + transparent capacity/fair-use policy
  + aggregate server protection
```

The 50 GB and 100 GB plans are easier to reason about because a per-key Outline limit maps directly to the entitlement. The premium plans remain an experiment until P90/P95/P99 usage is known. The small price gap between 100 GB and “unlimited” is attractive for conversion but can make heavy customers unprofitable.

### Identity, devices, and key sharing

The conversation initially explored whether Telegram could guarantee one account equals one physical device. It later corrected itself: Telegram identity is not hardware identity, and an Outline key can be copied.

The stronger commercial insight is:

> A key can represent a pooled bandwidth entitlement; it does not need to be bound to one physical device if sharing is not the primary economic risk.

This makes the product simpler. If a customer uses one key on several devices, aggregate usage still consumes one key’s allowance. The product must still define what “three devices” means:

- one shared key with pooled data;
- three separately registered keys;
- three keys with a pooled entitlement;
- or a support/usage promise only.

The conversation did not settle this. It should be decided before marketing the family plan.

### Acquisition ladder

The final distribution sequence was:

```text
direct sales
  → customer referral with GB/service credit
  → affiliate with cash commission
  → reseller with prepaid wholesale margin
```

| Channel | Operator | Reward/economics | Main purpose |
|---|---|---|---|
| Direct | AuriX | retail revenue | product validation |
| Referral | existing customer | GB/service credit | low-cost word of mouth |
| Affiliate | creator/marketer | cash commission | external audience acquisition |
| Reseller | business/seller | retail minus wholesale | repeat distribution and renewals |

The recommended referral rules were:

- reward only a new, qualified paying customer;
- do not reward a click or Telegram signup;
- use GB/service credit before cash;
- cap monthly rewards;
- record referrals as ledger/events, not only counters;
- promote proven referrers into affiliates or resellers after measured performance.

The suggested numbers—`+10 GB`, a `200 GB/month` cap, and `15%` affiliate commission—are candidate parameters, not validated economics.

The reseller recommendation was coherent:

- begin with a prepaid wallet;
- use an immutable ledger rather than a mutable balance alone;
- reserve funds before provisioning;
- capture on success and release on failure;
- keep infrastructure credentials away from resellers;
- retain central infrastructure, abuse, billing, and audit control;
- add credit, inventory, APIs, white-label, and multi-level structures only later.

## Chronological development

### 1. Feasibility: Render or VPS

The first exchanges answered the original question directly: use a VPS for Outline Server and reserve Render for backend/control-plane work. The test target became one cheap VPS, one client, then several real users—not a complete production network.

### 2. Provider selection became a region/bandwidth tradeoff

Hostinger and DigitalOcean were evaluated first. The discussion then broadened and found that raw included transfer can make EU Hetzner pricing look dramatically better than Singapore pricing. It also recognized that an EU exit location may be worse for Myanmar latency even when transfer allowance is larger.

This is where a benchmark became more valuable than a comparison table. The proposed benchmark was identical Outline configurations on several providers, measuring latency, TCP/UDP reachability, throughput, packet loss, stability, and GB/user.

### 3. The $6 SGP1 configuration became the experiment

The user supplied DigitalOcean creation settings, first New York and then Singapore. The assistant recommended Singapore for a Myanmar-facing test, Ubuntu 24.04 x64, public IPv4, SSH keys, and monitoring. It correctly treated the small instance as a laboratory and warned that website, bot, and Outline could share it only for a small prototype.

The answer to “how to expand it?” introduced both vertical resizing and horizontal addition of Outline servers. The long-term abstraction became a server registry and allocator rather than a hard-coded single VPS.

### 4. Bandwidth arithmetic exposed the business constraint

The hosting comparison moved the discussion away from RAM and toward included transfer, user usage, and peak concurrency. This was the first major business insight. It also exposed a weakness: the conversation sometimes converted provider transfer into user counts without a measured local traffic profile.

The corrected model is to track actual `GB per active paid customer per month`, preferably with P50/P75/P90/P95/P99 values. Average alone is unsafe for “unlimited” pricing.

### 5. Pricing introduced the unlimited-plan risk

The candidate plan ladder is commercially attractive because the 7,000 MMK premium plan is only 1,000 MMK above 100 GB. That same price anchor can turn heavy customers into losses. The conversation eventually recommended fair-use language and usage measurement.

### 6. The strategy expanded into distribution infrastructure

The pasted growth model proposed direct sales, referral, affiliate, reseller, master reseller, SaaS, and marketplace phases. The assistant accepted the direction but downgraded the forecast and moved the features later.

The most defensible framing became:

```text
VPN provides the initial product and customer contact.
The control plane captures payment, usage, retention, and distribution data.
The distribution network may become the long-term advantage.
```

### 7. Device enforcement was deprioritized

The “one Telegram account = one physical device” idea was rejected as technically unreliable and commercially unnecessary if bandwidth is the primary entitlement. The scarce resource moved from “number of devices” to “bytes consumed.”

### 8. Referral, affiliate, and reseller were separated

Referral became an early, low-complexity, in-product loop. Affiliate became a later cash-commission channel for creators. Reseller became a later prepaid wholesale channel with recurring renewal economics.

The repeated “judge” turns were useful mainly because they returned to one constraint: do not build every distribution layer before proving paid retention and positive contribution margin.

### 9. The final blueprint became a modular monolith

The final proposal was a Telegram-first commerce/control plane with PostgreSQL, background jobs, an Outline adapter, a server registry, usage snapshots, audit logs, and staged distribution modules. It explicitly rejected microservices, Kubernetes, a custom VPN client, and a marketplace in the first release.

## Canonical architecture

```text
Content / ads / direct sales
        │
        ▼
Telegram bot and optional web landing page
        │
        ▼
Modular monolith control plane
  ├── customers and identity
  ├── products and entitlements
  ├── orders and payment verification
  ├── subscriptions and renewals
  ├── provisioning jobs and notifications
  ├── usage and unit economics
  ├── referrals and rewards
  ├── affiliates and commissions
  ├── resellers and wallet ledger
  ├── server registry and capacity policy
  ├── risk, support, and audit
  └── admin operations
        │
        ▼
Outline adapter on a controlled management path
        │
        ├── Outline SGP-01
        ├── Outline SGP-02
        └── Outline SGP-03 ...
        │
        ▼
Customer VPN traffic exits from the selected VPS region
```

The control plane must not become the VPN traffic path. Existing customers should continue using an already-provisioned key if the bot/API is temporarily unavailable, subject to key expiry and limit.

## Minimum data model

### Customer and commerce

```text
customers
plans
orders
payments
subscriptions
```

Required invariants:

- one canonical customer record per Telegram user ID;
- one payment reference cannot confirm multiple orders;
- payment verification is authoritative;
- an order can be approved only once;
- a subscription has explicit start, expiry, and status;
- money is stored in integer minor units and a named currency.

### VPN and operations

```text
vpn_servers
vpn_keys
usage_snapshots
provisioning_jobs
notifications
audit_logs
```

Required invariants:

- one subscription cannot have two active keys;
- provisioning and revocation are idempotent;
- an ambiguous Outline response is reconciled before another key is created;
- key URLs and management secrets never enter ordinary logs;
- failed Telegram delivery does not recreate a key;
- an unhealthy or over-capacity server stops receiving new allocations.

### Distribution and money movement

```text
referrals
referral_rewards
affiliates
affiliate_clicks
affiliate_conversions
affiliate_payouts
resellers
reseller_customers
wallets
wallet_transactions
```

Wallets and rewards are financial records. They need immutable events, references, reversal entries, and reconciliation—not only a current balance column.

## Economic model

### Capacity

Let:

- `N` = active customers assigned to a server;
- `U` = measured monthly VPN GB/customer;
- `E` = provider/accounting/measurement overhead factor;
- `T` = included monthly transfer in the provider’s own unit;
- `R` = chosen operating fraction, such as 0.80.

```text
required monthly transfer = N × U × E
usable planning transfer  = T × R
servers required           = ceil(required / usable planning transfer)
```

`E` and `U` must come from measurement. Do not silently mix decimal TB, binary TiB, decimal GB, and GiB. Verify traffic direction and account-level pooling assumptions with the provider.

### Contribution margin

```text
contribution margin
  = collected revenue
  − payment cost
  − measured bandwidth cost
  − expected refund loss
  − support allocation
  − referral credit cost
  − affiliate commission
  − reseller margin
```

Referral, affiliate, and reseller costs are not always stacked on the same order. Record the actual attribution path and actual cost event.

### “Unlimited” decision

Before enabling a premium plan, calculate contribution margin at:

```text
P50 usage
P75 usage
P90 usage
P95 usage
P99 usage
```

If the plan is profitable only at the median but loses money in the upper tail, it needs a fair-use threshold, rate policy, upgrade path, or higher price.

### Referral decision

```text
expected referral reward cost
<
incremental contribution margin from the qualified referred customer
```

GB rewards are not free merely because they are non-cash; they consume the same scarce bandwidth resource.

## Strong, weak, and unresolved

### Strong reasoning

- Separating Render/control plane from VPS/VPN data plane.
- Choosing Singapore as a reasonable first latency hypothesis while requiring measurement.
- Distinguishing monthly transfer from concurrent throughput.
- Treating per-key quotas and business records as different responsibilities.
- Recognizing a rolling 30-day Outline limit as different from a calendar-month entitlement.
- Rejecting universal 1 TB giveaways as the core product.
- Treating referrals, affiliates, and resellers as different channels.
- Using ledger, reservation, capture, release, and reversal models for reseller wallets.
- Keeping the first implementation as a modular monolith with a worker.
- Prioritizing contribution margin and retention over vanity metrics.

### Weak or overconfident reasoning

- Provider prices, transfer allowances, regions, payment methods, and terms were time-sensitive and must be rechecked immediately before purchase.
- “Supports Outline” was sometimes too broad. Native/easy installation is materially different from manual Linux/Docker installation.
- Bandwidth-equivalent user counts were sometimes close to capacity claims. They do not establish safe concurrency, peak Mbps, or service quality.
- The exchange rate and MMK revenue examples are assumptions, not forecasts.
- Repeated 9/10 judgments were not based on customer interviews, payment tests, measured usage, or retained cohorts.
- A payment provider was discussed but never selected and tested.
- Legal and provider-policy risks were named but no written go/no-go record was produced.
- Automatic allocation, migration, and multi-region plans were described before key lifecycle and failure reconciliation were demonstrated.

### Important unresolved decisions

1. What payment method will be accepted, and how is it verified?
2. Is the initial service staff-assisted or fully automated?
3. What is the legal and provider-policy position for selling VPN access?
4. Is a paid entitlement a rolling 30-day key limit, a calendar subscription quota, or both?
5. What exactly does the one-device and three-device product promise?
6. What is the fair-use definition for premium plans?
7. Who owns customer support, refunds, and abuse decisions in the reseller model?
8. What happens after an Outline request times out after a remote key may have been created?
9. What is the server-wide capacity signal, since per-key limits do not protect aggregate provider transfer?
10. What retention, deletion, backup, and privacy policy applies to Telegram IDs, payment evidence, usage, and support records?

## What should not be built yet

The conversation was most persuasive when it deferred complexity. Keep these out of the first paid pilot:

- universal 1 TB accounts;
- physical-device fingerprinting;
- custom VPN protocol or mobile app;
- multi-level or MLM commissions;
- reseller credit and unsecured balances;
- inventory of pre-created keys;
- white-label bots and custom domains;
- multi-provider automatic provisioning;
- marketplace/SaaS packaging;
- Kubernetes and microservices;
- AI fraud scoring before basic rules and human review exist;
- a large affiliate portal before affiliate conversion is proven.

## Evidence-gated roadmap

The conversation’s several roadmaps can be reduced to these gates. Customer counts are review triggers, not promises.

### Gate 0 — legality, terms, and payment

Before collecting money:

- document the local legal/regulatory position;
- confirm the VPS provider permits intended VPN/resale traffic;
- confirm payment-provider rules and settlement behavior;
- define privacy, retention, refund, abuse, and support policies;
- nominate an operator for incidents and customer support.

### Gate 1 — infrastructure proof

Run one Singapore Outline server with 10–20 known testers. Record:

- client connection success by platform;
- Myanmar-to-server latency and packet loss;
- download/upload and peak Mbps;
- 24-hour and 7-day stability;
- key creation, limit application, usage reading, and revocation;
- actual GB consumed;
- provider firewall and TCP/UDP behavior.

Pass only when the service works acceptably for the target client mix and the management API contract is captured against the installed version.

### Gate 2 — paid concierge pilot

Implement:

```text
Telegram → plan → order → staff payment approval
         → subscription → idempotent provisioning
         → key delivery → expiry/revocation
```

Start with a small paid cohort. Use PostgreSQL for hosted production state. Keep payment approval staff-assisted until the provider workflow is reliable.

### Gate 3 — unit economics and retention

Measure:

- P50/P90/P95 GB/customer;
- activation success and support minutes;
- payment approval-to-key p95;
- provisioning and revocation failure rates;
- refund rate;
- 30/60/90-day retention and renewal;
- contribution margin by product and usage band.

Do not scale distribution or advertise unlimited plans aggressively before this gate passes.

### Gate 4 — simple referral

Add a Telegram deep link and qualified-referral ledger. Reward only after payment qualification and reversal eligibility are clear. Cap monthly credit and measure qualified referrals per active customer.

### Gate 5 — affiliate beta

Recruit 10–20 affiliates only after direct/referral economics are positive. Start with one commission rate, one attribution window, approval/reversal states, and approved marketing claims.

### Gate 6 — reseller beta

Recruit 5–10 candidates, not 100 registrants. Launch prepaid wallet, customer creation, renewal, sales history, and support. Give no Outline API, SSH, or infrastructure secrets. Promote only productive resellers.

### Gate 7 — server pool and scale

Add server registry, health checks, capacity thresholds, reconciliation, and multiple nodes only when actual traffic requires them. Add regions when customer experience or demand justifies them—not because a provider table shows a larger transfer allowance.

## Repository alignment

The current repository at `/Users/min/projects/tg-AuriX-bot` contains a narrower free-tier prototype:

| Current artifact | What it does |
|---|---|
| [`app.py`](../app.py) | `/start`, `/help`, `/claim`; one rolling 24-hour claim; fresh Outline key; 100 MiB per-key limit; SQLite; expiry revocation |
| [`test_app.py`](../test_app.py) | Eight tests covering claim timing, failure rollback, expiry, concurrency, and schema constraints |
| [`README.md`](../README.md) | Configuration and operational notes for the free-tier bot |
| [`.env.example`](../.env.example) | Telegram token, Outline API URL/fingerprint, and database path placeholders |

That code is aligned with a technical daily-claim proof, but not with the final conversation blueprint. Missing from the repository are:

```text
paid products and calendar subscription semantics
orders and payment verification
PostgreSQL migrations
async provisioning/reconciliation jobs
usage snapshots and economics
referrals and qualified rewards
affiliate attribution and payouts
reseller wallet and order reservation
server registry and capacity monitoring
admin identity, audit, backups, and operational runbooks
```

The cleanest next implementation step is not to expand the current free-claim code into every module. First decide whether the free bot is a disposable Gate 1 harness or the starting product. If it is the starting product, add an explicit migration path from “claim” to “trial entitlement” and test Outline contract, expiry, and reconciliation boundaries before adding acquisition features.

## Final decision memo

Adopt this as the canonical strategy:

```text
1. VPS-hosted Outline for the VPN data plane.
2. Telegram-first control plane with a modular monolith.
3. PostgreSQL and an idempotent worker for commercial state.
4. Bandwidth entitlement and measured usage as the economic core.
5. Staff-assisted paid pilot before payment automation.
6. Direct sales before referral; referral before affiliate; affiliate before reseller.
7. Prepaid, centralized reseller model; no infrastructure access for resellers.
8. Multi-server and multi-region only after measured saturation or demand.
9. Small controlled giveaways as marketing, never universal 1 TB access.
10. Every growth step gated by contribution margin, retention, reliability, and support evidence.
```

The business thesis is promising but still hypothetical. The practical north-star is not “10,000 Telegram members” or “1,000 resellers.” It is:

> Can AuriX acquire and retain a customer at a positive contribution margin while delivering acceptable Myanmar network quality and reliably managing the customer’s Outline entitlement?

That question should govern every next feature and every infrastructure purchase.
