# AuriX client subscriptions and pricing compatibility study

Status: architecture judgment against the Phase-8 commerce implementation  
Date: 2026-08-29 (Asia/Rangoon)

## Executive verdict

The existing commerce system can remain the financial and entitlement foundation for an AuriX client. Orders, immutable plan snapshots, verified payments, wallet accounting, activation-after-provisioning, quota, expiry, refund, revocation, and multiple simultaneous purchases remain useful.

It does **not** work unchanged as a complete consumer-app subscription system.

Exact classification:

```text
Current 50/100 GiB offers
= prepaid 30-day Telegram packages

Not yet
= app-store auto-renewable subscriptions
+ seamless renewal/extension
+ device-count plans
+ channel-specific prices/taxes/products
+ store receipt and lifecycle reconciliation
```

The correct design is to keep one AuriX entitlement model and add payment-channel adapters. Telegram receipt/wallet purchases, Apple transactions, Google Play purchases, future web payments, and reseller grants should all produce the same entitlement contract.

## What currently exists

Seeded offers:

| Code | Benefit | Current price | Duration |
|---|---:|---:|---:|
| `basic_50gb` | 50 GiB | 3,000 MMK | 30 days |
| `standard_100gb` | 100 GiB | 6,000 MMK | 30 days |

Current behavior:

- order records snapshot name, price, currency, quota, and duration;
- Telegram accepts receipt evidence or wallet payment;
- approval creates a pending subscription and durable provisioning job;
- paid time starts when remote provisioning succeeds;
- the subscription receives one Outline key and native server limit;
- quota exhaustion or wall-clock expiry schedules revocation;
- refund credits the internal wallet and revokes access;
- a customer may hold multiple simultaneous subscriptions and keys;
- `/renew` creates a new order from a previous plan.

These are good invariants for any client.

## Compatibility by workflow

| Workflow | Existing backend fit | Required client/API work |
|---|---:|---|
| Display 50/100 GiB plans | Good | Authenticated plans endpoint; never compile prices into the app |
| Buy through Telegram | Good | Client signs in and discovers entitlement after approval |
| Wallet payment | Good in Telegram | Expose carefully only if channel/store policy permits |
| Activate purchased plan in client | Missing transport | Device-code/deep-link login and entitlement/config API |
| Display quota and expiry | Data exists | Aggregate authoritative entitlement usage across endpoint credentials |
| Region assignment | V3 addition | Assignment/profile returned independently of payment channel |
| Renew same plan | Partial | Current renewal creates another independent entitlement, not an extension |
| Buy another device/profile | Partial | Multiple entitlements exist, but there is no explicit device model |
| Automatic recurring billing | Missing | Apple/Google/web provider records, signed-event verification, lifecycle workers |
| Upgrade/downgrade | Missing | Effective-date, proration/credit, and replacement policy |
| Cancel at period end | Missing | Provider lifecycle and `cancel_at_period_end` state |
| Billing grace/retry | Missing | Grace policy synchronized with config expiry and revocation |
| Restore purchases | Missing | Provider transaction ownership and idempotent entitlement adoption |
| Refund from app store | Missing | Store notification reconciliation; internal wallet is not the refund rail |
| One-device/three-device plan | Missing | Device registry, per-device credentials, limits, and replacement rules |

## Required separation

```text
Plan
  what the customer receives
  quota, duration, device allowance, region policy

Offer
  where/how it is sold
  channel, external product ID, currency, price presentation

Purchase
  verified financial event
  Telegram receipt, wallet, Apple, Google, web, reseller

Entitlement
  authoritative right to service
  active period, quota, devices, policy

Assignment/Credential
  current infrastructure fulfillment
```

Changing payment provider, client, endpoint, or transport must not change the entitlement semantics.

## Pricing architecture

The current `plans` row combines benefit definition with one MMK price. That is sufficient for Telegram but too narrow for app distribution.

Recommended model:

```text
plans
  id, code, name
  quota_bytes, duration_days, device_limit
  region_policy, active

channel_offers
  id, plan_id, channel
  external_product_id
  currency, configured_price_minor
  offer_type, active

provider_purchases
  id, channel, external_transaction_id
  original_transaction_id/purchase_token
  telegram/account_id, plan_id
  purchased_at, current_period_end
  renewal_state, verification_state

entitlements
  id, account_id, plan_id
  source_purchase_id
  starts_at, expires_at
  granted_bytes, committed_usage_bytes
  device_limit, status
```

For Apple and Google, the storefront is authoritative for the displayed and charged price. AuriX maps a store product ID to plan benefits and verifies store transactions server-side. Do not assume `3,000 MMK` can be presented identically in every storefront after fees, taxes, currency tiers, and local availability.

Telegram can retain the current MMK offers independently:

```text
basic_50gb / telegram_mm / 3,000 MMK
standard_100gb / telegram_mm / 6,000 MMK
```

Store offers may have different product IDs and prices while granting the same 50/100 GiB entitlement.

## Renewal decision required

Current code intentionally treats every approved order as an independent entitlement. This supports multiple packages or devices, but the client must not label that behavior “extend” without choosing a policy.

Recommended distinction:

```text
Renew
  schedules the next 30-day period for the same service/profile

Top up
  adds quota according to explicit stacking rules

Add device/profile
  creates another device allocation under the account/plan

Buy separate package
  creates an independent entitlement
```

For the first client release, use non-recurring prepaid packages and implement `Renew` as a scheduled successor:

- if active, next period begins at current expiry;
- if expired, it begins after successful provisioning/reactivation;
- do not issue a second visible profile merely because the user renewed;
- define whether unused bytes expire or roll over; default to no rollover unless advertised;
- keep order snapshots and ledger events immutable.

This is a deliberate change from the current simultaneous-entitlement renewal behavior.

## Device plans

The conversations discussed unlimited one-device and three-device plans, but the current schema implements only 50/100 GiB quota packages. An Outline key can be copied to several devices, so a name such as “one device” is not enforceable from the current server key alone.

An AuriX client makes device policy more credible:

```text
account
  → registered device
  → device-bound app session
  → separate endpoint credential
  → shared or per-device entitlement quota
```

Required rules:

- device registration and revocation;
- maximum active devices;
- one remote credential per device for attribution and selective revocation;
- replacement cooldown/recovery;
- global entitlement usage aggregated across device credentials;
- no claim of perfect anti-sharing on rooted/compromised devices.

Do not launch “Unlimited 1 device” or “Unlimited 3 devices” until costs, fair-use limits, abuse controls, and device enforcement are implemented and measured.

## Client activation workflow

Recommended Telegram-to-client flow:

```text
customer purchases in Telegram
→ payment verified
→ entitlement created
→ customer taps Open in AuriX
→ one-time short-lived activation code is exchanged
→ client receives device-bound refresh credentials
→ client requests signed entitlement/config
→ endpoint credential is provisioned/assigned
→ paid period activates after usable configuration is committed
```

Do not place a long-lived VPN key or Telegram bot token in the activation link. The one-time code must be hashed at rest, state-bound, expiring, single-use, and exchanged through HTTPS.

The client should show:

- plan name and commercial channel-neutral benefits;
- active/renewal/grace/expired state;
- expiry/current period end;
- authoritative remaining quota with measurement timestamp/tolerance;
- device count and replacement controls;
- assigned/recommended region;
- payment management route appropriate to the purchase channel.

## App-store payment constraints

This is policy-sensitive and must be rechecked at release time.

As of the study date:

- Apple generally requires in-app purchase to unlock digital app functionality. Its multiplatform-services rule allows access to externally acquired subscriptions when the same items are also available as in-app purchases; a free stand-alone companion path may apply only when there is no purchasing or external-purchase call to action in the app. Apple also requires VPN apps to use `NEVPNManager`, be offered by an organization developer, disclose data use before purchase/use, avoid selling or disclosing VPN data, and comply with local licensing laws. See [App Review Guidelines 3.1 and 5.4](https://developer.apple.com/app-store/review/guidelines/).
- Google Play generally requires Play Billing when a Play-distributed app accepts payment for digital app functionality/subscriptions, restricts steering to external payment methods except applicable programs/regions, and requires clear pricing. See [Google Play Payments policy](https://support.google.com/googleplay/android-developer/answer/9858738). Google also imposes disclosure, encryption, listing, declaration, and permitted-use requirements on apps using `VpnService`; see [VpnService policy](https://support.google.com/googleplay/android-developer/answer/12564964).

Consequences:

1. Existing Telegram purchases can technically activate the same AuriX account.
2. The store-distributed app cannot casually contain “pay in Telegram” buttons or receipt-upload payment flows everywhere.
3. If selling inside the app, implement StoreKit/Play Billing channel adapters and server-side verification.
4. Outside-app Telegram communication may continue, subject to the platform's current regional steering rules.
5. Refunds, renewals, chargebacks, grace periods, and cancellations must follow the original purchase channel.

### Free ad-supported VPN on Google Play

A free VPN supported by ordinary in-app advertising is not categorically prohibited by Google Play. The compliant boundary is narrow:

- VPN must be the app's genuine core functionality;
- complete the Play Console `VpnService` declaration and provide the requested short demonstration video;
- document `VpnService` use in the listing and encrypt traffic from device to tunnel endpoint;
- publish accurate privacy and Data Safety disclosures;
- obtain prominent in-app disclosure and affirmative consent before collecting sensitive data through `VpnService`;
- show ads only in the AuriX application UI;
- never inject ads into proxied pages or manipulate another app's traffic for monetization;
- never route advertising traffic through another country to alter attribution/revenue;
- never use browsing destinations, DNS queries, packet contents, or VPN traffic metadata for ad targeting;
- audit every advertising/analytics SDK because AuriX remains responsible for third-party collection and sharing.

Google's generic policy does not specify a government-issued VPN operator license as a universal Play submission artifact. It does require a verified developer account, the VPN declaration, privacy/data disclosures, and policy review. Country-specific authorization is separate: Play approval does not grant permission to operate where local law requires a VPN license or prohibits the service. Geo-restrict distribution until jurisdiction-specific requirements are confirmed.

Commercially, use a bounded free tier rather than ad-funded unlimited traffic:

```text
Free
  limited daily/monthly data
  limited eligible regions
  non-disruptive UI ads
  no traffic-derived targeting

Paid
  no ads
  larger quota
  more regions/support priority
```

Ad revenue must be measured against VPS transfer, abuse, support, and fraud costs. A free unlimited VPN can lose money even when Play policy permits it.

## Offline and control-plane behavior

The client should retain a signed last-known-good configuration so an AuriX API outage does not immediately disconnect a paid customer. That cache cannot grant indefinite service:

- configuration validity must not exceed the entitlement plus an explicit grace bound;
- revocation is enforced server-side as soon as workers can reach the endpoint;
- reconnect during control-plane outage may use last-known-good only within policy;
- store billing grace and AuriX connectivity grace must be modeled separately but resolved into one effective entitlement state.

## Recommended release contract

### Client MVP

- preserve current 50/100 GiB prepaid 30-day Telegram packages;
- client is activation/connect/status UI, not a payment storefront initially;
- static/dynamic fallback remains available during rollout;
- implement one account, one active service profile, and scheduled renewal semantics;
- defer unlimited/device plans and auto-renew billing.

### Store-billing release

- add channel offers and provider purchase records;
- verify purchases server-side;
- process Apple/Google lifecycle notifications idempotently;
- implement restore, cancellation, grace, refund, and chargeback workflows;
- map all verified channels to the same entitlement service.

### Device-plan release

- device registry and per-device credentials;
- shared entitlement quota ledger;
- explicit replacement and fair-use rules;
- measured unit economics before “unlimited” claims.

## Final decision

```text
Yes: reuse orders, snapshots, wallet, payment evidence,
     entitlement activation, quota, expiry, refund, and audit concepts.

No:  do not expose the current database workflow directly to the client
     or describe current renewal as seamless extension/auto-renewal.

Add: account/device authentication, client API, channel offers,
     provider-purchase lifecycle, scheduled renewals,
     cross-endpoint quota ledger, and store-policy-compliant UX.
```

The AuriX client should consume entitlements; it should not become the financial source of truth.
