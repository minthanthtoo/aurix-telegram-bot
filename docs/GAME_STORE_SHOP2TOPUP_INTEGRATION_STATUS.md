# SHOP2TOPUP integration status

## Status

The AuriX backend now has a **private, opt-in direct-top-up adapter** for
SHOP2TOPUP. It is not a live or customer-enabled backup yet. The runtime has no
`SHOP2TOPUP_API_KEY`, no verified reseller account snapshot, no approved SKU
routes, and no supplier-specific budget policy. No live account lookup,
player validation, or purchase has been made from this integration.

The existing FazerCards route remains the default. SHOP2TOPUP product IDs,
requirements and order UUIDs stay in private checkout/fulfillment data and do
not enter the public catalog response.

## Implemented backend path

- Typed HTTPS client for account, catalog, requirements, price, player
  validation, order creation, and order status reads.
- Redirect refusal, response/request size limits, ID and envelope validation,
  bounded error metadata, and no credential/body leakage.
- Separate opt-in gates for quota-consuming player checks and wallet-debiting
  order writes. A POST is never automatically retried.
- Private exact-SKU routes can freeze the provider, numeric category and
  subcategory, recipient field translation, and approval fingerprint into the
  encrypted order payload.
- Checkout uses the frozen provider to validate the player and fulfillment
  routes the order to the matching adapter. A deterministic UUID is reused for
  ambiguity reconciliation; an uncertain POST is held for a GET-only status
  check and is never blindly resent.
- Provider-specific spend preflight requires an enabled and verified reseller
  account, matching dynamic requirement schema, fresh USD price, confirmed
  `in_stock`, an exact approved cost ceiling, wallet reserve, and local daily
  budget reservation. The order includes `expected_unit_price` as a price
  guard.
- Separate `POST /api/v1/webhooks/shop2topup` receiver verifies the documented
  raw-body HMAC-SHA256 signature and event header, maps UUIDs into the private
  provider namespace, and stores an idempotent event for the existing
  reconciliation queue. A panel `webhook.test` is accepted without creating an
  order record. Configure `SHOP2TOPUP_WEBHOOK_SECRET` after registering the
  HTTPS callback in the reseller panel; the code cannot register it remotely.
- The existing order/payment safety boundary remains in force: supplier
  fulfillment starts only after the customer's payment is verified. Unknown
  outcomes and completed supplier statuses remain in operator reconciliation
  where needed; they are not automatically treated as a safe customer refund
  or as a verified wallet capture.
- Read-only `scripts/audit_shop2topup_catalog.py` enumerates account, categories,
  subcategories, item types and reported stock. It does not validate players or
  place orders; it can write a private mode-0600 JSON audit file.
- The authenticated admin SKU inventory now accepts a supplier filter and can
  read that audit file without making a network request. Rows distinguish
  supplier-listed price from cost and stock availability from reported count.
  Vouchers and unknown product types stay visible as browse-only and cannot be
  approved for direct-top-up fulfillment.
- SHOP2TOPUP mapping approvals are written to the separate private
  shop2topup-mapping-config.json file (or SHOP2TOPUP_MAPPING_CONFIG), never the
  FazerCards public-catalog mapping file. The operator route approval checks
  exact category/SKU, fresh audit, available stock, complete category
  requirements, exact input-field names and the player-ID validation map.
  Approval records are included in sanitized admin history. The shared private
  route reader can resolve this separate mapping, but the current customer
  catalog builder does not project SHOP2TOPUP listings.

## Remaining work before any pilot order

1. Configure the API key in the approved server secret store, confirm its IP
   allowlist, and set SHOP2TOPUP_CATALOG_AUDIT_FILE if the default
   .local/fazercards/shop2topup-catalog-audit.json is not suitable. Then run
   the read-only audit with output in a mode-0700 directory:
   python scripts/audit_shop2topup_catalog.py --output .local/fazercards/shop2topup-catalog-audit.json.
   Inspect the reseller account flags, wallet and exact product coverage in
   the protected admin inventory.
2. Review each exact game, region, dynamic player-field schema, provider
   eligibility, resale permission and supplier terms before recording a
   private route. The admin UI cannot infer or approve those facts.
3. Review exact game, region, player-field schema, provider eligibility,
   resale permission and supplier terms for each SKU. Only exact approved
   routes can be projected or ordered.
4. Configure a separate private SHOP2TOPUP USD spend policy, customer MMK
   price policy, per-order and daily limits, and customer support/refund owner.
   A reseller wallet balance by itself is not authorization to spend.
5. Register the HTTPS callback in the reseller panel, configure
   `SHOP2TOPUP_WEBHOOK_SECRET`, send its signed `webhook.test`, and verify the
   endpoint accepts it. The fallback poller reads each order by GET and now
   atomically limits reads to one every 20 seconds per order across workers
   sharing the same order database, matching the documented maximum of three
   reads per 60 seconds per account/order. Validate that deployed workers share
   the same database before enabling reconciliation; use the provider's batch
   endpoint if order volume later needs a higher-throughput reconciliation path.
6. Run a read-only live account/catalog/price check. Separately authorize a
   small supplier-only live delivery test to a consenting game account, within
   an explicit total spend cap. Do not treat a supplier-only test as proof of
   MMK customer-payment settlement.
7. Only after the above, deploy through the Games production gate and verify
   public listing, player check, customer payment review, supplier delivery,
   reconciliation, refund/exception handling, and admin audit trail.

## Current scope boundary

The provider filter and review path set up supplier evidence and private route
review only. They do not add SHOP2TOPUP rows to public offers, public pricing,
or customer checkout. Public checkout remains on the existing approved catalog
path until a separate pricing and publication design is reviewed.

## Provider constraints observed in documentation

SHOP2TOPUP documents no sandbox. `GET /account` and catalog reads are live
account reads; `POST /player/validate` consumes the reseller's daily check
quota; `POST /orders/create` immediately debits the reseller wallet. Player
validation and ordering therefore remain separately gated. Account, wallet,
stock and price are external state and must be rechecked at purchase time.

The supplier's published terms say reseller funding is cryptocurrency-only,
USD is the binding account currency, and non-USD prices are informational. This
may fit supplier settlement but creates treasury, FX, and customer-support risk
for an MMK-only storefront. Confirm the reseller funding route, locally
permitted handling, FX source, loss/refund exposure, and treasury owner before
funding a live account. Provider marketing claims about automatic refunds do
not override the account-specific contract and on-chain payment terms.

## Authoritative provider references

- [Reseller API overview](https://www.shop2topup.com/en/reseller-api)
- [Catalog, price, and dynamic requirements](https://www.shop2topup.com/en/reseller-api/docs/catalog)
- [Player validation](https://www.shop2topup.com/en/reseller-api/docs/player)
- [Create and reconcile orders](https://www.shop2topup.com/en/reseller-api/docs/orders)
- [Account and authentication](https://www.shop2topup.com/en/reseller-api/docs/account)
