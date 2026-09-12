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
    const c = s.counts || {}, q = s.consistency || {}, f = s.fleet || {}, scale = s.scale_out || {}, usageSnapshot = s.usage_snapshot || {}, devicePolicy = s.device_policy || {};
    const snapshotHint = usageSnapshot.latest_observed_at
      ? `last ${usageSnapshot.latest_observed_at}${usageSnapshot.failed_endpoint_count ? ` · ${fmt(usageSnapshot.failed_endpoint_count)} node(s) degraded` : ""}`
      : "maintenance has not produced a usable snapshot";
    const protocols = (s.protocol_readiness || s.protocols || []).map((p) => {
      const active = p.status === "enabled";
      const detail = active
        ? `${Object.entries(p.capabilities || {}).filter(([, v]) => v).length} capabilities`
        : (p.activation_gate || p.status || "not ready");
      return `<div class="protocol-row"><span>${esc(p.protocol)} ${badge(p.status, active ? "good" : "warn")}</span><span>${esc(detail)}</span></div>`;
    }).join("") || empty("No protocol readiness data.");
    $("view-overview").innerHTML = `<div class="metrics">
      ${card("Active accounts", fmt(c.active_accounts), `${fmt(c.accounts)} total`)}
      ${card("Managed devices", fmt(c.active_devices), `${fmt(c.devices)} enrolled`)}
      ${card("Active credentials", fmt(c.active_generations), `${fmt(c.active_leases)} active quota leases`)}
      ${card("Fleet health", `${fmt(f.healthy)} / ${fmt(f.endpoints)}`, "active endpoints", f.healthy === f.endpoints ? "good" : "warn")}
      ${card("Scale posture", scale.recommended ? "review" : (scale.status || "unavailable"), scale.trigger || scale.mode || "recommendation-only", scale.recommended ? "warn" : "neutral")}
      ${card("Usage snapshots", usageSnapshot.status || "unavailable", snapshotHint, usageSnapshot.status === "healthy" ? "good" : "warn")}
      ${card("Device policy", devicePolicy.status || "unconfigured", devicePolicy.max_active_devices == null ? "no active-device ceiling" : `up to ${fmt(devicePolicy.max_active_devices)} per account`, devicePolicy.status === "bounded" ? "good" : "neutral")}
    </div>
    <div class="grid two"><article class="panel"><div class="panel-head"><div><p class="eyebrow">SYSTEM INTEGRITY</p><h2>Consistency checks</h2></div>${badge((q.failed_jobs || 0) + (q.failed_revocations || 0) ? "attention" : "clear", (q.failed_jobs || 0) + (q.failed_revocations || 0) ? "warn" : "good")}</div>
      <div class="check-grid">${Object.entries(q).map(([k, v]) => `<div><span>${esc(k.replaceAll("_", " "))}</span><strong>${esc(v)}</strong></div>`).join("") || empty("No checks available.")}</div>
    </article><article class="panel"><div class="panel-head"><div><p class="eyebrow">PROTOCOL REGISTRY</p><h2>Capability posture</h2></div>${badge("read-only", "neutral")}</div><div class="protocol-list">${protocols}</div><p class="muted">Adapters are visible here before any live management action is enabled.</p></article></div>`;
  }

  function renderFleet(data) {
    const rows = (data.endpoints || []).map((e) => {
      const protocols = (e.protocols || []).map((p) => badge(p.protocol, p.status === "enabled" ? "good" : "warn")).join(" ") || "—";
      return `<tr><td><button class="link-button endpoint-link" data-endpoint="${esc(e.id)}">${esc(e.code || e.id)}</button><small>${esc(e.id)}</small></td><td>${esc(e.provider)}</td><td>${esc(e.region)}</td><td>${badge(e.state, e.state === "ACTIVE" ? "good" : "warn")}</td><td>${protocols}</td><td>${fmt(e.active_assignments)}${e.max_active_keys ? ` / ${fmt(e.max_active_keys)}` : ""}</td><td>${esc(e.last_healthy_at || "—")}</td></tr>`;
    });
    $("view-fleet").innerHTML = `<div class="section-intro"><div><p class="eyebrow">INFRASTRUCTURE</p><h2>Fleet / nodes</h2><p>Safe endpoint metadata only. Management URLs, certificates, public addresses, and provider IDs stay server-side. Select a node for assignment, protocol, and credential lifecycle detail.</p></div><span class="read-only">READ ONLY</span></div>${table(["Endpoint", "Provider", "Region", "State", "Protocols", "Assignments", "Last healthy"], rows)}<div id="endpoint-detail" class="endpoint-detail"></div>`;
    document.querySelectorAll(".endpoint-link").forEach((button) => button.addEventListener("click", () => loadEndpointDetail(button.dataset.endpoint)));
  }

  function renderEndpointDetail(data) {
    const endpoint = data.endpoint || {};
    const capacity = (data.capacity_by_plan || []).map((p) => `<tr><td><strong>${esc(p.plan_code)}</strong></td><td>${badge(p.enabled ? "enabled" : "blocked", p.enabled ? "good" : "warn")}</td><td>${fmt(p.active_assignments)}${p.max_active_assignments != null ? ` / ${fmt(p.max_active_assignments)}` : " / unlimited"}</td></tr>`);
    const assignments = (data.assignments || []).map((a) => `<tr><td><strong>${esc(a.plan_code)}</strong><small>${esc(a.id)}</small></td><td>${esc(a.protocol || "outline")}</td><td>${esc(a.subscription_id || `free:${a.free_key_id}`)}</td><td>${badge(a.status, a.status === "active" ? "good" : "neutral")}</td><td>${fmt(a.reserved_quota_bytes)}</td><td>${esc(a.reason || "—")}</td></tr>`);
    const credentials = (data.credentials || []).map((c) => `<tr><td><strong>${esc(c.protocol)}</strong><small>${esc(c.generation_id)}</small></td><td>${esc(c.entitlement_key)}</td><td>${badge(c.status, c.status === "active" ? "good" : "neutral")}</td><td>${badge(c.remote_state, c.remote_state === "observed" ? "good" : "warn")}</td><td>${c.quota_bytes == null ? "—" : `${fmt(c.consumed_bytes)} / ${fmt(c.quota_bytes)}`}</td><td>${fmt(c.active_lease_bytes)} / ${fmt(c.lease_used_bytes)}</td><td>${esc(c.created_at)}</td></tr>`);
    const inventory = data.inventory_reconciliation || {};
    const inventoryAvailable = inventory.status === "healthy" || inventory.status === "degraded";
    const inventoryProtocols = (inventory.protocols || []).map((p) => `<tr><td><strong>${esc(p.protocol)}</strong></td><td>${badge(p.status, p.status === "healthy" ? "good" : "warn")}</td><td>${fmt(p.present_keys)}</td><td>${fmt(p.managed_present)}</td><td>${fmt(p.unmanaged_present)}</td><td>${fmt(p.historical_keys)}</td><td>${esc(p.latest_observed_at || "—")}</td></tr>`);
    const protocols = (endpoint.protocols || []).map((p) => `<tr><td><strong>${esc(p.protocol)}</strong><small>${esc(p.adapter_type)}</small></td><td>${badge(p.status, p.status === "enabled" ? "good" : "warn")}</td><td>${fmt(Object.values(p.capabilities || {}).filter(Boolean).length)} / ${fmt(Object.keys(p.capabilities || {}).length)}</td><td>${esc(p.verified_at || "—")}</td><td>${esc(p.last_healthy_at || "—")}</td></tr>`);
    const readiness = (data.protocol_readiness || []).map((p) => `<tr><td><strong>${esc(p.protocol)}</strong><small>${esc(p.profile_status || "—")}</small></td><td>${badge(p.promotable ? "ready" : "blocked", p.promotable ? "good" : "warn")}</td><td>${esc((p.missing_signals || []).join(", ") || "—")}</td><td>${esc((p.missing_evidence || []).join(", ") || "—")}</td><td>${esc((p.missing_capabilities || []).join(", ") || "—")}</td><td>${esc(p.latest_healthy_at || "—")}</td></tr>`);
    const observations = (data.protocol_observations || []).map((o) => {
      const details = Object.entries(o.details || {}).map(([key, value]) => `${key}=${value}`).join(", ");
      return `<tr><td><strong>${esc(o.protocol)}</strong><small>${esc(o.signal)}</small></td><td>${badge(o.status, o.status === "healthy" ? "good" : "warn")}</td><td>${esc(details || "—")}</td><td>${esc(o.source)}</td><td>${esc(o.observed_at)}</td></tr>`;
    });
    $("endpoint-detail").innerHTML = `<article class="panel"><div class="panel-head"><div><p class="eyebrow">NODE DETAIL</p><h2>${esc(endpoint.code || endpoint.id)}</h2><p class="muted">${esc(endpoint.provider)} · ${esc(endpoint.region)} · ${esc(endpoint.state)}</p></div><span class="read-only">NO SECRETS</span></div><div class="metrics mini">${card("Health", endpoint.healthy ? "healthy" : "stale", endpoint.last_healthy_at ? `last ${endpoint.last_healthy_at}` : "no fresh observation", endpoint.healthy ? "good" : "warn")}${card("Active assignments", fmt(endpoint.active_assignments), endpoint.max_active_keys ? `of ${fmt(endpoint.max_active_keys)}` : "no hard cap")}${card("Credentials", fmt(credentials.length), "generation records")}${card("Allocation", endpoint.accepts_new_assignments ? "open" : "paused", "new assignments", endpoint.accepts_new_assignments ? "good" : "warn")}${card("Remote keys", inventoryAvailable ? fmt(inventory.present_keys) : "unavailable", inventoryAvailable ? `${fmt(inventory.managed_present)} managed · ${fmt(inventory.unmanaged_present)} unmanaged` : "maintenance snapshot required", inventoryAvailable ? (inventory.unmanaged_present ? "warn" : "good") : "warn")}</div><div class="detail-block"><h3>Remote inventory by protocol</h3>${table(["Protocol", "Status", "Present", "Managed", "Unmanaged", "Historical", "Latest observed"], inventoryProtocols)}</div><div class="detail-block"><h3>Capacity by plan</h3>${table(["Plan", "Policy", "Active assignments"], capacity)}</div><div class="grid two"><div><h3>Assignments</h3>${table(["Plan", "Protocol", "Entitlement", "State", "Reserved bytes", "Reason"], assignments)}</div><div><h3>Credential generations</h3>${table(["Protocol", "Entitlement", "State", "Remote", "Created"], credentials)}</div></div><div class="detail-block"><h3>Protocol profiles</h3>${table(["Protocol / adapter", "State", "Capabilities", "Verified", "Last healthy"], protocols)}</div><div class="detail-block"><h3>Promotion readiness</h3>${table(["Protocol / profile", "Gate", "Missing signals", "Missing evidence", "Missing capabilities", "Latest healthy"], readiness)}</div><div class="detail-block"><h3>Recent protocol evidence</h3>${table(["Protocol / signal", "Status", "Details", "Source", "Observed"], observations)}</div><p class="muted">Remote inventory is count-only in this console; unknown keys are preserved for review and are never auto-deleted.</p></article>`;
  }

  async function loadEndpointDetail(endpointId) {
    if (!endpointId) return;
    try {
      const data = await api(`/api/admin/fleet/${encodeURIComponent(endpointId)}`);
      renderEndpointDetail(data);
    } catch (error) {
      $("notice").textContent = error.message;
      $("notice").classList.remove("hidden");
    }
  }

  function renderAccounts(data) {
    const rows = (data.accounts || []).map((a) => `<tr><td><button class="link-button account-link" data-account="${esc(a.account_id)}">${esc(a.account_id)}</button><small>Telegram ${esc(a.telegram_id)}</small></td><td>${badge(a.status, a.status === "active" ? "good" : "warn")}</td><td>${fmt(a.active_device_count)} / ${fmt(a.device_count)}</td><td>${fmt(a.active_subscription_count)}</td><td>${esc(a.updated_at)}</td></tr>`);
    $("view-accounts").innerHTML = `<div class="section-intro"><div><p class="eyebrow">CUSTOMER CONTROL</p><h2>Accounts</h2><p>Opaque account ownership and entitlement posture, without access URLs or device public keys. Select an account for safe route and device detail.</p></div><input id="account-search" class="search" placeholder="Search account or Telegram ID" aria-label="Search accounts"></div>${table(["Account", "Status", "Devices", "Subscriptions", "Updated"], rows)}<div id="account-detail" class="endpoint-detail"></div>`;
    $("account-search").addEventListener("change", () => load("accounts", `?q=${encodeURIComponent($("account-search").value)}`));
    document.querySelectorAll(".account-link").forEach((button) => button.addEventListener("click", () => loadAccountDetail(button.dataset.account)));
  }

  function renderAccountDetail(data) {
    const account = data.account || {};
    const devices = (account.devices || []).map((d) => `<tr><td><strong>${esc(d.label || "Unnamed device")}</strong><small>${esc(d.device_id)}</small></td><td>${badge(d.status, d.status === "active" ? "good" : "warn")}</td><td>${esc(d.last_seen_at || "Never")}</td><td>${esc(d.revoked_at || "—")}</td></tr>`);
    const subscriptions = (account.subscriptions || []).map((s) => `<tr><td><strong>${esc(s.plan_name || s.plan_code)}</strong><small>${esc(s.subscription_id)}</small></td><td>${badge(s.status, s.status === "active" ? "good" : "neutral")}</td><td>${esc(s.endpoint_id || "Unassigned")}</td><td>${badge(s.key_status || "—", s.key_status === "active" ? "good" : "neutral")}</td><td>${esc(s.expires_at || "—")}</td></tr>`);
    const routes = (account.routes || []).map((r) => `<tr><td><strong>${esc(r.protocol)}</strong><small>${esc(r.route_id)}</small></td><td>${esc(r.endpoint_id)}</td><td>${esc(r.region)}</td><td>${esc(r.generation)}</td></tr>`);
    $("account-detail").innerHTML = `<article class="panel"><div class="panel-head"><div><p class="eyebrow">ACCOUNT DETAIL</p><h2>${esc(account.account_id)}</h2><p class="muted">Telegram ${esc(account.telegram_id)} · ${esc(account.status)}</p></div><span class="read-only">NO SECRETS</span></div><div class="metrics mini">${card("Subscriptions", fmt((account.subscriptions || []).length), "durable entitlements")}${card("Devices", fmt((account.devices || []).length), "enrolled records")}${card("Routes", fmt((account.routes || []).length), "active observed paths")}${card("Revocation epoch", fmt(account.revocation_epoch), "account-wide control")}</div><div class="detail-block"><h3>Subscriptions / entitlements</h3>${table(["Plan", "State", "Endpoint", "Credential", "Expires"], subscriptions)}</div><div class="grid two"><div><h3>Devices</h3>${table(["Device", "State", "Last seen", "Revoked"], devices)}</div><div><h3>Active routes</h3>${table(["Protocol / route", "Endpoint", "Region", "Generation"], routes)}</div></div></article>`;
  }

  async function loadAccountDetail(accountId) {
    if (!accountId) return;
    try {
      const data = await api(`/api/admin/accounts/${encodeURIComponent(accountId)}`);
      renderAccountDetail(data);
    } catch (error) {
      $("notice").textContent = error.message;
      $("notice").classList.remove("hidden");
    }
  }

  function renderCredentials(data) {
    const rows = (data.credentials || []).map((c) => `<tr><td><strong>${esc(c.protocol)}</strong><small>${esc(c.generation_id)}</small></td><td>${esc(c.endpoint_id)}</td><td>${badge(c.status, c.status === "active" ? "good" : "neutral")}</td><td>${badge(c.remote_state, c.remote_state === "observed" ? "good" : "warn")}</td><td>${c.quota_bytes == null ? "—" : `${fmt(c.consumed_bytes)} / ${fmt(c.quota_bytes)}`}</td><td>${fmt(c.active_lease_bytes)} / ${fmt(c.lease_used_bytes)}</td><td>${esc(c.entitlement_key)}</td><td>${esc(c.created_at)}</td></tr>`);
    $("view-credentials").innerHTML = `<div class="section-intro"><div><p class="eyebrow">ACCESS LIFECYCLE</p><h2>Credential generations</h2><p>Lifecycle and accounting state only. Credential material is never returned to this console.</p></div><span class="read-only">NO SECRETS</span></div>${table(["Protocol / generation", "Endpoint", "State", "Remote", "Usage", "Lease", "Entitlement", "Created"], rows)}`;
  }

  function renderDevices(data) {
    const rows = (data.devices || []).map((d) => `<tr><td><strong>${esc(d.label || "Unnamed device")}</strong><small>${esc(d.device_id)}</small></td><td>${esc(d.account_id)}</td><td>${badge(d.status, d.status === "active" ? "good" : "warn")}</td><td>${esc(d.account_status)}</td><td>${esc(d.last_seen_at || "Never")}</td></tr>`);
    $("view-devices").innerHTML = `<div class="section-intro"><div><p class="eyebrow">MANAGED DEVICES</p><h2>Device fleet</h2><p>Pairing, revocation, and heartbeat posture. Public keys remain outside the UI payload.</p></div><span class="read-only">READ ONLY</span></div>${table(["Device", "Account", "Status", "Account state", "Last seen"], rows)}`;
  }

  function renderOperations(data) {
    const jobs = (data.jobs || []).map((j) => `<tr><td><strong>${esc(j.job_id)}</strong><small>${esc(j.operation)}</small></td><td>${esc(j.plan_code)}</td><td>${badge(j.job_status, j.job_status === "failed" ? "warn" : "neutral")}</td><td>${fmt(j.attempts)}</td><td>${esc(j.last_error || "—")}</td></tr>`);
    const infrastructureJobs = (data.infrastructure_jobs || []).map((j) => `<tr><td><strong>${esc(j.job_id)}</strong><small>${esc(j.operation)}</small></td><td>${esc(j.endpoint_id || "unassigned")}</td><td>${badge(j.status, j.status === "failed" ? "warn" : j.status === "completed" ? "good" : "neutral")}</td><td>${fmt(j.attempts)}</td><td>${esc(j.error_type || "—")}</td><td>${esc(j.created_at || "—")}</td></tr>`);
    const orders = (data.pending_orders || []).map((o) => `<tr><td><strong>${esc(o.id)}</strong></td><td>${esc(o.plan_code)}</td><td>${esc(o.requested_protocol || "outline")}</td><td>${esc(o.requested_endpoint_id || "automatic")}</td><td>${badge(o.status, "neutral")}</td><td>${esc(o.stage || "—")}</td></tr>`);
    const decisions = (data.decisions || []).map((d) => {
      const policy = d.policy_created_at
        ? `v${fmt(d.policy_version || 1)} · fail ${fmt(d.policy_failure_threshold)} / recover ${fmt(d.policy_recovery_threshold)} · cool ${fmt(d.policy_cooldown_seconds)}s`
        : `v${fmt(d.policy_version || 1)} · snapshot unavailable`;
      return `<tr><td><button class="link-button decision-link" data-decision="${esc(d.decision_id)}">${esc(d.decision_id)}</button><small>${esc(d.trigger || "—")}</small></td><td>${esc(d.source_endpoint_id)} → ${esc(d.target_endpoint_id)}</td><td>${badge(d.state, d.state === "committed" ? "good" : d.state === "failed" ? "warn" : "neutral")}</td><td>${esc(policy)}</td><td>${fmt(d.attempts)}</td><td>${esc(d.error_type || "—")}</td></tr>`;
    });
    const safetyControls = (data.safety_controls || []).map((c) => `<tr><td><strong>${esc(c.scope)}</strong><small>${esc(c.scope_key)}</small></td><td>${badge(c.paused ? "paused" : "armed", c.paused ? "warn" : "good")}</td><td>${fmt(c.max_migrations_per_window)} / ${fmt(c.window_seconds)}s</td><td>${fmt(c.migration_count)}</td><td>${fmt(c.remaining_migrations)}</td><td>${esc(c.window_start || "—")}</td></tr>`);
    const events = (data.events || []).map((e) => `<tr><td><strong>${esc(e.action)}</strong><small>${esc(e.created_at)}</small></td><td>${esc(e.actor_type)}:${esc(e.actor_id)}</td><td>${esc(e.target_type)}:${esc(e.target_id)}</td></tr>`);
    $("view-operations").innerHTML = `<div class="section-intro"><div><p class="eyebrow">DURABLE WORK</p><h2>Operations</h2><p>Queue, infrastructure intents, failover, audit, and safety-control visibility. Actions remain in the existing Telegram-admin safety boundary.</p></div><span class="read-only">NO MUTATIONS</span></div><div class="grid two"><article class="panel"><div class="panel-head"><h2>Customer provisioning jobs</h2></div>${table(["Job", "Plan", "State", "Attempts", "Last error"], jobs)}</article><article class="panel"><div class="panel-head"><h2>Infrastructure intents</h2></div>${table(["Intent", "Endpoint", "State", "Attempts", "Error type", "Created"], infrastructureJobs)}</article><article class="panel"><div class="panel-head"><h2>Pending orders</h2></div>${table(["Order", "Plan", "Protocol", "Endpoint", "State", "Stage"], orders)}</article><article class="panel"><div class="panel-head"><h2>Failover decisions</h2></div>${table(["Decision", "Route", "State", "Policy", "Attempts", "Error type"], decisions)}</article><article class="panel"><div class="panel-head"><h2>Failover safety controls</h2></div>${table(["Scope", "State", "Budget / window", "Used", "Remaining", "Window start"], safetyControls)}</article><article class="panel"><div class="panel-head"><h2>Recent audit events</h2></div>${table(["Action", "Actor", "Target"], events)}</article></div><div id="failover-detail"></div>`;
    document.querySelectorAll(".decision-link").forEach((button) => button.addEventListener("click", () => loadDecisionDetail(button.dataset.decision)));
  }

  function renderDecisionDetail(data) {
    const decision = data.decision || {};
    const snapshot = decision.policy_snapshot_available
      ? `v${fmt(decision.policy_version)} · fail ${fmt(decision.policy_failure_threshold)} / recover ${fmt(decision.policy_recovery_threshold)} · cool ${fmt(decision.policy_cooldown_seconds)}s · max attempts ${fmt(decision.policy_max_attempts)}`
      : `v${fmt(decision.policy_version)} · historical snapshot unavailable`;
    $("failover-detail").innerHTML = `<article class="panel detail-block"><div class="panel-head"><div><p class="eyebrow">DECISION EXPLANATION</p><h2>${esc(decision.decision_id)}</h2><p class="muted">${esc(decision.source_endpoint_id)} → ${esc(decision.target_endpoint_id)} · ${esc(decision.state)}</p></div><span class="read-only">READ ONLY</span></div><div class="check-grid"><div><span>Trigger</span><strong>${esc(decision.trigger || "—")}</strong></div><div><span>Network bucket</span><strong>${esc(decision.network_bucket || "—")}</strong></div><div><span>Attempts</span><strong>${fmt(decision.attempts)}</strong></div><div><span>Policy snapshot</span><strong>${esc(snapshot)}</strong></div></div></article>`;
  }

  async function loadDecisionDetail(decisionId) {
    if (!decisionId) return;
    try {
      renderDecisionDetail(await api(`/api/admin/failover/${encodeURIComponent(decisionId)}`));
    } catch (error) {
      $("notice").textContent = error.message;
      $("notice").classList.remove("hidden");
    }
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
