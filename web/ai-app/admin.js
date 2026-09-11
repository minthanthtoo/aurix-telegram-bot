const adminAuth = document.querySelector("#admin-auth");
const adminApp = document.querySelector("#admin-app");
const adminAuthCopy = document.querySelector("#admin-auth-copy");
const adminAuthError = document.querySelector("#admin-auth-error");
const telegramLogin = document.querySelector("#admin-telegram-login");
const adminUser = document.querySelector("#admin-user");
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
const flash = document.querySelector("#admin-flash");
const accountDialog = document.querySelector("#account-dialog");
const accountForm = document.querySelector("#account-form");
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
    detail.textContent = `${event.model_id || "model unavailable"} · ${event.endpoint || "endpoint unavailable"} · ${tokenText}`;
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
    await Promise.all([loadAccounts(), loadActivity()]);
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
