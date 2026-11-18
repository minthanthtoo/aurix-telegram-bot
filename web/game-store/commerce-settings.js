"use strict";
(() => {
  const form = document.querySelector("#commerce-settings-form");
  if (!form) return;
  const panel = document.querySelector("#commerce-settings-panel");
  const status = document.querySelector("#commerce-settings-status");
  const exposureWarning = document.querySelector("#commerce-exposure-warning");
  const submit = form.querySelector("button[type=submit]");
  const flags = ["checkout_enabled", "account_checks_enabled", "payments_enabled", "supplier_orders_enabled"];
  let revision = null;
  const root = document.querySelector("#staff-workspace")
    ? "/api/v1/store-staff/commerce-settings" : "/api/v1/admin/commerce-settings";
  async function request(body) {
    const response = await fetch(root, { method: body ? "POST" : "GET", credentials: "same-origin",
      cache: "no-store", headers: { "Content-Type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}) });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || "Settings could not be saved.");
      error.status = response.status;
      throw error;
    }
    return result;
  }
  function render(result) {
    revision = result.revision;
    if (panel) panel.hidden = false;
    form.hidden = false;
    for (const key of flags) form.elements[key].checked = result.settings[key];
    for (const key of ["daily_limit_usd", "maximum_order_mmk"]) {
      form.elements[key].value = result.settings[key] ?? "";
    }
    const risks = [];
    if (result.settings.supplier_orders_enabled && result.settings.daily_limit_usd == null) {
      risks.push("paid supplier orders have no daily USD spend cap");
    }
    if (result.settings.checkout_enabled && result.settings.maximum_order_mmk == null) {
      risks.push("customer orders have no per-order MMK cap");
    }
    if (exposureWarning) {
      exposureWarning.hidden = risks.length === 0;
      exposureWarning.textContent = risks.length ? `Uncapped exposure: ${risks.join("; ")}. Set limits below to cap new activity.` : "";
    }
    submit.disabled = false;
    status.textContent = "Settings loaded.";
  }
  async function load() {
    try { render(await request()); }
    catch (error) {
      status.textContent = error.message;
      submit.disabled = true;
      if (panel) panel.hidden = error.status === 401 || error.status === 403;
    }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (revision === null) return;
    const settings = Object.fromEntries(flags.map((key) => [key, form.elements[key].checked]));
    settings.daily_limit_usd = form.elements.daily_limit_usd.value.trim() || null;
    settings.maximum_order_mmk = form.elements.maximum_order_mmk.value
      ? Number(form.elements.maximum_order_mmk.value) : null;
    submit.disabled = true;
    try {
      render(await request({ expected_revision: revision, settings }));
      status.textContent = "Saved. Controls apply to new orders immediately.";
    } catch (error) { status.textContent = error.message; }
    finally { submit.disabled = revision === null; }
  });
  document.querySelector("#commerce-settings-reload").addEventListener("click", load);
  void load();
})();
