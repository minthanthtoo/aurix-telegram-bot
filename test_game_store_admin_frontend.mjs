import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

test("mobile admin entry keeps payment-review link clear of centered store branding", async () => {
  const [markup, styles] = await Promise.all([
    readFile(new URL("./web/game-store/admin/index.html", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/admin/admin.css", import.meta.url), "utf8"),
  ]);
  assert.match(markup, /class="topbar admin-topbar"/);
  assert.match(markup, /admin\.css\?v=20261117-mapping-review-clarity-1/);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*\.admin-topbar \.brand \{ position: static;[\s\S]*\.admin-topbar \.admin-back \{ min-width: 0; margin-left: auto;/);
});

class ElementStub {
  constructor() {
    this.listeners = {};
    this.dataset = {};
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.checked = false;
    this.required = false;
    this.children = [];
  }

  addEventListener(type, callback) {
    this.listeners[type] = callback;
  }

  setAttribute(name, value) { this[name] = value; }
  getAttribute(name) { return this[name] ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  removeAttribute(name) { delete this[name]; }
  focus() {}
  scrollIntoView() {}
}

function makeResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

test("operator console authenticates in memory, loads status, and gates writes", async () => {
  const ids = [
    "auth-panel", "auth-form", "admin-token", "auth-status", "dashboard", "refresh-button",
    "tab-overview", "tab-catalog", "tab-supplier", "tab-settings", "tab-notices",
    "view-overview", "view-catalog", "view-supplier", "view-settings", "view-notices",
    "disconnect-button", "dashboard-status", "wallet-balance", "wallet-state", "wallet-reminder", "wallet-alerts",
    "subscription-state", "subscription-detail", "subscription-meta",
    "wallet-reminder-detail", "catalog-state", "catalog-detail", "catalog-offers", "catalog-observed",
    "pricing-form", "pricing-status", "pricing-state", "pricing-expiry", "pricing-rate-updated", "pricing-warning", "pricing-settings-quicklink",
    "pricing-fx-source", "pricing-fx-observed-at", "pricing-valid-until",
    "pricing-auto-fx", "pricing-auto-fx-help", "pricing-manual-fx-fields", "pricing-fx-rate-caption",
    "pricing-review-credential", "pricing-owner-approval", "funding-form", "funding-method", "funding-amount",
    "funding-intent-id", "funding-status", "funding-result", "funding-limits", "funding-submit", "funding-payment-link",
    "funding-review-status", "funding-review-list", "funding-credential", "catalog-status-detail", "catalog-generated",
    "store-notice-form", "store-notice-id", "store-notice-title", "store-notice-body", "store-notice-severity",
    "store-notice-status", "store-notice-save", "store-notice-clear", "store-notice-form-status",
    "store-notice-list-status", "store-notice-list",
    "catalog-source-observed", "catalog-max-age", "webhook-state", "webhook-url", "webhook-form",
    "webhook-url-input", "webhook-enabled", "webhook-status", "mapping-state", "mapping-mapped",
    "mapping-projected", "mapping-captured", "mapping-reason", "mapping-block-reasons", "mapping-review-summary", "inventory-count",
    "mapping-history-state", "mapping-history-count", "mapping-history-last", "mapping-history-events",
    "inventory-families", "inventory-captured", "inventory-hash", "inventory-state", "inventory-mapping-status",
    "inventory-mapping-gates", "inventory-projection", "inventory-search-form", "supplier-provider-filter",
    "inventory-family-filter", "inventory-category-filter", "inventory-query-filter", "inventory-previous",
    "inventory-next", "inventory-page-status", "inventory-review-rows", "mapping-review-editor",
    "mapping-review-title", "mapping-review-source", "mapping-review-schema", "mapping-review-form",
    "mapping-review-cancel", "mapping-product-select", "mapping-offer-id", "mapping-fulfillment",
    "mapping-region-dimension", "mapping-region-values", "mapping-region-label", "mapping-rights-evidence",
    "mapping-validation-category", "mapping-validation-fields", "mapping-validation-regions", "mapping-validation-policy",
    "mapping-reviewer", "mapping-input-labels", "mapping-allow-all-region", "mapping-approval-confirm",
    "mapping-approval-submit", "mapping-approval-cancel", "mapping-approval-status",
    "game-order-attention-status", "game-order-attention-list",
    "money-integrity-status", "money-integrity-findings",
    "catalog-policy-form", "catalog-policy-status", "catalog-policy-save", "catalog-policy-reload",
    "catalog-family-topups", "catalog-family-giftcards", "catalog-family-gamekeys", "catalog-family-manual_services",
    "catalog-excluded-products", "catalog-excluded-offers",
  ];
  const elements = new Map(ids.map((id) => [id, new ElementStub()]));
  const tabItems = ["overview", "catalog", "supplier", "settings", "notices"].map((view) => {
    const tab = elements.get(`tab-${view}`);
    tab.dataset.adminView = view;
    return tab;
  });
  const viewPanels = ["overview", "catalog", "supplier", "settings", "notices"].map((view) => {
    const panel = elements.get(`view-${view}`);
    panel.dataset.adminPanel = view;
    return panel;
  });
  const disclosures = [new ElementStub(), new ElementStub(), new ElementStub()];
  const policyKeys = [
    "sell_currency", "supplier_currency", "fx_rate", "target_profit_pct", "minimum_profit_pct",
    "target_profit_minor", "operating_fee_minor", "payment_fee_pct", "payment_fee_minor",
    "marketing_pct", "payback_pct", "promo_pct", "promo_fixed_minor", "rounding_minor",
    "minimum_price_minor", "maximum_price_minor",
  ];
  const policyInputs = new Map(policyKeys.map((key) => [key, new ElementStub()]));
  elements.get("funding-method").value = "usdt";
  elements.get("funding-amount").value = "50";
  elements.get("supplier-provider-filter").value = "fazercards";
  const calls = [];
  let liveCatalogUnavailable = false;
  let attentionState = "unknown";
  let reconciliationCount = 0;
  let resolvedFundingAttempt = false;
  let catalogChoices = { revision: 0, configured: false, policy: {
    enabled_families: ["topups", "giftcards", "gamekeys", "manual_services"], excluded_products: [], excluded_offers: [],
  } };
  const policy = {
    sell_currency: "MMK", supplier_currency: "USD", fx_rate: "4500", target_profit_pct: "0.15",
    minimum_profit_pct: null, target_profit_minor: 0, operating_fee_minor: 0,
    payment_fee_pct: "0", payment_fee_minor: 0, marketing_pct: "0.03", payback_pct: "0.02",
    promo_pct: "0.05", promo_fixed_minor: 0, rounding_minor: 100, minimum_price_minor: 0,
    maximum_price_minor: null,
  };
  const topupItem = {
    family: "topups", category_id: "mlbb", sku: "d55", name: "55 Diamonds",
    category_name: "Mobile Legends", price_usd: "1.2300", stock: 12, mapping_status: "stale",
    freshness_status: "fresh", source_category_complete: true,
    mapping_candidate: { status: "single_title_candidate", candidates: [
      { product_slug: "mobile-legends", product_name: "Mobile Legends", match_method: "exact_title" },
    ], review_required: true, approval_effect: "none" },
  };
  const manualServiceItem = {
    family: "manual_services", category_id: "manual-boost", sku: "boost-1",
    name: "Boost service", price_usd: "2.5000", stock: null, mapping_status: "stale",
    freshness_status: "stale", source_category_complete: true,
    input_schema: [{ key: "player_id", label: "Player ID", type: "text" }],
    manual_service: { kind: "game-service", chat_enabled: true, info: "Requires an account ID." },
  };
  const document = {
    createElement() { return new ElementStub(); },
    querySelector(selector) {
      if (selector.startsWith("[data-policy=\"")) return policyInputs.get(selector.slice(14, -2));
      if (selector.startsWith("#")) return elements.get(selector.slice(1));
      if (selector === ".admin-view-switcher") return new ElementStub();
      throw new Error(`unexpected selector ${selector}`);
    },
    querySelectorAll(selector) {
      if (selector === "[data-admin-view]") return tabItems;
      if (selector === "[data-admin-panel]") return viewPanels;
      if (selector === "#pricing-form input:not([type='password'])") return [...policyInputs.values()];
      if (selector === "#view-catalog > details.admin-disclosure") return disclosures;
      return [];
    },
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith('catalog-policy')) {
      if (options.method === 'PUT') {
        const body = JSON.parse(options.body);
        assert.equal(body.expected_revision, catalogChoices.revision);
        catalogChoices = { revision: catalogChoices.revision + 1, configured: true, policy: body.policy };
      }
      return makeResponse(catalogChoices);
    }
    if (url.endsWith("supplier-wallet/status")) {
      return makeResponse({
        balance: { balance_usd: "7.25", currency: "USD", observed_at: "2026-09-22T03:00:00Z" },
        low_wallet: { is_low: true, should_remind: true, reason: "below_threshold", threshold_usd: "10", suggested_topup_usd: "50" },
      });
    }
    if (url.endsWith("supplier-wallet/payment-methods")) {
      return makeResponse({ methods: [{ code: "usdt", label: "USDT", min_amount_usd: "5", max_amount_usd: "500" }] });
    }
    if (url.endsWith("supplier-wallet/funding-attempts")) {
      return makeResponse({ unresolved_attempts: resolvedFundingAttempt ? [] : [], invoice_creation_blocked: false });
    }
    if (url.includes("funding-attempts/fund-lost/resolve-absent")) {
      assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), {
        provider_reference: "case-2026-17", confirmed_absent: true,
      });
      assert.equal(options.headers.get("X-Aurix-Treasury-Token"), "t".repeat(32));
      resolvedFundingAttempt = true;
      return makeResponse({ resolution: { intent_id: "fund-lost", status: "confirmed_absent" },
        unresolved_attempts: [], invoice_creation_blocked: false });
    }
    if (url.includes("funding-attempts/fund-found/link-invoice")) {
      assert.equal(options.method, "POST");
      assert.equal(options.headers.get("X-Aurix-Treasury-Token"), "t".repeat(32));
      assert.deepEqual(JSON.parse(options.body), { provider_reference: "case-found", payment_id: "tx17", confirmed_match: true });
      return makeResponse({ intent: { payment_id: "tx17", status: "pending", expires_at: "2099-01-01T00:00:00Z",
        payment_url: "https://pay.fzr.cards/invoice/tx17" }, unresolved_attempts: [], invoice_creation_blocked: false });
    }
    if (url.endsWith("supplier-wallet/alerts?status=pending&limit=20")) {
      return makeResponse({ alerts: [{ payload: { balance_usd: "7.25", threshold_usd: "10", reason: "below_threshold" }, created_at: "2026-09-22T03:00:00Z" }] });
    }
    if (url.endsWith("money-integrity")) {
      return makeResponse({ status: "empty", total_findings: 0, findings: [], unavailable_checks: [] });
    }
    if (url.endsWith("game-orders/attention")) {
      const orders = attentionState === "delivered" ? [] : [{ order_id: "game-order-7", state: attentionState,
        product_slug: "mobile-legends", amount_minor: 5500, currency: "MMK", updated_at: "2026-09-22T03:00:00Z",
        reconcile_available: true }];
      return makeResponse({ orders, truncated: false });
    }
    if (url.endsWith("game-orders/game-order-7/reconcile")) {
      assert.equal(options.method, "POST");
      assert.equal(options.body, "{}");
      reconciliationCount += 1;
      attentionState = "delivered";
      return makeResponse({ supplier_writes: false, outcome: { applied: true, state: "delivered" } });
    }
    if (url.endsWith("pricing-policy") && options.method === "PUT") {
      const body = JSON.parse(options.body);
      if (body.automatic_fx) return makeResponse({
        policy: { ...policy, fx_rate: "4800" }, policy_status: "awaiting_rate", approval: null,
        automatic_fx: { enabled: true, status: "awaiting_rate", floor_rate: "4800" },
      });
      return makeResponse({
        policy, policy_status: "approved", approval: { approved_by: "staff:owner",
          fx_source: "test", fx_observed_at: "2026-11-13T12:00:00Z", valid_until: "2026-11-14T12:00:00Z" },
      });
    }
    if (url.endsWith("pricing-policy")) return makeResponse({
      policy_status: "approved", policy, approval: { approved_by: "staff:owner",
        fx_source: "test", fx_observed_at: "2026-11-13T12:00:00Z", valid_until: "2026-11-14T12:00:00Z" },
    });
    if (url.endsWith("live-catalog/status")) {
      return liveCatalogUnavailable
        ? makeResponse({ error: "unavailable" }, 503)
        : makeResponse({ status: "fresh", offer_count: 4, generated_at: "2026-09-22T03:01:00Z", source_observed_at: "2026-09-22T03:00:00Z", max_age_seconds: 900 });
    }
    if (url.endsWith("catalog-refresh-status")) {
      return makeResponse({ status: "fresh", offer_count: 4, observed_at: "2026-09-22T03:00:00Z" });
    }
    if (url.endsWith("store-notices")) return makeResponse({ notices: [] });
    if (url.endsWith("subscription/status")) {
      return makeResponse({ subscription: { plan: "trial", status: "active", trialEndsAt: "2026-09-23T00:00:00Z", autoRenew: false } });
    }
    if (url.endsWith("supplier-webhook") && options.method === "PUT") {
      return makeResponse({ webhook: { url: JSON.parse(options.body).url, enabled: JSON.parse(options.body).enabled, status: "active" }, write_applied: true });
    }
    if (url.endsWith("supplier-webhook")) {
      return makeResponse({ webhook: { url: "https://store.example.test/api/v1/webhooks/fazercards", enabled: true, status: "active" } });
    }
    if (url.endsWith("mapping-review")) {
      return makeResponse({ mapping_review: {
        status: "blocked", reason: "no_offer_passed_all_gates", mapped: 10011, projected: 0,
        captured_at: "2026-11-13T13:28:39.971314Z", block_reasons: {
          customer_visibility_disabled: 10002, region_rule_incomplete: 10002,
          resale_unverified: 10002, "review_state:blocked": 10002,
          stale_observation: 10011, supplier_disabled: 10002, supplier_unavailable: 4825,
        },
        writes: false, supplier_key_used: false,
      } });
    }
    if (url.endsWith("mapping-history")) {
      return makeResponse({ mapping_history: {
        status: "empty", count: 0, events: [], writes: false,
      } });
    }
    if (url.includes("/catalog-inventory?")) {
      const params = new URL(url, "http://local.test").searchParams;
      const offset = Number(params.get("offset") || 0);
      if (params.get("supplier_provider") === "shop2topup") {
        const shopItem = {
          supplier_provider: "shop2topup", family: "topups", category_id: "12", sku: "999",
          name: "86 Diamonds", price_usd: "0.95", price_source: "supplier_catalog_listing",
          stock_label: "available", supplier_available: true,
          input_schema: [{ field_name: "player_id", data_type: "text" }],
          source_category_complete: true, source_status: "complete", freshness_status: "fresh",
          mapping_status: "missing", mapping_gate: "missing", projection_status: "not_projected",
          mapping_candidate: { status: "single_title_candidate", candidates: [
            { product_slug: "mobile-legends", product_name: "Mobile Legends", match_method: "exact_title" },
          ] },
        };
        return makeResponse({
          status: "private_snapshot", total_count: 1, offset: 0, limit: 20, next_offset: null,
          counts: { shop2topup_topups: 1 }, captured_at: "2026-11-26T00:00:00Z",
          source_status: "complete", freshness_status: "fresh", mapping_config_status: "partial",
          providers: { shop2topup: { status: "private_snapshot", source_status: "complete",
            freshness_status: "fresh", mapping_config_status: "missing", captured_at: "2026-11-26T00:00:00Z" } },
          source_sha256: "0123456789abcdef", items: [shopItem], reference_products: [
            { slug: "mobile-legends", name: "Mobile Legends" },
          ], mapping_status_counts: { missing: 1 }, mapping_gate_counts: { missing: 1 },
          projection_status_counts: { not_projected: 1 }, customer_visible: false, checkout_enabled: false,
        });
      }
      return makeResponse({
        status: "private_snapshot", total_count: 10510, offset, limit: 20,
        next_offset: offset < 20 ? offset + 20 : null,
        counts: { topups: 5053, giftcards: 4673, gamekeys: 705, manual_services: 79 },
        captured_at: "2026-09-21T11:33:14.709684Z",
        source_status: "partial", freshness_status: "stale", mapping_config_status: "loaded",
        source_sha256: "abcdef1234567890",
        items: offset === 0 && !params.has("family") ? [topupItem, manualServiceItem] : [topupItem], auxiliary: {},
        reference_products: [{ slug: "mobile-legends", name: "Mobile Legends" }],
        mapping_status_counts: { missing: 79, stale: 10431 }, mapping_gate_counts: { blocked: 10431, missing: 79 },
        projection_status_counts: { not_projected: 10510 },
        customer_visible: false, checkout_enabled: false,
      });
    }
    if (url.endsWith("supplier-wallet/invoice")) {
      assert.equal(options.headers.get("X-Aurix-Treasury-Token"), "t".repeat(32));
      return makeResponse({ intent: { intent_id: "intent-1", method: "usdt", amount_usd: "50", status: "pending", payment_url: "https://pay.fzr.cards/invoice/tx1" } }, 201);
    }
    if (url.endsWith("/mapping-approval")) {
      return makeResponse({ error: "Route review requires correction." }, 400);
    }
    throw new Error(`unexpected request ${url}`);
  };
  const context = vm.createContext({ document, fetch, Headers, URL, URLSearchParams, Date, console, AbortController, AbortSignal,
    location: { search: "" }, window: { matchMedia: () => ({ matches: false }), setTimeout, clearTimeout } });
  const source = await readFile(new URL("./web/game-store/admin/admin.js", import.meta.url), "utf8");
  vm.runInContext(source, context);

  elements.get("admin-token").value = "admin-secret";
  await elements.get("auth-form").listeners.submit({ preventDefault() {} });
  assert.ok(calls.length > 0, vm.runInContext("`${storeAdminSession}:${adminToken}`", context));
  elements.get("tab-catalog").listeners.click();
  assert.equal(context.selectedAdminView(), "catalog");
  await context.loadDashboard("catalog");
  assert.equal(elements.get('catalog-family-gamekeys').checked, true, calls.map(({ url }) => url).join("\n"));
  assert.ok(calls.some(({ url }) => url.endsWith("live-catalog/status")));
  assert.equal(elements.get("catalog-state").textContent, "Fresh");
  assert.equal(elements.get("catalog-offers").textContent, "4");
  liveCatalogUnavailable = true;
  await context.loadDashboard("catalog");
  assert.equal(elements.get("catalog-state").textContent, "Unavailable");
  assert.equal(elements.get("catalog-offers").textContent, "—");
  assert.match(elements.get("catalog-detail").textContent, /Could not verify current published offers/);
  liveCatalogUnavailable = false;
  await context.loadDashboard("catalog");
  elements.get('catalog-family-gamekeys').checked = false;
  elements.get('catalog-excluded-products').value = 'hidden-game';
  elements.get('catalog-excluded-offers').value = 'hidden-pack';
  elements.get('catalog-policy-form').listeners.input();
  await elements.get('catalog-policy-form').listeners.submit({ preventDefault() {} });
  assert.equal(catalogChoices.policy.enabled_families.includes('gamekeys'), false);
  assert.deepEqual(catalogChoices.policy.excluded_products, ['hidden-game']);
  assert.deepEqual(catalogChoices.policy.excluded_offers, ['hidden-pack']);
  assert.match(elements.get('catalog-policy-status').textContent, /revision 1/);
  for (const disclosure of disclosures) {
    disclosure.open = true;
  }
  await Promise.all([context.loadCatalogDisclosure("mapping"), context.loadCatalogDisclosure("history"), context.loadInventory()]);
  elements.get("tab-supplier").listeners.click();
  await context.loadDashboard("supplier");
  elements.get("tab-settings").listeners.click();
  await context.loadDashboard("settings");
  elements.get("tab-overview").listeners.click();
  await context.loadDashboard("overview");
  assert.equal(elements.get("dashboard").hidden, false);
  assert.equal(elements.get("auth-panel").hidden, true);
  assert.equal(elements.get("wallet-balance").textContent, "7.25 USD");
  assert.equal(elements.get("wallet-reminder").textContent, "Reminder due");
  assert.match(elements.get("wallet-alerts").textContent, /1 pending reminder/);
  assert.match(elements.get("subscription-detail").textContent, /Plan: trial/);
  assert.equal(elements.get("catalog-state").textContent, "Fresh");
  assert.equal(elements.get("webhook-state").textContent, "active");
  assert.equal(elements.get("webhook-url-input").value, "https://store.example.test/api/v1/webhooks/fazercards");
  assert.equal(elements.get("funding-amount").min, "5");
  assert.equal(elements.get("funding-amount").max, "500");
  assert.match(elements.get("funding-limits").textContent, /5.*500/);
  assert.equal(elements.get("funding-submit").disabled, false);
  assert.match(elements.get("funding-review-status").textContent, /No unresolved invoice creations/);
  context.renderSubscription({ subscription: { plan: "bronze", subscriptionActive: true,
    planExpiresAt: "2026-12-12T19:07:29Z", planAutoRenew: false } });
  assert.equal(elements.get("subscription-state").textContent, "Active");
  assert.equal(elements.get("subscription-state").dataset.state, "success");
  assert.match(elements.get("subscription-meta").textContent, /Auto-renew: off/);
  context.renderSubscription({ subscription: { plan: "bronze", subscriptionActive: false } });
  assert.equal(elements.get("subscription-state").dataset.state, "error");
  context.renderSubscription({ subscription: { plan: "bronze" } });
  assert.equal(elements.get("subscription-state").dataset.state, "");
  assert.match(elements.get("game-order-attention-status").textContent, /1 order needs operator review/);
  assert.match(elements.get("money-integrity-status").textContent, /No ledger activity/);
  context.renderMoneyIntegrity({ status: "incomplete", findings: [], unavailable_checks: ["missing"] });
  assert.equal(elements.get("money-integrity-status").dataset.state, "error");
  context.renderMoneyIntegrity({ status: "attention", total_findings: 1,
    findings: [{ reference: "order-one", issue: "settlement_handoff_pending" }], unavailable_checks: [] });
  assert.match(elements.get("money-integrity-findings").children[0].children[0].textContent, /unfinished ledger handoff/);
  assert.equal(elements.get("game-order-attention-list").children[0].children[0].textContent, "game-order-7 · Unknown supplier result");
  assert.equal(elements.get("game-order-attention-list").children[0].children[2].textContent, "5,500 MMK");
  assert.equal(policyInputs.get("marketing_pct").value, "0.03");
  assert.equal(elements.get("mapping-state").textContent, "blocked · stale snapshot");
  assert.equal(elements.get("mapping-mapped").textContent, "10,011");
  assert.equal(elements.get("mapping-projected").textContent, "0");
  assert.equal(elements.get("mapping-review-summary").textContent, "Stale snapshot · not live stock");
  assert.match(elements.get("mapping-reason").textContent, /snapshot is stale/i);
  const blockerRows = elements.get("mapping-block-reasons").children;
  assert.equal(blockerRows.length, 7);
  const staleRow = blockerRows.find((row) => row.children[0].children[1].textContent === "stale_observation");
  assert.match(staleRow.children[0].children[0].textContent, /Supplier observation is stale/);
  assert.equal(staleRow.children[1].textContent, "10,011");
  const visibilityRow = blockerRows.find((row) => row.children[0].children[1].textContent === "customer_visibility_disabled");
  assert.match(visibilityRow.children[0].children[0].textContent, /Hidden from the customer catalog/);
  assert.equal(visibilityRow.children[1].textContent, "10,002");
  assert.equal(elements.get("mapping-history-state").textContent, "empty");
  assert.equal(elements.get("mapping-history-count").textContent, "0");
  assert.match(elements.get("mapping-history-events").textContent, /No approval events/);
  assert.equal(elements.get("inventory-count").textContent, "10,510");
  assert.match(elements.get("inventory-families").textContent, /topups: 5,053/);
  assert.match(elements.get("inventory-families").textContent, /manual_services: 79/);
  assert.match(elements.get("inventory-mapping-status").textContent, /stale: 10,431/);
  assert.match(elements.get("inventory-mapping-gates").textContent, /blocked: 10,431/);
  assert.match(elements.get("inventory-projection").textContent, /not projected: 10,510/);
  assert.match(elements.get("inventory-page-status").textContent, /Showing 1–2 of 10,510/);
  assert.equal(elements.get("inventory-review-rows").children.length, 2);
  assert.match(elements.get("inventory-review-rows").children[0].children[3].textContent, /Title hint only: Mobile Legends \(exact title\).*Verify manually; no approval/);
  assert.equal(elements.get("inventory-review-rows").children[0].children[4].disabled, false);
  await elements.get("inventory-review-rows").children[0].children[4].listeners.click();
  assert.equal(elements.get("mapping-review-editor").hidden, false);
  assert.equal(elements.get("mapping-approval-submit").disabled, false);
  assert.equal(elements.get("inventory-review-rows").children[1].children[4].disabled, true);
  assert.match(elements.get("inventory-review-rows").children[1].children[3].textContent, /Service game-service · supplier chat available · Requires an account ID/);
  assert.match(elements.get("inventory-review-rows").children[1].children[1].textContent, /stock unknown/);
  assert.match(elements.get("inventory-state").textContent, /FazerCards private snapshot/);
  assert.ok(calls.every(({ options }) => options.headers.get("Authorization") === "Bearer admin-secret"));

  elements.get("mapping-validation-category").value = "mobile_legends";
  elements.get("mapping-validation-fields").value = "{invalid";
  const beforeMapping = calls.length;
  await elements.get("mapping-review-form").listeners.submit({ preventDefault() {} });
  assert.equal(calls.length, beforeMapping, "Malformed route JSON never reaches the approval API");
  assert.equal(elements.get("mapping-approval-submit").disabled, false);
  elements.get("mapping-validation-fields").value = '{"user_id":"player_id"}';
  elements.get("mapping-validation-regions").value = '{"Myanmar":"mm"}';
  await elements.get("mapping-review-form").listeners.submit({ preventDefault() {} });
  const routeRequest = JSON.parse(calls.find(({ url }) => url.endsWith("/mapping-approval")).options.body);
  assert.equal(routeRequest.validation_category_id, "mobile_legends");
  assert.deepEqual(routeRequest.validation_field_map, { user_id: "player_id" });
  assert.deepEqual(routeRequest.validation_region_map, { Myanmar: "mm" });
  assert.equal(elements.get("mapping-review-editor").hidden, false, "Server rejection preserves the draft");
  assert.equal(elements.get("mapping-validation-fields").value, '{"user_id":"player_id"}');

  elements.get("inventory-family-filter").value = "topups";
  elements.get("inventory-category-filter").value = "mlbb";
  elements.get("inventory-query-filter").value = "55 Diamonds";
  await elements.get("inventory-search-form").listeners.submit({ preventDefault() {} });
  assert.ok(calls.some(({ url }) => url.includes("catalog-inventory?limit=20&supplier_provider=fazercards&family=topups&category=mlbb&q=55+Diamonds")), calls.map(({ url }) => url).join("\n"));
  assert.equal(elements.get("inventory-next").disabled, false);
  await elements.get("inventory-next").listeners.click();
  assert.ok(calls.some(({ url }) => url.includes("catalog-inventory?limit=20&supplier_provider=fazercards&family=topups&category=mlbb&q=55+Diamonds&offset=20")));
  assert.match(elements.get("inventory-page-status").textContent, /Showing 21–21 of 10,510/);

  elements.get("supplier-provider-filter").value = "shop2topup";
  await elements.get("supplier-provider-filter").listeners.change();
  assert.match(elements.get("inventory-state").textContent, /SHOP2TOPUP private audit/);
  assert.equal(elements.get("inventory-review-rows").children.length, 1);
  assert.equal(elements.get("inventory-review-rows").children[0].children[4].disabled, false);
  await elements.get("inventory-review-rows").children[0].children[4].listeners.click();
  assert.equal(elements.get("mapping-input-labels").value, "player_id");
  assert.equal(elements.get("mapping-validation-category").value, "12");
  elements.get("mapping-validation-fields").value = '{"player_id":"player_id"}';
  await elements.get("mapping-review-form").listeners.submit({ preventDefault() {} });
  const shopApprovalCall = calls.filter(({ url }) => url.endsWith("/mapping-approval")).at(-1);
  const shopApproval = JSON.parse(shopApprovalCall.options.body);
  assert.equal(shopApproval.supplier_provider, "shop2topup");
  assert.equal(shopApproval.validation_category_id, "12");

  elements.get("pricing-fx-source").value = "approved source";
  elements.get("pricing-fx-observed-at").value = "2026-11-13T12:00:00+06:30";
  elements.get("pricing-valid-until").value = "2026-11-14T12:00:00+06:30";
  elements.get("pricing-review-credential").value = "t".repeat(32);
  elements.get("pricing-owner-approval").checked = true;
  await elements.get("pricing-form").listeners.submit({ preventDefault() {} });
  const pricingCall = calls.find(({ url, options }) => url.endsWith("pricing-policy") && options.method === "PUT");
  assert.deepEqual(JSON.parse(pricingCall.options.body).policy.marketing_pct, "0.03");
  assert.equal(JSON.parse(pricingCall.options.body).owner_approval, true);
  assert.equal(pricingCall.options.headers.get("X-Aurix-Treasury-Token"), "t".repeat(32));
  assert.equal(elements.get("pricing-review-credential").value, "");
  assert.equal(elements.get("pricing-owner-approval").checked, false);
  assert.match(elements.get("pricing-status").textContent, /saved/);
  context.renderPolicy({ policy_status: "unconfigured", policy: null, approval: null });
  assert.equal(policyInputs.get("marketing_pct").value, "");
  assert.match(elements.get("pricing-status").textContent, /No approved pricing policy/);
  assert.equal(elements.get("pricing-warning").hidden, false);
  assert.match(elements.get("pricing-warning").textContent, /⚠.*listings are paused/);

  elements.get("pricing-auto-fx").checked = true;
  elements.get("pricing-auto-fx").listeners.change();
  assert.equal(elements.get("pricing-manual-fx-fields").hidden, true);
  assert.equal(elements.get("pricing-fx-source").required, false);
  elements.get("pricing-review-credential").value = "t".repeat(32);
  elements.get("pricing-owner-approval").checked = true;
  await elements.get("pricing-form").listeners.submit({ preventDefault() {} });
  const autoPricingCall = calls.find(({ url, options }) => url.endsWith("pricing-policy") && options.method === "PUT" && JSON.parse(options.body).automatic_fx);
  assert.equal(JSON.parse(autoPricingCall.options.body).automatic_fx, true);
  assert.equal("approval" in JSON.parse(autoPricingCall.options.body), false);
  assert.equal(elements.get("pricing-auto-fx").checked, true);
  assert.equal(elements.get("pricing-fx-rate-caption").textContent, "FX floor (MMK per USD)");
  assert.match(elements.get("pricing-status").textContent, /No daily rate entry is needed/);

  const liveRate = { enabled: true, market_rate: "3660", effective_rate: "4800", floor_rate: "4800",
    fetched_at: "2026-11-17T07:56:17Z", market_date: "2026-11-16", valid_until: "2099-11-19T17:30:00Z" };
  context.renderPolicy({ policy_status: "approved", policy: { ...policy, fx_rate: "4800" }, approval: null, automatic_fx: liveRate });
  assert.match(elements.get("pricing-rate-updated").textContent, /3,660 MMK\/USD/);
  assert.match(elements.get("pricing-rate-updated").textContent, /updated/);
  assert.equal(elements.get("pricing-warning").hidden, true);

  context.renderPolicy({ policy_status: "expired", policy: { ...policy, fx_rate: "4800" }, approval: null,
    automatic_fx: { ...liveRate, valid_until: "2026-11-16T17:30:00Z" } });
  assert.equal(elements.get("pricing-warning").hidden, false);
  assert.match(elements.get("pricing-warning").textContent, /⚠.*CBM quote is stale.*last approved rate/);
  assert.match(elements.get("pricing-rate-updated").textContent, /3,660 MMK\/USD/);
  elements.get("pricing-settings-quicklink").listeners.click();
  assert.equal(context.selectedAdminView(), "settings");

  context.renderPolicy({ policy_status: "expired", policy: { ...policy, fx_rate: "4800" },
    approval: { approved_by: "staff:owner", approved_at: "2026-11-14T22:26:10Z",
      valid_until: "2026-11-15T22:26:10Z", fx_source: "owner-set", fx_observed_at: "2026-11-14T20:00:00Z" },
    automatic_fx: { enabled: false } });
  assert.equal(elements.get("pricing-state").textContent, "Last rate in use");
  assert.match(elements.get("pricing-warning").textContent, /⚠ Pricing approval expired; listings continue/);
  assert.match(elements.get("pricing-rate-updated").textContent, /4,800 MMK\/USD/);
  assert.ok(elements.get("pricing-rate-updated").textContent.endsWith(new Date("2026-11-14T20:00:00Z").toLocaleString()));

  elements.get("funding-intent-id").value = "";
  elements.get("funding-amount").value = "50";
  const beforeMissingId = calls.filter((call) => call.url.endsWith("supplier-wallet/invoice")).length;
  await elements.get("funding-form").listeners.submit({ preventDefault() {} });
  assert.match(elements.get("funding-status").textContent, /request ID/);
  assert.equal(calls.filter((call) => call.url.endsWith("supplier-wallet/invoice")).length, beforeMissingId);
  elements.get("funding-intent-id").value = "intent-1";
  elements.get("funding-amount").value = "4";
  await elements.get("funding-form").listeners.submit({ preventDefault() {} });
  assert.equal(calls.some(({ url }) => url.endsWith("supplier-wallet/invoice")), false);
  assert.match(elements.get("funding-status").textContent, /current account limits/);
  elements.get("funding-amount").value = "50";
  elements.get("funding-credential").value = "t".repeat(32);
  await elements.get("funding-form").listeners.submit({ preventDefault() {} });
  const fundingCall = calls.find(({ url }) => url.endsWith("supplier-wallet/invoice"));
  assert.deepEqual(JSON.parse(fundingCall.options.body), { method: "usdt", amount_usd: "50", intent_id: "intent-1" });
  assert.equal(elements.get("funding-result").hidden, false);
  assert.equal(elements.get("funding-payment-link").href, "https://pay.fzr.cards/invoice/tx1");
  assert.equal(elements.get("funding-payment-link").target, "_blank");
  assert.equal(elements.get("funding-payment-link").rel, "noopener noreferrer");
  assert.equal(elements.get("funding-payment-link").hidden, false);
  assert.match(elements.get("funding-status").textContent, /intent created/);

  context.renderFundingReconciliation({ unresolved_attempts: [{ intent_id: "fund-lost", method: "usdt",
    amount_usd: "50", created_at: "2026-09-22T03:00:00Z" }], invoice_creation_blocked: true });
  assert.equal(elements.get("funding-submit").disabled, true);
  const fundingReviewRow = elements.get("funding-review-list").children[0];
  const fundingReviewForm = fundingReviewRow.children[2];
  const referenceInput = fundingReviewForm.children[0].children[0];
  const reviewerInput = fundingReviewForm.children[1].children[0];
  const providerAbsentCheckbox = fundingReviewForm.children[2].children[0];
  referenceInput.value = "case-2026-17";
  reviewerInput.value = "t".repeat(32);
  providerAbsentCheckbox.checked = true;
  await fundingReviewForm.listeners.submit({ preventDefault() {} });
  assert.equal(resolvedFundingAttempt, true);
  assert.equal(elements.get("funding-review-list").children.length, 0);
  assert.equal(elements.get("funding-submit").disabled, false);
  assert.match(elements.get("funding-status").textContent, /Provider-confirmed absence recorded/);

  context.renderFundingReconciliation({ unresolved_attempts: [{ intent_id: "fund-found", method: "usdt",
    amount_usd: "50", created_at: "2026-09-22T03:00:00Z" }], invoice_creation_blocked: true });
  const foundForm = elements.get("funding-review-list").children[0].children[2];
  foundForm.children[0].children[0].value = "case-found";
  foundForm.children[1].children[0].value = "t".repeat(32);
  const outcome = foundForm.children[3].children[0];
  outcome.value = "found";
  outcome.listeners.change();
  assert.equal(foundForm.children[4].hidden, false);
  assert.equal(foundForm.children[2].children[0].checked, false);
  foundForm.children[4].children[0].value = "tx17";
  foundForm.children[2].children[0].checked = true;
  await foundForm.listeners.submit({ preventDefault() {} });
  assert.equal(foundForm.children[1].children[0].value, "");
  assert.match(elements.get("funding-status").textContent, /invoice recovered/);
  assert.equal(elements.get("funding-payment-link").href, "https://pay.fzr.cards/invoice/tx17");

  context.renderFundingReconciliation({ unresolved_attempts: [], invoice_creation_blocked: true });
  assert.equal(elements.get("funding-submit").disabled, true);
  assert.match(elements.get("funding-review-status").textContent, /state is invalid/);
  context.renderFundingReconciliation({});
  assert.equal(elements.get("funding-submit").disabled, true);
  elements.get("funding-intent-id").value = "intent-while-unavailable";
  elements.get("funding-amount").value = "50";
  const invoiceCountBeforeUnavailableSubmit = calls.filter(({ url }) => url.endsWith("supplier-wallet/invoice")).length;
  await elements.get("funding-form").listeners.submit({ preventDefault() {} });
  assert.equal(calls.filter(({ url }) => url.endsWith("supplier-wallet/invoice")).length,
    invoiceCountBeforeUnavailableSubmit);
  assert.match(elements.get("funding-status").textContent, /status are required/);

  elements.get("webhook-enabled").checked = false;
  await elements.get("webhook-form").listeners.submit({ preventDefault() {} });
  const webhookCall = calls.find(({ url, options }) => url.endsWith("supplier-webhook") && options.method === "PUT");
  assert.deepEqual(JSON.parse(webhookCall.options.body), { url: "https://store.example.test/api/v1/webhooks/fazercards", enabled: false });
  assert.match(elements.get("webhook-status").textContent, /saved/);

  const attentionRow = elements.get("game-order-attention-list").children[0];
  const reconcileButton = attentionRow.children.find((child) => child.textContent === "Check supplier status");
  assert.ok(reconcileButton);
  await reconcileButton.listeners.click();
  assert.equal(reconciliationCount, 1);
  assert.equal(elements.get("game-order-attention-list").children.length, 0);
  assert.match(elements.get("dashboard-status").textContent, /Supplier status checked/);

  elements.get("disconnect-button").listeners.click();
  assert.equal(elements.get("dashboard").hidden, true);
  assert.equal(elements.get("admin-token").value, "");
});
