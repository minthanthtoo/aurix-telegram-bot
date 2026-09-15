const adminAuth = document.querySelector("#admin-auth");
const adminApp = document.querySelector("#admin-app");
const adminAuthCopy = document.querySelector("#admin-auth-copy");
const adminAuthError = document.querySelector("#admin-auth-error");
const telegramLogin = document.querySelector("#admin-telegram-login");
const adminUser = document.querySelector("#admin-user");
const adminRole = document.querySelector("#admin-role");
const logoutButton = document.querySelector("#admin-logout");
const accountsList = document.querySelector("#accounts-list");
const accountFilter = document.querySelector("#account-filter");
const accountSearch = document.querySelector("#account-search");
const accountCount = document.querySelector("#account-count");
const activityList = document.querySelector("#activity-list");
const activityAccountFilter = document.querySelector("#activity-account-filter");
const activityStatusFilter = document.querySelector("#activity-status-filter");
const activityModelFilter = document.querySelector("#activity-model-filter");
const activityEndpointFilter = document.querySelector("#activity-endpoint-filter");
const activityKeyFilter = document.querySelector("#activity-key-filter");
const activityUserFilter = document.querySelector("#activity-user-filter");
const activityClearFilters = document.querySelector("#activity-clear-filters");
const activityPrevious = document.querySelector("#activity-previous");
const activityNext = document.querySelector("#activity-next");
const activityPageLabel = document.querySelector("#activity-page-label");
const usageRange = document.querySelector("#usage-range");
const refreshAnalyticsButton = document.querySelector("#refresh-analytics");
const breakdownDimension = document.querySelector("#breakdown-dimension");
const breakdownCaption = document.querySelector("#breakdown-caption");
const breakdownTableBody = document.querySelector("#breakdown-table-body");
const usageChart = document.querySelector("#usage-chart");
const chartTokensButton = document.querySelector("#chart-tokens");
const chartRequestsButton = document.querySelector("#chart-requests");
const capabilitySummary = document.querySelector("#capability-summary");
const capabilityList = document.querySelector("#capability-list");
const capabilityAccount = document.querySelector("#capability-account");
const refreshCapabilitiesButton = document.querySelector("#refresh-capabilities");
const flash = document.querySelector("#admin-flash");
const accountDialog = document.querySelector("#account-dialog");
const accountForm = document.querySelector("#account-form");
const policyDialog = document.querySelector("#policy-dialog");
const policyForm = document.querySelector("#policy-form");
const issueDialog = document.querySelector("#issue-dialog");
const issueForm = document.querySelector("#issue-form");
const secretDialog = document.querySelector("#secret-dialog");
const secretValue = document.querySelector("#secret-value");
const copySecretButton = document.querySelector("#copy-secret");
const shared = window.AuriXShared;

let currentSecret = "";
let issueAccountName = "Partner site";
let inventoryAccounts = [];
let activityOffset = 0;
let analyticsReport = null;
let chartMetric = "tokens";
let analyticsRequestSerial = 0;
const adminNavLinks = Array.from(document.querySelectorAll(".admin-nav-link"));

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setHidden(node, hidden) {
  node.hidden = Boolean(hidden);
}

async function requestJSON(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      "X-AuriX-Admin": "1",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const raw = await response.text();
  let payload = {};
  try {
    payload = raw ? JSON.parse(raw) : {};
  } catch (_error) {
    payload = { error: "The server returned an invalid response." };
  }
  if (!response.ok) {
    const error = new Error(payload.error || "The request failed.");
    error.status = response.status;
    error.requestId = payload.request_id || response.headers.get("X-Request-ID");
    throw error;
  }
  return payload;
}

function showFlash(text, kind = "success") {
  flash.textContent = text;
  flash.className = `admin-flash ${kind}`;
  setHidden(flash, false);
  window.clearTimeout(showFlash.timer);
  showFlash.timer = window.setTimeout(() => setHidden(flash, true), 5000);
}

function showAuthError(text) {
  adminAuthError.textContent = text;
  setHidden(adminAuthError, false);
}

function clearAuthError() {
  adminAuthError.textContent = "";
  setHidden(adminAuthError, true);
}

function updateActiveNav() {
  const activeHash = window.location.hash || "#overview";
  adminNavLinks.forEach((link) => {
    const isActive = link.getAttribute("href") === activeHash;
    link.classList.toggle("active", isActive);
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function renderLoginWidget(botUsername) {
  shared.renderTelegramWidget(telegramLogin, botUsername, "onTelegramAdminAuth", finishTelegramLogin);
}

function showAdmin(user) {
  clearAuthError();
  setHidden(adminAuth, true);
  setHidden(adminApp, false);
  adminUser.textContent = user.username ? `@${user.username}` : user.first_name || "Telegram admin";
  setHidden(adminUser, false);
  setHidden(logoutButton, false);
  requestJSON("/api/admin/access").then((access) => {
    const roleLabels = {
      platform_owner: "Platform owner · all accounts",
      operator: "Operator · all accounts",
      operator_token: "Operator token · all accounts",
      account_owner: "Account owner · your accounts",
    };
    adminRole.textContent = roleLabels[access.role] || "Scoped console";
    setHidden(adminRole, false);
  }).catch(() => {
    adminRole.textContent = "Scoped console";
    setHidden(adminRole, false);
  });
}

async function finishTelegramLogin(user) {
  try {
    await requestJSON("/api/auth/telegram", {
      method: "POST",
      body: JSON.stringify(user),
    });
    await loadAdmin();
  } catch (error) {
    showAuthError(error instanceof Error ? error.message : "Telegram sign-in failed.");
  }
}

function formatDate(value) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function compactNumber(value) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(Number(value) || 0);
}

function exactNumber(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(Number(value) || 0);
}

function formatPercent(value) {
  return value == null ? "—" : `${Number(value).toFixed(Number(value) % 1 ? 1 : 0)}%`;
}

function formatMilliseconds(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const milliseconds = Number(value);
  return milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(2)} s`
    : `${Math.round(milliseconds)} ms`;
}

function formatCost(value) {
  return value == null ? "—" : new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 4,
  }).format(Number(value) || 0);
}

function analyticsRangeQuery() {
  const now = new Date();
  const start = new Date(now);
  switch (usageRange.value) {
    case "today":
      start.setHours(0, 0, 0, 0);
      break;
    case "24h":
      start.setTime(now.getTime() - 24 * 60 * 60 * 1000);
      break;
    case "30d":
      start.setTime(now.getTime() - 30 * 24 * 60 * 60 * 1000);
      break;
    case "60d":
      start.setTime(now.getTime() - 60 * 24 * 60 * 60 * 1000);
      break;
    case "month":
      start.setDate(1);
      start.setHours(0, 0, 0, 0);
      break;
    case "7d":
    default:
      start.setTime(now.getTime() - 7 * 24 * 60 * 60 * 1000);
      break;
  }
  return new URLSearchParams({
    format: "analytics",
    from: start.toISOString(),
    to: now.toISOString(),
  }).toString();
}

function svgNode(tag, attributes = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
  if (text) node.textContent = text;
  return node;
}

function renderUsageChart(daily) {
  usageChart.replaceChildren();
  const rows = Array.isArray(daily) ? daily : [];
  if (!rows.length) {
    usageChart.appendChild(element("div", "empty-state large-empty", "No usage in the selected period."));
    return;
  }
  const width = 820;
  const height = 280;
  const padding = { top: 20, right: 20, bottom: 42, left: 56 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const series = chartMetric === "tokens"
    ? [
      { label: "Input", color: "#ff835c", values: rows.map((row) => Number(row.input_tokens) || 0) },
      { label: "Output", color: "#63e6be", values: rows.map((row) => Number(row.output_tokens) || 0) },
    ]
    : [
      { label: "Successful", color: "#63e6be", values: rows.map((row) => Number(row.successful_requests) || 0) },
      { label: "Failed", color: "#ff9c9c", values: rows.map((row) => Number(row.failed_requests) || 0) },
    ];
  const maxValue = Math.max(1, ...series.flatMap((item) => item.values));
  const x = (index) => rows.length === 1
    ? padding.left + plotWidth / 2
    : padding.left + (index / (rows.length - 1)) * plotWidth;
  const y = (value) => padding.top + plotHeight - (value / maxValue) * plotHeight;
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "presentation",
    "aria-hidden": "true",
  });
  for (let index = 0; index <= 4; index += 1) {
    const value = maxValue * (index / 4);
    const lineY = y(value);
    svg.appendChild(svgNode("line", {
      x1: padding.left, y1: lineY, x2: width - padding.right, y2: lineY,
      class: "chart-grid-line",
    }));
    svg.appendChild(svgNode("text", {
      x: padding.left - 10, y: lineY + 4, "text-anchor": "end", class: "chart-axis-label",
    }, compactNumber(value)));
  }
  const labelStep = Math.max(1, Math.ceil(rows.length / 6));
  rows.forEach((row, index) => {
    if (index % labelStep !== 0 && index !== rows.length - 1) return;
    const dateLabel = String(row.date || "").slice(5) || "—";
    svg.appendChild(svgNode("text", {
      x: x(index), y: height - 14, "text-anchor": "middle", class: "chart-axis-label",
    }, dateLabel));
  });
  series.forEach((item) => {
    const points = item.values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
    svg.appendChild(svgNode("polyline", { points, class: "chart-series", stroke: item.color }));
    item.values.forEach((value, index) => {
      svg.appendChild(svgNode("circle", { cx: x(index), cy: y(value), r: 3.5, fill: item.color, class: "chart-point" }));
    });
  });
  usageChart.appendChild(svg);
  const legend = element("div", "chart-legend");
  series.forEach((item) => {
    const entry = element("span", "chart-legend-item");
    const swatch = element("span", "chart-legend-swatch");
    swatch.style.backgroundColor = item.color;
    entry.append(swatch, element("span", "", item.label));
    legend.appendChild(entry);
  });
  usageChart.appendChild(legend);
}

function renderAnalyticsMetrics(report) {
  const summary = report.summary || {};
  document.querySelector("#analytics-requests").textContent = compactNumber(summary.requests);
  document.querySelector("#analytics-successful").textContent = compactNumber(summary.successful_requests);
  document.querySelector("#analytics-success-rate").textContent = `success rate ${formatPercent(summary.success_rate)}`;
  document.querySelector("#analytics-input").textContent = compactNumber(summary.input_tokens);
  document.querySelector("#analytics-output").textContent = compactNumber(summary.output_tokens);
  document.querySelector("#analytics-total").textContent = compactNumber(summary.total_tokens);
  document.querySelector("#analytics-reported").textContent = `${exactNumber(summary.usage_reported_requests)} usage-reported requests`;
  document.querySelector("#analytics-failed").textContent = compactNumber(summary.failed_requests);
  document.querySelector("#analytics-in-out").textContent = `${compactNumber(summary.input_tokens)} / ${compactNumber(summary.output_tokens)}`;
  document.querySelector("#analytics-cached").textContent = compactNumber(summary.cached_tokens);
  document.querySelector("#analytics-cost").textContent = formatCost(summary.cost);
  document.querySelector("#analytics-last-used").textContent = formatDate(summary.last_used_at);
  document.querySelector("#analytics-first-event").textContent = formatMilliseconds(summary.avg_first_event_ms);
  document.querySelector("#analytics-duration").textContent = formatMilliseconds(summary.avg_duration_ms);
  renderUsageChart(report.daily);
}

function breakdownLabel(row, dimension) {
  if (dimension === "keys") {
    return { primary: row.label || "Unnamed key", secondary: `${row.token_prefix || "key_…"} · ${row.account_name || "site unavailable"}` };
  }
  if (dimension === "models") {
    return { primary: row.model_id || "Unknown model", secondary: row.provider || "Provider unavailable" };
  }
  if (dimension === "endpoints") {
    return { primary: row.endpoint || "Unknown endpoint", secondary: "API endpoint" };
  }
  return { primary: row.account_name || "Partner site", secondary: `${row.account_id || "account unavailable"} · ${row.account_status || "unknown"}` };
}

function renderBreakdown(report) {
  const dimension = breakdownDimension.value;
  const labels = {
    accounts: "Usage by site",
    keys: "Usage by API key",
    models: "Usage by model",
    endpoints: "Usage by endpoint",
  };
  breakdownCaption.textContent = labels[dimension] || "Usage breakdown";
  breakdownTableBody.replaceChildren();
  const rows = Array.isArray(report[dimension]) ? report[dimension] : [];
  if (!rows.length) {
    const empty = element("tr");
    const cell = element("td", "empty-state", "No usage in the selected period.");
    cell.colSpan = 8;
    empty.appendChild(cell);
    breakdownTableBody.appendChild(empty);
    return;
  }
  rows.forEach((row) => {
    const tr = element("tr");
    const label = breakdownLabel(row, dimension);
    const nameCell = element("td", "breakdown-name");
    nameCell.append(element("strong", "breakdown-primary", label.primary), element("span", "breakdown-secondary", label.secondary));
    tr.append(
      nameCell,
      element("td", "numeric-cell", exactNumber(row.requests)),
      element("td", "numeric-cell", exactNumber(row.successful_requests)),
      element("td", "numeric-cell", `${compactNumber(row.input_tokens)} / ${compactNumber(row.output_tokens)}`),
      element("td", "numeric-cell", compactNumber(row.total_tokens)),
      element("td", "numeric-cell", formatPercent(row.success_rate)),
      element("td", "numeric-cell", formatCost(row.cost)),
      element("td", "time-cell", formatDate(row.last_used_at)),
    );
    breakdownTableBody.appendChild(tr);
  });
}

async function loadAnalytics() {
  const serial = ++analyticsRequestSerial;
  const report = await requestJSON(`/api/admin/usage?${analyticsRangeQuery()}`);
  if (serial !== analyticsRequestSerial) return report;
  analyticsReport = report;
  renderAnalyticsMetrics(report);
  renderBreakdown(report);
  return report;
}

const capabilityLabels = {
  chat: "Chat and streaming",
  embeddings: "Embeddings",
  speech_to_text: "Speech to text",
  text_to_speech: "Text to speech",
  image_generation: "Image generation",
  video_generation: "Video generation",
};

function renderCapabilities(report) {
  capabilitySummary.replaceChildren();
  capabilityList.replaceChildren();
  capabilityAccount.replaceChildren();
  if (!report || report.error) {
    capabilitySummary.appendChild(element("p", "capability-error", report?.error || "Capability discovery is unavailable."));
    return;
  }
  const summary = report.summary || {};
  const summaryItems = [
    `${exactNumber(summary.categories_ok)} / ${exactNumber(summary.categories_total)} categories available`,
    `${exactNumber(summary.models_discovered)} models discovered`,
    summary.account_quota_observed ? "Account quota observed" : "Account quota unavailable",
  ];
  summaryItems.forEach((text) => capabilitySummary.appendChild(element("span", "capability-summary-item", text)));

  Object.entries(report.categories || {}).forEach(([key, category]) => {
    const card = element("article", "capability-card");
    const header = element("div", "capability-card-header");
    const title = element("h4", "capability-title", capabilityLabels[key] || key);
    const status = category.status === "ok" ? "available" : "discovery error";
    header.append(title, element("span", `status-pill ${category.status === "ok" ? "status-active" : "status-revoked"}`, status));
    card.appendChild(header);
    const models = Array.isArray(category.models) ? category.models : [];
    const modelCount = element("p", "capability-model-count", `${models.length} model${models.length === 1 ? "" : "s"}`);
    card.appendChild(modelCount);
    if (models.length) {
      const modelList = element("div", "capability-models");
      models.forEach((model) => modelList.appendChild(element("code", "capability-model", model.id)));
      card.appendChild(modelList);
    } else if (category.error?.message) {
      card.appendChild(element("p", "capability-error-detail", category.error.message));
    } else {
      card.appendChild(element("p", "capability-empty", "No models reported."));
    }
    capabilityList.appendChild(card);
  });

  const accountHeading = element("h4", "capability-account-title", "Account and quota discovery");
  capabilityAccount.appendChild(accountHeading);
  const accountItems = Object.entries(report.account || {});
  if (!accountItems.length) {
    capabilityAccount.appendChild(element("p", "capability-empty", "No account metadata reported."));
    return;
  }
  const accountGrid = element("div", "capability-account-grid");
  accountItems.forEach(([key, value]) => {
    const row = element("div", "capability-account-row");
    const label = key.replaceAll("_", " ");
    const ok = value?.status === "ok";
    let detail = ok ? "Observed" : (value?.error?.message || value?.status || "Unavailable");
    if (ok && value?.data && Array.isArray(value.data.providers)) {
      detail = `${value.data.providers.length} provider${value.data.providers.length === 1 ? "" : "s"} reported`;
    }
    row.append(
      element("span", "capability-account-label", label),
      element("span", `status-pill ${ok ? "status-active" : "status-revoked"}`, ok ? "observed" : "unavailable"),
      element("span", "capability-account-detail", detail),
    );
    accountGrid.appendChild(row);
  });
  capabilityAccount.appendChild(accountGrid);
}

async function loadCapabilities() {
  try {
    const report = await requestJSON("/api/admin/capabilities");
    renderCapabilities(report);
    return report;
  } catch (error) {
    const message = error instanceof Error ? error.message : "Capability discovery is unavailable.";
    renderCapabilities({ error: message });
    return null;
  }
}

function renderActivity(events) {
  activityList.replaceChildren();
  if (!Array.isArray(events) || !events.length) {
    activityList.appendChild(element("div", "empty-state large-empty", "No request activity in the current period."));
    return;
  }
  events.forEach((event) => {
    const row = element("article", "activity-row");
    const main = element("div", "activity-main");
    main.append(
      element("strong", "activity-account", event.account_name || "Partner site"),
      element("code", "activity-request", event.request_id || "request unavailable"),
    );
    const detail = element("div", "activity-detail");
    const tokenText = event.total_tokens == null ? "Usage unavailable" : `${compactNumber(event.total_tokens)} tokens`;
    const timingText = event.duration_ms == null ? "timing unavailable" : `${formatMilliseconds(event.duration_ms)} total`;
    detail.textContent = `${event.model_id || "model unavailable"} · ${event.endpoint || "endpoint unavailable"} · ${tokenText} · ${timingText}`;
    const meta = element("div", "activity-meta");
    meta.append(
      element("span", `status-pill ${event.status === "completed" ? "status-active" : "status-revoked"}`, event.status || "unknown"),
      element("span", "activity-status", event.http_status ? `HTTP ${event.http_status}` : "HTTP unavailable"),
      element("time", "activity-time", formatDate(event.created_at)),
    );
    row.append(main, detail, meta);
    activityList.appendChild(row);
  });
}

function ownerLabel(account) {
  const owner = String(account.owner_id || "");
  if (!owner) return "Operator-managed";
  return `${account.owner_type || "owner"} · ···${owner.slice(-4)}`;
}

function renderKey(account, key) {
  const row = element("div", "key-row");
  const details = element("div", "key-details");
  details.append(
    element("code", "key-prefix", key.token_prefix || "ak_live_…"),
    element("span", "key-label", key.label || "Unnamed key"),
  );
  const metadata = element("div", "key-metadata");
  metadata.append(
    element("span", `status-pill ${key.status === "active" ? "status-active" : "status-revoked"}`, key.status),
    element("span", "key-meta-text", `Created ${formatDate(key.created_at)}`),
    element("span", "key-meta-text", key.expires_at ? `Expires ${formatDate(key.expires_at)}` : "No expiry"),
    element("span", "key-meta-text", key.last_used_at ? `Used ${formatDate(key.last_used_at)}` : "Not used"),
  );
  const actions = element("div", "key-actions");
  if (key.status === "active") {
    const revoke = element("button", "button-danger", "Revoke");
    revoke.type = "button";
    revoke.addEventListener("click", () => revokeKey(account, key));
    actions.appendChild(revoke);
  }
  row.append(details, metadata, actions);
  return row;
}

function renderAccount(account) {
  const card = element("article", "account-card");
  const header = element("div", "account-card-header");
  const title = element("div");
  title.append(element("h4", "account-name", account.name));
  title.append(element("span", "account-id", account.id));
  const status = element("span", `status-pill ${account.status === "active" ? "status-active" : "status-revoked"}`, account.status);
  header.append(title, status);

  const metadata = element("div", "account-metadata");
  metadata.append(
    element("span", "account-meta", `Owner: ${ownerLabel(account)}`),
    element("span", "account-meta", `${account.requests_per_minute} requests/minute`),
    element("span", "account-meta", `Modes: ${(account.allowed_modes || ["*"]).join(", ")}`),
    element("span", "account-meta", `Models: ${(account.allowed_models || ["*"]).join(", ")}`),
    element("span", "account-meta", `${compactNumber(account.requests || 0)} requests this period`),
    element("span", "account-meta", `${compactNumber(account.total_tokens || 0)} tokens`),
  );

  const keysHeading = element("div", "keys-heading");
  keysHeading.append(element("h5", "keys-title", "Keys"));
  const activity = element("button", "button-quiet", "View activity");
  activity.type = "button";
  activity.addEventListener("click", async () => {
    activityAccountFilter.value = account.id;
    activityOffset = 0;
    document.querySelector("#activity").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      await loadActivity({ reset: true });
    } catch (error) {
      showFlash(error instanceof Error ? error.message : "Account activity unavailable.", "error");
    }
  });
  keysHeading.appendChild(activity);
  if (account.status === "active") {
    const policy = element("button", "button-quiet", "Edit policy");
    policy.type = "button";
    policy.addEventListener("click", () => openPolicyDialog(account));
    keysHeading.appendChild(policy);
  }
  if (account.status === "active") {
    const issue = element("button", "button-quiet", "Issue new key");
    issue.type = "button";
    issue.addEventListener("click", () => openIssueDialog(account));
    keysHeading.appendChild(issue);
  } else {
    keysHeading.appendChild(element("span", "key-action-disabled", "Account revoked"));
  }

  const keys = element("div", "keys-list");
  const accountKeys = Array.isArray(account.keys) ? account.keys : [];
  if (!accountKeys.length) {
    keys.appendChild(element("p", "empty-state", "No keys issued."));
  } else {
    accountKeys.forEach((key) => keys.appendChild(renderKey(account, key)));
  }
  card.append(header, metadata, keysHeading, keys);
  return card;
}

function renderReport(report) {
  const accounts = Array.isArray(report.accounts) ? report.accounts : [];
  inventoryAccounts = accounts;
  const activeAccounts = accounts.filter((account) => account.status === "active");
  const keys = accounts.flatMap((account) => Array.isArray(account.keys) ? account.keys : []);
  const activeKeys = keys.filter((key) => key.status === "active");
  const requests = accounts.reduce((sum, account) => sum + (Number(account.requests) || 0), 0);
  const tokens = accounts.reduce((sum, account) => sum + (Number(account.total_tokens) || 0), 0);
  document.querySelector("#metric-sites").textContent = compactNumber(activeAccounts.length);
  document.querySelector("#metric-keys").textContent = compactNumber(activeKeys.length);
  document.querySelector("#metric-requests").textContent = compactNumber(requests);
  document.querySelector("#metric-tokens").textContent = compactNumber(tokens);
  if (report.period) {
    document.querySelector("#metric-period").textContent = `${formatDate(report.period.start_at)} — ${formatDate(report.period.end_at)}`;
  }
  populateActivityAccounts(accounts);
  renderInventory();
}

function populateActivityAccounts(accounts) {
  const selected = activityAccountFilter.value;
  activityAccountFilter.replaceChildren(element("option", "", "All accounts"));
  activityAccountFilter.firstElementChild.value = "";
  accounts.forEach((account) => {
    const option = element("option", "", `${account.name} · ${account.id}`);
    option.value = account.id;
    activityAccountFilter.appendChild(option);
  });
  if (accounts.some((account) => account.id === selected)) activityAccountFilter.value = selected;
}

function renderInventory() {
  const filter = accountFilter.value;
  const query = accountSearch.value.trim().toLowerCase();
  const filtered = inventoryAccounts.filter((account) => {
    if (filter === "active" && account.status !== "active") return false;
    if (filter === "revoked" && account.status !== "revoked") return false;
    if (!query) return true;
    return [account.name, account.id, account.owner_type, account.owner_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query));
  });
  const viewLabel = filter === "active" ? "active" : filter === "revoked" ? "revoked" : "total";
  accountCount.textContent = `${filtered.length} ${viewLabel} ${filtered.length === 1 ? "account" : "accounts"}`;
  accountsList.replaceChildren();
  if (!filtered.length) {
    const message = inventoryAccounts.length
      ? "No accounts match this view."
      : "No partner sites yet. Create the first site key to begin.";
    accountsList.appendChild(element("div", "empty-state large-empty", message));
    return;
  }
  filtered.forEach((account) => accountsList.appendChild(renderAccount(account)));
}

async function loadAccounts() {
  const report = await requestJSON("/api/admin/accounts");
  renderReport(report);
  return report;
}

function activityQuery() {
  const params = new URLSearchParams({ limit: "25", offset: String(activityOffset) });
  const fields = [
    ["account_id", activityAccountFilter.value],
    ["status", activityStatusFilter.value],
    ["model_id", activityModelFilter.value.trim()],
    ["endpoint", activityEndpointFilter.value.trim()],
    ["key_id", activityKeyFilter.value.trim()],
    ["user_id", activityUserFilter.value.trim()],
  ];
  fields.forEach(([name, value]) => { if (value) params.set(name, value); });
  return params.toString();
}

async function loadActivity({ reset = false } = {}) {
  if (reset) activityOffset = 0;
  const report = await requestJSON(`/api/admin/usage?${activityQuery()}`);
  renderActivity(report.requests);
  const pagination = report.pagination || {};
  activityPrevious.disabled = Number(pagination.offset || 0) <= 0;
  activityNext.disabled = !pagination.has_more;
  activityPageLabel.textContent = pagination.has_more
    ? `Showing ${Number(pagination.offset || 0) + 1}–${Number(pagination.offset || 0) + (pagination.limit || 0)} and more`
    : `Showing ${report.requests.length} record${report.requests.length === 1 ? "" : "s"}`;
  return report;
}

async function loadAdmin() {
  try {
    const session = await requestJSON("/api/session");
    if (!session.user) {
      setHidden(adminApp, true);
      setHidden(adminAuth, false);
      const config = await shared.getAuthConfig().catch(() => ({}));
      if (config.telegram_login_enabled && config.bot_username) renderLoginWidget(config.bot_username);
      throw new Error("Telegram sign-in is required.");
    }
    await Promise.all([loadAccounts(), loadActivity(), loadAnalytics(), loadCapabilities()]);
    showAdmin(session.user);
  } catch (error) {
    if (error && error.status === 401 && error.message.toLowerCase().includes("admin")) {
      setHidden(adminApp, true);
      showAuthError("This Telegram account is signed in, but it is not authorized for AI administration.");
    } else if (error && error.status === 401) {
      const config = await requestJSON("/api/auth/config").catch(() => ({}));
      adminAuthCopy.textContent = "Sign in with any Telegram account to manage AuriX AI access.";
      if (config.telegram_login_enabled && config.bot_username) renderLoginWidget(config.bot_username);
      else showAuthError("Telegram sign-in is not configured.");
    } else {
      showAuthError(error instanceof Error ? error.message : "Admin console unavailable.");
    }
  }
}

let adminCheckInFlight = null;
async function refreshAdmin() {
  if (adminCheckInFlight) return adminCheckInFlight;
  adminCheckInFlight = loadAdmin().finally(() => { adminCheckInFlight = null; });
  return adminCheckInFlight;
}

function openPolicyDialog(account) {
  document.querySelector("#policy-account-id").value = account.id;
  document.querySelector("#policy-account-copy").textContent = `${account.name} · changes apply to new requests immediately.`;
  document.querySelector("#policy-name").value = account.name || "";
  document.querySelector("#policy-rpm").value = account.requests_per_minute || 60;
  document.querySelector("#policy-modes").value = (account.allowed_modes || ["*"]).join(", ");
  document.querySelector("#policy-models").value = (account.allowed_models || ["*"]).join(", ");
  document.querySelector("#policy-form-error").hidden = true;
  policyDialog.showModal();
}

function openIssueDialog(account) {
  issueAccountName = account.name || "Partner site";
  document.querySelector("#issue-account-id").value = account.id;
  document.querySelector("#issue-account-copy").textContent = `Issue a new credential for ${account.name}. The current keys will remain active.`;
  document.querySelector("#issue-label").value = "rotation";
  document.querySelector("#issue-form-error").hidden = true;
  issueDialog.showModal();
}

function showSecret(key, accountName) {
  currentSecret = key.token;
  secretValue.textContent = currentSecret;
  document.querySelector("#secret-copy").textContent = `${accountName || "Partner site"} · ${key.label}. This value cannot be recovered after this window is closed.`;
  copySecretButton.textContent = "Copy";
  secretDialog.showModal();
}

async function revokeKey(account, key) {
  const confirmed = window.confirm(`Revoke ${key.label || "this key"} for ${account.name}? The partner will stop authenticating immediately.`);
  if (!confirmed) return;
  try {
    await requestJSON(`/api/admin/keys/${encodeURIComponent(key.id)}/revoke`, { method: "POST" });
    showFlash(`Revoked ${key.label || "API key"}.`);
    await loadAccounts();
  } catch (error) {
    showFlash(error instanceof Error ? error.message : "Key revocation failed.", "error");
  }
}

document.querySelector("#create-account").addEventListener("click", () => {
  document.querySelector("#account-form-error").hidden = true;
  accountDialog.showModal();
});

document.querySelector("#refresh-accounts").addEventListener("click", async () => {
  try {
    await loadAccounts();
    showFlash("Inventory refreshed.");
  } catch (error) {
    showFlash(error instanceof Error ? error.message : "Refresh failed.", "error");
  }
});

document.querySelector("#refresh-activity").addEventListener("click", async () => {
  try {
    await loadActivity({ reset: true });
    showFlash("Activity refreshed.");
  } catch (error) {
    showFlash(error instanceof Error ? error.message : "Activity refresh failed.", "error");
  }
});

usageRange.addEventListener("change", () => {
  loadAnalytics().catch((error) => showFlash(error instanceof Error ? error.message : "Analytics refresh failed.", "error"));
});
refreshAnalyticsButton.addEventListener("click", async () => {
  try {
    await loadAnalytics();
    showFlash("Analytics refreshed.");
  } catch (error) {
    showFlash(error instanceof Error ? error.message : "Analytics refresh failed.", "error");
  }
});
refreshCapabilitiesButton.addEventListener("click", async () => {
  refreshCapabilitiesButton.disabled = true;
  try {
    await loadCapabilities();
    showFlash("Router capabilities refreshed.");
  } finally {
    refreshCapabilitiesButton.disabled = false;
  }
});
breakdownDimension.addEventListener("change", () => {
  if (analyticsReport) renderBreakdown(analyticsReport);
});
function selectChartMetric(metric) {
  chartMetric = metric;
  chartTokensButton.classList.toggle("active", metric === "tokens");
  chartRequestsButton.classList.toggle("active", metric === "requests");
  if (analyticsReport) renderUsageChart(analyticsReport.daily);
}
chartTokensButton.addEventListener("click", () => selectChartMetric("tokens"));
chartRequestsButton.addEventListener("click", () => selectChartMetric("requests"));

async function refreshFilteredActivity() {
  try {
    await loadActivity({ reset: true });
  } catch (error) {
    showFlash(error instanceof Error ? error.message : "Activity filter failed.", "error");
  }
}

[activityAccountFilter, activityStatusFilter].forEach((control) => {
  control.addEventListener("change", refreshFilteredActivity);
});
[activityModelFilter, activityEndpointFilter, activityKeyFilter, activityUserFilter].forEach((control) => {
  control.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      refreshFilteredActivity();
    }
  });
});
activityClearFilters.addEventListener("click", () => {
  activityAccountFilter.value = "";
  activityStatusFilter.value = "";
  activityModelFilter.value = "";
  activityEndpointFilter.value = "";
  activityKeyFilter.value = "";
  activityUserFilter.value = "";
  refreshFilteredActivity();
});
activityPrevious.addEventListener("click", () => {
  activityOffset = Math.max(0, activityOffset - 25);
  loadActivity().catch((error) => showFlash(error instanceof Error ? error.message : "Activity page failed.", "error"));
});
activityNext.addEventListener("click", () => {
  activityOffset += 25;
  loadActivity().catch((error) => showFlash(error instanceof Error ? error.message : "Activity page failed.", "error"));
});

accountFilter.addEventListener("change", renderInventory);
accountSearch.addEventListener("input", renderInventory);

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.querySelector("#account-form-error");
  errorNode.hidden = true;
  const submit = document.querySelector("#account-submit");
  submit.disabled = true;
  try {
    const expiryValue = document.querySelector("#account-expiry").value;
    const result = await requestJSON("/api/admin/accounts", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#account-name").value.trim(),
        requests_per_minute: Number(document.querySelector("#account-rpm").value),
        allowed_modes: document.querySelector("#account-modes").value,
        allowed_models: document.querySelector("#account-models").value,
        key_label: document.querySelector("#account-key-label").value.trim(),
        expires_in_days: expiryValue === "null" ? null : Number(expiryValue),
      }),
    });
    accountDialog.close();
    accountForm.reset();
    showSecret(result.key, result.account.name);
    await loadAccounts().catch(() => showFlash("Key created. Inventory refresh failed; refresh after saving the key.", "error"));
  } catch (error) {
    errorNode.textContent = error instanceof Error ? error.message : "Account creation failed.";
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

policyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.querySelector("#policy-form-error");
  errorNode.hidden = true;
  const submit = document.querySelector("#policy-submit");
  submit.disabled = true;
  try {
    const accountId = document.querySelector("#policy-account-id").value;
    await requestJSON(`/api/admin/accounts/${encodeURIComponent(accountId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: document.querySelector("#policy-name").value.trim(),
        requests_per_minute: Number(document.querySelector("#policy-rpm").value),
        allowed_modes: document.querySelector("#policy-modes").value,
        allowed_models: document.querySelector("#policy-models").value,
      }),
    });
    policyDialog.close();
    showFlash("Account policy updated.");
    await loadAccounts();
  } catch (error) {
    errorNode.textContent = error instanceof Error ? error.message : "Policy update failed.";
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

issueForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.querySelector("#issue-form-error");
  errorNode.hidden = true;
  const submit = document.querySelector("#issue-submit");
  submit.disabled = true;
  try {
    const expiryValue = document.querySelector("#issue-expiry").value;
    const result = await requestJSON("/api/admin/keys", {
      method: "POST",
      body: JSON.stringify({
        account_id: document.querySelector("#issue-account-id").value,
        label: document.querySelector("#issue-label").value.trim(),
        expires_in_days: expiryValue === "null" ? null : Number(expiryValue),
      }),
    });
    issueDialog.close();
    showSecret(result.key, issueAccountName);
    await loadAccounts().catch(() => showFlash("Key issued. Inventory refresh failed; refresh after saving the key.", "error"));
  } catch (error) {
    const message = error instanceof Error ? error.message : "Key issuance failed.";
    errorNode.textContent = message === "active account not found"
      ? "This site account was revoked. Refresh the inventory and choose an active account."
      : message;
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => {
    const dialog = document.querySelector(`#${button.dataset.closeDialog}`);
    dialog.close();
  });
});

secretDialog.addEventListener("close", () => {
  currentSecret = "";
  secretValue.textContent = "";
});

copySecretButton.addEventListener("click", async () => {
  if (!currentSecret) return;
  try {
    await navigator.clipboard.writeText(currentSecret);
    copySecretButton.textContent = "Copied";
  } catch (_error) {
    copySecretButton.textContent = "Copy failed";
  }
});

logoutButton.addEventListener("click", async () => {
  await shared.logout().catch(() => {});
  setHidden(adminApp, true);
  setHidden(adminAuth, false);
  adminAuthCopy.textContent = "Signed out. Sign in with any Telegram account to continue.";
  const config = await shared.getAuthConfig().catch(() => ({}));
  if (config.telegram_login_enabled && config.bot_username) renderLoginWidget(config.bot_username);
});

window.addEventListener("hashchange", updateActiveNav);
window.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshAdmin();
});
window.addEventListener("focus", () => refreshAdmin());
shared.subscribeSession(() => refreshAdmin());
updateActiveNav();
refreshAdmin();
