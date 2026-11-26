"use strict";

const $ = (id) => document.querySelector(`#${id}`);
let adminToken = "";
const storeAdminSession = new URLSearchParams(location.search).get("session") === "1";
const ADMIN_READ_TIMEOUT_MS = 30_000;
const INVENTORY_PAGE_SIZE = 20;
let inventoryOffset = 0;
let selectedInventoryItem = null;
let currentInventoryProducts = [];
let fundingLimitsReady = false;
let fundingCanCreate = false;
let fundingReconciliationReady = false;
let fundingBlockedByUnknown = false;
let fundingRequestBusy = false;
let catalogPolicyRevision = null;
let catalogPolicyDirty = false;
let pricingPolicyDirty = false;
const adminViewLoads = new Map();
const CATALOG_FAMILIES = ["topups", "giftcards", "gamekeys", "manual_services"];

const adminViewTabs = [...document.querySelectorAll("[data-admin-view]")];
function selectAdminView(view, focus = false) {
  const previousView = adminViewTabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset.adminView;
  for (const tab of adminViewTabs) {
    const selected = tab.dataset.adminView === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  }
  for (const panel of document.querySelectorAll("[data-admin-panel]")) {
    panel.hidden = panel.dataset.adminPanel !== view;
  }
  if (previousView && previousView !== view) {
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    document.querySelector("#dashboard .admin-heading")?.scrollIntoView({ behavior, block: "start" });
    if (!$("dashboard").hidden) void loadDashboard(view);
  }
}
for (const tab of adminViewTabs) {
  tab.addEventListener("click", () => selectAdminView(tab.dataset.adminView));
}
$("pricing-settings-quicklink").addEventListener("click", () => selectAdminView("settings", true));
document.querySelector(".admin-view-switcher")?.addEventListener("keydown", (event) => {
  const index = adminViewTabs.indexOf(document.activeElement);
  if (index < 0 || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? adminViewTabs.length - 1
    : (index + (event.key === "ArrowRight" ? 1 : -1) + adminViewTabs.length) % adminViewTabs.length;
  selectAdminView(adminViewTabs[next].dataset.adminView, true);
});
selectAdminView("overview");

function renderCatalogPolicy(payload) {
  if (!payload || !Number.isSafeInteger(payload.revision) || payload.revision < 0
      || !payload.policy || !Array.isArray(payload.policy.enabled_families)
      || !Array.isArray(payload.policy.excluded_products) || !Array.isArray(payload.policy.excluded_offers)) {
    throw new Error("Catalog choices could not be verified.");
  }
  if (catalogPolicyDirty) {
    setStatus($("catalog-policy-status"), "Unsaved choices retained. Reload saved choices to discard your draft.", "warning");
    return;
  }
  catalogPolicyRevision = payload.revision;
  for (const family of CATALOG_FAMILIES) $("catalog-family-" + family).checked = payload.policy.enabled_families.includes(family);
  $("catalog-excluded-products").value = payload.policy.excluded_products.join("\n");
  $("catalog-excluded-offers").value = payload.policy.excluded_offers.join("\n");
  $("catalog-policy-save").disabled = false;
  setStatus($("catalog-policy-status"), payload.configured
    ? `Saved catalog revision ${payload.revision}.`
    : "Catalog controls are inactive. Save these family choices to activate the filter; SKU approvals still apply.", payload.configured ? "success" : "warning");
}

async function reloadCatalogPolicy() {
  $("catalog-policy-save").disabled = true;
  catalogPolicyRevision = null;
  try { renderCatalogPolicy(await adminFetch("/api/v1/admin/catalog-policy")); }
  catch (error) { setStatus($("catalog-policy-status"), error.message, "error"); }
}

function syncFundingSubmit() {
  $("funding-submit").disabled = !fundingLimitsReady || !fundingCanCreate
    || !fundingReconciliationReady || fundingBlockedByUnknown || fundingRequestBusy;
}

function setStatus(element, message, state = "") {
  element.textContent = message;
  element.dataset.state = state;
}

function formatNumber(value, fractionDigits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString(undefined, { maximumFractionDigits: fractionDigits });
}

function formatMinorAmount(amount, currency) {
  if (!Number.isSafeInteger(amount) || amount < 0 || !/^[A-Z]{3}$/.test(currency || "")) return "—";
  try {
    const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
    return `${(amount / 10 ** digits).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${currency}`;
  } catch { return `${amount.toLocaleString("en-US")} ${currency}`; }
}

function formatDate(value) {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function adminFetch(path, options = {}) {
  if (!storeAdminSession && !adminToken) throw new Error("Connect an admin token first.");
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  const readRequest = method === "GET" || method === "HEAD";
  const controller = readRequest ? new AbortController() : null;
  const timeout = controller ? window.setTimeout(() => controller.abort(), ADMIN_READ_TIMEOUT_MS) : null;
  const requestPath = storeAdminSession
    ? path.replace(/^\/api\/v1\/admin(?=\/)/, "/api/v1/store-staff/admin")
    : path;
  if (storeAdminSession) headers.delete("Authorization");
  else headers.set("Authorization", `Bearer ${adminToken}`);
  headers.set("Accept", "application/json");
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let response;
  let payload = {};
  try {
    response = await fetch(requestPath, {
      ...options,
      credentials: "same-origin",
      headers,
      ...(controller ? { signal: controller.signal } : {}),
    });
    try { payload = await response.json(); } catch (_) { payload = {}; }
  } catch (error) {
    if (controller?.signal.aborted) throw new Error("Admin status check timed out. Refresh to retry.");
    throw error;
  } finally {
    if (timeout !== null) window.clearTimeout(timeout);
  }
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showDashboard(visible) {
  $("auth-panel").hidden = visible;
  $("dashboard").hidden = !visible;
}

function renderWallet(payload) {
  const balance = payload.balance || {};
  const low = payload.low_wallet || {};
  $("wallet-balance").textContent = `${formatNumber(balance.balance_usd)} ${balance.currency || "USD"}`;
  $("wallet-state").textContent = `Observed ${formatDate(balance.observed_at)}`;
  $("wallet-reminder").textContent = low.is_low ? (low.should_remind ? "Reminder due" : "Low") : "Healthy";
  $("wallet-reminder").dataset.state = low.is_low ? "warning" : "success";
  $("wallet-reminder-detail").textContent = low.is_low
    ? `${low.reason || "below threshold"} · threshold ${formatNumber(low.threshold_usd)} USD`
    : `Threshold ${formatNumber(low.threshold_usd)} USD`;
}

function renderFundingMethods(payload) {
  fundingLimitsReady = true;
  fundingCanCreate = false;
  const methods = Array.isArray(payload && payload.methods) ? payload.methods : [];
  const matches = methods.filter((method) => method && method.code === "usdt");
  if (matches.length !== 1) {
    syncFundingSubmit();
    setStatus($("funding-limits"), "USDT funding is unavailable for this account.", "error");
    return;
  }
  const method = matches[0];
  const minimum = Number(method.min_amount_usd);
  const maximum = Number(method.max_amount_usd);
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum) || minimum <= 0 || maximum < minimum) {
    syncFundingSubmit();
    setStatus($("funding-limits"), "Current account funding limits are invalid.", "error");
    return;
  }
  const input = $("funding-amount");
  input.min = String(method.min_amount_usd);
  input.max = String(method.max_amount_usd);
  input.step = "any";
  $("funding-method").value = "usdt";
  fundingCanCreate = true;
  syncFundingSubmit();
  setStatus($("funding-limits"), `Allowed invoice range: ${formatNumber(minimum, 4)}–${formatNumber(maximum, 4)} USD.`, "");
}

function renderFundingReconciliation(payload) {
  const list = $("funding-review-list");
  list.replaceChildren();
  const attempts = payload?.unresolved_attempts;
  const blocked = payload?.invoice_creation_blocked;
  const valid = Array.isArray(attempts) && typeof blocked === "boolean"
    && blocked === (attempts.length > 0)
    && attempts.every((attempt) => attempt && typeof attempt.intent_id === "string"
      && typeof attempt.method === "string" && typeof attempt.amount_usd === "string"
      && typeof attempt.created_at === "string");
  if (!valid) {
    fundingReconciliationReady = false;
    fundingBlockedByUnknown = true;
    syncFundingSubmit();
    setStatus($("funding-review-status"), "Reconciliation state is invalid; new invoice creation is disabled.", "error");
    return;
  }
  fundingReconciliationReady = true;
  fundingBlockedByUnknown = blocked;
  syncFundingSubmit();
  if (!attempts.length) {
    setStatus($("funding-review-status"), "No unresolved invoice creations.", "success");
    return;
  }
  setStatus($("funding-review-status"), `${attempts.length} unresolved create${attempts.length === 1 ? "" : "s"}; new invoices are blocked.`, "error");
  for (const attempt of attempts) {
    const row = document.createElement("article");
    row.className = "admin-order-attention__row";
    const title = document.createElement("strong");
    title.textContent = `${attempt.intent_id} · ${attempt.method.toUpperCase()} · ${attempt.amount_usd} USD`;
    const created = document.createElement("small");
    created.textContent = `Create started: ${formatDate(attempt.created_at)}. Check the supplier invoice/account records or support before resolving.`;
    const form = document.createElement("form");
    form.className = "admin-form admin-funding-review__form";
    const referenceLabel = document.createElement("label");
    referenceLabel.textContent = "Provider account/support reference";
    const reference = document.createElement("input");
    reference.maxLength = 128;
    reference.autocomplete = "off";
    reference.required = true;
    referenceLabel.append(reference);
    const reviewerLabel = document.createElement("label");
    reviewerLabel.textContent = "Named treasury credential";
    const reviewer = document.createElement("input");
    reviewer.type = "password";
    reviewer.maxLength = 256;
    reviewer.autocomplete = "off";
    reviewer.required = true;
    reviewerLabel.append(reviewer);
    const attestationLabel = document.createElement("label");
    attestationLabel.className = "admin-checkbox";
    const attestation = document.createElement("input");
    attestation.type = "checkbox";
    const attestationText = document.createElement("span");
    attestationText.textContent = "FazerCards confirmed that no invoice was created for this attempt.";
    attestationLabel.append(attestation, attestationText);
    const outcomeLabel = document.createElement("label");
    outcomeLabel.textContent = "Provider-confirmed outcome";
    const outcome = document.createElement("select");
    for (const [value, label] of [["absent", "No invoice created"], ["found", "Existing invoice found"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      outcome.append(option);
    }
    outcome.value = "absent";
    outcomeLabel.append(outcome);
    const invoiceLabel = document.createElement("label");
    invoiceLabel.textContent = "Existing provider payment ID";
    invoiceLabel.hidden = true;
    const invoice = document.createElement("input");
    invoice.maxLength = 128;
    invoice.autocomplete = "off";
    invoiceLabel.append(invoice);
    outcome.addEventListener("change", () => {
      const found = outcome.value === "found";
      invoiceLabel.hidden = !found;
      invoice.required = found;
      attestation.checked = false;
      attestationText.textContent = found
        ? "FazerCards confirmed this invoice belongs to this exact creation attempt."
        : "FazerCards confirmed that no invoice was created for this attempt.";
    });
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "admin-button admin-button--primary";
    submit.textContent = "Record provider confirmation";
    const status = document.createElement("p");
    status.className = "admin-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    form.append(referenceLabel, reviewerLabel, attestationLabel, outcomeLabel, invoiceLabel, submit, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const found = outcome.value === "found";
      if (!attestation.checked || !reference.value.trim() || !reviewer.value.trim() || (found && !invoice.value.trim())) {
        setStatus(status, "Enter the provider reference, treasury credential, invoice if found, and confirmation.", "error");
        return;
      }
      submit.disabled = true;
      setStatus(status, "Recording the provider reconciliation…");
      try {
        const result = await adminFetch(
          `/api/v1/admin/supplier-wallet/funding-attempts/${encodeURIComponent(attempt.intent_id)}/${found ? "link-invoice" : "resolve-absent"}`,
          { method: "POST", headers: { "X-Aurix-Treasury-Token": reviewer.value.trim() },
            body: JSON.stringify({ provider_reference: reference.value.trim(),
              ...(found ? { payment_id: invoice.value.trim(), confirmed_match: true } : { confirmed_absent: true }) }) },
        );
        reviewer.value = "";
        renderFundingReconciliation(result);
        if (found) {
          const payable = result.intent?.status === "pending" && Date.parse(result.intent.expires_at) > Date.now();
          showFundingPaymentLink(payable ? result.intent.payment_url : null);
          $("funding-result").textContent = JSON.stringify(result.intent, null, 2);
          $("funding-result").hidden = false;
        }
        setStatus($("funding-status"), found ? "Existing supplier invoice recovered. Check its current status before any transfer."
          : "Provider-confirmed absence recorded. Use a new request ID for any new invoice.", "success");
      } catch (error) {
        setStatus(status, error.message || "Funding attempt could not be resolved.", "error");
        submit.disabled = false;
      }
    });
    row.append(title, created, form);
    list.append(row);
  }
}

async function refreshFundingReconciliation() {
  try {
    renderFundingReconciliation(await adminFetch("/api/v1/admin/supplier-wallet/funding-attempts"));
  } catch (error) {
    fundingReconciliationReady = false;
    syncFundingSubmit();
    setStatus($("funding-review-status"), "Reconciliation state is unavailable; new invoice creation is disabled.", "error");
    throw error;
  }
}

function renderGameOrderAttention(payload) {
  const list = $("game-order-attention-list");
  const orders = Array.isArray(payload?.orders) ? payload.orders : [];
  list.replaceChildren();
  if (!orders.length) {
    setStatus($("game-order-attention-status"), "No supplier exceptions are currently queued.");
    return;
  }
  const labels = { unknown: "Unknown supplier result", reconciliation: "Reconciliation needed",
    review_refund: "Customer refund review", failed: "Failed order" };
  for (const order of orders) {
    const row = document.createElement("article");
    row.className = "admin-order-attention__row";
    const title = document.createElement("strong");
    title.textContent = `${order.order_id} · ${labels[order.state] || "Needs review"}`;
    const amount = document.createElement("small");
    amount.textContent = formatMinorAmount(order.amount_minor, order.currency);
    const product = document.createElement("small");
    product.textContent = String(order.product_slug || "Game order").replaceAll("-", " ");
    const updated = document.createElement("small");
    updated.textContent = `Last state change: ${formatDate(order.updated_at)}`;
    row.append(title, product, amount, updated);
    if (order.reconcile_available === true) {
      const status = document.createElement("small");
      status.className = "admin-order-attention__action-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "admin-button admin-button--quiet admin-order-attention__action";
      button.textContent = "Check supplier status";
      button.addEventListener("click", async () => {
        button.disabled = true;
        status.textContent = "Checking the accepted supplier order…";
        status.dataset.state = "";
        let outcome;
        try {
          const outcomePayload = await adminFetch(
            `/api/v1/admin/game-orders/${encodeURIComponent(order.order_id)}/reconcile`,
            { method: "POST", body: "{}" },
          );
          outcome = outcomePayload.outcome || {};
          status.textContent = outcome.applied
            ? `Supplier status applied · order is ${outcome.state || "updated"}.`
            : `Supplier status checked · order remains ${outcome.state || "unchanged"}.`;
          status.dataset.state = outcome.applied ? "success" : "";
        } catch (error) {
          status.textContent = error.status === 503
            ? "Supplier read access is not configured; local order state is unchanged."
            : error.status === 409
              ? "This order is not ready for supplier polling; local state is unchanged."
              : "Supplier status could not be checked; local order state is unchanged.";
          status.dataset.state = "error";
          button.disabled = false;
          return;
        }
        try {
          const refreshed = await adminFetch("/api/v1/admin/game-orders/attention");
          renderGameOrderAttention(refreshed);
          const supplierStatus = outcome.provider_status ? ` (${outcome.provider_status})` : "";
          setStatus($("dashboard-status"), `Supplier status checked for ${order.order_id}${supplierStatus}.`, "success");
        } catch (_) {
          status.textContent += " Queue refresh failed; reload the operator view.";
          status.dataset.state = "error";
          button.disabled = false;
        }
      });
      row.append(status, button);
    } else if (order.reconcile_unavailable_reason) {
      const reason = {
        supplier_attempt_missing: "No accepted supplier attempt is linked to this order.",
        supplier_read_not_configured: "Supplier read access is not configured.",
        not_pollable: "This order needs the payment or refund review workflow.",
      }[order.reconcile_unavailable_reason];
      if (reason) {
        const note = document.createElement("small");
        note.className = "admin-order-attention__note";
        note.textContent = reason;
        row.append(note);
      }
    }
    list.append(row);
  }
  setStatus($("game-order-attention-status"), payload.truncated
    ? `Showing ${orders.length} oldest unresolved orders; more remain.`
    : `${orders.length} order${orders.length === 1 ? " needs" : "s need"} operator review.`);
}

function renderMoneyIntegrity(payload) {
  const list = $("money-integrity-findings");
  list.replaceChildren();
  const issues = {
    account_balance_drift: "Account balance differs from ledger entries",
    reservation_balance_drift: "Reserved balance differs from held orders",
    entry_running_balance_drift: "Ledger running balance differs from its entries",
    entry_account_mismatch: "Ledger account or currency mismatch",
    reservation_identity_mismatch: "Reservation differs from the frozen order",
    unresolved_supplier_dispatch: "Supplier dispatch has no confirmed receipt",
    accepted_task_handoff_pending: "Accepted receipt has an unfinished local task handoff",
    settlement_handoff_pending: "Verified transfer has an unfinished ledger handoff",
    payment_decision_not_applied: "Payment review decision has not finished applying",
    external_refund_not_completed: "Authorized bank refund has no recorded completion",
    bank_and_wallet_refund_conflict: "Bank refund and wallet reimbursement both recorded",
    bank_refund_with_allocated_transfer: "Bank-refunded transfer was also allocated to an order",
    settled_bank_refund_pending: "Settled-order bank reimbursement awaits verified completion",
    settled_bank_refund_order_handoff: "Bank payout recorded; order status handoff needs recovery",
    unmatched_supplier_event: "Supplier event has no matching local order",
  };
  for (const finding of payload.findings || []) {
    const row = makeElement("div", "");
    row.className = "admin-order-attention__row";
    row.append(makeElement("strong", issues[finding.issue] || finding.issue.replaceAll("_", " ")),
      makeElement("small", finding.reference));
    list.append(row);
  }
  const count = Number(payload.total_findings || 0);
  const unavailable = (payload.unavailable_checks || []).length;
  const text = count ? `${count} local issue${count === 1 ? "" : "s"} require review.${payload.truncated ? " Showing the first 100." : ""}`
    : unavailable ? `Checks incomplete: ${unavailable} workflow schema${unavailable === 1 ? " is" : "s are"} unavailable.`
      : payload.status === "empty" ? "No ledger activity to reconcile yet."
        : "No local discrepancies found. Bank and supplier funds still need independent evidence.";
  setStatus($("money-integrity-status"), text, count || unavailable ? "error" : "");
}

function renderStoreNotices(payload) {
  const storeNotices = Array.isArray(payload?.notices) ? payload.notices : [];
  const list = $("store-notice-list");
  list.replaceChildren();
  if (!storeNotices.length) {
    setStatus($("store-notice-list-status"), "No notices yet.", "success");
    return;
  }
  setStatus($("store-notice-list-status"), `${storeNotices.length} recent notice${storeNotices.length === 1 ? "" : "s"}.`);
  for (const notice of storeNotices) {
    const row = document.createElement("article");
    row.className = "admin-notice-row";
    row.dataset.status = notice.status;
    const heading = document.createElement("div");
    heading.className = "admin-notice-row__heading";
    const title = document.createElement("strong");
    title.textContent = notice.title;
    const state = document.createElement("span");
    state.textContent = `${notice.severity} · ${notice.status}`;
    heading.append(title, state);
    const body = document.createElement("p");
    body.textContent = notice.body;
    const time = document.createElement("small");
    time.textContent = `Updated ${formatDate(notice.updated_at)}`;
    const actions = document.createElement("div");
    actions.className = "admin-heading__actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "admin-button";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => {
      $("store-notice-id").value = notice.notice_id;
      $("store-notice-title").value = notice.title;
      $("store-notice-body").value = notice.body;
      $("store-notice-severity").value = notice.severity;
      $("store-notice-status").value = notice.status;
      $("store-notice-save").textContent = "Save notice";
      $("store-notice-title").focus();
    });
    const visibility = document.createElement("button");
    visibility.type = "button";
    visibility.className = "admin-button";
    visibility.textContent = notice.status === "active" ? "Archive" : "Republish";
    visibility.addEventListener("click", async () => {
      visibility.disabled = true;
      try {
        await adminFetch(`/api/v1/admin/store-notices/${encodeURIComponent(notice.notice_id)}`, {
          method: "PUT", body: JSON.stringify({
            title: notice.title, body: notice.body, severity: notice.severity,
            status: notice.status === "active" ? "archived" : "active",
          }),
        });
        await loadStoreNotices();
      } catch (error) {
        setStatus($("store-notice-list-status"), error.message || "Notice visibility could not be changed.", "error");
      } finally { visibility.disabled = false; }
    });
    actions.append(edit, visibility);
    row.append(heading, body, time, actions);
    list.append(row);
  }
}

async function loadStoreNotices() {
  setStatus($("store-notice-list-status"), "Loading notices…");
  try { renderStoreNotices(await adminFetch("/api/v1/admin/store-notices")); }
  catch (error) { setStatus($("store-notice-list-status"), error.message || "Store notices are unavailable.", "error"); }
}

function clearStoreNoticeForm() {
  $("store-notice-form").reset();
  $("store-notice-id").value = "";
  $("store-notice-status").value = "active";
  $("store-notice-save").textContent = "Publish notice";
  setStatus($("store-notice-form-status"), "");
}

function showFundingPaymentLink(value) {
  const link = $("funding-payment-link");
  link.hidden = true;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.removeAttribute("href");
  if (typeof value !== "string" || value.length > 2048) return;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return;
    link.href = url.href;
    link.hidden = false;
  } catch (_) {
    return;
  }
}

function renderCatalog(payload) {
  const fresh = payload.status === "fresh";
  $("catalog-state").textContent = fresh ? "Fresh" : (payload.reason || "Unavailable");
  $("catalog-state").dataset.state = fresh ? "success" : "error";
  $("catalog-offers").textContent = fresh ? formatNumber(payload.offer_count, 0) : "—";
  $("catalog-detail").textContent = fresh ? `Generated ${formatDate(payload.generated_at)}`
    : (payload.reason && payload.reason !== payload.status ? payload.reason : "No current projection");
  $("catalog-observed").textContent = fresh ? `Max age ${formatNumber((payload.max_age_seconds || 0) / 60, 0)} min` : "No sellable projection";
  $("catalog-status-detail").textContent = payload.status || "—";
  $("catalog-generated").textContent = formatDate(payload.generated_at);
  $("catalog-source-observed").textContent = formatDate(payload.source_observed_at);
  $("catalog-max-age").textContent = payload.max_age_seconds ? `${formatNumber(payload.max_age_seconds / 60, 0)} minutes` : "—";
}

function renderCatalogRefreshEvidence(payload) {
  const list = $("catalog-refresh-evidence-list");
  list.replaceChildren();
  const status = payload?.status;
  const offers = Array.isArray(payload?.offers) ? payload.offers : [];
  if (!new Set(["fresh", "stale", "unavailable", "invalid"]).has(status)) {
    setStatus($("catalog-refresh-evidence-status"), "Supplier refresh evidence could not be verified.", "error");
    return;
  }
  const stateLabel = status === "fresh" ? "Current" : status === "stale" ? "Stale" : "Unavailable";
  setStatus($("catalog-refresh-evidence-status"), `${stateLabel} · checked ${formatDate(payload.checked_at)} · ${offers.length} mapped offer checks`,
    status === "fresh" ? "success" : status === "stale" ? "warning" : "error");
  if (!offers.length) {
    list.textContent = status === "fresh" ? "No mapped offer observations were recorded." : "No current offer evidence is available.";
    return;
  }
  for (const offer of offers) {
    if (!offer || ![offer.offer_id, offer.product_slug, offer.product_name, offer.label, offer.observed_at]
      .every((value) => typeof value === "string" && value.length <= 200)) continue;
    const row = makeElement("article", "", "admin-order-attention__row");
    row.append(makeElement("strong", `${offer.product_name} · ${offer.offer_id}`));
    row.append(makeElement("small", `${offer.label} · checked ${formatDate(offer.observed_at)} · quantity ${offer.stock_status || "unreported"}`));
    list.append(row);
  }
}

function renderMappingReview(payload) {
  const review = payload && typeof payload.mapping_review === "object" ? payload.mapping_review : {};
  const state = review.status || "Unknown";
  const blockReasons = review.block_reasons && typeof review.block_reasons === "object"
    ? review.block_reasons : {};
  const staleCount = blockReasons.stale_observation;
  const stale = Number.isSafeInteger(staleCount) && staleCount > 0;
  const stateElement = $("mapping-state");
  stateElement.textContent = stale ? `${state} · stale snapshot` : String(state);
  stateElement.dataset.state = state === "ready" && !stale ? "success"
    : (state === "blocked" || state === "invalid" ? "error" : stale ? "warning" : "");
  $("mapping-mapped").textContent = review.mapped == null ? "—" : formatNumber(review.mapped, 0);
  $("mapping-projected").textContent = review.projected == null ? "—" : formatNumber(review.projected, 0);
  $("mapping-captured").textContent = formatDate(review.captured_at);
  $("mapping-review-summary").textContent = stale ? "Stale snapshot · not live stock" : "Review-only snapshot · not live stock";
  const reason = review.reason || "unknown";
  const reasonElement = $("mapping-reason");
  const reasonLabels = {
    approved_projection_available: "At least one offer passes every mapping gate.",
    no_offer_passed_all_gates: stale
      ? "This snapshot is stale; use the mapped-offer checks above for current availability."
      : "No offer passed every mapping gate in this snapshot.",
    mapping_config_missing: "The private supplier mapping file is missing.",
    pricing_policy_invalid: "The saved pricing policy needs review.",
    pricing_policy_expired: "Pricing approval has expired.",
  };
  reasonElement.textContent = reasonLabels[reason] || `Review status: ${String(reason).replaceAll("_", " ")}.`;
  reasonElement.dataset.reasonCode = String(reason);
  reasonElement.dataset.state = review.projected > 0 ? "success" : "warning";
  const reasonLabelsByCode = {
    missing_mapping: "No AuriX mapping recorded",
    supplier_unavailable: "Supplier marked unavailable",
    customer_visibility_disabled: "Hidden from the customer catalog",
    supplier_disabled: "Supplier offer disabled",
    resale_unverified: "Resale rights not verified",
    region_rule_incomplete: "Region rule incomplete",
    stale_observation: "Supplier observation is stale",
  };
  const reasons = Object.entries(blockReasons)
    .filter(([key, value]) => typeof key === "string" && Number.isSafeInteger(value) && value > 0)
    .sort(([a, countA], [b, countB]) => countB - countA || a.localeCompare(b));
  const list = $("mapping-block-reasons");
  list.replaceChildren();
  if (!reasons.length) {
    list.append(makeElement("li", "No blocker details were reported.", "admin-mapping-blockers__empty"));
    return;
  }
  for (const [code, count] of reasons) {
    const label = code.startsWith("review_state:")
      ? `Mapping review state · ${code.slice("review_state:".length).replaceAll("_", " ")}`
      : reasonLabelsByCode[code] || code.replaceAll("_", " ");
    const row = makeElement("li", "", "admin-mapping-blockers__row");
    const details = makeElement("span", "", "admin-mapping-blockers__details");
    details.append(makeElement("strong", label, "admin-mapping-blockers__label"));
    details.append(makeElement("code", code, "admin-mapping-blockers__code"));
    row.append(details, makeElement("strong", formatNumber(count, 0), "admin-mapping-blockers__count"));
    list.append(row);
  }
}

function renderMappingHistory(payload) {
  const history = payload && typeof payload.mapping_history === "object" ? payload.mapping_history : {};
  const state = history.status || "Unknown";
  const stateElement = $("mapping-history-state");
  stateElement.textContent = String(state);
  stateElement.dataset.state = state === "available" ? "success" : (state === "invalid" ? "error" : "warning");
  $("mapping-history-count").textContent = history.count == null ? "—" : formatNumber(history.count, 0);
  $("mapping-history-last").textContent = formatDate(history.last_reviewed_at);
  const rows = Array.isArray(history.events) ? history.events : [];
  const lines = rows.map((event) => {
    const provider = Array.isArray(event.provider_key) ? event.provider_key.join(" · ") : "unknown provider key";
    const region = event.region_rule && event.region_rule.allow_all
      ? "all regions"
      : event.region_rule && event.region_rule.dimension
        ? `${event.region_rule.dimension}: ${(event.region_rule.allowed_values || []).join(", ")}`
        : "region rule unavailable";
    return `${formatDate(event.approved_at)} · ${provider} · ${event.product_slug || "—"} · ${region} · rights evidence ${event.rights_evidence_recorded ? "recorded" : "missing"} · reviewer ${event.reviewer || "—"}`;
  });
  $("mapping-history-events").textContent = lines.length ? lines.join("\n") : "No approval events recorded.";
}

function makeElement(tagName, text = "", className = "") {
  const element = document.createElement(tagName);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function closeMappingReview() {
  selectedInventoryItem = null;
  $("mapping-review-editor").hidden = true;
  setStatus($("mapping-approval-status"), "");
}

function beginMappingReview(item, products, approvalReady) {
  selectedInventoryItem = item;
  const editor = $("mapping-review-editor");
  editor.hidden = false;
  const supplier = item.supplier_provider === "shop2topup" ? "SHOP2TOPUP" : "FazerCards";
  const providerKey = `${supplier} · ${item.family} · ${item.category_id} · ${item.sku}`;
  const priceSource = item.price_source === "supplier_catalog_listing" ? "supplier-listed price" : "supplier cost";
  $("mapping-review-source").textContent = `${providerKey} · ${item.name || "Unnamed supplier item"} · ${priceSource} ${item.price_usd || "—"} USD · snapshot ${item.freshness_status || "unknown"}/${item.source_status || "unknown"}.`;
  $("mapping-review-schema").textContent = item.input_schema == null
    ? "No recipient schema was included for this exact offer. Verify the recipient fields from the supplier/publisher fulfillment instructions before entering them below."
    : JSON.stringify(item.input_schema, null, 2);

  const productSelect = $("mapping-product-select");
  productSelect.replaceChildren();
  const placeholder = makeElement("option", "Select the exact game or product");
  placeholder.value = "";
  productSelect.append(placeholder);
  for (const product of products) {
    if (!product || typeof product.slug !== "string" || typeof product.name !== "string") continue;
    const option = makeElement("option", product.name);
    option.value = product.slug;
    productSelect.append(option);
  }

  const fulfillmentByFamily = {
    topups: "direct_topup",
    giftcards: "gift_card",
    gamekeys: "game_key",
    manual_services: "manual_service",
  };
  $("mapping-fulfillment").value = fulfillmentByFamily[item.family] || "";
  const offerNamespace = item.supplier_provider === "shop2topup" ? "shop2topup-" : "";
  $("mapping-offer-id").value = `${offerNamespace}${item.family}-${item.sku}`.toLowerCase().replace(/[^a-z0-9._:-]+/g, "-").slice(0, 160);
  $("mapping-region-dimension").value = "game_server";
  $("mapping-region-values").value = "";
  $("mapping-region-label").value = "";
  $("mapping-rights-evidence").value = "";
  $("mapping-reviewer").value = "";
  $("mapping-input-labels").value = "";
  for (const field of ["category", "fields", "regions", "policy"]) {
    $("mapping-validation-" + field).value = "";
  }
  if (item.supplier_provider === "shop2topup" && Array.isArray(item.input_schema)) {
    $("mapping-input-labels").value = item.input_schema.map((field) => field.field_name).join("\n");
    $("mapping-validation-category").value = String(item.category_id || "");
  }
  $("mapping-allow-all-region").checked = false;
  $("mapping-approval-confirm").checked = false;
  $("mapping-approval-submit").disabled = !approvalReady;
  setStatus(
    $("mapping-approval-status"),
    approvalReady
      ? "Fresh exact-category approval is an explicit private configuration write; it does not publish the customer catalog."
      : "Approval is locked until this exact category and offer are complete and fresh, and the private mapping queue is loaded.",
    approvalReady ? "" : "error",
  );
}

function renderInventoryReviewRows(rows, products, approvalConfigured) {
  const container = $("inventory-review-rows");
  container.replaceChildren();
  for (const item of rows) {
    if (!item || typeof item !== "object") continue;
    const card = makeElement("article", "", "admin-inventory-row");
    const supplier = item.supplier_provider === "shop2topup" ? "SHOP2TOPUP" : "FazerCards";
    const title = makeElement("strong", `${item.name || "Unnamed supplier item"} · ${item.family || "unknown family"}`);
    const stock = item.stock_label || (item.stock == null ? "unknown" : String(item.stock));
    const priceSource = item.price_source === "supplier_catalog_listing" ? "listed" : "cost";
    const identifiers = makeElement("p", `${supplier} · category ${item.category_id || "—"} · SKU ${item.sku || "—"} · ${priceSource} ${item.price_usd || "—"} USD · stock ${stock}`);
    const alreadyApproved = item.mapping_gate === "ready";
    const approvalReady = approvalConfigured && item.freshness_status === "fresh" && item.source_category_complete === true;
    const state = makeElement("p", `Mapping ${item.mapping_status || "unmapped"} · region ${item.region_restriction || item.region || "not specified"} · ${alreadyApproved ? `AuriX offer ${item.mapping_offer_id || "mapped"}` : "mapping review required"}${item.catalog_family_enabled === false ? " · family hidden from customers" : ""}`);
    const candidate = item.mapping_candidate;
    const candidates = candidate && Array.isArray(candidate.candidates) ? candidate.candidates : [];
    const candidateLabels = candidates.slice(0, 3).map((match) => {
      if (!match || typeof match.product_name !== "string") return null;
      const method = match.match_method === "exact_title" ? "exact title"
        : match.match_method === "normalized_title" ? "similar title" : "name hint";
      return `${match.product_name} (${method})`;
    }).filter(Boolean);
    const moreCandidates = Math.max(0, candidates.length - candidateLabels.length);
    const hint = candidate && typeof candidate === "object"
      ? makeElement("p", candidateLabels.length
        ? `Title hint only: ${candidateLabels.join(" · ")}${moreCandidates ? ` · +${moreCandidates} more` : ""}. Verify manually; no approval.`
        : "No title match. Choose and verify the product manually; no approval.", "admin-inventory-row__hint")
      : null;
    const service = item.family === "manual_services" && item.manual_service && typeof item.manual_service === "object"
      ? makeElement("p", `Service ${item.manual_service.kind || "—"} · supplier chat ${item.manual_service.chat_enabled === true ? "available" : "not listed"}${item.manual_service.info ? ` · ${item.manual_service.info}` : ""}`)
      : null;
    const button = makeElement("button", alreadyApproved ? "Already approved" : "Review exact SKU", "admin-button admin-button--quiet");
    button.type = "button";
    const supportedFamily = item.supplier_provider === "shop2topup"
      ? item.family === "topups" && item.supplier_available === true
      : ["topups", "giftcards", "gamekeys", "manual_services"].includes(item.family);
    button.disabled = alreadyApproved || !approvalReady || !supportedFamily;
    button.addEventListener("click", () => beginMappingReview(item, products, approvalReady));
    card.append(title, identifiers, state);
    if (hint) card.append(hint);
    if (service) card.append(service);
    card.append(button);
    container.append(card);
  }
}

function renderInventory(payload) {
  const counts = payload && typeof payload.counts === "object" ? payload.counts : {};
  const families = Object.entries(counts).map(([key, value]) => `${key}: ${formatNumber(value, 0)}`);
  const mappingStatuses = payload && typeof payload.mapping_status_counts === "object" ? payload.mapping_status_counts : {};
  const mappingGates = payload && typeof payload.mapping_gate_counts === "object" ? payload.mapping_gate_counts : {};
  const projectionStatuses = payload && typeof payload.projection_status_counts === "object" ? payload.projection_status_counts : {};
  const summarize = (values) => {
    const entries = Object.entries(values).map(([key, value]) => `${key.replaceAll("_", " ")}: ${formatNumber(value, 0)}`);
    return entries.length ? entries.join(" · ") : "—";
  };
  $("inventory-count").textContent = payload.total_count == null ? "—" : formatNumber(payload.total_count, 0);
  $("inventory-families").textContent = families.length ? families.join(" · ") : "—";
  $("inventory-captured").textContent = formatDate(payload.captured_at);
  const hash = typeof payload.source_sha256 === "string" ? payload.source_sha256 : "";
  $("inventory-hash").textContent = hash ? `${hash.slice(0, 12)}…` : "—";
  $("inventory-mapping-status").textContent = summarize(mappingStatuses);
  $("inventory-mapping-gates").textContent = summarize(mappingGates);
  $("inventory-projection").textContent = summarize(projectionStatuses);
  const rows = Array.isArray(payload.items) ? payload.items : [];
  currentInventoryProducts = Array.isArray(payload.reference_products) ? payload.reference_products : [];
  const supplierProvider = $("supplier-provider-filter").value || "fazercards";
  const selectedProviderStatus = payload.providers && typeof payload.providers === "object"
    ? payload.providers[supplierProvider] || {}
    : { mapping_config_status: payload.mapping_config_status };
  const providerConfigStatus = selectedProviderStatus.mapping_config_status || payload.mapping_config_status;
  const approvalConfigured = payload.status === "private_snapshot"
    && (providerConfigStatus === "loaded" || (supplierProvider === "shop2topup" && providerConfigStatus === "missing"));
  for (const item of rows) {
    if (item && typeof item === "object") {
      item.source_status = item.source_status || selectedProviderStatus.source_status || payload.source_status;
    }
  }
  renderInventoryReviewRows(rows, currentInventoryProducts, approvalConfigured);
  inventoryOffset = Number(payload.offset || 0);
  const pageStart = rows.length ? Number(payload.offset || 0) + 1 : 0;
  const pageEnd = Number(payload.offset || 0) + rows.length;
  $("inventory-page-status").textContent = rows.length
    ? `Showing ${formatNumber(pageStart, 0)}–${formatNumber(pageEnd, 0)} of ${formatNumber(payload.total_count || 0, 0)}`
    : `No rows · ${formatNumber(payload.total_count || 0, 0)} total`;
  $("inventory-previous").disabled = Number(payload.offset || 0) <= 0;
  $("inventory-next").disabled = payload.next_offset == null;
  $("inventory-next").dataset.nextOffset = payload.next_offset == null ? "" : String(payload.next_offset);
  if (supplierProvider === "shop2topup" && selectedProviderStatus.status === "unavailable") {
    $("inventory-state").textContent = "No SHOP2TOPUP audit snapshot yet. Configure the server key, then run python -m aurix_games.shop2topup_catalog_audit. This screen does not call the supplier or start orders.";
  } else {
    const captured = selectedProviderStatus.captured_at || payload.captured_at;
    const freshness = selectedProviderStatus.freshness_status || payload.freshness_status || "unknown";
    $("inventory-state").textContent = payload.status === "private_snapshot"
      ? `${supplierProvider === "shop2topup" ? "SHOP2TOPUP private audit" : "FazerCards private snapshot"} · ${selectedProviderStatus.source_status || payload.source_status || "unknown"} · ${freshness} · captured ${formatDate(captured)}. No customer publication.`
      : "Inventory unavailable.";
  }
}

function inventoryRequestPath() {
  const parameters = new URLSearchParams();
  parameters.set("limit", String(INVENTORY_PAGE_SIZE));
  parameters.set("supplier_provider", $("supplier-provider-filter").value || "fazercards");
  const family = $("inventory-family-filter").value;
  const category = $("inventory-category-filter").value.trim();
  const query = $("inventory-query-filter").value.trim();
  if (family) parameters.set("family", family);
  if (category) parameters.set("category", category);
  if (query) parameters.set("q", query);
  if (inventoryOffset > 0) parameters.set("offset", String(inventoryOffset));
  return `/api/v1/admin/catalog-inventory?${parameters.toString()}`;
}

async function loadInventory() {
  $("inventory-state").textContent = "Loading the protected exact inventory…";
  try {
    const payload = await adminFetch(inventoryRequestPath());
    renderInventory(payload);
  } catch (error) {
    if (error && error.status === 401) {
      disconnect("The admin token was rejected.");
      return;
    }
    $("inventory-state").textContent = `Inventory unavailable — ${error.message}`;
    $("inventory-state").dataset.state = "error";
  }
}

function renderSubscription(payload) {
  const subscription = payload && typeof payload.subscription === "object" ? payload.subscription : {};
  const nested = subscription.subscription && typeof subscription.subscription === "object"
    ? subscription.subscription : {};
  const active = subscription.subscriptionActive ?? nested.subscriptionActive;
  const state = typeof active === "boolean" ? (active ? "Active" : "Inactive")
    : subscription.status || subscription.state || nested.status || nested.state || "Unknown";
  const plan = subscription.planName || subscription.plan_name || subscription.plan || nested.planName || nested.plan || "Plan not reported";
  const trialEnd = subscription.trialEndsAt || subscription.trial_ends_at || nested.trialEndsAt || nested.trial_ends_at;
  const expires = subscription.planExpiresAt || nested.planExpiresAt || subscription.expiresAt || subscription.expires_at || subscription.endsAt || subscription.ends_at || nested.expiresAt || nested.expires_at;
  const autoRenew = subscription.planAutoRenew ?? nested.planAutoRenew ?? subscription.autoRenew ?? subscription.auto_renew ?? nested.autoRenew ?? nested.auto_renew;
  const stateElement = $("subscription-state");
  stateElement.textContent = String(state);
  stateElement.dataset.state = /expired|inactive|cancel/i.test(String(state)) ? "error"
    : /^(active|trial)$/i.test(String(state)) ? "success" : "";
  const parts = [`Plan: ${plan}`];
  if (trialEnd) parts.push(`Trial ends: ${formatDate(trialEnd)}`);
  if (expires) parts.push(`Ends: ${formatDate(expires)}`);
  $("subscription-detail").textContent = parts.join(" · ");
  const meta = [];
  if (autoRenew !== undefined) meta.push(`Auto-renew: ${autoRenew ? "on" : "off"}`);
  const remaining = subscription.remainingDays ?? subscription.remaining_days ?? nested.remainingDays ?? nested.remaining_days;
  if (remaining !== undefined) meta.push(`Remaining days: ${remaining}`);
  $("subscription-meta").textContent = meta.join(" · ") || "Read-only supplier state; inspect a quote before any paid-plan decision.";
}

function renderWebhook(payload) {
  const webhook = payload && typeof payload.webhook === "object" ? payload.webhook : {};
  const state = webhook.status || webhook.state || (webhook.enabled ? "enabled" : "not configured");
  const stateElement = $("webhook-state");
  stateElement.textContent = String(state);
  stateElement.dataset.state = webhook.enabled ? "success" : "";
  $("webhook-url").textContent = webhook.url || "Not configured";
  const input = $("webhook-url-input");
  if (webhook.url && !input.value) input.value = webhook.url;
  if (typeof webhook.enabled === "boolean") $("webhook-enabled").checked = webhook.enabled;
}

function renderAlerts(payload) {
  const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
  const element = $("wallet-alerts");
  if (alerts.length === 0) {
    element.textContent = "No pending wallet reminders.";
    element.dataset.state = "";
    return;
  }
  const summaries = alerts.map((alert) => {
    const data = alert && typeof alert.payload === "object" ? alert.payload : {};
    const balance = data.balance_usd || "—";
    const threshold = data.threshold_usd || "—";
    const created = formatDate(alert.created_at);
    return `${balance} USD · threshold ${threshold} USD · ${data.reason || "low balance"} · ${created}`;
  });
  element.textContent = `${alerts.length} pending reminder${alerts.length === 1 ? "" : "s"}: ${summaries.join("; ")}`;
  element.dataset.state = "warning";
}

const POLICY_FIELDS = [
  "sell_currency", "supplier_currency", "fx_rate", "target_profit_pct", "minimum_profit_pct",
  "target_profit_minor", "operating_fee_minor", "payment_fee_pct", "payment_fee_minor",
  "marketing_pct", "payback_pct", "promo_pct", "promo_fixed_minor", "rounding_minor",
  "minimum_price_minor", "maximum_price_minor",
];

function updateFxMode() {
  const automatic = $("pricing-auto-fx").checked;
  $("pricing-manual-fx-fields").hidden = automatic;
  $("pricing-fx-source").required = !automatic;
  $("pricing-fx-observed-at").required = !automatic;
  $("pricing-valid-until").required = !automatic;
  $("pricing-fx-rate-caption").textContent = automatic ? "FX floor (MMK per USD)" : "FX rate (MMK per USD)";
  $("pricing-auto-fx-help").hidden = !automatic;
}

function renderPricingSummary(payload, policy, approval, automatic) {
  const automaticEnabled = automatic.enabled === true;
  const rate = automaticEnabled ? automatic.market_rate : policy.fx_rate;
  const updatedAt = automaticEnabled
    ? automatic.fetched_at
    : approval.fx_observed_at || approval.approved_at;
  $("pricing-rate-updated").textContent = rate == null
    ? (automaticEnabled ? "Last CBM rate: not received" : "Last FX rate: not available")
    : `${automaticEnabled ? "Last CBM rate" : "Last FX rate"}: ${formatNumber(rate, 2)} MMK/USD · updated ${formatDate(updatedAt)}`;

  let warning = "";
  if (automaticEnabled && payload.policy_status === "awaiting_rate") {
    warning = "⚠ No CBM rate is cached; listings are paused until refresh succeeds.";
  } else if (automaticEnabled && payload.policy_status === "expired") {
    warning = "⚠ The CBM quote is stale; listings continue at the last approved rate. Refresh or review Pricing settings.";
  } else if (automaticEnabled && payload.policy_status === "approved"
      && Date.parse(automatic.valid_until || "") - Date.now() <= 24 * 60 * 60 * 1000) {
    warning = "⚠ The CBM quote is nearing its 96-hour limit; confirm the next refresh succeeds.";
  } else if (!automaticEnabled && payload.policy_status === "expired") {
    warning = "⚠ Pricing approval expired; listings continue at the last approved rate. Update Pricing settings.";
  } else if (!automaticEnabled && payload.policy_status === "unconfigured") {
    warning = "⚠ No approved pricing rate; listings are paused. Configure Pricing settings.";
  } else if (!automaticEnabled && payload.policy_status === "approved"
      && Date.parse(approval.valid_until || "") - Date.now() <= 24 * 60 * 60 * 1000) {
    warning = "⚠ Manual pricing expires within 24 hours; renew it in Pricing settings.";
  }
  const warningElement = $("pricing-warning");
  warningElement.textContent = warning;
  warningElement.hidden = !warning;
}

function renderPolicy(payload) {
  if (pricingPolicyDirty) {
    setStatus($("pricing-status"), "Unsaved pricing edits retained. Save them or reload the page to discard.", "warning");
    return;
  }
  const policy = payload.policy || {};
  const automatic = payload.automatic_fx || { enabled: false };
  $("pricing-auto-fx").checked = automatic.enabled === true;
  updateFxMode();
  for (const key of POLICY_FIELDS) {
    const input = document.querySelector(`[data-policy="${key}"]`);
    const value = key === "fx_rate" && automatic.enabled ? automatic.floor_rate : policy[key];
    if (input) input.value = key === "sell_currency" ? "MMK" : key === "supplier_currency" ? "USD" : (value == null ? "" : String(value));
  }
  const approval = payload.approval || {};
  $("pricing-fx-source").value = approval.fx_source || "";
  $("pricing-fx-observed-at").value = approval.fx_observed_at || "";
  $("pricing-valid-until").value = approval.valid_until || "";
  renderPricingSummary(payload, policy, approval, automatic);
  if (automatic.enabled === true) {
    if (payload.policy_status === "approved") {
      const expiresAt = Date.parse(automatic.valid_until || "");
      const expiresSoon = Number.isFinite(expiresAt) && expiresAt - Date.now() <= 24 * 60 * 60 * 1000;
      const effectiveRate = Number(automatic.effective_rate).toLocaleString("en-US");
      const floorRate = Number(automatic.floor_rate).toLocaleString("en-US");
      const marketRate = Number(automatic.market_rate).toLocaleString("en-US");
      $("pricing-state").textContent = expiresSoon ? "Automatic rate aging" : "Automatic rate active";
      $("pricing-expiry").textContent = `CBM ${automatic.market_date} · ${effectiveRate} MMK/USD · expires ${formatDate(automatic.valid_until)}`;
      $("pricing-state").dataset.state = expiresSoon ? "warning" : "success";
      setStatus($("pricing-status"), `CBM market rate ${marketRate}; approved floor ${floorRate}; effective rate ${effectiveRate} MMK/USD. The catalog refresh updates this automatically; a stale quote pauses listings.`, expiresSoon ? "warning" : "success");
    } else if (payload.policy_status === "awaiting_rate") {
      $("pricing-state").textContent = "First rate pending";
      $("pricing-expiry").textContent = "Waiting for the next catalog refresh to fetch CBM";
      $("pricing-state").dataset.state = "warning";
      setStatus($("pricing-status"), "Automatic FX is approved, but no fresh quote is cached yet; listings remain paused until the scheduled refresh succeeds.", "warning");
    } else {
      $("pricing-state").textContent = "Last rate in use";
      $("pricing-expiry").textContent = `CBM quote expired ${formatDate(automatic.valid_until)}`;
      $("pricing-state").dataset.state = "warning";
      setStatus($("pricing-status"), "The CBM quote is older than 96 hours. Listings continue at the last approved rate until a new quote is fetched.", "warning");
    }
    return;
  }
  if (payload.policy_status === "approved") {
    const expiresAt = Date.parse(approval.valid_until || "");
    const expiresSoon = Number.isFinite(expiresAt) && expiresAt - Date.now() <= 24 * 60 * 60 * 1000;
    $("pricing-state").textContent = expiresSoon ? "Renewal due" : "Approved";
    $("pricing-expiry").textContent = `Expires ${formatDate(approval.valid_until)}`;
    $("pricing-state").dataset.state = expiresSoon ? "warning" : "success";
    setStatus($("pricing-status"), `Approved by ${approval.approved_by}; expires ${formatDate(approval.valid_until)}.${expiresSoon ? " Renew FX approval within 24 hours." : ""}`, expiresSoon ? "warning" : "success");
  } else {
    const expired = payload.policy_status === "expired";
    $("pricing-state").textContent = expired ? "Last rate in use" : "Unconfigured";
    $("pricing-expiry").textContent = expired
      ? `Approval expired ${formatDate(approval.valid_until)}`
      : "An owner-approved rate is required";
    $("pricing-state").dataset.state = expired ? "warning" : "error";
    setStatus($("pricing-status"), expired
      ? "Pricing approval expired; listings continue at the last approved rate. Confirm or update it in Pricing settings."
      : "No approved pricing policy; catalog publication is blocked.", "warning");
  }
}

function readPolicy() {
  const policy = {};
  for (const key of POLICY_FIELDS) {
    const input = document.querySelector(`[data-policy="${key}"]`);
    if (!input) continue;
    const value = key === "sell_currency" ? "MMK" : key === "supplier_currency" ? "USD" : input.value.trim();
    policy[key] = value === "" ? null : (key === "sell_currency" || key === "supplier_currency" ? value.toUpperCase() : value);
  }
  return policy;
}

function selectedAdminView() {
  return adminViewTabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset.adminView || "overview";
}

function adminViewRequests(view) {
  const unavailable = (element, message) => (error) => setStatus(element, `${message} ${error.message}`, "error");
  const requests = {
    overview: [
      { label: "wallet", path: "/api/v1/admin/supplier-wallet/status", render: renderWallet },
      { label: "pricing", path: "/api/v1/admin/pricing-policy", render: renderPolicy },
      { label: "catalog", path: "/api/v1/admin/live-catalog/status", render: renderCatalog },
      {
        label: "game order queue", path: "/api/v1/admin/game-orders/attention", render: renderGameOrderAttention,
        onError: unavailable($("game-order-attention-status"), "The game order queue is unavailable."),
      },
      {
        label: "money integrity", path: "/api/v1/admin/money-integrity", render: renderMoneyIntegrity,
        onError: (error) => {
          $("money-integrity-findings").replaceChildren();
          unavailable($("money-integrity-status"), "Integrity checks unavailable; do not infer that balances reconcile.")(error);
        },
      },
    ],
    catalog: [
      {
        label: "current customer catalog", path: "/api/v1/admin/live-catalog/status", render: renderCatalog,
        onError: () => {
          $("catalog-state").textContent = "Unavailable";
          $("catalog-state").dataset.state = "error";
          $("catalog-offers").textContent = "—";
          $("catalog-detail").textContent = "Could not verify current published offers.";
          $("catalog-observed").textContent = "No current confirmation";
          $("catalog-status-detail").textContent = "Unavailable";
          $("catalog-generated").textContent = "—";
          $("catalog-source-observed").textContent = "—";
          $("catalog-max-age").textContent = "—";
        },
      },
      {
        label: "catalog choices", path: "/api/v1/admin/catalog-policy",
        render: renderCatalogPolicy,
        onError: (error) => {
          $("catalog-policy-save").disabled = true;
          catalogPolicyRevision = null;
          unavailable($("catalog-policy-status"), "Catalog choices unavailable; saving is locked until reloaded.")(error);
        },
      },
      {
        label: "supplier refresh evidence", path: "/api/v1/admin/catalog-refresh-status",
        render: renderCatalogRefreshEvidence,
        onError: unavailable($("catalog-refresh-evidence-status"), "Latest supplier evidence is unavailable."),
      },
    ],
    supplier: [
      { label: "wallet", path: "/api/v1/admin/supplier-wallet/status", render: renderWallet },
      { label: "wallet alerts", path: "/api/v1/admin/supplier-wallet/alerts?status=pending&limit=20", render: renderAlerts },
      { label: "subscription", path: "/api/v1/admin/subscription/status", render: renderSubscription },
      {
        label: "supplier payment limits", path: "/api/v1/admin/supplier-wallet/payment-methods",
        render: renderFundingMethods,
        onError: (error) => {
          fundingLimitsReady = true;
          fundingCanCreate = false;
          syncFundingSubmit();
          unavailable($("funding-limits"), "Could not load current account limits; invoice creation is disabled.")(error);
        },
      },
      {
        label: "funding reconciliation", path: "/api/v1/admin/supplier-wallet/funding-attempts",
        render: renderFundingReconciliation,
        onError: (error) => {
          fundingReconciliationReady = false;
          syncFundingSubmit();
          unavailable($("funding-review-status"), "Reconciliation state is unavailable; new invoice creation is disabled.")(error);
        },
      },
    ],
    settings: [
      { label: "pricing", path: "/api/v1/admin/pricing-policy", render: renderPolicy },
      { label: "supplier webhook", path: "/api/v1/admin/supplier-webhook", render: renderWebhook },
    ],
    notices: [
      {
        label: "store notices", path: "/api/v1/admin/store-notices", render: renderStoreNotices,
        onError: unavailable($("store-notice-list-status"), "Store notices could not be loaded."),
      },
    ],
  };
  return requests[view] || requests.overview;
}

function loadDashboard(view = selectedAdminView()) {
  const pending = adminViewLoads.get(view);
  if (pending) return pending;
  const load = (async () => {
    setStatus($("dashboard-status"), `Refreshing ${view.replaceAll("-", " ")}…`);
    const requests = adminViewRequests(view);
    const errors = [];
    let finished = 0;
    const updateStatus = () => {
      if ($("dashboard").hidden || selectedAdminView() !== view) return;
      if (finished < requests.length) {
        setStatus($("dashboard-status"), `Loading ${view.replaceAll("-", " ")} · ${finished}/${requests.length} checks complete…`);
      } else {
        setStatus($("dashboard-status"), errors.length
          ? `Loaded with unavailable checks — ${errors.join(" · ")}`
          : `Updated ${new Date().toLocaleTimeString()}`, errors.length ? "error" : "success");
      }
    };
    const results = await Promise.all(requests.map(async (request) => {
      try {
        request.render(await adminFetch(request.path));
        return { ok: true };
      } catch (error) {
        if (error.status === 401) {
          disconnect(storeAdminSession ? "Store admin session expired; sign in again." : "The admin token was rejected.");
          return { authFailure: true };
        }
        errors.push(`${request.label}: ${error.message}`);
        try { request.onError?.(error); } catch (_) { /* keep other panels usable */ }
        return { ok: false };
      } finally {
        finished += 1;
        updateStatus();
      }
    }));
    if (results.some((result) => result.authFailure)) return;
    updateStatus();
  })();
  adminViewLoads.set(view, load);
  load.then(
    () => { if (adminViewLoads.get(view) === load) adminViewLoads.delete(view); },
    () => { if (adminViewLoads.get(view) === load) adminViewLoads.delete(view); },
  );
  return load;
}

async function loadCatalogDisclosure(kind) {
  const configurations = {
    mapping: {
      path: "/api/v1/admin/mapping-review",
      render: renderMappingReview,
      errorElement: $("mapping-reason"),
      message: "Supplier snapshot review unavailable.",
    },
    history: {
      path: "/api/v1/admin/mapping-history",
      render: renderMappingHistory,
      errorElement: $("mapping-history-state"),
      message: "Mapping approval history unavailable.",
    },
  };
  const config = configurations[kind];
  if (!config) return;
  setStatus($("dashboard-status"), "Loading the requested catalog review…");
  try {
    config.render(await adminFetch(config.path));
    if (selectedAdminView() === "catalog") setStatus($("dashboard-status"), `Updated ${new Date().toLocaleTimeString()}`, "success");
  } catch (error) {
    if (error.status === 401) {
      disconnect(storeAdminSession ? "Store admin session expired; sign in again." : "The admin token was rejected.");
      return;
    }
    setStatus(config.errorElement, `${config.message} ${error.message}`, "error");
    if (selectedAdminView() === "catalog") setStatus($("dashboard-status"), `${config.message} ${error.message}`, "error");
  }
}

function disconnect(message = "Disconnected.") {
  adminToken = "";
  catalogPolicyRevision = null;
  catalogPolicyDirty = false;
  $("catalog-policy-save").disabled = true;
  $("funding-credential").value = "";
  $("funding-review-list").replaceChildren();
  $("money-integrity-findings").replaceChildren();
  setStatus($("money-integrity-status"), "Not loaded");
  closeMappingReview();
  showDashboard(false);
  $("admin-token").value = "";
  setStatus($("auth-status"), message, message === "Disconnected." ? "" : "error");
  if (storeAdminSession) $("auth-panel").hidden = false;
}

async function connectStoreAdmin() {
  $("auth-title").textContent = "Store admin access";
  $("auth-copy").textContent = "Operator tools are limited to the configured Store super admin.";
  $("auth-form").hidden = true;
  $("admin-sign-in").hidden = true;
  setStatus($("auth-status"), "Checking your Store admin role…");
  try {
    const response = await fetch("/api/v1/store-staff/session", {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(10000),
    });
    const session = await response.json();
    if (!response.ok) {
      const error = new Error(session.error || "Store admin sign-in is required.");
      error.status = response.status;
      throw error;
    }
    if (session.role !== "super_admin") {
      throw new Error("This Store account does not have super-admin access.");
    }
    $("auth-panel").hidden = true;
    showDashboard(true);
    await loadDashboard();
  } catch (error) {
    $("auth-panel").hidden = false;
    if (error.status === 401) {
      $("admin-sign-in").hidden = false;
      setStatus($("auth-status"), "Store owner sign-in required.", "warning");
    } else {
      setStatus($("auth-status"), error.message || "Admin access could not be checked. Reload to retry.", "error");
    }
  }
}

$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (storeAdminSession) return;
  adminToken = $("admin-token").value;
  if (!adminToken) return;
  showDashboard(true);
  setStatus($("auth-status"), "");
  await loadDashboard();
});

$("refresh-button").addEventListener("click", () => loadDashboard());
$("store-notice-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const noticeId = $("store-notice-id").value;
  const payload = {
    title: $("store-notice-title").value.trim(),
    body: $("store-notice-body").value.trim(),
    severity: $("store-notice-severity").value,
    status: $("store-notice-status").value,
  };
  $("store-notice-save").disabled = true;
  setStatus($("store-notice-form-status"), "Saving notice…");
  try {
    await adminFetch(noticeId ? `/api/v1/admin/store-notices/${encodeURIComponent(noticeId)}` : "/api/v1/admin/store-notices", {
      method: noticeId ? "PUT" : "POST", body: JSON.stringify(payload),
    });
    clearStoreNoticeForm();
    setStatus($("store-notice-form-status"), "Notice saved.", "success");
    await loadStoreNotices();
  } catch (error) {
    setStatus($("store-notice-form-status"), error.message || "Notice could not be saved.", "error");
  } finally { $("store-notice-save").disabled = false; }
});
$("store-notice-clear").addEventListener("click", clearStoreNoticeForm);
$("disconnect-button").addEventListener("click", () => {
  if (storeAdminSession) location.href = "../staff/";
  else disconnect();
});
$("inventory-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  inventoryOffset = 0;
  await loadInventory();
});
$("supplier-provider-filter").addEventListener("change", async () => {
  inventoryOffset = 0;
  closeMappingReview();
  await loadInventory();
});
$("inventory-previous").addEventListener("click", async () => {
  inventoryOffset = Math.max(0, inventoryOffset - INVENTORY_PAGE_SIZE);
  await loadInventory();
});
$("inventory-next").addEventListener("click", async () => {
  const nextOffset = Number($("inventory-next").dataset.nextOffset);
  if (!Number.isInteger(nextOffset) || nextOffset <= inventoryOffset) return;
  inventoryOffset = nextOffset;
  await loadInventory();
});

$("mapping-review-cancel").addEventListener("click", closeMappingReview);
$("mapping-approval-cancel").addEventListener("click", closeMappingReview);
$("mapping-allow-all-region").addEventListener("change", () => {
  const allowAll = $("mapping-allow-all-region").checked;
  $("mapping-region-dimension").disabled = allowAll;
  $("mapping-region-values").disabled = allowAll;
  $("mapping-region-values").required = !allowAll;
});
$("mapping-product-select").addEventListener("change", () => {
  const slug = $("mapping-product-select").value;
  if (!slug || !selectedInventoryItem) return;
  const suffix = String(selectedInventoryItem.sku || "").toLowerCase().replace(/[^a-z0-9._:-]+/g, "-");
  const namespace = selectedInventoryItem.supplier_provider === "shop2topup" ? "shop2topup-" : "";
  $("mapping-offer-id").value = `${namespace}${slug}-${suffix}`.slice(0, 160);
});
$("mapping-review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedInventoryItem) {
    setStatus($("mapping-approval-status"), "Choose an exact supplier row first.", "error");
    return;
  }
  const allowAll = $("mapping-allow-all-region").checked;
  const regionValues = $("mapping-region-values").value.split(",").map((value) => value.trim()).filter(Boolean);
  const body = {
    supplier_provider: selectedInventoryItem.supplier_provider || "fazercards",
    family: selectedInventoryItem.family,
    category_id: selectedInventoryItem.category_id,
    sku: selectedInventoryItem.sku,
    offer_id: $("mapping-offer-id").value.trim(),
    product_slug: $("mapping-product-select").value,
    fulfillment_kind: $("mapping-fulfillment").value,
    region_rule: allowAll
      ? { allow_all: true }
      : { allow_all: false, dimension: $("mapping-region-dimension").value, allowed_values: regionValues },
    region_label: $("mapping-region-label").value.trim() || null,
    required_input_labels: $("mapping-input-labels").value.split("\n").map((value) => value.trim()).filter(Boolean),
    rights_evidence: $("mapping-rights-evidence").value.trim(),
    reviewer: $("mapping-reviewer").value.trim(),
    confirm_approval: $("mapping-approval-confirm").checked,
  };
  const submit = $("mapping-approval-submit");
  submit.disabled = true;
  setStatus($("mapping-approval-status"), "Recording one private mapping approval…");
  try {
    const category = $("mapping-validation-category").value.trim();
    const settings = {
      validation_field_map: $("mapping-validation-fields").value.trim(),
      validation_region_map: $("mapping-validation-regions").value.trim(),
      validation_region_policy: $("mapping-validation-policy").value.trim(),
    };
    if (category) body.validation_category_id = category;
    for (const [key, text] of Object.entries(settings)) {
      if (!text) continue;
      const value = JSON.parse(text);
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("Validation settings must be JSON objects.");
      }
      body[key] = value;
    }
    const result = await adminFetch("/api/v1/admin/mapping-approval", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const approval = result.mapping_approval || {};
    closeMappingReview();
    setStatus(
      $("inventory-state"),
      `Private mapping recorded for ${approval.offer_id || "the selected SKU"}. Supplier write: no; customer publication: no.`,
      "success",
    );
    await loadInventory();
    const [review, history] = await Promise.all([
      adminFetch("/api/v1/admin/mapping-review"),
      adminFetch("/api/v1/admin/mapping-history"),
    ]);
    renderMappingReview(review);
    renderMappingHistory(history);
  } catch (error) {
    if (error && error.status === 401) {
      disconnect("The admin token was rejected.");
      return;
    }
    setStatus($("mapping-approval-status"), error.message, "error");
  } finally {
    if (!$("mapping-review-editor").hidden) submit.disabled = false;
  }
});

$("catalog-policy-form").addEventListener("input", () => { catalogPolicyDirty = true; });
$("catalog-policy-reload").addEventListener("click", () => { catalogPolicyDirty = false; void reloadCatalogPolicy(); });
$("catalog-policy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (catalogPolicyRevision === null || $("catalog-policy-save").disabled) return;
  const rows = (id) => $(id).value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const policy = { enabled_families: CATALOG_FAMILIES.filter((family) => $("catalog-family-" + family).checked),
    excluded_products: rows("catalog-excluded-products"), excluded_offers: rows("catalog-excluded-offers") };
  $("catalog-policy-save").disabled = true;
  try {
    const result = await adminFetch("/api/v1/admin/catalog-policy", {
      method: "PUT", body: JSON.stringify({ policy, expected_revision: catalogPolicyRevision }),
    });
    catalogPolicyDirty = false;
    renderCatalogPolicy(result);
  } catch (error) {
    catalogPolicyRevision = null;
    setStatus($("catalog-policy-status"), `${error.message} Reload saved choices before saving again.`, "error");
  }
});

$("pricing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus($("pricing-status"), "Saving policy…");
  try {
    const payload = await adminFetch("/api/v1/admin/pricing-policy", {
      method: "PUT",
      headers: { "X-Aurix-Treasury-Token": $("pricing-review-credential").value },
      body: JSON.stringify({
        policy: readPolicy(),
        ...( $("pricing-auto-fx").checked ? { automatic_fx: true } : { approval: {
          fx_source: $("pricing-fx-source").value.trim(),
          fx_observed_at: $("pricing-fx-observed-at").value.trim(),
          valid_until: $("pricing-valid-until").value.trim(),
        } }),
        owner_approval: $("pricing-owner-approval").checked,
      }),
    });
    pricingPolicyDirty = false;
    renderPolicy(payload);
    setStatus($("pricing-status"), $("pricing-auto-fx").checked
      ? "Automatic FX saved. No daily rate entry is needed; the next catalog refresh will fetch CBM and publish eligible offers if all other checks pass. Check Overview."
      : "Pricing saved. The catalog refresh runs about every five minutes; check Overview for published offers.", "success");
  } catch (error) {
    setStatus($("pricing-status"), error.message, "error");
  } finally {
    $("pricing-review-credential").value = "";
    $("pricing-owner-approval").checked = false;
  }
});

$("pricing-auto-fx").addEventListener("change", () => {
  updateFxMode();
  pricingPolicyDirty = true;
});

for (const input of document.querySelectorAll("#pricing-form input:not([type='password'])")) {
  input.addEventListener("input", () => { pricingPolicyDirty = true; });
}

$("funding-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (fundingRequestBusy) return;
  if (!fundingLimitsReady || !fundingCanCreate || !fundingReconciliationReady || fundingBlockedByUnknown) {
    setStatus($("funding-status"), "Current supplier limits and reconciliation status are required; invoice creation is disabled.", "error");
    return;
  }
  $("funding-result").hidden = true;
  showFundingPaymentLink(null);
  const amountInput = $("funding-amount");
  const amount = Number(amountInput.value);
  if (!Number.isFinite(amount) || amount < Number(amountInput.min) || amount > Number(amountInput.max)) {
    setStatus($("funding-status"), "Amount is outside the current account limits.", "error");
    return;
  }
  setStatus($("funding-status"), "Creating restart-safe funding intent…");
  const intentId = $("funding-intent-id").value.trim();
  if (!intentId) {
    setStatus($("funding-status"), "Enter a funding request ID and keep it for retries or reconciliation.", "error");
    return;
  }
  const body = {
    method: $("funding-method").value,
    amount_usd: $("funding-amount").value,
  };
  body.intent_id = intentId;
  fundingRequestBusy = true;
  syncFundingSubmit();
  let created = false;
  try {
    const payload = await adminFetch("/api/v1/admin/supplier-wallet/invoice", {
      method: "POST",
      headers: { "X-Aurix-Treasury-Token": $("funding-credential").value },
      body: JSON.stringify(body),
    });
    showFundingPaymentLink(payload && payload.intent && payload.intent.payment_url);
    $("funding-result").textContent = JSON.stringify(payload, null, 2);
    $("funding-result").hidden = false;
    setStatus($("funding-status"), "Funding intent created. Verify destination and amount independently before any transfer.", "success");
    created = true;
  } catch (error) {
    setStatus($("funding-status"), `${error.message} Keep this request ID; reconcile an uncertain creation before starting another.`, "error");
    await refreshFundingReconciliation().catch(() => {});
  } finally {
    $("funding-credential").value = "";
    fundingRequestBusy = false;
    syncFundingSubmit();
  }
  if (created) await refreshFundingReconciliation().catch(() => {});
});

$("webhook-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus($("webhook-status"), "Saving webhook configuration…");
  try {
    const payload = await adminFetch("/api/v1/admin/supplier-webhook", {
      method: "PUT",
      body: JSON.stringify({
        url: $("webhook-url-input").value.trim(),
        enabled: $("webhook-enabled").checked,
      }),
    });
    renderWebhook(payload);
    setStatus($("webhook-status"), "Webhook configuration saved. Secret material remains server-side.", "success");
  } catch (error) {
    setStatus($("webhook-status"), error.message, "error");
  }
});

const catalogDisclosures = [...document.querySelectorAll("#view-catalog > details.admin-disclosure")];
catalogDisclosures[0]?.addEventListener("toggle", () => {
  if (catalogDisclosures[0].open && !$("dashboard").hidden) void loadCatalogDisclosure("mapping");
});
catalogDisclosures[1]?.addEventListener("toggle", () => {
  if (catalogDisclosures[1].open && !$("dashboard").hidden) void loadCatalogDisclosure("history");
});
catalogDisclosures[2]?.addEventListener("toggle", () => {
  if (catalogDisclosures[2].open && !$("dashboard").hidden) void loadInventory();
});

showDashboard(false);
if (storeAdminSession) void connectStoreAdmin();
