# AuriX Games supplier selection and integration gates

**Reviewed:** 2026-11-26. This is a vendor-document review, not independent
proof of licensing, product availability, support quality, delivery SLA, or
financial solvency. Supplier statements need account-level verification.

## Recommended provider roles

| Provider | Best fit for AuriX | Readiness in this repo | Decision |
| --- | --- | --- | --- |
| FazerCards | Existing game top-up and digital-product provider | Existing integration; current account-specific live stock and offers still need a fresh authenticated audit | Keep as the current primary only for exact offers that are approved, in stock, region-compatible, and within the live cost ceiling. |
| SHOP2TOPUP | Potential direct top-up backup; broad game catalog with player validation | Private adapter, order path, webhook receiver and read-only stock audit are implemented; no key, account, live audit, routes, or spend policy configured | Technically ready for controlled qualification, but **do not fund or publish it yet**. Its production-only API and crypto treasury terms make account/terms review mandatory. |
| Xsolla Reseller / Digital Distribution Hub | Best strategic route for verified, publisher-sourced game content and regional catalogs | Application / partner onboarding required; no AuriX account, API credentials, product routes, or contract | Apply for the Southeast Asia reseller pilot. If accepted for the exact titles/regions, qualify it as the preferred official-content supplier. DDH API access requires integration-manager-issued parameters and webhook work. |
| SEAGM | Southeast Asian regional supply and reseller partnership | Business application only; no public partner API or account-specific offer data found in the reviewed sources | Submit a partnership request; proceed only if the team supplies API access, Myanmar/Thailand SKU coverage, wholesale terms, settlement/refund rules, and a test route. |
| Tillo | Gift-card rewards and secondary store inventory | API / StoreFront product, but contact-led; no account or catalog | Consider later for gaming gift cards and reward incentives. It is not a proven direct MLBB/PUBG currency supplier for this storefront. |
| Reloadly | Airtime and gift-card APIs | Public developer docs and sandbox; no AuriX account or evidence of the target game top-ups | Defer for mobile airtime or supported gift cards. Do not count it as direct game-credit backup until its account catalog proves those products. |
| TURGAME Wholesale | Digital gift-card / code inventory | Dealer application and technical onboarding required | Consider after approval for code products; not currently evidenced as the best local direct top-up route. |
| FoxReload | Aggregated multi-source catalog, potentially broad game top-ups | Public API materials exist, but vendor materials conflict on onboarding/deposit requirements; no AuriX account, executed supply terms, or verified source-by-SKU data | Hold pending written terms, account demo, sandbox credential, named underlying sources, per-SKU regions, balance/refund treatment, and a successful reconciliation trial. Do not rely on claimed automatic fallback as proof of availability. |

## Why SHOP2TOPUP is not automatically the safest choice

Its API documentation has useful technical controls: dynamic category
requirements, player validation, an idempotent UUID order key, a fresh-price
guard, per-order status reads, HMAC-signed webhooks, and explicit stock flags.
The vendor states the API has no sandbox, and the create-order call debits the
supplier wallet immediately. [API overview](https://www.shop2topup.com/en/reseller-api),
[catalog and price](https://www.shop2topup.com/en/reseller-api/docs/catalog),
[orders and webhooks](https://www.shop2topup.com/en/reseller-api/docs/orders).

Its published reseller material says the account balance is USD-funded via
crypto and the reseller integration has no sandbox. Its posted terms also say
the platform is independent and not endorsed by game publishers, display
currency is informational, and reloads are non-refundable/non-withdrawable
under those terms. The contract governing reseller wallet funds must be
confirmed directly before any money is sent. This is a material treasury and
consumer-protection mismatch for a storefront that collects MMK. [Reseller
program](https://shop2topup.com/en/resellers),
[terms](https://shop2topup.com/en/terms-of-service).

The technical integration therefore remains disabled until AuriX has a real
key, a successful account and catalog audit, approved exact product/region
routes, an explicit spend policy, and documented treasury/refund ownership.

## Evidence from the other candidates

- Xsolla's reseller page describes official, region-specific publisher content
  and has a Southeast Asia pilot application. Its DDH integration documentation
  says partners need an integration manager to provide project and API
  parameters; it is not a drop-in public reseller key. [Reseller program](https://xsolla.com/for/resellers/),
  [DDH integration](https://developers.xsolla.com/distribute-game/ddh/).
- SEAGM's own guide says partnership applications are reviewed by its Business
  Team and only selected applicants are contacted; it publishes no response
  estimate. [Partnership application](https://help.seagm.com/hc/en-us/articles/33635648325145-How-to-submit-a-business-partnership-request-with-SEAGM).
- Tillo markets gaming gift-card API/StoreFront rewards and lists PlayStation,
  Xbox, Roblox, and Nintendo; its gaming page routes prospects to a commercial
  conversation. [Gaming platforms](https://www.tillo.com/gaming-platforms).
- Reloadly documents Airtime and Gift Cards plus sandbox tools; the reviewed
  docs do not establish MLBB/PUBG direct top-up coverage. [Developer docs](https://developers.reloadly.com/),
  [getting started](https://support.reloadly.com/getting-started).
- TURGAME presents a dealer application and API integration with real-time
  stock for gift-card codes; exact local game top-ups and Myanmar/Thailand
  acceptance remain unverified. [Wholesale products and onboarding](https://wholesale.turgame.com/products/).
- FoxReload's public pages claim broad supplier aggregation and API access, but
  the onboarding details differ across its own materials: some pages say no
  KYC/no minimum deposit, while its distributor onboarding guide specifies
  KYC and a $500 minimum. This conflict must be resolved in writing before
  treating it as a safer backup. [Platform](https://foxreload.com/en),
  [API documentation](https://foxreload.com/en/library/api),
  [distributor onboarding](https://foxreload.com/en/library/distributors/distributor-getting-started).

## Operating policy for adding any provider

1. Qualify the provider account and the exact SKU; a brand-name match is not
   enough. Record region, player fields, fulfillment type, resale rights,
   provider terms, status semantics, price currency, and stock semantics.
2. Audit live inventory read-only. Use catalog stock for display, then refresh
   exact USD price and stock immediately before dispatch. Treat unavailable,
   stale, unknown, or mismatched responses as no new purchase.
3. Keep each provider's key, webhook secret, approved route file, supplier
   budget, and customer-facing MMK pricing policy separate.
4. Verify customer MMK receipt first. Only the durable paid-order worker may
   create a supplier order. Never infer payment from an uploaded receipt or
   supplier success.
5. Reconcile by provider-specific webhook plus throttled GET reads. A timeout
   is unknown: query the same idempotency reference before retrying. Never send
   a new supplier order ID to resolve ambiguity.
6. Run a supplier-only acceptance test to a consenting account under an
   explicit total spend cap; then separately test the MMK payment review,
   customer settlement, refund, and support workflows.
7. Add the provider to public listings only after the private route, price,
   region, account confirmation, monitoring, support, and audit trail all pass.

Do not add a second supplier to every SKU by default. Each added route increases
the work needed to keep current prices, region rules, webhook interpretation,
wallet reconciliation, and customer remedies correct.
