(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  const initData = tg ? tg.initData : "";
  const state = { view: "overview", summary: null, loaded: {} };
  const titles = { overview: "Overview", fleet: "Fleet", accounts: "Customers", credentials: "Access", devices: "Devices", operations: "Operations" };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? "—" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmt = (value) => new Intl.NumberFormat().format(Number(value || 0));
  const badge = (value, tone) => `<span class="badge ${tone || String(value || "").toLowerCase()}">${esc(value || "unknown")}</span>`;
  const card = (label, value, hint, tone) => `<article class="metric"><p>${esc(label)}</p><strong class="${tone || ""}">${esc(value)}</strong><small>${esc(hint || "")}</small></article>`;
  const empty = (text) => `<div class="empty">${esc(text)}</div>`;

  async function api(path) {
    const response = await fetch(path, { headers: { "X-Telegram-Init-Data": initData }, credentials: "same-origin" });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function table(headers, rows) {
    if (!rows.length) return empty("No records match the current view.");
    return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function renderOverview() {
    const s = state.summary || {};
    const c = s.counts || {}, q = s.consistency || {}, f = s.fleet || {};
    const protocols = (s.protocols || []).map((p) => `<div class="protocol-row"><span>${esc(p.protocol)}</span><span>${Object.entries(p.capabilities || {}).filter(([, v]) => v).length} capabilities</span></div>`).join("") || empty("No protocol adapters registered.");
    $("view-overview").innerHTML = `<div class="metrics">
      ${card("Active accounts", fmt(c.active_accounts), `${fmt(c.accounts)} total`)}
      ${card("Managed devices", fmt(c.active_devices), `${fmt(c.devices)} enrolled`)}
      ${card("Active credentials", fmt(c.active_generations), `${fmt(c.active_leases)} active quota leases`)}
      ${card("Fleet health", `${fmt(f.healthy)} / ${fmt(f.endpoints)}`, "active endpoints", f.healthy === f.endpoints ? "good" : "warn")}
    </div>
    <div class="grid two"><article class="panel"><div class="panel-head"><div><p class="eyebrow">SYSTEM INTEGRITY</p><h2>Consistency checks</h2></div>${badge((q.failed_jobs || 0) + (q.failed_revocations || 0) ? "attention" : "clear", (q.failed_jobs || 0) + (q.failed_revocations || 0) ? "warn" : "good")}</div>
      <div class="check-grid">${Object.entries(q).map(([k, v]) => `<div><span>${esc(k.replaceAll("_", " "))}</span><strong>${esc(v)}</strong></div>`).join("") || empty("No checks available.")}</div>
    </article><article class="panel"><div class="panel-head"><div><p class="eyebrow">PROTOCOL REGISTRY</p><h2>Capability posture</h2></div>${badge("read-only", "neutral")}</div><div class="protocol-list">${protocols}</div><p class="muted">Adapters are visible here before any live management action is enabled.</p></article></div>`;
  }

  function renderFleet(data) {
    const rows = (data.endpoints || []).map((e) => `<tr><td><strong>${esc(e.code || e.id)}</strong><small>${esc(e.id)}</small></td><td>${esc(e.provider)}</td><td>${esc(e.region)}</td><td>${badge(e.state, e.state === "ACTIVE" ? "good" : "warn")}</td><td>${fmt(e.active_assignments)}${e.max_active_keys ? ` / ${fmt(e.max_active_keys)}` : ""}</td><td>${esc(e.last_healthy_at || "—")}</td></tr>`);
    $("view-fleet").innerHTML = `<div class="section-intro"><div><p class="eyebrow">INFRASTRUCTURE</p><h2>Fleet / nodes</h2><p>Safe endpoint metadata only. Management URLs, certificates, public addresses, and provider IDs stay server-side.</p></div><span class="read-only">READ ONLY</span></div>${table(["Endpoint", "Provider", "Region", "State", "Assignments", "Last healthy"], rows)}`;
  }

  function renderAccounts(data) {
    const rows = (data.accounts || []).map((a) => `<tr><td><strong>${esc(a.account_id)}</strong><small>Telegram ${esc(a.telegram_id)}</small></td><td>${badge(a.status, a.status === "active" ? "good" : "warn")}</td><td>${fmt(a.active_device_count)} / ${fmt(a.device_count)}</td><td>${fmt(a.active_subscription_count)}</td><td>${esc(a.updated_at)}</td></tr>`);
    $("view-accounts").innerHTML = `<div class="section-intro"><div><p class="eyebrow">CUSTOMER CONTROL</p><h2>Accounts</h2><p>Opaque account ownership and entitlement posture, without access URLs or device public keys.</p></div><input id="account-search" class="search" placeholder="Search account or Telegram ID" aria-label="Search accounts"></div>${table(["Account", "Status", "Devices", "Subscriptions", "Updated"], rows)}`;
    $("account-search").addEventListener("change", () => load("accounts", `?q=${encodeURIComponent($("account-search").value)}`));
  }

  function renderCredentials(data) {
    const rows = (data.credentials || []).map((c) => `<tr><td><strong>${esc(c.protocol)}</strong><small>${esc(c.generation_id)}</small></td><td>${esc(c.endpoint_id)}</td><td>${badge(c.status, c.status === "active" ? "good" : "neutral")}</td><td>${badge(c.remote_state, c.remote_state === "observed" ? "good" : "warn")}</td><td>${esc(c.entitlement_key)}</td><td>${esc(c.created_at)}</td></tr>`);
    $("view-credentials").innerHTML = `<div class="section-intro"><div><p class="eyebrow">ACCESS LIFECYCLE</p><h2>Credential generations</h2><p>Lifecycle and accounting state only. Credential material is never returned to this console.</p></div><span class="read-only">NO SECRETS</span></div>${table(["Protocol / generation", "Endpoint", "State", "Remote", "Entitlement", "Created"], rows)}`;
  }

  function renderDevices(data) {
    const rows = (data.devices || []).map((d) => `<tr><td><strong>${esc(d.label || "Unnamed device")}</strong><small>${esc(d.device_id)}</small></td><td>${esc(d.account_id)}</td><td>${badge(d.status, d.status === "active" ? "good" : "warn")}</td><td>${esc(d.account_status)}</td><td>${esc(d.last_seen_at || "Never")}</td></tr>`);
    $("view-devices").innerHTML = `<div class="section-intro"><div><p class="eyebrow">MANAGED DEVICES</p><h2>Device fleet</h2><p>Pairing, revocation, and heartbeat posture. Public keys remain outside the UI payload.</p></div><span class="read-only">READ ONLY</span></div>${table(["Device", "Account", "Status", "Account state", "Last seen"], rows)}`;
  }

  function renderOperations(data) {
    const jobs = (data.jobs || []).map((j) => `<tr><td><strong>${esc(j.job_id)}</strong><small>${esc(j.operation)}</small></td><td>${esc(j.plan_code)}</td><td>${badge(j.job_status, j.job_status === "failed" ? "warn" : "neutral")}</td><td>${fmt(j.attempts)}</td><td>${esc(j.last_error || "—")}</td></tr>`);
    const orders = (data.pending_orders || []).map((o) => `<tr><td><strong>${esc(o.id)}</strong></td><td>${esc(o.plan_code)}</td><td>${badge(o.status, "neutral")}</td><td>${esc(o.stage || "—")}</td></tr>`);
    const decisions = (data.decisions || []).map((d) => `<tr><td><strong>${esc(d.decision_id)}</strong><small>${esc(d.trigger || "—")}</small></td><td>${esc(d.source_endpoint_id)} → ${esc(d.target_endpoint_id)}</td><td>${badge(d.state, d.state === "committed" ? "good" : d.state === "failed" ? "warn" : "neutral")}</td><td>${fmt(d.attempts)}</td><td>${esc(d.last_error || "—")}</td></tr>`);
    const events = (data.events || []).map((e) => `<tr><td><strong>${esc(e.action)}</strong><small>${esc(e.created_at)}</small></td><td>${esc(e.actor_type)}:${esc(e.actor_id)}</td><td>${esc(e.target_type)}:${esc(e.target_id)}</td></tr>`);
    $("view-operations").innerHTML = `<div class="section-intro"><div><p class="eyebrow">DURABLE WORK</p><h2>Operations</h2><p>Queue, failover, audit, and invariant visibility. Actions remain in the existing Telegram-admin safety boundary.</p></div><span class="read-only">NO MUTATIONS</span></div><div class="grid two"><article class="panel"><div class="panel-head"><h2>Provisioning jobs</h2></div>${table(["Job", "Plan", "State", "Attempts", "Last error"], jobs)}</article><article class="panel"><div class="panel-head"><h2>Pending orders</h2></div>${table(["Order", "Plan", "State", "Stage"], orders)}</article><article class="panel"><div class="panel-head"><h2>Failover decisions</h2></div>${table(["Decision", "Route", "State", "Attempts", "Last error"], decisions)}</article><article class="panel"><div class="panel-head"><h2>Recent audit events</h2></div>${table(["Action", "Actor", "Target"], events)}</article></div>`;
  }

  async function load(view, suffix) {
    const endpoints = { overview: "/api/admin/summary", fleet: "/api/admin/fleet", accounts: "/api/admin/accounts", credentials: "/api/admin/credentials", devices: "/api/admin/devices", operations: "/api/admin/operations" };
    try {
      let data;
      if (view === "operations") {
        const [operations, failover, audit] = await Promise.all([
          api(endpoints[view] + (suffix || "")),
          api("/api/admin/failover"),
          api("/api/admin/audit"),
        ]);
        data = { ...operations, decisions: failover.decisions || [], events: audit.events || [] };
      } else {
        data = await api(endpoints[view] + (suffix || ""));
      }
      state.loaded[view] = data;
      if (view === "overview") { state.summary = data; renderOverview(); }
      if (view === "fleet") renderFleet(data);
      if (view === "accounts") renderAccounts(data);
      if (view === "credentials") renderCredentials(data);
      if (view === "devices") renderDevices(data);
      if (view === "operations") renderOperations(data);
      $("refresh-time").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      $("notice").classList.add("hidden");
    } catch (error) {
      $("notice").textContent = error.message;
      $("notice").classList.remove("hidden");
    }
  }

  function select(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
    $("page-title").textContent = titles[view];
    load(view);
  }

  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => select(item.dataset.view)));
  $("refresh").addEventListener("click", () => load(state.view));
  $("session-id").textContent = initData ? "verified" : "Telegram context missing";
  load("overview");
}());
