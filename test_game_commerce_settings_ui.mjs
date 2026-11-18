import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

test("store controls save current revisions and retain drafts after a conflict", async () => {
  const flags = ["checkout_enabled", "account_checks_enabled", "payments_enabled", "supplier_orders_enabled"];
  const fields = Object.fromEntries([...flags, "daily_limit_usd", "maximum_order_mmk"].map((key) => [key, { value: "", checked: false }]));
  const submit = { disabled: true };
  const status = { textContent: "" };
  const callbacks = {};
  const form = { elements: fields, querySelector: () => submit,
    addEventListener: (event, callback) => callbacks[event] = callback };
  const reload = { addEventListener: (_, callback) => callbacks.reload = callback };
  const elements = { "#commerce-settings-form": form, "#commerce-settings-status": status,
    "#commerce-settings-reload": reload, "#staff-workspace": {} };
  const requests = [];
  let conflict = false;
  const settings = { ...Object.fromEntries(flags.map((key) => [key, true])),
    daily_limit_usd: null, maximum_order_mmk: null };
  const fetch = async (url, options) => {
    requests.push({ url, options });
    const body = options.body ? JSON.parse(options.body) : null;
    return { ok: !conflict, json: async () => conflict ? { error: "Reload settings." }
      : { revision: body ? 3 : 2, settings: body?.settings || settings } };
  };
  vm.runInNewContext(await readFile("web/game-store/commerce-settings.js", "utf8"),
    { document: { querySelector: (selector) => elements[selector] }, fetch });
  await new Promise(setImmediate);
  assert.ok(flags.every((key) => fields[key].checked));
  fields.daily_limit_usd.value = "20";
  fields.maximum_order_mmk.value = "50000";
  await callbacks.submit({ preventDefault() {} });
  const saved = JSON.parse(requests.at(-1).options.body);
  assert.equal(saved.expected_revision, 2);
  assert.equal(saved.settings.daily_limit_usd, "20");
  assert.equal(saved.settings.maximum_order_mmk, 50000);
  assert.equal(requests.at(-1).options.credentials, "same-origin");
  conflict = true;
  fields.maximum_order_mmk.value = "10000";
  await callbacks.submit({ preventDefault() {} });
  assert.equal(fields.maximum_order_mmk.value, "10000");
  assert.equal(status.textContent, "Reload settings.");
  assert.equal(submit.disabled, false);
});
