(() => {
  const dialog = document.querySelector("#account-dialog");
  if (!dialog) return;

  const status = document.querySelector("#account-status");
  const openButton = document.querySelector("#account-open");
  const accountRetry = document.querySelector("#account-retry");
  const logoutButton = document.querySelector("#account-logout");
  const linkButton = document.querySelector("#account-link");
  const accountTitle = document.querySelector("#account-title");
  const accountReference = document.querySelector("#account-reference");
  const staffLink = document.querySelector("#account-staff");
  const telegramSlot = document.querySelector("#telegram-login-slot");
  const googleSlot = document.querySelector("#google-login-slot");
  const profileDialog = document.querySelector("#auxiliary-dialog");
  const profileTitle = document.querySelector("#auxiliary-title");
  const profileCopy = document.querySelector("#auxiliary-copy");
  const profileContent = document.querySelector("#auxiliary-content");
  const profileStatus = document.querySelector("#auxiliary-status");
  const configUrl = "/api/v1/auth/config";
  const signInDestination = (() => {
    const intent = new URLSearchParams(window.location?.search || "").get("signin");
    return intent === "admin" ? "/game-store/admin/index.html?session=1"
      : intent === "staff" ? "/game-store/staff/" : "";
  })();
  let config = null;
  let account = null;
  let linkingCustomer = null;
  let authMode = null;
  let signingIn = false;
  let googleExpiresAt = 0;
  let googleScriptPromise = null;
  let telegramScriptPromise = null;
  let profileLogoutButton = null;
  let wallet = null;
  let balanceVisible = false;
  let sessionRevision = 0;
  let historyRevision = 0;
  let historyNavigation = 0;
  let pendingHistory = null;
  let pendingOrderId = null;
  let pendingWalletOpen = false;
  let orderDetailRevision = 0;
  let paymentStatusRefreshCleanup = null;
  const balance = document.querySelector("#wallet-balance");
  const balanceToggle = document.querySelector("#wallet-balance-toggle");
  const walletNote = document.querySelector("#wallet-account-note");
  const accountOverview = document.querySelector("#home-account-overview");
  const walletCard = document.querySelector("#home-wallet-card");
  const bottomProfile = document.querySelector("#bottom-profile");

  const orderStateLabels = {
    awaiting_payment: "Awaiting payment", paid_reserved: "Funds reserved",
    fulfillment_queued: "Delivery queued", submitted: "Sent to supplier", processing: "Delivering",
    delivered: "Delivered", failed: "Needs attention", unknown: "Checking delivery",
    reconciliation: "Checking delivery", review_refund: "Refund review", refunded: "Refund completed", cancelled: "Cancelled",
  };

  function orderStateLabel(order) {
    if (order.refund?.status === "pending") return "Bank refund pending";
    if (order.refund?.status === "completed") {
      return order.refund.route === "bank" ? "Refunded to original sender" : "Returned to wallet";
    }
    if (order.state === "awaiting_payment" && order.payment_review?.review_status === "in_review") return "Payment review pending";
    if (order.state === "awaiting_payment" && order.payment_review?.review_status === "not_verified") return "Receipt needs attention";
    return orderStateLabels[order.state] || "Status unavailable";
  }

  function updateOrderHistoryRow(list, order) {
    const row = [...list.querySelectorAll(".customer-history-row")]
      .find((item) => item.dataset.orderId === order.order_id);
    const heading = row?.querySelector(".customer-history-order-state");
    if (heading) heading.textContent = `${heading.dataset.productLabel} · ${orderStateLabel(order)}`;
  }

  function stopPaymentStatusRefresh() {
    paymentStatusRefreshCleanup?.();
    paymentStatusRefreshCleanup = null;
  }

  async function resumeOrderSignIn(orderId) {
    sessionRevision += 1;
    account = null;
    clearPrivateViews();
    await openAccount();
    pendingHistory = "orders";
    pendingOrderId = orderId;
  }

  function orderRefreshDelay(order) {
    if (order.refund?.status === "pending") return 15_000;
    if (order.refund?.status === "completed") return 0;
    if (["in_review", "refund_pending"].includes(order.payment_review?.review_status)) return 30_000;
    if (["delivered", "refunded", "cancelled"].includes(order.state)) return 0;
    if (order.state === "awaiting_payment") return 30_000;
    return 15_000;
  }

  function startPaymentStatusRefresh(orderId, list, current, pageStatus, order, detailRevision) {
    const delay = orderRefreshDelay(order);
    if (!delay) return;
    let refreshing = false;
    let timer;
    let expiryTimer;
    const controller = new AbortController();
    const isCurrent = () => !controller.signal.aborted && current() && orderDetailRevision === detailRevision;
    const updateExpiry = () => {
      if (!isCurrent()) return;
      const remaining = Date.parse(order.payment?.expires_at) - Date.now();
      const panel = list.querySelector(".customer-order-detail");
      if (order.state !== "awaiting_payment" || order.payment?.expired || !Number.isFinite(remaining)) return;
      if (remaining <= 0) {
        panel?.querySelector(".customer-payment-instructions")?.remove();
        const expiry = panel?.querySelector(".customer-payment-expiry");
        if (expiry) expiry.textContent = "These saved instructions have expired. Do not transfer using them.";
        return;
      }
      const countdown = panel?.querySelector(".customer-payment-countdown");
      if (countdown) countdown.textContent = `${Math.ceil(remaining / 60_000)} min remaining`;
      expiryTimer = window.setTimeout(updateExpiry, Math.min(1000, remaining + 1));
    };
    const schedule = () => {
      window.clearTimeout(timer);
      let next = delay;
      const expiresAt = Date.parse(order.payment?.expires_at);
      if (order.state === "awaiting_payment" && !order.payment?.expired && expiresAt > Date.now()) {
        next = Math.min(next, expiresAt - Date.now() + 20);
      }
      timer = window.setTimeout(() => void refresh(), next);
    };
    const refresh = async () => {
      if (refreshing || !isCurrent()) return;
      if (document.visibilityState === "hidden") { schedule(); return; }
      refreshing = true;
      try {
        const latest = await requestJson(`/api/v1/orders/${encodeURIComponent(orderId)}`, { signal: controller.signal });
        if (!isCurrent()) return;
        updateOrderHistoryRow(list, latest);
        if (JSON.stringify(latest) !== JSON.stringify(order)) {
          await showOrderDetails(orderId, list, current, pageStatus, latest);
          return;
        }
        pageStatus.textContent = "Order status is up to date. Updates appear automatically.";
      } catch (error) {
        if (!isCurrent()) return;
        if (error.status === 401) {
          await resumeOrderSignIn(orderId);
          return;
        }
        if (error.status === 404) {
          stopPaymentStatusRefresh();
          list.querySelectorAll(".customer-order-detail").forEach((panel) => panel.remove());
          pageStatus.textContent = "This order is unavailable in your account. Refresh History or contact support.";
          return;
        }
        pageStatus.textContent = "Order status refresh is delayed. We’ll retry automatically; do not transfer again.";
      } finally {
        refreshing = false;
        if (isCurrent()) schedule();
      }
    };
    const refreshWhenAvailable = () => {
      if (document.visibilityState !== "hidden") { window.clearTimeout(expiryTimer); updateExpiry(); void refresh(); }
    };
    document.addEventListener("visibilitychange", refreshWhenAvailable);
    window.addEventListener("focus", refreshWhenAvailable);
    window.addEventListener("online", refreshWhenAvailable);
    paymentStatusRefreshCleanup = () => {
      controller.abort();
      window.clearTimeout(timer);
      window.clearTimeout(expiryTimer);
      document.removeEventListener("visibilitychange", refreshWhenAvailable);
      window.removeEventListener("focus", refreshWhenAvailable);
      window.removeEventListener("online", refreshWhenAvailable);
    };
    schedule();
    updateExpiry();
  }

  async function requestJson(url, { timeoutMs = 12_000, ...options } = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof body.error === "string" ? body.error : "Request failed");
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function requestAuthJson(url) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await requestJson(url, { timeoutMs: 5_000 });
      } catch (error) {
        const statusCode = Number.isInteger(error?.status) ? error.status : 0;
        const retryable = !statusCode || [408, 500, 502, 503, 504].includes(statusCode);
        if (attempt > 0 || !retryable) throw error;
        await new Promise((resolve) => window.setTimeout(resolve, 300));
      }
    }
    throw new Error("Account service is temporarily unavailable.");
  }

  async function loadAuthConfig() {
    const value = await requestAuthJson(configUrl);
    if (!value || typeof value.providers !== "object" || value.providers === null || Array.isArray(value.providers)) {
      throw new Error("Authentication configuration is invalid.");
    }
    config = value;
    return value;
  }

  async function postJson(url, payload, timeoutMs = 12_000) {
    return requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs,
    });
  }

  function money(amount, currency) {
    if (!Number.isSafeInteger(amount) || amount < 0 || !/^[A-Z]{3}$/.test(currency || "")) return "—";
    try {
      const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
      return `${(amount / 10 ** digits).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${currency}`;
    } catch { return "—"; }
  }

  function paintWallet() {
    accountOverview.hidden = !account;
    walletCard.hidden = !account;
    balance.textContent = wallet ? (balanceVisible ? money(wallet.available_minor, wallet.currency) : `•••• ${wallet.currency}`) : "—";
    balanceToggle.disabled = !wallet;
    balanceToggle.textContent = balanceVisible ? "Hide" : "Show";
    balanceToggle.setAttribute("aria-pressed", String(balanceVisible));
  }

  function clearPrivateViews() {
    if (accountReference) accountReference.hidden = true;
    if (staffLink) staffLink.hidden = true;
    stopPaymentStatusRefresh();
    orderDetailRevision += 1;
    historyRevision += 1;
    wallet = null;
    balanceVisible = false;
    paintWallet();
    const page = document.querySelector("#auxiliary-dialog");
    if (page.dataset.surface === "customer_history") {
      document.querySelector("#auxiliary-content").replaceChildren();
      document.querySelector("#auxiliary-status").textContent = "";
      page.close();
    }
  }

  async function loadWallet(revision) {
    const owner = account?.customer_id;
    if (!owner) { walletNote.textContent = "Sign in to see your available balance."; return; }
    walletNote.textContent = "Updating your balance…";
    try {
      const result = await requestJson("/api/v1/wallet");
      if (revision !== sessionRevision || account?.customer_id !== owner) return;
      const value = result.wallet;
      if (!value || !Number.isSafeInteger(value.available_minor) || value.available_minor < 0
          || !Number.isSafeInteger(value.reserved_minor) || value.reserved_minor < 0
          || money(value.available_minor, value.currency) === "—") throw new Error("Invalid wallet response");
      wallet = value;
      paintWallet();
      walletNote.textContent = value.reserved_minor
        ? `${money(value.reserved_minor, value.currency)} held for orders · balance shown is available`
        : "Available to you · private account balance";
    } catch {
      if (revision !== sessionRevision || account?.customer_id !== owner) return;
      wallet = null;
      paintWallet();
      walletNote.textContent = "Balance unavailable. Reopen Wallet to retry.";
    }
  }

  async function loadAccount() {
    const revision = ++sessionRevision;
    const session = await requestAuthJson("/api/v1/auth/session");
    if (!session || typeof session.authenticated !== "boolean"
        || (session.authenticated && typeof session.user?.customer_id !== "string")
        || (!session.authenticated && session.user != null)) {
      throw new Error("Customer session response is invalid.");
    }
    if (revision !== sessionRevision) return account;
    const next = session.authenticated ? session.user : null;
    if (next?.customer_id !== account?.customer_id || !next) {
      linkingCustomer = null;
      clearPrivateViews();
    }
    account = next;
    paintWallet();
    if (accountReference) {
      accountReference.hidden = true;
      accountReference.textContent = "";
    }
    if (staffLink) staffLink.hidden = true;
    openButton.textContent = account ? "Account" : "Sign in";
    openButton.setAttribute("aria-label", account ? "Open account settings" : "Sign in");
    if (bottomProfile) {
      bottomProfile.setAttribute("aria-label", account ? "Open profile" : "Sign in");
      const label = bottomProfile.querySelector("small");
      if (label) label.textContent = "Account";
    }
    logoutButton.hidden = true;
    void loadWallet(revision);
    return account;
  }

  function linked(provider) {
    return Array.isArray(account?.providers) && account.providers.includes(provider);
  }

  function resetProviderMode() {
    authMode = null;
    googleExpiresAt = 0;
    telegramSlot.replaceChildren();
    googleSlot.replaceChildren();
    telegramSlot.removeAttribute("aria-busy");
    googleSlot.removeAttribute("aria-busy");
  }

  function showProviderLoading(slot, label) {
    const message = document.createElement("span");
    message.className = "account-provider-message";
    message.textContent = `Loading ${label} sign-in…`;
    slot.setAttribute("aria-busy", "true");
    slot.replaceChildren(message);
  }

  function showProviderError(slot, label, retry) {
    const message = document.createElement("span");
    message.className = "account-provider-message account-provider-message--error";
    message.textContent = `${label} sign-in is unavailable.`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "account-provider-retry";
    button.textContent = "Retry";
    button.addEventListener("click", retry, { once: true });
    slot.setAttribute("aria-busy", "false");
    slot.replaceChildren(message, button);
  }

  function loadGoogleScript() {
    if (window.google?.accounts?.id) return Promise.resolve();
    if (googleScriptPromise) return googleScriptPromise;
    googleScriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://accounts.google.com/gsi/client";
      script.async = true;
      script.onload = resolve;
      script.onerror = () => {
        googleScriptPromise = null;
        script.remove();
        reject(new Error("Sign-in provider could not be loaded"));
      };
      document.head.append(script);
    });
    return googleScriptPromise;
  }

  async function finishSignIn(provider, proof, mode) {
    if (signingIn || !dialog.open || !mode || mode !== authMode) return;
    signingIn = true;
    if (linkButton) linkButton.disabled = true;
    logoutButton.disabled = true;
    status.textContent = mode.customerId ? "Verifying link…" : "Verifying sign-in…";
    try {
      if (mode.customerId) {
        await postJson(`/api/v1/auth/link/${provider}`, { customer_id: mode.customerId, proof });
      } else {
        await postJson(`/api/v1/auth/${provider}`, proof);
      }
      linkingCustomer = null;
      await loadAccount();
      if (signInDestination) {
        window.location.replace(signInDestination);
        return;
      }
      await renderProviders();
      if (pendingWalletOpen) {
        pendingWalletOpen = false;
        dialog.close();
        window.AurixStore?.showWallet();
      } else if (pendingHistory) {
        const target = pendingHistory;
        const targetOrderId = pendingOrderId;
        dialog.close();
        await renderHistory(target, 0, targetOrderId);
      } else {
        dialog.close();
        await openProfile();
      }
    } catch (error) {
      if (mode !== authMode) return;
      if (mode.customerId && error.status === 409) {
        linkingCustomer = null;
        await renderProviders();
      }
      status.textContent = error.message || "Sign-in could not be completed.";
      if (provider === "google" && mode === authMode) {
        googleExpiresAt = 0;
        googleSlot.replaceChildren();
        googleSlot.removeAttribute("aria-busy");
        void renderGoogle();
      }
    } finally {
      signingIn = false;
      if (linkButton) linkButton.disabled = false;
      logoutButton.disabled = false;
    }
  }

  async function renderTelegram() {
    const mode = authMode;
    const provider = config?.providers?.telegram;
    if (!mode || !provider?.enabled || linked("telegram")) {
      telegramSlot.replaceChildren();
      telegramSlot.removeAttribute("aria-busy");
      return null;
    }
    if (telegramSlot.firstChild && telegramSlot.getAttribute("aria-busy") !== "true") return null;
    showProviderLoading(telegramSlot, "Telegram");
    try {
      if (!telegramScriptPromise) {
        const script = document.createElement("script");
        script.src = "https://telegram.org/js/telegram-widget.js?22";
        script.async = true;
        telegramScriptPromise = new Promise((resolve, reject) => {
          script.onload = resolve;
          script.onerror = () => { script.remove(); reject(new Error("Telegram sign-in could not be loaded")); };
          document.head.append(script);
        });
      }
      await telegramScriptPromise;
      if (mode !== authMode) return;
      if (!window.Telegram?.Login?.auth || !/^\d+$/.test(provider.bot_id || "")) throw new Error("Telegram sign-in is unavailable");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "guide-primary-action";
      button.textContent = "Continue with Telegram";
      button.addEventListener("click", () => {
        if (mode !== authMode || signingIn || !dialog.open) return;
        // The widget's data-onauth string callback requires unsafe-eval; the SDK callback does not.
        window.Telegram.Login.auth({ bot_id: provider.bot_id }, (user) => {
          if (user) void finishSignIn("telegram", user, mode);
        });
      });
      telegramSlot.setAttribute("aria-busy", "false");
      telegramSlot.replaceChildren(button);
      return null;
    } catch {
      if (mode !== authMode) return null;
      telegramScriptPromise = null;
      showProviderError(telegramSlot, "Telegram", () => {
        telegramSlot.replaceChildren();
        void renderTelegram();
      });
      return "Telegram";
    }
  }

  async function renderGoogle() {
    const mode = authMode;
    const provider = config?.providers?.google;
    if (!mode || !provider?.enabled || linked("google")) {
      googleSlot.replaceChildren();
      googleSlot.removeAttribute("aria-busy");
      return null;
    }
    if (googleSlot.getAttribute("aria-busy") === "true") return null;
    if (googleSlot.firstChild && !googleSlot.querySelector(".account-provider-retry")) return null;
    showProviderLoading(googleSlot, "Google");
    try {
      await loadGoogleScript();
      if (mode !== authMode) return;
      if (!window.google?.accounts?.id) throw new Error("Google sign-in could not be loaded");
      if (Date.now() >= googleExpiresAt) {
        const challenge = await postJson("/api/v1/auth/google/challenge", {});
        if (mode !== authMode) return;
        googleExpiresAt = Date.now() + 9 * 60 * 1000;
        window.google.accounts.id.initialize({
          client_id: provider.client_id,
          nonce: challenge.nonce,
          callback: (credential) => {
            void finishSignIn("google", { credential: credential?.credential || "", nonce: challenge.nonce }, mode);
          },
        });
      }
      window.google.accounts.id.renderButton(googleSlot, {
        type: "standard", theme: "outline", size: "large", shape: "pill",
        text: account ? "continue_with" : "signin_with",
        width: Math.max(180, Math.min(400, Math.floor(googleSlot.getBoundingClientRect().width || 400))),
      });
      googleSlot.setAttribute("aria-busy", "false");
      return null;
    } catch {
      if (mode !== authMode) return null;
      showProviderError(googleSlot, "Google", () => {
        googleExpiresAt = 0;
        void renderGoogle();
      });
      return "Google";
    }
  }

  async function renderProviders() {
    if (!config) return;
    const methods = ["telegram", "google"].filter((provider) => config.providers?.[provider]?.enabled);
    const canLink = Boolean(account && methods.some((provider) => !linked(provider)));
    const isLinking = canLink && linkingCustomer === account.customer_id;
    const nextMode = isLinking ? account.customer_id : !account && methods.length ? "" : null;
    if (!authMode || authMode.customerId !== nextMode) {
      resetProviderMode();
      if (nextMode !== null) authMode = { customerId: nextMode };
    }
    if (linkButton) {
      linkButton.hidden = !canLink;
      linkButton.textContent = isLinking ? "Cancel linking" : "Link a sign-in method";
    }
    if (accountTitle) accountTitle.textContent = account ? "Your Game Store account" : "Sign in or create an account";
    if (isLinking) status.textContent = "Verify your other login to link it to this wallet and order history.";
    else if (account) {
      const names = ["telegram", "google"].filter(linked).map((provider) => provider === "telegram" ? "Telegram" : "Google");
      status.textContent = names.length ? `Linked sign-in methods: ${names.join(" and ")}.` : "Signed in.";
    } else {
      status.textContent = methods.length
        ? "Loading secure sign-in options…"
        : "Sign-in methods are unavailable on this server.";
    }
    const failed = (await Promise.all([renderTelegram(), renderGoogle()])).filter(Boolean);
    if (!dialog.open || authMode?.customerId !== nextMode) return;
    if (!account && !failed.length && methods.length) {
      status.textContent = "Choose a sign-in method. Your first sign-in creates your AuriX account.";
    } else if (failed.length) {
      const label = failed.join(" and ");
      status.textContent = account
        ? `${label} sign-in is unavailable. Your linked methods still work.`
        : `${label} sign-in is unavailable. Retry it or use another available method.`;
    }
  }

  async function openAccount() {
    if (!dialog.open) dialog.showModal();
    if (accountRetry) {
      accountRetry.hidden = true;
      accountRetry.disabled = true;
    }
    status.textContent = "Checking account connection…";
    try {
      if (!config) await loadAuthConfig();
      await loadAccount();
    } catch {
      status.textContent = accountServiceUnavailableMessage();
      if (accountRetry) accountRetry.hidden = false;
      return;
    } finally {
      if (accountRetry) accountRetry.disabled = false;
    }
    try {
      await renderProviders();
    } catch {
      status.textContent = "Sign-in options could not be refreshed. Retry the connection.";
      if (accountRetry) accountRetry.hidden = false;
    }
  }

  function accountServiceUnavailableMessage() {
    return account
      ? "Your account could not be refreshed. Your current account view is kept; retry to refresh."
      : "Account service is temporarily unavailable. Check your connection and retry.";
  }

  function profileNode(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderProfile(leaderboardProfile = { display_name: "", opted_in: false }, leaderboardError = "") {
    window.AurixStore?.resetAuxiliary();
    profileDialog.dataset.surface = "customer_profile";
    profileTitle.textContent = "Your profile";
    profileCopy.textContent = "Manage your account ID, sign-in connections, and access.";
    profileStatus.textContent = "";

    const profile = profileNode("section", "customer-profile");
    const hero = profileNode("section", "customer-profile__hero");
    const brand = profileNode("span", "customer-profile__brand");
    const brandIcon = document.createElement("img");
    brandIcon.src = "/game-store/icon.svg";
    brandIcon.alt = "";
    brandIcon.setAttribute("aria-hidden", "true");
    brand.append(brandIcon);
    const heroCopy = profileNode("div", "customer-profile__hero-copy");
    heroCopy.append(
      profileNode("p", "eyebrow", "AURIX ACCOUNT"),
      profileNode("h3", "", "Store account"),
      profileNode("p", "", "One account for the AuriX Game Store."),
    );
    hero.append(brand, heroCopy, profileNode("span", "customer-profile__active", "Active"));
    profile.append(hero);

    const identity = profileNode("section", "customer-profile__identity");
    identity.append(
      profileNode("span", "customer-profile__eyebrow", "ACCOUNT ID"),
      profileNode("code", "customer-profile__id", account.customer_id),
    );
    profile.append(identity);

    const methods = ["google", "telegram"]
      .filter((provider) => config?.providers?.[provider]?.enabled || linked(provider));
    const methodSection = profileNode("section", "customer-profile__methods");
    const methodHeading = profileNode("div", "customer-profile__section-heading");
    methodHeading.append(
      profileNode("h3", "", "Sign-in methods"),
      profileNode("span", "", `${methods.filter(linked).length} linked`),
    );
    const methodList = profileNode("div", "customer-profile__method-list");
    for (const provider of methods) {
      const isLinked = linked(provider);
      const row = profileNode("div", `customer-profile__method${isLinked ? " is-linked" : ""}`);
      const icon = profileNode("span", `customer-profile__method-icon customer-profile__method-icon--${provider}`);
      const image = document.createElement("img");
      image.src = `/game-store/assets/providers/${provider === "google" ? "google-g.png" : "telegram-logo.png"}`;
      image.alt = "";
      image.setAttribute("aria-hidden", "true");
      icon.append(image);
      const methodCopy = profileNode("span", "customer-profile__method-copy");
      methodCopy.append(
        profileNode("strong", "", provider === "google" ? "Google" : "Telegram"),
        profileNode("small", "", isLinked ? "Connected to this account" : "Ready to link"),
      );
      row.append(icon, methodCopy, profileNode("span", "customer-profile__method-state", isLinked ? "Linked" : "Not linked"));
      methodList.append(row);
    }
    if (!methods.length) methodList.append(profileNode("p", "customer-profile__empty", "No sign-in method is currently available."));
    methodSection.append(methodHeading, methodList);
    profile.append(methodSection);

    profile.append(profileNode("p", "customer-profile__note",
      "Link Google and Telegram to use either sign-in for this same account. Only link accounts you control."));

    const leaderboard = profileNode("section", "customer-profile__leaderboard");
    leaderboard.append(profileNode("h3", "", "Most active buyers leaderboard"));
    leaderboard.append(profileNode("p", "customer-profile__note",
      "Joining is optional. Only your chosen nickname and delivered top-up count from the last 30 days are public; order IDs, game IDs, and payment amounts stay private. Never use your real name, email, phone, or player ID."));
    const leaderboardForm = document.createElement("form");
    leaderboardForm.className = "customer-profile__leaderboard-form";
    const optInLabel = profileNode("label", "customer-profile__leaderboard-optin");
    const optIn = document.createElement("input");
    optIn.type = "checkbox";
    optIn.checked = leaderboardProfile.opted_in === true;
    const optInCopy = profileNode("span", "", "Show my chosen name on Most active buyers");
    optInLabel.append(optIn, optInCopy);
    const nameLabel = profileNode("label", "", "Public display name");
    const displayName = document.createElement("input");
    displayName.type = "text";
    displayName.minLength = 2;
    displayName.maxLength = 24;
    displayName.autocomplete = "nickname";
    displayName.placeholder = "Game nickname";
    displayName.value = leaderboardProfile.display_name || "";
    displayName.disabled = !optIn.checked;
    displayName.required = optIn.checked;
    nameLabel.append(displayName);
    const preferenceStatus = profileNode("p", "customer-profile__leaderboard-status", leaderboardError);
    preferenceStatus.setAttribute("role", "status");
    const savePreference = profileNode("button", "guide-primary-action", "Save leaderboard settings");
    savePreference.type = "submit";
    optIn.addEventListener("change", () => {
      displayName.disabled = !optIn.checked;
      displayName.required = optIn.checked;
      if (!optIn.checked) preferenceStatus.textContent = "Your name will be removed from the public ranking.";
    });
    leaderboardForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      savePreference.disabled = true;
      preferenceStatus.textContent = "Saving…";
      try {
        const saved = await requestJson("/api/v1/leaderboard/profile", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name: displayName.value.trim(), opted_in: optIn.checked }),
        });
        optIn.checked = saved.opted_in === true;
        displayName.value = saved.display_name || "";
        displayName.disabled = !optIn.checked;
        displayName.required = optIn.checked;
        preferenceStatus.textContent = optIn.checked
          ? "Settings saved. Your nickname appears while you have delivered top-ups in the last 30 days."
          : "You’re opted out and no longer appear on the public board.";
      } catch (error) {
        preferenceStatus.textContent = error.message || "Leaderboard settings could not be saved.";
      } finally { savePreference.disabled = false; }
    });
    leaderboardForm.append(optInLabel, nameLabel, savePreference, preferenceStatus);
    leaderboard.append(leaderboardForm);
    profile.append(leaderboard);

    const actions = profileNode("div", "customer-profile__actions");
    if (methods.some((provider) => config?.providers?.[provider]?.enabled && !linked(provider))) {
      const link = profileNode("button", "guide-primary-action", "Link another sign-in method");
      link.type = "button";
      link.addEventListener("click", () => {
        if (!account || signingIn) return;
        linkingCustomer = account.customer_id;
        profileDialog.close();
        void openAccount().then(() => window.AurixStore?.setTab("profile"));
      });
      actions.append(link);
    }
    if (account.staff_role) {
      const admin = profileNode("a", "customer-profile__admin", "Store administration");
      admin.href = "/game-store/staff/";
      actions.append(admin);
    }
    profileLogoutButton = profileNode("button", "account-logout", "Sign out");
    profileLogoutButton.type = "button";
    profileLogoutButton.disabled = signingIn;
    profileLogoutButton.addEventListener("click", () => void signOut(true));
    actions.append(profileLogoutButton);
    profile.append(actions);
    profileContent.replaceChildren(profile);
  }

  async function openProfile() {
    window.AurixStore?.setTab("profile");
    try {
      if (!config) await loadAuthConfig();
      await loadAccount();
    } catch {
      await openAccount();
      return;
    }
    if (!account) {
      await openAccount();
      return;
    }
    let leaderboardProfile = { display_name: "", opted_in: false };
    let leaderboardError = "";
    try { leaderboardProfile = await requestJson("/api/v1/leaderboard/profile"); }
    catch { leaderboardError = "Leaderboard settings could not be loaded. You can retry by reopening your profile."; }
    renderProfile(leaderboardProfile, leaderboardError);
    if (!profileDialog.open) profileDialog.showModal();
  }

  async function renderHistory(kind, offset = 0, selectedOrderId = null) {
    window.AurixStore?.setTab(kind === "topups" ? "wallet" : "history");
    stopPaymentStatusRefresh();
    orderDetailRevision += 1;
    const navigation = ++historyNavigation;
    const existingPage = document.querySelector("#auxiliary-dialog");
    if (!offset && existingPage.dataset.surface === "customer_history") {
      document.querySelector("#auxiliary-content").replaceChildren();
      document.querySelector("#auxiliary-status").textContent = "Loading your records…";
    }
    try { await loadAccount(); }
    catch {
      clearPrivateViews();
      await openAccount();
      return;
    }
    if (navigation !== historyNavigation) return;
    if (!account) {
      await openAccount();
      pendingHistory = kind;
      pendingOrderId = selectedOrderId;
      return;
    }
    const owner = account.customer_id;
    const auxiliaryDialog = document.querySelector("#auxiliary-dialog");
    const title = document.querySelector("#auxiliary-title");
    const copy = document.querySelector("#auxiliary-copy");
    const content = document.querySelector("#auxiliary-content");
    const pageStatus = document.querySelector("#auxiliary-status");
    const revision = ++historyRevision;
    const orders = kind === "orders";
    const current = () => navigation === historyNavigation && revision === historyRevision && account?.customer_id === owner
      && auxiliaryDialog.open && auxiliaryDialog.dataset.surface === "customer_history";
    title.textContent = selectedOrderId ? "Your order" : "History";
    copy.textContent = selectedOrderId ? "Payment, receipt and delivery status" : "Orders and verified wallet credits, private to your account.";
    auxiliaryDialog.dataset.surface = "customer_history";
    if (!offset) window.AurixStore?.resetAuxiliary();
    delete auxiliaryDialog.dataset.guidePage;
    document.querySelector("#auxiliary-header-actions").replaceChildren();
    if (selectedOrderId) {
      const allOrders = document.createElement("button");
      allOrders.type = "button";
      allOrders.className = "history-order-open";
      allOrders.textContent = "All orders";
      allOrders.addEventListener("click", () => void renderHistory("orders"));
      document.querySelector("#auxiliary-header-actions").append(allOrders);
    }
    let list;
    if (!offset) {
      window.AurixStore?.setTab(orders ? "history" : "wallet");
      content.replaceChildren();
      const tabs = document.createElement("div");
      tabs.className = "history-tabs";
      tabs.setAttribute("aria-label", "History type");
      for (const [target, label] of [["orders", "Orders"], ["topups", "Wallet credits"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.setAttribute("aria-pressed", String(kind === target));
        button.addEventListener("click", () => void renderHistory(target));
        tabs.append(button);
      }
      list = document.createElement("div");
      list.id = "customer-history-list";
      if (!selectedOrderId) content.append(tabs);
      content.append(list);
    } else {
      list = document.querySelector("#customer-history-list");
      list?.querySelectorAll(".history-more, .history-retry").forEach((button) => button.remove());
    }
    pageStatus.textContent = "Loading your records…";
    if (!auxiliaryDialog.open) auxiliaryDialog.showModal();
    if (orders && selectedOrderId) {
      await showOrderDetails(selectedOrderId, list, current, pageStatus);
      return;
    }
    try {
      const result = await requestJson(orders ? "/api/v1/orders" : `/api/v1/top-up-history?limit=50&offset=${offset}`);
      if (!current() || !list) return;
      const entries = orders ? result.orders : result.entries;
      if (!Array.isArray(entries)) throw new Error("Invalid history response");
      if (!entries.length && !offset) {
        const empty = document.createElement("p");
        empty.className = "history-empty";
        empty.setAttribute("role", "status");
        empty.textContent = orders ? "No orders yet. Completed and pending orders will appear here." : "No verified wallet credits yet.";
        list.append(empty);
      }
      for (const entry of (orders ? [...entries].reverse() : entries)) {
        const row = document.createElement("article");
        row.className = "customer-history-row";
        const heading = document.createElement("strong");
        const productLabel = String(entry.product_slug || "Game order").replaceAll("-", " ");
        heading.textContent = orders ? `${productLabel} · ${orderStateLabel(entry)}` : "Wallet credit · posted";
        if (orders) {
          row.dataset.orderId = String(entry.order_id || "");
          heading.className = "customer-history-order-state";
          heading.dataset.productLabel = productLabel;
        }
        const detail = document.createElement("small");
        detail.textContent = money(entry.amount_minor, entry.currency);
        row.append(heading, detail);
        const reference = document.createElement(orders ? "small" : "time");
        if (orders) reference.textContent = `Order ${entry.order_id || "—"}`;
        else {
          const date = new Date(entry.occurred_at);
          reference.textContent = Number.isNaN(date.getTime()) ? "Date unavailable"
            : `${new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Yangon" }).format(date)} MMT`;
          if (!Number.isNaN(date.getTime())) reference.dateTime = date.toISOString();
        }
        row.append(reference);
        if (orders) {
          const view = document.createElement("button");
          view.type = "button";
          view.className = "history-order-open";
          view.textContent = "View order";
          view.setAttribute("aria-label", `View order ${entry.order_id || "details"}`);
          view.addEventListener("click", () => void showOrderDetails(entry.order_id, list, current, pageStatus));
          row.append(view);
        }
        list.append(row);
      }
      pageStatus.textContent = entries.length || offset
        ? (orders ? "Private records · open an order to follow its payment status" : "Private records · open History to refresh wallet credits")
        : "";
      if (!orders && entries.length === 50) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "history-more";
        more.textContent = "Load more top-ups";
        more.addEventListener("click", () => { more.disabled = true; void renderHistory(kind, offset + entries.length); });
        list.append(more);
      }
    } catch (error) {
      if (!current()) return;
      if (error.status === 401) {
        account = null;
        clearPrivateViews();
        await openAccount();
        return;
      }
      pageStatus.textContent = "Your history could not be loaded. Please try again.";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "history-retry";
      retry.textContent = "Try again";
      retry.addEventListener("click", () => void renderHistory(kind, offset));
      list?.append(retry);
    }
  }

  async function showOrderDetails(orderId, list, current, pageStatus, savedOrder = null) {
    stopPaymentStatusRefresh();
    const detailRevision = ++orderDetailRevision;
    const isCurrent = () => current() && orderDetailRevision === detailRevision;
    if (typeof orderId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(orderId) || !isCurrent()) return;
    const oldPanel = list.querySelector(".customer-order-detail");
    const oldForm = oldPanel?.dataset.orderId === orderId ? oldPanel.querySelector(".payment-evidence-form") : null;
    const draft = oldForm ? {
      revision: oldForm.dataset.revision,
      reference: oldForm.querySelector('input[type="text"]').value,
      fileInput: oldForm.querySelector('input[type="file"]'),
      focused: oldForm.contains(document.activeElement) ? document.activeElement.type : null,
    } : oldPanel?.dataset.orderId === orderId ? oldPanel.receiptDraft : null;
    list.querySelectorAll(".customer-order-detail").forEach((node) => node.remove());
    const panel = document.createElement("section");
    panel.className = "customer-order-detail";
    panel.dataset.orderId = orderId;
    panel.receiptDraft = draft;
    panel.setAttribute("aria-live", "polite");
    const heading = document.createElement("h3");
    heading.textContent = `Order ${orderId}`;
    const status = document.createElement("p");
    status.textContent = "Loading saved order and payment details…";
    panel.append(heading, status);
    list.append(panel);
    pageStatus.textContent = "Loading selected order…";
    try {
      const order = savedOrder || await requestJson(`/api/v1/orders/${encodeURIComponent(orderId)}`);
      if (!isCurrent()) return;
      updateOrderHistoryRow(list, order);
      panel.replaceChildren(heading);
      const orderState = document.createElement("p");
      orderState.className = "customer-order-state";
      orderState.dataset.state = order.refund?.status === "completed" ? "refunded" : order.state;
      orderState.textContent = `Status: ${orderStateLabel(order)}`;
      panel.append(orderState);
      const amount = document.createElement("p");
      amount.className = "customer-order-total";
      amount.textContent = `Order total: ${money(order.amount_minor, order.currency)}`;
      panel.append(amount);
      if (order.payment) {
        const payment = order.payment;
        const method = document.createElement("p");
        method.textContent = `Payment method: ${({ kpay: "KPay", wavepay: "WavePay", aya: "AYA Pay", uab: "UABPay", cb: "CB Pay" })[payment.rail] || payment.rail || "Saved"}`;
        panel.append(method);
        const locallyExpired = !Number.isFinite(Date.parse(payment.expires_at)) || Date.parse(payment.expires_at) <= Date.now();
        if ((payment.expired || locallyExpired) && order.state === "awaiting_payment") {
          const expired = document.createElement("p");
          expired.className = "customer-payment-expiry";
          expired.textContent = "These saved instructions have expired. Do not transfer using them.";
          panel.append(expired);
        } else if (order.state === "awaiting_payment" && order.payment_review?.revision > 0) {
          const submitted = document.createElement("p");
          submitted.textContent = "Receipt saved. Do not transfer again.";
          panel.append(submitted);
        } else if (order.state === "awaiting_payment" && payment.instructions_unavailable) {
          const unavailable = document.createElement("p");
          unavailable.textContent = "Payment instructions are temporarily unavailable. If you already transferred, keep your receipt and do not pay again.";
          panel.append(unavailable);
        } else if (order.state === "awaiting_payment") {
          const instructions = document.createElement("div");
          instructions.className = "customer-payment-instructions";
          for (const [label, value] of [["Account name", payment.account_name], ["Account", payment.account]]) {
            if (typeof value !== "string" || !value.trim()) continue;
            const line = document.createElement("p");
            line.textContent = `${label}: ${value}`;
            instructions.append(line);
            if (label === "Account") {
              const copy = document.createElement("button");
              copy.type = "button";
              copy.className = "history-order-open";
              copy.textContent = "Copy account";
              copy.addEventListener("click", async () => {
                try {
                  await navigator.clipboard.writeText(value);
                  if (isCurrent()) copy.textContent = "Copied";
                } catch { if (isCurrent()) copy.textContent = "Copy failed"; }
              });
              instructions.append(copy);
            }
          }
          if (typeof payment.qr_asset === "string") {
            const qr = document.createElement("img");
            qr.className = "payment-receiver-qr";
            qr.alt = `${({ kpay: "KPay", wavepay: "WavePay", aya: "AYA Pay", uab: "UABPay", cb: "CB Pay" })[payment.rail] || payment.rail} receiver QR code`;
            qr.loading = "lazy";
            qr.decoding = "async";
            qr.referrerPolicy = "no-referrer";
            qr.addEventListener("error", () => {
              qr.remove();
              const unavailable = document.createElement("p");
              unavailable.textContent = "QR code unavailable. Use the listed account details or contact support.";
              instructions.append(unavailable);
            });
            qr.src = `/api/v1/orders/${encodeURIComponent(orderId)}/payment-qr`;
            instructions.append(qr);
          }
          const expiry = document.createElement("p");
          expiry.className = "customer-payment-expiry";
          const expiresAt = new Date(payment.expires_at);
          expiry.textContent = Number.isNaN(expiresAt.getTime()) ? "Payment expiry unavailable"
            : `Pay by ${new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Yangon" }).format(expiresAt)} MMT`;
          const countdown = document.createElement("span");
          countdown.className = "customer-payment-countdown";
          countdown.setAttribute("aria-hidden", "true");
          instructions.append(countdown);
          panel.append(instructions, expiry);
          const caution = document.createElement("p");
          caution.textContent = "Transfer the exact order total only. Payment is not confirmed until AuriX verifies the merchant record.";
          panel.append(caution);
        } else {
          const settled = document.createElement("p");
          settled.textContent = "This order has moved past payment. Do not send another transfer.";
          panel.append(settled);
        }
      } else if (order.state === "awaiting_payment") {
        const instruction = document.createElement("p");
        instruction.textContent = "No payment method has been saved for this order yet.";
        panel.append(instruction);
        const config = await requestJson("/api/v1/checkout/config");
        if (!isCurrent()) return;
        const rails = config?.payments_enabled === true && Array.isArray(config.payment_rails)
          ? [...new Set(config.payment_rails.filter((rail) => typeof rail === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(rail)))]
          : [];
        for (const rail of rails) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "history-order-payment";
          button.textContent = `Show ${({ kpay: "KPay", wavepay: "WavePay", aya: "AYA Pay", uab: "UABPay", cb: "CB Pay" })[rail] || rail.toUpperCase()} instructions`;
          button.addEventListener("click", async () => {
            button.disabled = true;
            try {
              await postJson("/api/v1/checkout/payment", { order_id: orderId, rail });
              await showOrderDetails(orderId, list, current, pageStatus);
            } catch (error) {
              if (!isCurrent()) return;
              const failure = document.createElement("p");
              failure.textContent = error.message || "Payment instructions could not be opened. Reload this order before retrying.";
              panel.append(failure);
              button.disabled = false;
            }
          });
          panel.append(button);
        }
        if (!rails.length) {
          const disabled = document.createElement("p");
          disabled.textContent = "Payment is not enabled. Do not transfer money; check this order for updates.";
          panel.append(disabled);
        }
      }
      if (order.payment) {
        const evidenceStatus = document.createElement("p");
        panel.append(evidenceStatus);
        try {
          const review = order.payment_review;
          if (!review) throw new Error("Payment review is unavailable");
          if (!isCurrent()) return;
          const canCorrect = order.state === "awaiting_payment" && review.receipt_upload_enabled && review.can_correct;
          evidenceStatus.textContent = review.review_status === "not_verified" && canCorrect
            ? "Receipt needs an update. Use the correction form below; do not transfer again."
            : ({
              in_review: "Receipt received. Merchant transfer confirmation is pending.",
              verified: "Merchant transfer verified. Payment is confirmed.",
              not_verified: "Payment could not be verified. Contact support with your order number.",
              refund_pending: "Your transfer was verified. This order is cancelled and a full refund to the original sender is being completed.",
              refunded: "The full transfer refund to the original sender was verified. Contact support with your order number if it has not appeared.",
            })[review.review_status] || (order.state === "cancelled"
              ? "This order is cancelled. Do not send money using its saved payment instructions."
              : "Already transferred? Send your receipt for review.");
          if (order.state === "awaiting_payment" && review.receipt_upload_enabled
              && (review.review_status === "not_submitted" || review.can_correct)) {
            const form = document.createElement("form");
            form.className = "payment-evidence-form";
            form.dataset.revision = String(review.revision ?? 0);
            const restoreDraft = draft?.revision === form.dataset.revision;
            if (review.revision) {
              const correction = document.createElement("p");
              correction.textContent = `Correct receipt ${review.revision} below. Previous submissions stay saved. Do not transfer again.`;
              form.append(correction);
            }
            const referenceLabel = document.createElement("label");
            referenceLabel.textContent = "Transfer transaction reference";
            const reference = document.createElement("input");
            reference.type = "text";
            reference.required = true;
            reference.maxLength = 128;
            reference.autocomplete = "off";
            if (restoreDraft) reference.value = draft.reference;
            referenceLabel.append(reference);
            const fileLabel = document.createElement("label");
            const receiptLimit = Number.isSafeInteger(review.receipt_max_bytes) && review.receipt_max_bytes > 0
              ? Math.min(review.receipt_max_bytes, 5 * 1024 * 1024) : 256 * 1024;
            const limitLabel = receiptLimit >= 1024 * 1024 ? `${receiptLimit / (1024 * 1024)} MB` : `${receiptLimit / 1024} KB`;
            fileLabel.textContent = `Receipt image or PDF (maximum ${limitLabel})`;
            const file = restoreDraft ? draft.fileInput : document.createElement("input");
            file.type = "file";
            file.accept = "image/png,image/jpeg,application/pdf";
            file.required = true;
            file.disabled = false;
            fileLabel.append(file);
            const send = document.createElement("button");
            send.type = "submit";
            send.textContent = review.revision ? "Send corrected receipt" : "Send receipt for review";
            form.append(referenceLabel, fileLabel, send);
            const previousRevision = Number.isSafeInteger(review.revision) ? review.revision : 0;
            form.addEventListener("submit", async (event) => {
              event.preventDefault();
              if (!isCurrent() || send.disabled) return;
              stopPaymentStatusRefresh();
              const receipt = file.files?.[0];
              if (!receipt || receipt.size < 1 || receipt.size > receiptLimit) {
                evidenceStatus.textContent = `Choose a receipt of up to ${limitLabel}.`;
                startPaymentStatusRefresh(orderId, list, current, pageStatus, order, detailRevision);
                return;
              }
              send.disabled = true;
              reference.disabled = true;
              file.disabled = true;
              panel.querySelector(".customer-payment-instructions")?.remove();
              const transferReference = reference.value.trim();
              send.textContent = "Sending receipt…";
              try {
                const bytes = new Uint8Array(await receipt.arrayBuffer());
                let encoded = "";
                // Each chunk is divisible by three, so only the final chunk has base64 padding.
                for (let offset = 0; offset < bytes.length; offset += 24576) {
                  encoded += btoa(String.fromCharCode(...bytes.subarray(offset, offset + 24576)));
                }
                await postJson(`/api/v1/orders/${encodeURIComponent(orderId)}/payment-evidence`, {
                  transaction_reference: transferReference, receipt_base64: encoded,
                  ...(review.revision ? { expected_revision: review.revision } : {}),
                }, 60_000);
                if (!isCurrent()) return;
                await showOrderDetails(orderId, list, current, pageStatus);
              } catch (error) {
                if (!isCurrent()) return;
                if (error.status === 401) {
                  await resumeOrderSignIn(orderId);
                  return;
                }
                let readConfirmed = false;
                try {
                  const latest = await requestJson(`/api/v1/orders/${encodeURIComponent(orderId)}/payment-evidence`);
                  if (!isCurrent()) return;
                  readConfirmed = latest.order_id === orderId && Number.isSafeInteger(latest.revision ?? 0)
                    && (latest.revision ?? 0) >= 0 && (latest.revision ?? 0) <= 10
                    && typeof latest.receipt_upload_enabled === "boolean"
                    && ["not_submitted", "in_review", "verified", "not_verified", "refund_pending", "refunded"].includes(latest.review_status);
                  if (!readConfirmed) throw new Error("Invalid saved review");
                  if ((Number.isSafeInteger(latest.revision) ? latest.revision : 0) > previousRevision
                      || latest.review_status !== review.review_status) {
                    await showOrderDetails(orderId, list, current, pageStatus);
                    return;
                  }
                } catch (readError) {
                  if (readError.status === 401 && isCurrent()) {
                    await resumeOrderSignIn(orderId);
                    return;
                  }
                }
                if (!isCurrent()) return;
                evidenceStatus.textContent = readConfirmed && error.status === 400
                  ? `Receipt was not accepted. Check the reference and use a PNG, JPEG or PDF up to ${limitLabel}.`
                  : "Receipt response is uncertain. Reload this order before retrying; do not pay again.";
                send.textContent = readConfirmed ? "Retry receipt submission" : "Checking saved submission…";
                send.disabled = !readConfirmed;
                reference.disabled = !readConfirmed;
                file.disabled = !readConfirmed;
                if (!readConfirmed) {
                  const reload = document.createElement("button");
                  reload.type = "button";
                  reload.textContent = "Reload saved order";
                  reload.addEventListener("click", () => void showOrderDetails(orderId, list, current, pageStatus));
                  form.append(reload);
                }
                startPaymentStatusRefresh(orderId, list, current, pageStatus, order, detailRevision);
              }
            });
            panel.append(form);
            if (restoreDraft && draft.focused) {
              (draft.focused === "file" ? file : reference).focus({ preventScroll: true });
            }
          }
        } catch {
          evidenceStatus.textContent = "Receipt review is unavailable. Contact support if you have already transferred; do not pay again.";
        }
      }
      if (order.refund) {
        const refund = document.createElement("p");
        refund.className = "customer-order-refund";
        const destination = order.refund.route === "bank" ? "the original sender" : "your wallet";
        refund.textContent = order.refund.status === "completed"
          ? `${money(order.refund.amount_minor, order.refund.currency)} returned to ${destination}.`
          : `${money(order.refund.amount_minor, order.refund.currency)} refund to ${destination} is pending.`;
        panel.append(refund);
      }
      startPaymentStatusRefresh(orderId, list, current, pageStatus, order, detailRevision);
      pageStatus.textContent = orderRefreshDelay(order)
        ? "Order saved. Status updates appear automatically; do not transfer again."
        : "Order details refreshed from your private account.";
    } catch (error) {
      if (!isCurrent()) return;
      if (error.status === 401) {
        await resumeOrderSignIn(orderId);
        return;
      }
      status.textContent = error.status === 404 ? "Order was not found in this account."
        : "Order details could not be loaded. Please try again.";
      if (error.status === 404) delete panel.receiptDraft;
      else {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "history-order-open";
        retry.textContent = "Reload saved order";
        retry.addEventListener("click", () => void showOrderDetails(orderId, list, current, pageStatus));
        panel.append(retry);
      }
      pageStatus.textContent = status.textContent;
    }
  }

  async function openOrder(orderId) {
    await renderHistory("orders", 0, orderId);
  }

  async function openWallet() {
    pendingWalletOpen = true;
    window.AurixStore?.showWallet();
    try { await loadAccount(); }
    catch { await openAccount(); return; }
    if (!account) { await openAccount(); return; }
    pendingWalletOpen = false;
    window.AurixStore?.showWallet();
  }

  window.AurixCustomer = Object.freeze({ openAccount, openProfile, openHistory: renderHistory, openOrder, openWallet });
  balanceToggle.addEventListener("click", () => {
    if (!wallet) return;
    balanceVisible = !balanceVisible;
    paintWallet();
  });
  document.querySelector("#auxiliary-dialog").addEventListener("close", () => {
    stopPaymentStatusRefresh();
    orderDetailRevision += 1;
    historyNavigation += 1;
    historyRevision += 1;
    window.AurixStore?.setTab("home");
  });
  dialog.addEventListener("close", () => {
    linkingCustomer = null;
    resetProviderMode();
    pendingHistory = null;
    pendingOrderId = null;
    pendingWalletOpen = false;
    (window.matchMedia("(max-width: 760px)").matches ? bottomProfile : openButton)?.focus({ preventScroll: true });
    window.AurixStore?.setTab("home");
  });

  openButton.addEventListener("click", () => void openProfile());
  accountRetry?.addEventListener("click", () => void openAccount());
  linkButton?.addEventListener("click", () => {
    if (!account || signingIn) return;
    linkingCustomer = linkingCustomer ? null : account.customer_id;
    void renderProviders();
  });
  document.querySelector("#account-close").addEventListener("click", () => dialog.close());
  async function signOut(fromProfile = false) {
    if (signingIn) return;
    linkingCustomer = null;
    resetProviderMode();
    logoutButton.disabled = true;
    if (profileLogoutButton) profileLogoutButton.disabled = true;
    try {
      await postJson("/api/v1/auth/logout", {});
      sessionRevision += 1;
      account = null;
      clearPrivateViews();
      walletNote.textContent = "Sign in to see your available balance.";
      googleExpiresAt = 0;
      openButton.textContent = "Sign in";
      openButton.setAttribute("aria-label", "Sign in");
      bottomProfile?.setAttribute("aria-label", "Sign in");
      const bottomProfileLabel = bottomProfile?.querySelector("small");
      if (bottomProfileLabel) bottomProfileLabel.textContent = "Account";
      logoutButton.hidden = true;
      await renderProviders();
      if (fromProfile && profileDialog.open) profileDialog.close();
    } catch {
      await renderProviders();
      const message = "Sign-out could not be completed.";
      if (fromProfile && profileDialog.open) profileStatus.textContent = message;
      else status.textContent = message;
    } finally {
      logoutButton.disabled = false;
      if (profileLogoutButton) profileLogoutButton.disabled = false;
    }
  }
  logoutButton.addEventListener("click", () => void signOut());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  for (const [selector, kind] of [
    ["#bottom-history", "orders"],
  ]) {
    document.querySelector(selector).addEventListener("click", (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      void renderHistory(kind);
    }, true);
  }

  paintWallet();
  loadAuthConfig()
    .then(async () => {
      await loadAccount();
      if (accountRetry) accountRetry.hidden = true;
      if (signInDestination) {
        if (account) window.location.replace(signInDestination);
        else await openAccount();
        return;
      }
      status.textContent = account ? "Your private account is ready." : "Choose a sign-in method to continue.";
    })
    .catch(() => {
      status.textContent = accountServiceUnavailableMessage();
      if (accountRetry) accountRetry.hidden = false;
    });
})();
