(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor("#07111f");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#07111f");
  }

  const state = {
    catalog: null,
    dashboard: null,
    servers: [],
    initData: tg && tg.initData ? tg.initData : "",
    selectedEndpointId: null,
    selectedKeyId: null,
  };
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const formatBytes = (bytes) => {
    let amount = Math.max(0, Number(bytes || 0));
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
    return `${unit ? amount.toFixed(2) : Math.round(amount)} ${units[unit]}`;
  };
  const formatDate = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value).slice(0, 16) : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const titleCase = (value) => String(value || "unknown").replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
  const api = async (path, options) => {
    const request = options || {};
    const headers = Object.assign({ Accept: "application/json" }, request.headers || {});
    if (state.initData) headers["X-Telegram-Init-Data"] = state.initData;
    if (request.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const response = await fetch(path, Object.assign({}, request, { headers }));
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  };
  const setConnection = (kind, label) => {
    const el = $("#connection-state");
    el.className = `connection-state ${kind ? `is-${kind}` : ""}`;
    el.innerHTML = `<i></i>${escapeHtml(label)}`;
  };
  const showNotice = (message) => { $("#auth-notice").innerHTML = message || ""; };
  const initials = (user) => {
    const value = `${user && user.first_name || "A"}${user && user.last_name || ""}`.trim();
    return value.slice(0, 2).toUpperCase() || "A";
  };
  const serverFor = (endpointId) => state.servers.find((item) => String(item.id) === String(endpointId)) || (state.dashboard && (state.dashboard.servers || []).find((item) => String(item.id) === String(endpointId)));
  const serverLabel = (server) => server ? (server.code || server.region || "AuriX server") : "Automatic capacity selection";
  const serverStatus = (server) => {
    if (!server) return "Not selected";
    if (server.eligible) return "Available";
    if (server.healthy) return "Limited";
    return "Offline check";
  };
  const activeKeys = () => (state.dashboard && state.dashboard.keys || []).filter((key) => key.status === "active");
  const keyIdentity = (key) => `${key.endpoint_id || "legacy-default"}:${key.outline_key_id || ""}`;
  const selectedKey = () => {
    const keys = activeKeys();
    return keys.find((key) => keyIdentity(key) === state.selectedKeyId) || keys[0];
  };
  const go = (view) => {
    const target = view === "home" ? "home" : view;
    document.querySelectorAll(".nav-item, .app-view").forEach((element) => element.classList.remove("is-active"));
    const nav = document.querySelector(`.nav-item[data-view="${target}"]`);
    const section = $(`#view-${target}`);
    if (nav) nav.classList.add("is-active");
    if (section) section.classList.add("is-active");
    window.location.hash = target;
    window.scrollTo(0, 0);
  };
  const telegramLink = (url) => {
    const link = $("#telegram-link");
    if (url && /^https:\/\/(?:t\.me|telegram\.me)\//i.test(url)) { link.href = url; link.removeAttribute("aria-disabled"); }
    else { link.href = "#"; link.setAttribute("aria-disabled", "true"); }
  };
  const renderIdentity = () => {
    const user = state.dashboard && state.dashboard.user;
    const mark = initials(user);
    [$("#avatar"), $("#settings-avatar")].forEach((element) => { if (element) element.textContent = mark; });
    if (!user) {
      $("#home-title").innerHTML = "Open in Telegram<br><em>to get started.</em>";
      $("#welcome-copy").textContent = "Open AuriX VPN inside Telegram to link your account and manage access.";
      $("#settings-name").textContent = "Telegram account";
      $("#settings-handle").textContent = "Verified session required";
      $("#settings-state").textContent = "Locked";
      return;
    }
    const name = [user.first_name, user.last_name].filter(Boolean).join(" ") || "Telegram account";
    $("#welcome-copy").textContent = `Welcome, ${name}. Your portal is linked to this Telegram account.`;
    $("#settings-name").textContent = name;
    $("#settings-handle").textContent = user.username ? `@${user.username}` : `Telegram ID ${user.telegram_id}`;
    $("#settings-state").textContent = "Verified";
    const key = selectedKey();
    $("#home-title").innerHTML = key ? "Your secure<br><em>line is ready.</em>" : "Choose access.<br><em>Connect when ready.</em>";
  };
  const renderCurrentServer = () => {
    const key = selectedKey();
    const server = key && serverFor(key.endpoint_id);
    $("#current-server-name").textContent = server ? serverLabel(server) : (key ? String(key.endpoint_id || "Assigned server") : "Not connected");
    $("#current-server-region").textContent = server ? String(server.region || "Location confirmed") : (key ? "Assigned location" : "Choose a server in Servers");
    $("#current-server-note").textContent = server ? `${server.region || "AuriX network"} · ${serverStatus(server)}` : "Your selected location appears here.";
    $("#current-server-state").textContent = key ? (server ? serverStatus(server) : "Assigned") : "Status";
    $("#current-server-state").className = `state-pill ${key && server && server.eligible ? "state-pill--ready" : ""}`;
  };
  const renderUsage = () => {
    const keys = activeKeys();
    const key = selectedKey();
    const picker = $("#key-picker");
    const select = $("#active-key-select");
    picker.hidden = keys.length <= 1;
    select.innerHTML = keys.map((item) => `<option value="${escapeHtml(keyIdentity(item))}" ${key && keyIdentity(item) === keyIdentity(key) ? "selected" : ""}>${escapeHtml(item.tier || item.plan_code || "VPN access")} · ${escapeHtml(serverLabel(serverFor(item.endpoint_id)))}</option>`).join("");
    const quota = key ? Number(key.quota_bytes || 0) : 0;
    const used = key ? Number(key.used_bytes || 0) : 0;
    const remaining = key ? Number(key.remaining_bytes || Math.max(0, quota - used)) : 0;
    const percent = quota ? Math.min(100, Math.round(used * 100 / quota)) : 0;
    $("#usage-title").textContent = key ? (key.tier || "Active package") : "No active package";
    $("#usage-bar-fill").style.width = `${percent}%`;
    $("#usage-used").textContent = key ? `${formatBytes(used)} used` : "— used";
    $("#usage-remaining").textContent = key ? `${formatBytes(remaining)} remaining` : "— remaining";
    $("#usage-note").textContent = key ? (state.dashboard.usage_available ? `Observed through ${serverLabel(serverFor(key.endpoint_id))}. Expires ${formatDate(key.expires_at)}.` : "Latest transfer observation is temporarily unavailable.") : "AuriX access appears after an approved payment is provisioned.";
    $("#access-badge").textContent = state.dashboard && state.dashboard.access_available ? (key ? "Ready" : "Locked") : "Status only";
    $("#access-badge").className = `state-pill state-pill--gold ${key && state.dashboard.access_available ? "state-pill--ready" : ""}`;
    const actions = $("#key-actions");
    const secret = $("#key-secret");
    actions.innerHTML = "";
    secret.hidden = true;
    secret.textContent = "";
    if (key && key.access_url) {
      actions.innerHTML = `<button class="button button--primary" id="open-key" type="button">Add to Outline</button><button class="button button--quiet" id="copy-key" type="button">Copy key</button><button class="text-button" id="reveal-key" type="button">Reveal</button>`;
      $("#open-key").addEventListener("click", () => { if (/^ss(?:conf)?:\/\//i.test(key.access_url)) window.location.href = key.access_url; });
      $("#copy-key").addEventListener("click", async () => { if (navigator.clipboard) { await navigator.clipboard.writeText(key.access_url); $("#copy-key").textContent = "Copied"; setTimeout(() => { $("#copy-key").textContent = "Copy key"; }, 1400); } });
      $("#reveal-key").addEventListener("click", () => { secret.hidden = !secret.hidden; secret.textContent = secret.hidden ? "" : key.access_url; $("#reveal-key").textContent = secret.hidden ? "Reveal" : "Hide"; });
    } else if (key) {
      actions.innerHTML = `<span class="fine-print">Key is being synchronized. Refresh shortly.</span>`;
    }
  };
  const renderPackagesSummary = () => {
    const subscriptions = (state.dashboard && state.dashboard.subscriptions || []).filter((item) => ["active", "pending"].includes(item.status) || item.key_status === "active");
    $("#package-count").textContent = `${subscriptions.length} package${subscriptions.length === 1 ? "" : "s"}`;
    $("#package-summary-list").innerHTML = subscriptions.length ? subscriptions.slice(0, 3).map((item) => `<div class="summary-row"><div><strong>${escapeHtml(item.plan_name || item.plan_code || "AuriX VPN")}</strong><span>${escapeHtml(serverLabel(serverFor(item.endpoint_id)))} · ${escapeHtml(titleCase(item.status))}</span></div><span>${escapeHtml(formatDate(item.expires_at))}</span></div>`).join("") : `<div class="empty-state">No active package yet. Choose a location and package to start.</div>`;
  };
  const renderHome = () => { renderIdentity(); renderCurrentServer(); renderUsage(); renderPackagesSummary(); };
  const renderServers = () => {
    const list = $("#server-list");
    if (!state.initData) { list.innerHTML = `<div class="empty-state">Open this page inside Telegram to see live server availability.</div>`; return; }
    if (!state.servers.length) { list.innerHTML = `<div class="empty-state">No server health data is available yet. Try refresh shortly.</div>`; return; }
    list.innerHTML = state.servers.map((server) => {
      const selected = String(state.selectedEndpointId || "") === String(server.id);
      const current = (state.dashboard && state.dashboard.keys || []).some((key) => key.status === "active" && String(key.endpoint_id) === String(server.id));
      const latency = server.management_latency_ms != null ? `${Math.round(Number(server.management_latency_ms))} ms check` : "No recent check";
      return `<article class="server-row ${selected ? "is-selected" : ""}"><span class="server-orb server-orb--small" aria-hidden="true">⌁</span><div class="server-main"><div><strong>${escapeHtml(server.code || server.region || server.id)}</strong><span>${escapeHtml(server.region || "AuriX network")}</span></div><div class="server-health"><span class="status-dot ${server.eligible ? "is-good" : server.healthy ? "is-warn" : "is-bad"}"></span>${escapeHtml(serverStatus(server))} · ${escapeHtml(latency)}</div></div><button class="button ${selected ? "button--selected" : "button--quiet"} choose-server" data-endpoint="${escapeHtml(server.id)}" type="button">${current ? "Current" : selected ? "Selected" : server.eligible ? "Choose" : "Unavailable"}</button></article>`;
    }).join("");
  };
  const renderPlans = () => {
    const catalog = state.catalog || { plans: [] };
    const selected = serverFor(state.selectedEndpointId);
    $("#selected-server-label").textContent = selected ? `${serverLabel(selected)} · ${selected.region || ""}` : "Automatic capacity selection";
    const list = $("#plan-list");
    if (!catalog.plans || !catalog.plans.length) { list.innerHTML = `<div class="empty-state">No active package is published yet.</div>`; return; }
    list.innerHTML = catalog.plans.map((plan) => {
      const quota = plan.quota_bytes ? formatBytes(plan.quota_bytes) : "Fair-use";
      const action = state.initData ? `<button class="button button--primary buy-button" data-plan="${escapeHtml(plan.code)}" type="button">Get this package</button>` : `<a class="button button--quiet" href="${escapeHtml(catalog.telegram_url || "#")}">Open in Telegram</a>`;
      return `<article class="plan-card"><div class="plan-tag">${escapeHtml(plan.code)}</div><h2>${escapeHtml(plan.name)}</h2><div class="plan-price">${Number(plan.price_minor || 0).toLocaleString()} <small>${escapeHtml(plan.currency || "MMK")}</small></div><div class="plan-specs"><span>${escapeHtml(quota)}</span><span>${escapeHtml(plan.duration_days)} days</span></div>${action}</article>`;
    }).join("");
  };
  const renderClaimCapabilities = () => {
    const capabilities = state.dashboard && state.dashboard.claim_capabilities;
    document.querySelectorAll(".claim-button").forEach((button) => {
      const allowed = capabilities && capabilities[button.dataset.claim];
      button.disabled = !state.initData || !allowed;
    });
    $("#promo-form").querySelector("button").disabled = !state.initData;
    if (!state.initData) $("#claim-note").textContent = "Open this app in Telegram to claim free or promotional access.";
    else if (capabilities && (!capabilities.daily || !capabilities.trial)) $("#claim-note").textContent = "Regular free claims are paused while paid or promotional access is active, or when your account is outside the trial allow-list.";
    else $("#claim-note").textContent = "Daily access renews after 24 hours; the monthly trial renews after 30 days. Free access uses automatic capacity selection.";
  };
  const renderOrders = () => {
    const orders = state.dashboard && state.dashboard.orders;
    const list = $("#order-list");
    if (!orders) { list.innerHTML = `<div class="empty-state">Open this app from Telegram to load orders.</div>`; return; }
    if (!orders.length) { list.innerHTML = `<div class="empty-state">No orders yet.</div>`; return; }
    const textPayment = state.catalog && state.catalog.text_reference_payment_enabled;
    list.innerHTML = orders.map((order) => {
      const open = ["awaiting_payment", "payment_submitted"].includes(order.status);
      const payment = open && textPayment ? `<form class="payment-form" data-order="${escapeHtml(order.id)}"><input name="provider" maxlength="64" placeholder="Payment provider" required><input name="reference" maxlength="128" placeholder="Payment reference" required><button class="button button--primary" type="submit">Submit</button></form>` : open ? `<p class="fine-print">Use Telegram to upload the receipt and continue review.</p>` : "";
      return `<article class="order-row"><div><strong>${escapeHtml(order.plan_name || order.plan_code)}</strong><span>#${escapeHtml(String(order.id).slice(0, 12))} · ${escapeHtml(formatDate(order.created_at))}</span></div><div class="order-right"><b>${escapeHtml(titleCase(order.stage || order.status))}</b><span>${Number(order.amount_minor || 0).toLocaleString()} ${escapeHtml(order.currency || "")}</span></div>${payment}</article>`;
    }).join("");
  };
  const renderCatalogLinks = () => {
    const catalog = state.catalog || { client_downloads: {} };
    $("#client-links").innerHTML = Object.entries(catalog.client_downloads || {}).map(([name, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(name)}</a>`).join("");
    telegramLink(catalog.telegram_url);
  };
  const loadServers = async () => {
    if (!state.initData) return;
    const payload = await api("/api/servers");
    state.servers = payload.servers || [];
    const active = (state.dashboard && state.dashboard.keys || []).find((key) => key.status === "active");
    const firstEligible = state.servers.find((server) => server.eligible);
    if (!state.selectedEndpointId) state.selectedEndpointId = active && active.endpoint_id || firstEligible && firstEligible.id || null;
    renderServers(); renderPackagesSummary(); renderCurrentServer(); renderPlans();
  };
  const loadDashboard = async () => {
    if (!state.initData) {
      setConnection("", "Preview");
      showNotice(`For private keys and orders, open this page inside the <a href="${escapeHtml((state.catalog && state.catalog.telegram_url) || "#")}">AuriX Telegram bot</a>.`);
      renderHome(); renderServers(); renderOrders(); return;
    }
    setConnection("", "Loading");
    try {
      state.dashboard = await api("/api/dashboard");
      const keys = activeKeys();
      if (!keys.some((key) => keyIdentity(key) === state.selectedKeyId)) state.selectedKeyId = keys[0] ? keyIdentity(keys[0]) : null;
      setConnection("online", "Verified");
      showNotice("");
      renderHome(); renderOrders(); renderClaimCapabilities();
      await loadServers();
    } catch (error) {
      state.dashboard = null;
      setConnection("error", "Unavailable");
      showNotice(escapeHtml(error.message));
      renderHome(); renderServers(); renderOrders(); renderClaimCapabilities();
    }
  };
  const createOrder = async (planCode) => {
    try {
      const result = await api("/api/orders", { method: "POST", body: JSON.stringify({ plan_code: planCode, endpoint_id: state.selectedEndpointId }) });
      await loadDashboard();
      go("settings");
      if (result.plan_conflict) showNotice("You already have an open order for another package. Continue or cancel it in Telegram before choosing a new package.");
      else if (result.created) showNotice("Order created. Continue in Telegram to upload the payment receipt.");
      else showNotice("Your existing open order is ready. Continue in Telegram to upload the payment receipt.");
    } catch (error) { showNotice(escapeHtml(error.message)); }
  };
  const claimAccess = async (kind, body) => {
    const path = kind === "promo" ? "/api/claims/promo" : `/api/claims/${kind}`;
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      await loadDashboard();
      go("home");
      if (kind === "promo") {
        const messages = {
          won: `Promo redeemed. Winner #${result.winner_number || "—"}; your access is ready.`,
          already_won: "This account already redeemed that promo.",
          full: "This promo window is full.",
          paused: "This promo is currently paused.",
          scheduled: "This promo has not started yet.",
          ended: "This promo has ended.",
          unavailable: "That promo code is unavailable.",
          ineligible: result.reason || "This account is not eligible for that promo.",
        };
        showNotice(escapeHtml(messages[result.outcome] || result.reason || titleCase(result.outcome)));
      } else if (result.issued) showNotice(kind === "daily" ? "Daily 300 MiB access is ready." : "Monthly 3 GiB access is ready.");
      else if (result.next_claim_at) showNotice(`Available again ${escapeHtml(formatDate(result.next_claim_at))}.`);
      else showNotice("This claim is not currently available.");
    } catch (error) { showNotice(escapeHtml(error.message)); }
  };
  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-view], [data-view-link]");
    if (nav) { go(nav.dataset.view || nav.dataset.viewLink); return; }
    const choose = event.target.closest(".choose-server");
    if (choose) {
      const server = state.servers.find((item) => String(item.id) === String(choose.dataset.endpoint));
      if (!server || !server.eligible) { showNotice("That server is not currently available for new packages."); return; }
      state.selectedEndpointId = server.id;
      renderServers(); renderPlans();
      showNotice(`${escapeHtml(serverLabel(server))} selected for your next package.`);
      go("packages");
      return;
    }
    const buy = event.target.closest(".buy-button");
    if (buy) createOrder(buy.dataset.plan);
    const claim = event.target.closest(".claim-button");
    if (claim) claimAccess(claim.dataset.claim);
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".payment-form");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector("button"); button.disabled = true;
    try {
      await api(`/api/orders/${encodeURIComponent(form.dataset.order)}/payment`, { method: "POST", body: JSON.stringify({ provider: form.provider.value, reference: form.reference.value }) });
      await loadDashboard(); showNotice("Payment reference submitted for review.");
    } catch (error) { showNotice(escapeHtml(error.message)); }
    finally { button.disabled = false; }
  });
  $("#promo-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    await claimAccess("promo", { code: event.currentTarget.code.value });
    button.disabled = false;
  });
  $("#active-key-select").addEventListener("change", (event) => {
    state.selectedKeyId = event.currentTarget.value;
    renderCurrentServer(); renderUsage();
  });
  $("#settings-button").addEventListener("click", () => go("settings"));
  $("#servers-refresh").addEventListener("click", async () => { try { await loadServers(); showNotice("Server availability refreshed."); } catch (error) { showNotice(escapeHtml(error.message)); } });
  window.addEventListener("hashchange", () => {
    const requested = window.location.hash.slice(1);
    if (["home", "servers", "packages", "settings"].includes(requested)) go(requested);
  });
  (async function init() {
    try { state.catalog = await api("/api/plans"); setConnection("online", "Online"); }
    catch (error) { state.catalog = { plans: [], client_downloads: {}, telegram_url: null }; setConnection("error", "Unavailable"); showNotice(escapeHtml(error.message)); }
    renderCatalogLinks(); renderPlans(); renderHome(); renderClaimCapabilities();
    const requested = window.location.hash.slice(1);
    if (["home", "servers", "packages", "settings"].includes(requested)) go(requested);
    await loadDashboard();
  }());
}());
