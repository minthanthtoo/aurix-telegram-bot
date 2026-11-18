import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFile } from "node:fs/promises";
const code = await readFile(new URL("./web/game-store/review/review.js", import.meta.url), "utf8");
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.events = {}; this.value = ""; this.checked = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, callback) { this.events[name] = callback; }
  setAttribute() {}
  reportValidity() { return true; }
}
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(setImmediate); };
const all = (root) => [root, ...root.children.flatMap(all)];
function setup(currency = "MMK", amountMinor = 5000) {
  const ids = new Map(["review-login", "review-token", "review-controls", "review-status", "review-cases", "review-refresh", "review-disconnect"].map(id => [id, new Element()]));
  const calls = [], events = {};
  const item = { order_id: "order-1", revision: 2, state: "awaiting_payment", amount_minor: amountMinor, currency,
    claim: { transaction_reference: "unverified", evidence_sha256: "a".repeat(64) },
    payment: { rail: "kpay", receiver_id: "receiver-1" }, submitted_at: "2026-11-13T00:00:00Z", decision: null };
  let failure = false;
  let queueData = { actor: "staff:alice", cases: [item], refunds: [], limit: 200 };
  vm.runInNewContext(code, { document: { querySelector: selector => ids.get(selector.slice(1)), createElement: tag => new Element(tag) },
    window: { addEventListener: (name, cb) => { events[name] = cb; } }, AbortSignal, Number, JSON, Error, encodeURIComponent,
    fetch: async (path, options) => { calls.push({ path, options });
      return { ok: !failure, status: failure ? 503 : 200,
        json: async () => queueData }; },
  });
  return { ids, calls, item, events, fail: () => { failure = true; }, setQueue: value => { queueData = value; } };
}
test("reviewer pins the visible revision, requires attestation, and stops after an uncertain decision", async () => {
  const state = setup();
  state.ids.get("review-token").value = "private-reviewer-token";
  state.ids.get("review-login").events.submit({ preventDefault() {} });
  await flush();
  assert.equal(state.ids.get("review-token").value, "");
  assert.equal(state.calls[0].options.credentials, "omit");
  assert.equal(state.calls[0].options.cache, "no-store");
  const elements = all(state.ids.get("review-cases"));
  assert.ok(elements.some(el => el.tagName === "dd" && el.textContent === "5,000 MMK"));
  assert.ok(elements.some(el => el.tagName === "label" && el.textContent === "Actual amount received (MMK)"));
  const form = elements.find(el => el.tagName === "form"), fields = elements.find(el => el.tagName === "fieldset");
  elements.find(el => el.maxLength === 128).value = "bank-reference";
  elements.find(el => el.type === "number").value = "5000";
  elements.find(el => el.placeholder?.startsWith("2026-")).value = "2026-11-13T11:30:00+06:30";
  elements.find(el => el.tagName === "textarea").value = "merchant records matched";
  form.events.submit({ preventDefault() {} }); await flush();
  assert.equal(state.calls.length, 1);
  elements.find(el => el.type === "checkbox").checked = true;
  state.fail();
  form.events.submit({ preventDefault() {} }); await flush();
  assert.equal(state.calls.length, 2);
  assert.match(state.calls[1].path, /\/admin\/payment-review\/orders\/order-1\/revisions\/2\/decision$/);
  const body = JSON.parse(state.calls[1].options.body);
  assert.equal(body.verified_transfer.transaction_id, "bank-reference");
  assert.equal(body.verified_transfer.receiver_id, "receiver-1");
  assert.equal(fields.disabled, true);
  state.ids.get("review-disconnect").events.click();
  assert.equal(state.ids.get("review-cases").children.length, 0);
});
test("review displays decimal currencies while keeping the integer minor-unit entry contract", async () => {
  const state = setup("USD", 5000);
  state.ids.get("review-token").value = "private-reviewer-token";
  state.ids.get("review-login").events.submit({ preventDefault() {} });
  await flush();
  const elements = all(state.ids.get("review-cases"));
  assert.ok(elements.some(el => el.tagName === "dd" && el.textContent === "50.00 USD"));
  const amount = elements.find(el => el.type === "number");
  assert.equal(amount.placeholder, "5000");
  assert.ok(elements.some(el => el.tagName === "label" && el.textContent === "Actual amount received (USD minor units)"));
});
test("disconnect discards an in-flight queue response", async () => {
  const state = setup();
  state.ids.get("review-token").value = "private-reviewer-token";
  state.ids.get("review-login").events.submit({ preventDefault() {} });
  state.events.pagehide(); await flush();
  assert.equal(state.ids.get("review-cases").children.length, 0);
});

test("reviewer authorizes an exact full sender refund and records outbound completion", async () => {
  const state = setup();
  state.ids.get("review-token").value = "private-reviewer-token";
  state.ids.get("review-login").events.submit({ preventDefault() {} });
  await flush();
  let elements = all(state.ids.get("review-cases"));
  elements.find(el => el.maxLength === 128).value = "late-inbound-reference";
  elements.find(el => el.type === "number").value = "3200";
  elements.find(el => el.placeholder?.startsWith("2026-")).value = "2026-11-13T11:30:00+06:30";
  elements.find(el => el.tagName === "textarea").value = "late partial payment; return the full transfer";
  elements.filter(el => el.type === "checkbox")[1].checked = true;
  state.setQueue({ actor: "staff:alice", cases: [], refunds: [{ order_id: "order-1", revision: 2,
    incoming_amount_minor: 3200, currency: "MMK", incoming_rail: "kpay", incoming_transaction_id: "late-inbound-reference",
    incoming_occurred_at: "2026-11-13T11:30:00+06:30", refund_rail: "kpay", authorized_by: "staff:alice",
    authorized_at: "2026-11-13T11:40:00+06:30", reason: "late partial payment" }], limit: 200 });
  elements.find(el => el.textContent === "Authorize full sender refund").events.click();
  await flush();
  assert.ok(all(state.ids.get("review-cases")).some(el => el.tagName === "dd" && el.textContent === "3,200 MMK"));
  const authorizationCall = state.calls.find(call => call.path.endsWith("/decision"));
  const authorization = JSON.parse(authorizationCall.options.body);
  assert.equal(authorization.external_refund.amount_minor, 3200);
  assert.equal(authorization.external_refund.refund_destination_confirmed, true);

  elements = all(state.ids.get("review-cases"));
  const refundForm = elements.find(el => el.tagName === "form");
  elements.find(el => el.maxLength === 128).value = "outbound-refund-reference";
  elements.find(el => el.placeholder?.startsWith("2026-")).value = "2026-11-13T11:45:00+06:30";
  elements.find(el => el.tagName === "textarea").value = "outbound transfer verified";
  elements.find(el => el.type === "checkbox").checked = true;
  state.setQueue({ actor: "staff:alice", cases: [], refunds: [], limit: 200 });
  refundForm.events.submit({ preventDefault() {} });
  await flush();
  const completionCall = state.calls.find(call => call.path.endsWith("/refund-complete"));
  assert.ok(completionCall);
  assert.equal(JSON.parse(completionCall.options.body).refund_transaction_id, "outbound-refund-reference");
});

function setupStaffAccess(status, error = "") {
  const ids = new Map([
    "staff-workspace", "review-cases", "review-controls", "review-status", "review-refresh",
    "staff-role", "staff-dashboard", "staff-tab-history", "staff-tab-controls", "staff-tab-access",
    "staff-catalog-console", "staff-admin-list", "staff-history-result", "staff-recent-orders",
    "staff-sign-in", "staff-grant", "staff-account-id", "staff-grant-button", "staff-order-search",
    "staff-order-id", "staff-access-status",
  ].map(id => [id, new Element()]));
  ids.get("staff-role").textContent = "Checking access";
  ids.get("staff-sign-in").hidden = true;
  const events = {};
  vm.runInNewContext(code, {
    document: {
      querySelector: selector => ids.get(selector.slice(1)) || null,
      querySelectorAll: () => [],
      getElementById: id => ids.get(id) || null,
    },
    window: { addEventListener: (name, callback) => { events[name] = callback; } },
    AbortSignal,
    fetch: async () => ({ ok: status === 200, status, json: async () => ({ error }) }),
  });
  return { ids, events };
}

test("staff 401 replaces the pending badge with an explicit sign-in state", async () => {
  const state = setupStaffAccess(401);
  await flush();
  assert.equal(state.ids.get("staff-role").textContent, "Sign-in required");
  assert.equal(state.ids.get("review-status").textContent, "Sign in to your Store account to review payments.");
  assert.equal(state.ids.get("staff-sign-in").hidden, false);
  assert.equal(state.ids.get("staff-dashboard").hidden, true);
});

test("staff 403 shows access denied instead of requesting a retry", async () => {
  const state = setupStaffAccess(403, "This store account has no admin access.");
  await flush();
  assert.equal(state.ids.get("staff-role").textContent, "Access denied");
  assert.equal(state.ids.get("review-status").textContent, "This store account has no admin access.");
});

test("staff access errors do not leave the page stuck on checking access", async () => {
  const state = setupStaffAccess(503);
  await flush();
  assert.equal(state.ids.get("staff-role").textContent, "Access check failed");
  assert.equal(state.ids.get("review-status").textContent, "Could not verify access. Refresh this page to retry.");
});
