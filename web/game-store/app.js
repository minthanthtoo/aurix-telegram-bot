"use strict";

function renderSupportChat(item) {
  auxiliaryTitle.textContent = "Help & support";
  auxiliaryCopy.textContent = "Your order reference keeps every question connected to your purchase.";
  auxiliaryContent.replaceChildren();
  const notice = document.createElement("p");
  notice.className = "history-empty";
  notice.textContent = "Live support is not connected yet. Open Order History to recover saved payment instructions and check your order status.";
  auxiliaryContent.append(notice);
  auxiliaryStatus.textContent = "Support unavailable · no messages are sent.";
}


const categoryList = document.querySelector("#category-list");
const productGrid = document.querySelector("#product-grid");
const catalogSection = document.querySelector(".catalog");
const catalogStatus = document.querySelector("#catalog-status");
const searchForm = document.querySelector("#search-form");
const searchInput = document.querySelector("#search");
const searchToggle = document.querySelector("#search-toggle");
const searchToggleLabel = document.querySelector("#search-toggle-label");
const dialog = document.querySelector("#product-dialog");
const detailArt = document.querySelector("#detail-art");
const detailArtImage = document.querySelector("#detail-art-image");
const detailHeroMeta = document.querySelector("#detail-hero-meta");
const detailFavorite = document.querySelector("#detail-favorite");
const detailNotifications = document.querySelector("#detail-notices");
const detailMore = document.querySelector("#detail-more");
const detailViewButtons = {
  packages: document.querySelector("#detail-tab-packages"),
  stock: document.querySelector("#detail-tab-stock"),
  info: document.querySelector("#detail-tab-info"),
};
const detailViewPanels = {
  packages: document.querySelector("#detail-panel-packages"),
  stock: document.querySelector("#detail-panel-stock"),
  info: document.querySelector("#detail-panel-info"),
};
const detailStockHeading = document.querySelector("#detail-stock-heading");
const detailStockCopy = document.querySelector("#detail-stock-copy");
const detailStockRegions = document.querySelector("#detail-stock-regions");
const referenceNote = document.querySelector("#detail-reference-note");
const referenceDetail = document.querySelector("#detail-reference");
const detailTabs = document.querySelector("#detail-tabs");
const detailRegionLabel = document.querySelector("#detail-region-label");
const referenceDescription = document.querySelector("#detail-reference-description");
const packageNotes = document.querySelector(".package-notes");
const regionList = document.querySelector("#detail-region-list");
const pinnedTitle = document.querySelector("#detail-pinned-title");
const pinnedOfferList = document.querySelector("#detail-pinned-offers");
const offerTitle = document.querySelector("#detail-offers-title");
const offerList = document.querySelector("#detail-offers");
const emptyOffers = document.querySelector("#detail-empty-offers");
const recipientSection = document.querySelector("#detail-recipient");
const recipientFields = document.querySelector("#detail-recipient-fields");
const recipientStatus = document.querySelector("#detail-recipient-status");
const accountCheckButton = document.querySelector("#detail-account-check");
const accountCheckReview = document.querySelector("#detail-account-check-review");
const accountCheckName = document.querySelector("#detail-account-check-name");
const accountCheckRegion = document.querySelector("#detail-account-check-region");
const accountCheckConfirm = document.querySelector("#detail-account-check-confirm");
const purchaseSteps = document.querySelector("#purchase-steps");
const accountProgressStep = document.querySelector("#purchase-step-account");
const packageProgressStep = document.querySelector("#purchase-step-package");
const packageGate = document.querySelector("#detail-package-gate");
const orderPromise = document.querySelector(".detail-order-promise");
const detailPackageStep = document.querySelector("#detail-package-step");
const detailLiveState = document.querySelector(".dialog-state");
const detailLiveStatus = document.querySelector("#detail-live-status");
const detailRetry = document.querySelector("#detail-retry");
const detailHelp = document.querySelector("#detail-help");
const detailHelpCopy = document.querySelector("#detail-help-copy");
const detailSelection = document.querySelector("#detail-selection");
const detailActionStatus = document.querySelector("#detail-action-status");
const detailBuy = document.querySelector("#detail-buy");
const detailClear = document.querySelector("#detail-clear");
const detailActions = document.querySelector(".detail-actions");
const walletDialog = document.querySelector("#wallet-dialog");
const walletClose = document.querySelector("#wallet-close");
const walletAmounts = document.querySelector("#wallet-amounts");
const walletCustomAmount = document.querySelector("#wallet-custom-amount");
const walletCustomAmountHint = document.querySelector("#wallet-custom-amount-hint");
const walletMethods = document.querySelector("#wallet-methods");
const walletSelection = document.querySelector("#wallet-selection");
const walletNext = document.querySelector("#wallet-next");
const walletStatus = document.querySelector("#wallet-status");
const walletStepOneAmount = document.querySelector("#wallet-step-one-amount");
const walletStepOneMethod = document.querySelector("#wallet-step-one-method");
const walletStepOneBoundary = document.querySelector("#wallet-step-one-boundary");
const walletConfirmationPreview = document.querySelector("#wallet-confirmation-preview");
const walletTransferAmount = document.querySelector("#wallet-transfer-amount");
const walletTransferMethod = document.querySelector("#wallet-transfer-method");
const walletAccountIcon = document.querySelector("#wallet-account-icon");
const walletAccountLabel = document.querySelector("#wallet-account-label");
const walletStepItems = [document.querySelector("#wallet-step-1"), document.querySelector("#wallet-step-2")].filter(Boolean);
const navigationDialog = document.querySelector("#navigation-dialog");
const navigationList = document.querySelector("#navigation-list");
const navigationSummary = document.querySelector("#navigation-summary");
const connectionStatus = document.querySelector("#connection-status");
const offlineBanner = document.querySelector("#offline-banner");
const storeStage = document.querySelector("#store-stage");
const availabilityCopy = document.querySelector("#availability-copy");
const walletBalance = document.querySelector("#wallet-balance");
const walletBalanceToggle = document.querySelector("#wallet-balance-toggle");
const floatingWallet = document.querySelector("#bottom-wallet");
const DRAWER_TOOL_IDS = new Set(["mbb-hero-guide", "myanmar-game-guide", "mobile-legends-region-checker", "leaderboard"]);
const auxiliaryDialog = document.querySelector("#auxiliary-dialog");
const auxiliaryTitle = document.querySelector("#auxiliary-title");
const auxiliaryCopy = document.querySelector("#auxiliary-copy");
const auxiliaryContent = document.querySelector("#auxiliary-content");
const auxiliaryStatus = document.querySelector("#auxiliary-status");
const auxiliaryClose = document.querySelector("#auxiliary-close");
const auxiliaryHeaderActions = document.querySelector("#auxiliary-header-actions");
const notificationOpen = document.querySelector("#notification-open");
const notificationCount = document.querySelector("#notification-count");
const leaderboardStatus = document.querySelector("#home-leaderboard-status");
const leaderboardPreview = document.querySelector("#home-leaderboard-preview");
let activeCategory = "";
let savedOnly = false;
let searchTimer = null;
let catalogCache = null;
let catalogRenderMode = "";
let cachedRenderKey = "";
let activeStoreTab = "home";
const storeScrollPositions = { home: null, games: null, wallet: null };
const savedGamesButton = document.querySelector("#saved-games");
const searchClear = document.querySelector("#search-clear");
const catalogReset = document.querySelector("#catalog-reset");
const catalogRetry = document.querySelector("#catalog-retry");
let activeLoadController = null;
let activeDetailController = null;
let navigationItems = [];
let guideRouteStack = [];
let activeGuideRoute = null;
const guidePayloadCache = new Map();
let activeReferenceRegion = "";
let activeReferenceRegions = [];
let activeSupplierPreview = false;
let selectedReferenceOffer = null;
let activeProductSlug = "";
let activeProduct = null;
let activeDetailView = "packages";
let selectedWalletAmount = "";
let selectedWalletMethod = "";
let activeLiveOffers = [];
let activeLiveRegion = "";
let activeLivePurchaseEnabled = false;
let recipientCheckoutEnabled = false;
let accountCheckEnabled = false;
let checkoutPaymentsEnabled = false;
let checkoutPaymentRails = [];
let checkoutConfigLoaded = false;
let availableLiveCategoryIds = null;
let checkoutReview = null;
let checkoutOrder = null;
let checkoutPaymentAttemptRail = "";
let checkoutRevision = 0;
let checkoutBusy = false;
let accountCheckBusy = false;
let accountCheckRevision = 0;
let playerAccountCheck = null;
let playerAccountConfirmed = false;
let mlbbCheckoutZonesPromise = null;

const MLBB_ACCOUNT_HISTORY_KEY = "aurix-mlbb-account-ids";
const MLBB_ZONE_HISTORY_KEY = "aurix-mlbb-zone-ids";
const GAME_LIST_CACHE_KEY = "aurix-game-list-v2";
const GAME_LIST_CACHE_MAX_AGE = 30 * 24 * 60 * 60 * 1000;
const CATALOG_DISCOVERY_REFRESH_INTERVAL = 5 * 60 * 1000;
let catalogDiscoveryAttemptedAt = 0;
const MLBB_ACCOUNT_ID_PATTERN = /^[0-9]{4,20}$/;
const MLBB_ZONE_ID_PATTERN = /^\d{1,10}$/;

function recentMlbbAccountIds() {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(MLBB_ACCOUNT_HISTORY_KEY) || "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id) => typeof id === "string" && MLBB_ACCOUNT_ID_PATTERN.test(id)))] : [];
  } catch { return []; }
}

function recentPlayerIds(productSlug = activeProductSlug) {
  if (productSlug === "mobile-legends") return recentMlbbAccountIds().slice(0, 12);
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(`aurix-game-account-ids-${productSlug}`) || "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id) => typeof id === "string"
      && /^[A-Za-z0-9._-]{1,64}$/.test(id)))].slice(0, 12) : [];
  } catch { return []; }
}

function rememberPlayerId(value, productSlug = activeProductSlug) {
  if (productSlug === "mobile-legends") return rememberMlbbAccountId(value);
  const id = String(value || "").trim();
  const ids = recentPlayerIds(productSlug);
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(id)) return { ids, saved: true };
  const next = [id, ...ids.filter((previous) => previous !== id)].slice(0, 12);
  try {
    if (!globalThis.localStorage) return { ids: next, saved: false };
    globalThis.localStorage.setItem(`aurix-game-account-ids-${productSlug}`, JSON.stringify(next));
    return { ids: next, saved: true };
  } catch { return { ids: next, saved: false }; }
}

function clearPlayerIds(productSlug = activeProductSlug) {
  if (productSlug === "mobile-legends") return clearMlbbAccountIds();
  try {
    if (!globalThis.localStorage) return false;
    globalThis.localStorage.removeItem(`aurix-game-account-ids-${productSlug}`);
    return true;
  } catch { return false; }
}

function rememberMlbbAccountId(value) {
  const id = String(value || "").trim();
  if (!MLBB_ACCOUNT_ID_PATTERN.test(id)) return { ids: recentMlbbAccountIds(), saved: true };
  const ids = [id, ...recentMlbbAccountIds().filter((previous) => previous !== id)];
  try {
    if (!globalThis.localStorage) return { ids, saved: false };
    globalThis.localStorage.setItem(MLBB_ACCOUNT_HISTORY_KEY, JSON.stringify(ids));
    return { ids, saved: true };
  } catch { return { ids, saved: false }; }
}

function clearMlbbAccountIds() {
  try {
    if (!globalThis.localStorage) return false;
    globalThis.localStorage.removeItem(MLBB_ACCOUNT_HISTORY_KEY);
    return true;
  }
  catch { return false; }
}

function recentMlbbZoneIds() {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(MLBB_ZONE_HISTORY_KEY) || "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id) => typeof id === "string" && MLBB_ZONE_ID_PATTERN.test(id)))].slice(0, 12) : [];
  } catch { return []; }
}

function rememberMlbbZoneId(value) {
  const id = String(value || "").trim();
  if (!MLBB_ZONE_ID_PATTERN.test(id)) return false;
  const ids = [id, ...recentMlbbZoneIds().filter((previous) => previous !== id)].slice(0, 12);
  try {
    if (!globalThis.localStorage) return false;
    globalThis.localStorage.setItem(MLBB_ZONE_HISTORY_KEY, JSON.stringify(ids));
    return true;
  } catch { return false; }
}

function clearMlbbZoneIds() {
  try {
    if (!globalThis.localStorage) return false;
    globalThis.localStorage.removeItem(MLBB_ZONE_HISTORY_KEY);
    return true;
  } catch { return false; }
}

function loadMlbbCheckoutZones() {
  if (!mlbbCheckoutZonesPromise) {
    mlbbCheckoutZonesPromise = fetch("/api/v1/games/mlbb/zones", { headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error("Zone list is unavailable");
        return response.json();
      })
      .then((payload) => Array.isArray(payload.zones) ? payload.zones.filter((zone) =>
        zone && typeof zone.name === "string" && zone.name.trim()
        && typeof zone.id === "string" && /^\d{1,10}$/.test(zone.id)) : [])
      .catch(() => { mlbbCheckoutZonesPromise = null; return []; });
  }
  return mlbbCheckoutZonesPromise;
}

function resolveMlbbCheckoutZone(value, zones) {
  const text = String(value || "").trim();
  if (/^\d{1,10}$/.test(text)) return text;
  const normalized = text.toLocaleLowerCase();
  const matches = zones.filter((zone) => normalized === zone.name.toLocaleLowerCase()
    || normalized === `${zone.name} (${zone.id})`.toLocaleLowerCase());
  return matches.length === 1 ? matches[0].id : "";
}

function invalidateCheckoutReview() {
  checkoutReview = null;
  checkoutRevision += 1;
  document.querySelector("#recipient-checkout-review")?.remove();
  updateCheckoutAction();
}

function recipientFieldValues() {
  return Object.fromEntries([...recipientFields.querySelectorAll("input")]
    .map((input) => [input.dataset.fieldKey, input.value.trim()]));
}

function playerAccountDetailsMatch() {
  const fields = recipientFieldValues();
  return Boolean(playerAccountCheck?.fields
    && Object.entries(playerAccountCheck.fields).length === Object.keys(fields).length
    && Object.entries(playerAccountCheck.fields).every(([key, value]) => fields[key] === value));
}

function playerAccountIsCurrent() {
  return playerAccountConfirmed && playerAccountDetailsMatch();
}

function updatePurchaseProgress() {
  const accountReady = playerAccountIsCurrent();
  const currentFields = recipientFieldValues();
  const hasAccountReview = Boolean(playerAccountCheck?.fields
    && Object.entries(playerAccountCheck.fields).length === Object.keys(currentFields).length
    && Object.entries(playerAccountCheck.fields).every(([key, value]) => currentFields[key] === value));
  const hasPackage = Boolean(selectedReferenceOffer);
  purchaseSteps.hidden = !activeLiveOffers.length;
  orderPromise.hidden = !activeLiveOffers.length;
  accountProgressStep.dataset.state = accountReady ? "complete" : hasAccountReview ? "review" : "current";
  packageProgressStep.dataset.state = !accountReady ? "locked" : hasPackage ? "complete" : "current";
  if (accountReady) accountProgressStep.removeAttribute("aria-current");
  else accountProgressStep.setAttribute("aria-current", "step");
  if (accountReady && !hasPackage) packageProgressStep.setAttribute("aria-current", "step");
  else packageProgressStep.removeAttribute("aria-current");
  packageGate.hidden = !activeLiveOffers.length || accountReady || !hasAccountReview;
  packageGate.textContent = hasAccountReview
    ? "Confirm player details to unlock packages."
    : "Packages unlock after player check.";
}

function setDetailView(view, focus = false, transition = true) {
  if (!detailViewButtons[view] || !detailViewPanels[view]) return;
  const previousIndex = Object.keys(detailViewButtons).indexOf(activeDetailView);
  const nextIndex = Object.keys(detailViewButtons).indexOf(view);
  const changed = view !== activeDetailView;
  dialog.dataset.detailView = view;
  for (const [name, button] of Object.entries(detailViewButtons)) {
    const selected = name === view;
    button?.setAttribute("aria-selected", String(selected));
    button?.setAttribute("tabindex", selected ? "0" : "-1");
    if (detailViewPanels[name]) detailViewPanels[name].hidden = !selected;
  }
  activeDetailView = view;
  const panel = detailViewPanels[view];
  if (changed && transition && typeof panel.animate === "function"
      && !window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
    panel.animate([
      { opacity: 0, transform: `translateX(${nextIndex > previousIndex ? 12 : -12}px)` },
      { opacity: 1, transform: "translateX(0)" },
    ], { duration: 180, easing: "cubic-bezier(.2,.65,.3,1)" });
  }
  if (focus) detailViewButtons[view].focus();
}

function detailTabKeydown(event, current) {
  const views = Object.keys(detailViewButtons);
  const index = views.indexOf(current);
  const next = event.key === "ArrowRight" ? views[(index + 1) % views.length]
    : event.key === "ArrowLeft" ? views[(index + views.length - 1) % views.length]
      : event.key === "Home" ? views[0]
        : event.key === "End" ? views.at(-1) : "";
  if (!next) return;
  event.preventDefault();
  setDetailView(next, true);
}

for (const [view, button] of Object.entries(detailViewButtons)) {
  button?.addEventListener("click", () => setDetailView(view));
  button?.addEventListener("keydown", (event) => detailTabKeydown(event, view));
}

function updateDetailFavorite() {
  if (!detailFavorite || !activeProductSlug) return;
  const pinned = pinnedProductSlugs.has(activeProductSlug);
  detailFavorite.setAttribute("aria-pressed", String(pinned));
  detailFavorite.setAttribute("aria-label", pinned ? "Remove saved game" : "Save game");
  detailFavorite.title = pinned ? "Saved · pinned first in your list" : "Save game · pins it first in your list";
  const card = Array.from(productGrid.children).find((item) => item.dataset.slug === activeProductSlug);
  if (!card) return;
  card.dataset.pinned = String(pinned);
  const pinButton = Array.from(card.children).find((item) => item.className.split(/\s+/).includes("product-card__favorite"));
  if (pinButton) {
    pinButton.className = pinned ? "product-card__favorite is-pinned" : "product-card__favorite";
    pinButton.setAttribute("aria-label", pinned ? `Unpin ${activeProduct.name}` : `Pin ${activeProduct.name}`);
    pinButton.setAttribute("aria-pressed", String(pinned));
  }
  const cover = Array.from(card.children).find((item) => item.className === "product-card__cover");
  const badge = cover && Array.from(cover.children).find((item) => item.className === "product-card__pin-badge");
  if (badge) badge.hidden = !pinned;
}

detailFavorite?.addEventListener("click", () => {
  if (!activeProductSlug) return;
  if (pinnedProductSlugs.has(activeProductSlug)) pinnedProductSlugs.delete(activeProductSlug);
  else pinnedProductSlugs.add(activeProductSlug);
  try { globalThis.localStorage?.setItem("aurix-favorite-games", JSON.stringify([...pinnedProductSlugs].slice(0, 200))); } catch { /* Browser storage may be disabled. */ }
  updateDetailFavorite();
  refreshSavedButton();
  if (savedOnly) void loadCatalog();
  else sortProductCards();
});

detailNotifications?.addEventListener("click", () => notificationOpen?.click());
detailMore?.addEventListener("click", () => document.querySelector("#nav-open")?.click());

function updateCheckoutAction() {
  const inputs = [...recipientFields.querySelectorAll("input")];
  const fields = recipientFieldValues();
  const missingInput = inputs.find((input) => !input.value.trim());
  const missingFieldLabel = missingInput?.dataset.fieldKey === "player_id" ? "Player ID"
    : ["server_id", "zone_id"].includes(missingInput?.dataset.fieldKey)
      ? activeProductSlug === "mobile-legends" ? "Zone ID" : "Server ID"
      : missingInput?.dataset.fieldKey.replaceAll("_", " ");
  const mlbbZone = fields.server_id || fields.zone_id || "";
  const accountReady = playerAccountIsCurrent();
  const supportedOffer = activeLivePurchaseEnabled
    && selectedReferenceOffer?.fulfillment_kind === "direct_topup" && inputs.length > 0;
  const eligible = activeLivePurchaseEnabled && checkoutConfigLoaded && recipientCheckoutEnabled && checkoutPaymentsEnabled
    && checkoutPaymentRails.length > 0 && supportedOffer && inputs.every((input) => input.value.trim())
    && accountReady;
  const confirmed = document.querySelector("#recipient-checkout-confirmed")?.checked === true;
  detailBuy.textContent = checkoutBusy ? "Checking…" : checkoutReview ? "Confirm account & create order"
    : !activeLivePurchaseEnabled ? "Purchases unavailable"
      : !checkoutConfigLoaded ? "Checking account availability…"
        : !accountCheckEnabled ? "Player check unavailable"
          : !accountReady ? playerAccountCheck ? "Confirm your player above"
            : missingInput ? `Enter ${missingFieldLabel} above` : "Check your player ID above"
            : !selectedReferenceOffer ? "Choose a package"
              : !recipientCheckoutEnabled ? "Account check unavailable"
                : !checkoutPaymentsEnabled || !checkoutPaymentRails.length ? "Payment unavailable"
                  : !supportedOffer ? "Checkout unavailable"
                    : inputs.some((input) => !input.value.trim()) ? "Enter account details"
                      : "Continue to order";
  detailBuy.disabled = checkoutBusy || (!eligible && !checkoutReview) || Boolean(checkoutReview && !confirmed);
  detailBuy.setAttribute("aria-disabled", String(detailBuy.disabled));
  detailClear.hidden = !selectedReferenceOffer && !checkoutReview;
  detailClear.disabled = detailClear.hidden;
  detailActions.classList.toggle("detail-actions--clear-hidden", detailClear.hidden);
  detailActions.classList.toggle("detail-actions--has-offer", Boolean(selectedReferenceOffer));
  if (accountCheckButton) {
    accountCheckButton.disabled = accountCheckBusy || !accountCheckEnabled
      || inputs.length === 0 || inputs.some((input) => !input.value.trim())
      || (activeProductSlug === "mobile-legends"
        && (!MLBB_ACCOUNT_ID_PATTERN.test(fields.player_id || "") || !mlbbZone));
  }
  updatePurchaseProgress();
}

async function recipientLookupRequest(payload, productSlug = activeProductSlug) {
  const path = productSlug === "mobile-legends"
    ? "/api/v1/games/mlbb/account-check"
    : `/api/v1/games/${encodeURIComponent(productSlug)}/account-check`;
  const response = await fetch(path, {
    method: "POST", credentials: "omit",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
  let body = {};
  try { body = await response.json(); } catch (_) { body = {}; }
  if (!response.ok) {
    const error = new Error(typeof body.error === "string" ? body.error : "Account check is unavailable.");
    error.status = response.status;
    throw error;
  }
  return body;
}

async function checkoutRequest(path, payload) {
  let response;
  try {
    response = await fetch(path, { method: "POST", credentials: "same-origin", signal: AbortSignal.timeout(15_000),
      headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(payload) });
  } catch {
    const error = new Error("Checkout service could not be reached. Please retry.");
    error.status = 0;
    throw error;
  }
  let body = {};
  try {
    const payload = await response.json();
    if (payload && typeof payload === "object") body = payload;
  } catch { /* Gateways may return HTML or an empty error body. */ }
  if (response.status === 401) globalThis.AurixCustomer?.openAccount();
  if (!response.ok) {
    const error = new Error(typeof body.error === "string" ? body.error
      : response.status >= 500 ? "Checkout service is temporarily unavailable. Please retry."
        : "Checkout is unavailable.");
    error.status = response.status;
    error.details = body;
    throw error;
  }
  return body;
}
const pinnedProductSlugs = new Set();
try {
  const saved = JSON.parse(globalThis.localStorage?.getItem("aurix-favorite-games") || "[]");
  if (Array.isArray(saved)) saved.slice(0, 200).filter((slug) => typeof slug === "string" && /^[a-z0-9-]{1,80}$/.test(slug)).forEach((slug) => pinnedProductSlugs.add(slug));
} catch { /* Favorites remain usable when browser storage is unavailable. */ }
// Catalog artwork supplies title context; the MLBB fan cover remains custom.
const GAME_ART = Object.freeze({
  ...(globalThis.AURIX_GAME_ARTWORK || {}),
  "mobile-legends": Object.freeze({
    src: "/game-store/assets/aurix/mlbb-moonlight.jpg",
    source: "AI-generated Miya fan illustration; not official game art",
    className: "game-art--mlbb",
  }),
});

function attachGameArt(container, product) {
  const art = GAME_ART[product?.slug] || GAME_ART[product?.artwork_slug];
  if (!art || !container) return;
  const image = document.createElement("img");
  image.className = `product-card__cover-art ${art.className}`;
  image.alt = "";
  // Load larger cached catalog covers only as their cards approach the viewport.
  image.hidden = false;
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.setAttribute("aria-hidden", "true");
  image.addEventListener("error", () => {
    image.hidden = true;
    container.dataset.artState = "fallback";
  });
  image.addEventListener("load", () => {
    container.dataset.artState = "loaded";
  });
  image.src = art.src;
  container.dataset.artSource = art.source;
  container.append(image);
}

function detailOfferIconKind(offerName) {
  const name = String(offerName || "").toLowerCase();
  // Product-form words win over embedded currency words: a "Weekly Diamond
  // Pass" is a pass/package tile, not a raw diamond denomination.
  if (/\b(gifts?|passes?|packs?|bundles?|packages?|subscriptions?|weekly|monthly|passage)\b/.test(name)) return "gift";
  if (/\b(spells?|magic)\b/.test(name)) return "spell";
  if (/\b(diamonds?|gems?)\b/.test(name)) return "diamond";
  // UC, credits, and generic numeric denominations are deliberately shown
  // with a generic coin glyph, never a publisher-specific currency mark.
  return "coins";
}

function attachDetailOfferIcon(container, offerName, iconKindOverride = "") {
  const kind = iconKindOverride || detailOfferIconKind(offerName);
  container.dataset.iconKind = kind;
  const image = document.createElement("img");
  image.src = `/game-store/assets/aurix/item-${kind}.svg`;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.setAttribute("aria-hidden", "true");
  image.addEventListener("error", () => {
    image.hidden = true;
    container.dataset.artState = "fallback";
  });
  image.addEventListener("load", () => {
    container.dataset.artState = "loaded";
  });
  container.append(image);
}

const REFERENCE_WALLET_AMOUNTS = [
  "5,000 Ks", "10,000 Ks", "20,000 Ks", "30,000 Ks", "50,000 Ks",
  "70,000 Ks", "100,000 Ks", "300,000 Ks", "500,000 Ks",
];
const REFERENCE_WALLET_METHODS = ["Wave Money", "KBZ Pay"];
const WALLET_AMOUNT_HINT = "Enter a whole MMK amount, or choose a quick amount below.";
const GUIDE_TOOLS = Object.freeze([
  { id: "draft-strategy", title: "Team lane coverage", copy: "Plan roles across five lanes", tone: "blue" },
  { id: "counter-picks", title: "Enemy lane coverage", copy: "Inspect recorded roles and lanes", tone: "red" },
]);
const GUIDE_WIKI = Object.freeze([
  { id: "hero-tier-list", icon: "hero-tier-list", title: "Hero Reference", copy: "Browse roles and lanes · rankings paused" },
  { id: "item-list", icon: "item-list", title: "Item Reference", copy: "Saved values · verify in-game" },
  { id: "emblems-talents", icon: "emblems-talents", title: "Emblem Examples", copy: "Illustrative stats · not current data" },
]);
const GUIDE_ICON_SHAPES = Object.freeze({
  "draft-strategy": [
    ["path", { d: "M12 2.8v3.4m0 11.6v3.4M2.8 12h3.4m11.6 0h3.4" }],
    ["circle", { cx: "12", cy: "12", r: "7.1" }],
    ["circle", { cx: "12", cy: "12", r: "2.4" }],
  ],
  "counter-picks": [
    ["path", { d: "M4 7h13.5l-3-3M20 17H6.5l3 3" }],
    ["path", { d: "m17 4 3 3-3 3M7 14l-3 3 3 3" }],
  ],
  "hero-synergy": [
    ["circle", { cx: "9", cy: "8", r: "3" }],
    ["circle", { cx: "17", cy: "9", r: "2.2" }],
    ["path", { d: "M3.5 20c.3-3.2 2.3-5 5.5-5s5.2 1.8 5.5 5M14.5 15.4c3-.6 5.5 1.2 6 4.6" }],
  ],
  "matchup-prediction": [
    ["path", { d: "M4 19V5m0 14h10M7 15l3-3 2 1 3-5" }],
    ["circle", { cx: "17.4", cy: "16.8", r: "3.1" }],
    ["path", { d: "m19.7 19.1 2 2" }],
  ],
  "hero-tier-list": [
    ["path", { d: "M12 3 19 6v5c0 4.5-2.7 7.8-7 10-4.3-2.2-7-5.5-7-10V6z" }],
    ["path", { d: "m9 12 2 2 4-4" }],
  ],
  "item-list": [
    ["rect", { x: "4", y: "4", width: "6", height: "6", rx: "1.5" }],
    ["path", { d: "M13 5h7M13 8h5M4 14h16M4 18h16" }],
  ],
  "emblems-talents": [
    ["path", { d: "m12 3 2.2 5.1 5.6.5-4.3 3.7 1.3 5.5-4.8-2.9-4.8 2.9 1.3-5.5-4.3-3.7 5.6-.5z" }],
    ["circle", { cx: "12", cy: "12", r: "9.2" }],
  ],
  "item-attack": [
    ["path", { d: "m6 18 9.8-9.8 2.5 2.5L8.5 20.5H6zM14.8 6.7l2.5-2.5 3.1 3.1-2.5 2.5M5 13l6 6M8 10l6 6" }],
  ],
  "item-magic": [
    ["path", { d: "M5 19 15.8 8.2m-2-2 2.4-2.4 4 4-2.4 2.4M5 19l-1 2 2-1z" }],
    ["path", { d: "M7 4v3M5.5 5.5h3M19 14v3m-1.5-1.5h3" }],
  ],
  "item-defense": [
    ["path", { d: "M12 3 19 6v5c0 4.5-2.7 7.8-7 10-4.3-2.2-7-5.5-7-10V6z" }],
    ["path", { d: "m9 12 2 2 4-4" }],
  ],
  "item-movement": [
    ["path", { d: "M4 17h7l2-3 3 1 3 4H7l-3-2zM12 10l2-4 3 1 1 4-4 1z" }],
  ],
  "item-jungling": [
    ["path", { d: "M12 20V9m0 5C6 14 5 10 5 6c4 0 7 1 7 7m0-1c0-5 3-8 7-8 0 5-1 9-7 10" }],
  ],
  "item-roaming": [
    ["path", { d: "M3 16c4-1 5-5 7-9 2 3 2 7 0 10 3-1 5-4 6-8 2 2 3 5 2 8 1-1 2-2 3-4" }],
  ],
  "emblem-assassin": [
    ["path", { d: "m4 5 16 14M20 5 4 19M7 3l2 2M17 3l-2 2M7 21l2-2M17 21l-2-2" }],
    ["circle", { cx: "12", cy: "12", r: "2.3" }],
  ],
  "emblem-fighter": [
    ["path", { d: "m6 19 9.8-9.8 2.5 2.5L8.5 20.5H6zM14.8 6.7l2.5-2.5 3.1 3.1-2.5 2.5M4 8l5 5M7 5l5 5" }],
  ],
  "emblem-mage": [
    ["path", { d: "M12 3v12m-3-8 3-4 3 4M8 15a4 4 0 1 0 8 0" }],
    ["path", { d: "M12 19v2M6 21h12" }],
  ],
  "emblem-marksman": [
    ["circle", { cx: "12", cy: "12", r: "7.5" }],
    ["circle", { cx: "12", cy: "12", r: "2.3" }],
    ["path", { d: "M12 2v3m0 14v3M2 12h3m14 0h3" }],
  ],
  "emblem-support": [
    ["path", { d: "M12 4v16M4 12h16" }],
    ["path", { d: "M9 7c0-2 6-2 6 0 0 1.5-3 2-3 4m0 5h.01" }],
  ],
  "emblem-tank": [
    ["path", { d: "M12 3 19 6v5c0 4.5-2.7 7.8-7 10-4.3-2.2-7-5.5-7-10V6z" }],
    ["path", { d: "M9 12h6M12 9v6" }],
  ],
});
const GUIDE_KENNEY_ICONS = Object.freeze({
  "draft-strategy": "multiplayer",
  "counter-picks": "target",
  "hero-synergy": "gamepad",
  "matchup-prediction": "barsVertical",
  "hero-tier-list": "trophy",
  "emblems-talents": "leaderboardsComplex",
  "myanmar-guide": "joystick",
});
const GUIDE_EXTERNAL_GAME_ICONS = Object.freeze({
  "item-attack": "crossed-swords",
  "item-magic": "spell-book",
  "item-defense": "shield-reflect",
  "item-movement": "boots",
  "item-jungling": "jungle",
  "item-roaming": "footsteps",
  "emblem-assassin": "assassin-pocket",
  "emblem-fighter": "crossed-swords",
  "emblem-mage": "wizard-staff",
  "emblem-marksman": "bow-arrow",
  "emblem-support": "healing",
  "emblem-tank": "shield-reflect",
});
const GUIDE_ROLES = ["Tank", "Fighter", "Assassin", "Mage", "Marksman", "Support"];
const GUIDE_LANES = ["Gold Lane", "Exp Lane", "Mid Lane", "Roam", "Jungle"];
const GUIDE_ROLE_ART = Object.freeze({
  Tank: { icon: "shield-reflect", hue: 28 },
  Fighter: { icon: "crossed-swords", hue: 5 },
  Assassin: { icon: "assassin-pocket", hue: 276 },
  Mage: { icon: "wizard-staff", hue: 210 },
  Marksman: { icon: "bow-arrow", hue: 45 },
  Support: { icon: "healing", hue: 150 },
});
const GUIDE_ITEMS = [
  { name: "Berserker’s Fury", price: "2390", stats: "+65 Physical ATK  +25% Crit Chance", detail: "Doom: Unique Attribute: +40% Crit Damage" },
  { name: "Blade of Despair", price: "3010", stats: "+160 Physical ATK  +5 Movement SPD", detail: "Despair: Unique Passive increases Physical Attack against weakened enemies." },
  { name: "Blade of the Heptaseas", price: "1950", stats: "+70 Physical ATK  +250 HP", detail: "Ambush: Unique Attribute: +15 Physical Penetration" },
  { name: "Corrosion Scythe", price: "2050", stats: "+30 Physical ATK  +5% Attack Speed", detail: "Corrosion: Unique Passive slows targets and increases attack speed." },
];
const GUIDE_LOCALE_TEXT = Object.freeze({
  en: Object.freeze({
    itemList: "Item Reference",
    emblems: "Emblem Examples",
    mainEmblems: "Main Emblems",
    searchItems: "Search items…",
    sortName: "Sort by Name",
    sortPrice: "Sort by Price",
    all: "All",
    attack: "Attack",
    magic: "Magic",
    defense: "Defense",
    movement: "Movement",
    jungling: "Jungling",
    roaming: "Roaming",
    attackMagic: "Attack & Magic",
    attackMagicDefense: "Attack, Magic & Defense",
    buildsFrom: "Builds from",
    usefulFor: "Useful for",
    itemCountPrefix: "Game data ·",
    gameDataNote: "Game item names, descriptions, and values remain in the source language for exact reference.",
    snapshotNote: "Saved snapshot · verify current values in-game",
    referenceSampleNote: "Illustrative reference sample, not a live game database.",
    emblemsNote: "Illustrative values only · not maintained game data; verify the current client.",
  }),
  my: Object.freeze({
    itemList: "ပစ္စည်းရည်ညွှန်းစာရင်း",
    emblems: "Emblem နမူနာများ",
    mainEmblems: "အဓိက Emblems",
    searchItems: "ပစ္စည်းအမည် ရှာရန်…",
    sortName: "အမည်အလိုက် စီရန်",
    sortPrice: "ဈေးနှုန်းအလိုက် စီရန်",
    all: "အားလုံး",
    attack: "တိုက်ခိုက်မှု",
    magic: "မန္တန်",
    defense: "ကာကွယ်ရေး",
    movement: "လှုပ်ရှားမှု",
    jungling: "Jungle",
    roaming: "Roam",
    attackMagic: "တိုက်ခိုက်မှုနှင့် မန္တန်",
    attackMagicDefense: "တိုက်ခိုက်မှု၊ မန္တန်နှင့် ကာကွယ်ရေး",
    buildsFrom: "ပေါင်းစပ်ပစ္စည်းများ",
    usefulFor: "အသုံးဝင်သူ",
    itemCountPrefix: "ဂိမ်းဒေတာ ·",
    gameDataNote: "ပစ္စည်းအမည်၊ စွမ်းရည်ဖော်ပြချက်၊ ပေါင်းစပ်နည်းနှင့် တန်ဖိုးများကို မူရင်းဘာသာဖြင့်သာ ဖော်ပြထားပါသည်။",
    snapshotNote: "သိမ်းထားသော snapshot · လက်ရှိဂိမ်းတွင် တန်ဖိုးများကို ပြန်စစ်ပါ",
    referenceSampleNote: "နမူနာကိုးကားချက်သာဖြစ်ပြီး တိုက်ရိုက်ဂိမ်းဒေတာ မဟုတ်ပါ။",
    emblemsNote: "Emblem အမည်နှင့် ဂုဏ်သတ္တိများကို အင်္ဂလိပ်ဘာသာဖြင့် ဖော်ပြထားပါသည်။ တန်ဖိုးများသည် နမူနာသာဖြစ်ပြီး လက်ရှိဂိမ်းဒေတာအဖြစ် အတည်မပြုထားပါ။",
  }),
});
const GUIDE_TOOL_TITLES = Object.freeze({
  "hero-tier-list": "Hero Reference",
  "item-list": "Item Reference",
  "emblems-talents": "Emblem Examples",
  "counter-picks": "Enemy lane coverage",
  "draft-strategy": "Team lane coverage",
  "hero-synergy": "Hero Synergy",
  "matchup-prediction": "Matchup Prediction",
});
const GUIDE_EMBLEMS = [
  { name: "Assassin", tone: "purple", stats: ["Adaptive Penetration +14.00", "Adaptive Attack +10.00", "Movement Speed +3.00%"] },
  { name: "Common", tone: "cyan", stats: ["Hybrid Regen +12.00", "HP +275.00", "Adaptive Attack +22.00"] },
  { name: "Fighter", tone: "red", stats: ["Spell Vamp +10.00%", "Adaptive Attack +22.00", "Hybrid Defense +6.00"] },
  { name: "Mage", tone: "blue", stats: ["Magic Power +30.00", "Cooldown Reduction +5.00%", "Magic Penetration +8.00"] },
  { name: "Marksman", tone: "gold", stats: ["Attack Speed +15.00%", "Adaptive Attack +16.00", "Lifesteal +5.00%"] },
  { name: "Support", tone: "green", stats: ["Healing Effect +12.00%", "Cooldown Reduction +10.00%", "Movement Speed +6.00"] },
  { name: "Tank", tone: "orange", stats: ["HP +500", "Hybrid Defense +10.00", "HP Regen +4.00"] },
];
const GUIDE_HERO_METADATA = Object.freeze({
  Freya: { roles: ["Fighter"], lanes: ["Exp Lane", "Jungle"] },
  Gloo: { roles: ["Tank"], lanes: ["Exp Lane", "Roam"] },
  Harley: { roles: ["Mage", "Assassin"], lanes: ["Mid Lane", "Jungle"] },
  Marcel: { roles: ["Support"], lanes: ["Roam"] },
  Sora: { roles: ["Fighter"], lanes: ["Exp Lane"] },
  Estes: { roles: ["Support"], lanes: ["Roam"] },
  Guinevere: { roles: ["Fighter"], lanes: ["Exp Lane"] },
  Helcurt: { roles: ["Assassin"], lanes: ["Jungle"] },
  Zhuxin: { roles: ["Mage"], lanes: ["Mid Lane"] },
});
const MYANMAR_GUIDE_SECTIONS = [
  {
    title: "အဖွဲ့တာဝန်များ",
    titleEn: "Squad roles",
    cards: [
      ["IGL / Shot-caller", "ဝိုင်းပိတ်ရာလမ်း၊ အချိန်နှင့် ပြန်ဆုတ်မည့်နေရာကို အဖွဲ့လိုက်ဆုံးဖြတ်ပါ။"],
      ["Entry", "ပထမဆုံးမြင်ကွင်းဖွင့်သူဖြစ်ပြီး အဖွဲ့နှင့် အကွာအဝေးမကျော်ပါနှင့်။"],
      ["Support", "Smoke၊ heal နှင့် ammo ကို စောင့်ရှောက်ပြီး knock ပြန်ယူရာတွင် ကူညီပါ။"],
      ["Scout", "ကား၊ တောင်ကြောနှင့် ရန်သူတည်နေရာကို ကြိုတင်သတင်းပို့ပါ။"],
    ],
  },
  {
    title: "ဝိုင်းပိတ်စီမံချက်",
    titleEn: "Circle plan",
    cards: [
      ["Early", "Loot ကို မလိုအပ်ဘဲ မရှည်ကြာစေဘဲ ပထမဝိုင်းမပိတ်မီ ကားနှင့် compound ရွေးပါ။"],
      ["Mid", "အလယ်ဝိုင်းကို လိုက်ပြီး high ground နှင့် cover နှစ်ခုလုံး ရှိသောနေရာကို ဦးစားပေးပါ။"],
      ["Late", "Smoke အရေအတွက်၊ လူအရေအတွက်နှင့် နောက်ဆုံး cover ကို အဖွဲ့လိုက်ရေတွက်ပြီးမှ ဝင်ပါ။"],
    ],
  },
  {
    title: "အွန်လိုင်းကစားရန် စစ်ဆေးစာရင်း",
    titleEn: "Connection & fair play",
    cards: [
      ["Before queue", "Wi‑Fi နှင့် mobile data latency ကို တစ်ကြိမ်စမ်းပြီး packet loss ရှိလျှင် ranked မဝင်ပါနှင့်။"],
      ["During match", "Ping spike ဖြစ်လျှင် push မလုပ်ဘဲ cover ပြန်ယူပြီး voice call ကို လျှော့ပါ။"],
      ["Fair play", "Account login၊ cheat၊ third-party injector သို့မဟုတ် မသေချာသော player API မသုံးပါနှင့်။"],
    ],
  },
];
// Games pricing stores MMK in whole kyat; currencies with ISO minor units use
// their normal decimal scale. Unknown currencies fail to a conservative
// two-decimal display instead of silently showing a 100x value.
const CURRENCY_DECIMALS = Object.freeze({
  BIF: 0, CLP: 0, DJF: 0, GNF: 0, ISK: 0, JPY: 0, KMF: 0,
  KRW: 0, MGA: 0, PYG: 0, RWF: 0, UGX: 0, UYW: 0, VND: 0, VUV: 0,
  XAF: 0, XOF: 0, XPF: 0, MMK: 0,
});

function formatMinorAmount(value, currency) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || typeof currency !== "string" || !currency) {
    return `— ${currency || ""}`.trim();
  }
  const decimals = Object.prototype.hasOwnProperty.call(CURRENCY_DECIMALS, currency)
    ? CURRENCY_DECIMALS[currency]
    : 2;
  const formatted = (amount / (10 ** decimals)).toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${formatted} ${currency}`;
}

function liveProductPriceSummary(product) {
  if (!product?.live || !Array.isArray(product.offers)) return null;
  const priced = product.offers
    .filter((offer) => Number.isFinite(Number(offer?.price_minor)) && typeof offer?.currency === "string" && offer.currency)
    .map((offer) => ({
      price: Number(offer.price_minor),
      currency: offer.currency,
      promo: Number(offer.promo_discount_minor || 0),
    }))
    .filter((offer) => offer.price >= 0);
  if (!priced.length) return null;
  priced.sort((left, right) => left.price - right.price);
  const lowest = priced[0];
  const promo = priced.find((offer) => offer.promo > 0 && offer.currency === lowest.currency);
  return { lowest, promo };
}

function updateConnectionState(online) {
  if (connectionStatus) {
    connectionStatus.dataset.state = online ? "online" : "offline";
    connectionStatus.textContent = online ? "Service online" : "Offline · order checks unavailable";
  }
  if (offlineBanner) offlineBanner.hidden = online;
}

function updateCatalogContext(state) {
  if (storeStage) storeStage.textContent = state === "live" ? "Current listings"
    : state === "refreshing" ? "Updating listings"
      : state === "cached" ? "Browse only" : state === "error" ? "Catalog unavailable" : "Catalog preview";
  const catalogAvailable = state !== "error" || Boolean(catalogCache);
  savedGamesButton.hidden = !catalogAvailable;
  searchToggle.hidden = !catalogAvailable;
  searchForm.hidden = !catalogAvailable;
  const catalogPulse = document.querySelector(".catalog-pulse");
  const trustNote = document.querySelector(".trust-note");
  if (trustNote) trustNote.hidden = state === "cached" || state === "error";
  const pulseTitle = catalogPulse?.querySelector("strong");
  const pulseDetail = catalogPulse?.querySelector("small");
  if (pulseTitle) pulseTitle.textContent = state === "live" ? "Current listings"
    : state === "refreshing" ? "Updating listings"
      : state === "cached" ? "Saved names" : state === "error" ? "Catalog unavailable" : "Catalog preview";
  if (pulseDetail) pulseDetail.textContent = state === "live"
    ? "Supplier does not report unit stock counts"
    : state === "refreshing"
      ? "Checking current prices and packages"
    : state === "cached"
      ? "Game names are saved here; prices and stock are never cached"
    : state === "error"
      ? "Approved listings and prices are temporarily unavailable"
      : "Offers appear after supplier and pricing review";
  if (!availabilityCopy) return;
  const lead = document.createElement("strong");
  lead.textContent = state === "live" ? "Before you pay. "
    : state === "refreshing" ? "Checking listings. "
    : state === "cached" ? "Browse only. " : state === "error" ? "Catalog unavailable. " : "We show what we can verify. ";
  const detail = document.createTextNode(state === "live"
    ? "Prices come from the latest approved supplier snapshot. Your game account must be checked before payment."
    : state === "refreshing"
      ? "Saved game names stay visible while current prices and packages are checked."
    : state === "cached"
      ? "Saved names are not active offers. Check availability before choosing a package."
    : state === "error"
      ? "Current offers and prices are unavailable right now. No order or payment has started; please check back later."
      : "Offers appear after supplier, region, and pricing review. Your game account must be checked before payment.");
  availabilityCopy.replaceChildren(lead, detail);
}

function setStatus(message, state = "") {
  catalogStatus.textContent = message;
  catalogStatus.dataset.state = state;
}

function renderWalletChoices() {
  for (const amount of REFERENCE_WALLET_AMOUNTS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "wallet-choice";
    button.dataset.value = amount;
    button.setAttribute("aria-pressed", "false");
    button.textContent = amount;
    button.addEventListener("click", () => {
      selectedWalletAmount = amount;
      walletCustomAmount.value = amount.replace(/\s*Ks$/, "");
      walletCustomAmount.setAttribute("aria-invalid", "false");
      walletCustomAmountHint.textContent = WALLET_AMOUNT_HINT;
      updateWalletSelection();
    });
    walletAmounts.append(button);
  }
  for (const method of REFERENCE_WALLET_METHODS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "wallet-choice wallet-choice--method";
    button.dataset.value = method;
    button.dataset.method = method.toLowerCase().replaceAll(" ", "-");
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", method);
    const icon = document.createElement("span");
    icon.className = `wallet-method-icon wallet-method-icon--${button.dataset.method}`;
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = method === "Wave Money" ? "W" : "K";
    const label = document.createElement("span");
    label.className = "wallet-method-label";
    label.textContent = method;
    button.append(icon, label);
    button.addEventListener("click", () => {
      selectedWalletMethod = method;
      updateWalletSelection();
    });
    walletMethods.append(button);
  }
}

function parseWalletAmount(value) {
  const rawValue = String(value || "").trim();
  const validGrouping = /^(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+)$/.test(rawValue);
  if (!validGrouping) return null;
  const normalized = rawValue.replace(/[ ,]/g, "");
  const amount = Number(normalized);
  return Number.isSafeInteger(amount) && amount > 0 ? amount : null;
}

function formatWalletAmount(amount) {
  return `${amount.toLocaleString("en-US")} Ks`;
}

walletCustomAmount.addEventListener("input", () => {
  const rawValue = walletCustomAmount.value.trim();
  const amount = parseWalletAmount(rawValue);
  const invalid = rawValue !== "" && amount === null;
  selectedWalletAmount = amount === null ? "" : formatWalletAmount(amount);
  walletCustomAmount.setAttribute("aria-invalid", String(invalid));
  walletCustomAmountHint.textContent = invalid
    ? "Enter a positive whole MMK amount; commas or spaces must group thousands."
    : WALLET_AMOUNT_HINT;
  updateWalletSelection();
});

function updateWalletSelection() {
  for (const button of walletAmounts.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.value === selectedWalletAmount));
  }
  for (const button of walletMethods.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.value === selectedWalletMethod));
  }
  walletNext.disabled = !selectedWalletAmount || !selectedWalletMethod;
  walletSelection.textContent = selectedWalletAmount && selectedWalletMethod
    ? `${selectedWalletAmount} · ${selectedWalletMethod}`
    : "Choose an amount and method to continue.";
  walletStatus.textContent = "";
}

function setWalletStep(step) {
  const confirmation = step === 2;
  walletDialog.dataset.step = String(step);
  walletClose.setAttribute("aria-label", confirmation ? "Back to amount and method" : "Back to store");
  if (confirmation) {
    const methodClass = selectedWalletMethod.toLowerCase().replaceAll(" ", "-");
    walletTransferAmount.textContent = selectedWalletAmount || "Choose amount";
    walletTransferMethod.textContent = selectedWalletMethod || "Choose payment method";
    walletAccountLabel.textContent = selectedWalletMethod || "No payment method selected";
    walletAccountIcon.className = `wallet-method-icon wallet-method-icon--${methodClass}`;
    walletAccountIcon.textContent = selectedWalletMethod === "Wave Money" ? "W" : selectedWalletMethod === "KBZ Pay" ? "K" : "?";
  }
  walletStepItems.forEach((item, index) => {
    item.setAttribute("aria-current", String((index + 1) === step ? "step" : "false"));
  });
  walletStepOneAmount.hidden = confirmation;
  walletStepOneMethod.hidden = confirmation;
  walletStepOneBoundary.hidden = confirmation;
  walletSelection.hidden = confirmation;
  walletConfirmationPreview.hidden = !confirmation;
  walletNext.textContent = confirmation ? "Confirm Topup" : "Next";
  walletNext.disabled = confirmation || !selectedWalletAmount || !selectedWalletMethod;
}

function renderNavigation(items) {
  navigationItems = items;
  navigationList.replaceChildren();
  for (const item of items) {
    if (!DRAWER_TOOL_IDS.has(item.id)) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "navigation-item";
    const label = ({ "mbb-hero-guide": "MLBB Reference Guide", "myanmar-game-guide": "PUBG Myanmar Guide" })[item.id]
      || (item.id === "top-up" ? "Wallet" : item.label);
    button.textContent = label;
    const icon = document.createElement("span");
    icon.className = "navigation-item__icon";
    icon.setAttribute("aria-hidden", "true");
    icon.dataset.kind = /guide/.test(item.id) ? "guide" : /history/.test(item.id) ? "history" : /profile/.test(item.id) ? "profile" :
      /top-up/.test(item.id) ? "wallet" : /gift/.test(item.id) ? "gift" : /region/.test(item.id) ? "region" : /support/.test(item.id) ? "help" : /news|event/.test(item.id) ? "event" : "game";
    button.append(icon);
    button.dataset.surface = item.surface || "unknown";
    button.dataset.status = ["order-history", "product-order-history", "top-up-history", "update-profile"].includes(item.id) ? "account" : item.status || "available";
    button.addEventListener("click", () => openNavigationItem({ ...item, label }));
    navigationList.append(button);
  }
}

function openNavigationById(id) {
  const item = navigationItems.find((candidate) => candidate.id === id);
  if (item) {
    openNavigationItem(item);
    return;
  }
  openAuxiliary({
    id,
    label: "Reference surface",
    surface: "not_captured",
    status: "unavailable",
    notice: "The read-only navigation manifest is still loading; no action is available.",
  });
}

function renderAuxiliaryEmptyState(item) {
  auxiliaryContent.replaceChildren();
  if (Array.isArray(item.tabs) && item.tabs.length > 0) {
    const tabs = document.createElement("div");
    tabs.className = "auxiliary-tabs";
    tabs.setAttribute("role", "tablist");
    const emptyMessage = document.createElement("p");
    emptyMessage.className = "auxiliary-empty";
    emptyMessage.textContent = item.empty_label || "No Data";
    for (const [index, tab] of item.tabs.entries()) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "auxiliary-tab";
      button.textContent = tab;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(index === 0));
      button.addEventListener("click", () => {
        for (const sibling of tabs.querySelectorAll("button")) {
          sibling.setAttribute("aria-selected", String(sibling === button));
        }
        auxiliaryStatus.textContent = `${tab} · ${item.empty_label || "No Data"}`;
      });
      tabs.append(button);
    }
    auxiliaryContent.append(tabs, emptyMessage);
    auxiliaryStatus.textContent = `${item.tabs[0]} · ${item.empty_label || "No Data"}`;
    return;
  }
  const emptyMessage = document.createElement("p");
  emptyMessage.className = "auxiliary-empty";
  emptyMessage.textContent = item.empty_label || "No Data";
  auxiliaryContent.append(emptyMessage);
  auxiliaryStatus.textContent = item.empty_label || "No Data";
}

function renderReferenceFeed(item) {
  auxiliaryContent.replaceChildren();
  const entries = Array.isArray(item.items) ? item.items : [];
  if (entries.length === 0) {
    const emptyMessage = document.createElement("p");
    emptyMessage.className = "auxiliary-empty";
    emptyMessage.textContent = "No reference news was captured.";
    auxiliaryContent.append(emptyMessage);
    auxiliaryStatus.textContent = "Reference feed is empty.";
    return;
  }
  const feed = document.createElement("div");
  feed.className = "reference-feed";
  for (const entry of entries) {
    const article = document.createElement("article");
    article.className = "reference-feed__item";
    const title = document.createElement("h3");
    title.textContent = entry.title || "Untitled reference item";
    const date = document.createElement("time");
    date.textContent = entry.date || "Date not captured";
    const summary = document.createElement("p");
    summary.textContent = entry.summary || "Reference summary not captured.";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "reference-feed__open";
    open.textContent = "Open details";
    open.addEventListener("click", () => renderReferenceNewsDetail(entry, item));
    article.append(title, date, summary, open);
    feed.append(article);
  }
  auxiliaryContent.append(feed);
  auxiliaryStatus.textContent = `${entries.length} reference news items · images and links unavailable`;
}

function renderReferenceNewsDetail(entry, item) {
  auxiliaryTitle.textContent = `${item.label} · News Detail`;
  auxiliaryContent.replaceChildren();
  const back = document.createElement("button");
  back.type = "button";
  back.className = "reference-feed__back";
  back.textContent = "← Back to Events/News";
  back.addEventListener("click", () => renderReferenceFeed(item));
  const article = document.createElement("article");
  article.className = "reference-feed__detail";
  const title = document.createElement("h3");
  title.textContent = entry.title || "Untitled reference item";
  const date = document.createElement("time");
  date.textContent = entry.date || "Date not captured";
  const body = document.createElement("p");
  body.textContent = entry.summary || "Reference detail body was not captured.";
  const note = document.createElement("p");
  note.className = "reference-feed__note";
  note.textContent = "Reference metadata only · original artwork, links, and full article body are not copied.";
  article.append(title, date, body, note);
  auxiliaryContent.append(back, article);
  auxiliaryStatus.textContent = "Reference detail · no external link or action is available.";
}

function guideElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function guideSnapshotDate(value) {
  if (typeof value !== "string") return "";
  const source = value.trim();
  const match = /^(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$/.exec(source);
  if (!match) return "";
  const timestamp = Date.parse(source);
  if (!Number.isFinite(timestamp)) return "";
  const utcDate = new Date(timestamp).toISOString().slice(0, 10);
  if (source.length === 10 && utcDate !== match[1]) return "";
  return utcDate;
}

function mlbbRankingsFreshnessPresentation(freshness) {
  return {
    status: "withheld",
    copy: "Live rankings are paused until source terms and data freshness are verified.",
  };
}

function guideSvgIcon(iconName, className) {
  const wrapper = guideElement("span", className);
  wrapper.dataset.icon = iconName;
  wrapper.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("aria-hidden", "true");
  (GUIDE_ICON_SHAPES[iconName] || []).forEach(([tag, attributes]) => {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => shape.setAttribute(name, value));
    svg.append(shape);
  });
  wrapper.append(svg);
  const kenneyName = GUIDE_KENNEY_ICONS[iconName];
  if (kenneyName) {
    wrapper.dataset.asset = kenneyName;
    const image = guideElement("img", "guide-game-icon");
    image.src = `/game-store/assets/kenney/${kenneyName}.png`;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.setAttribute("aria-hidden", "true");
    wrapper.append(image);
  }
  const gameIconName = GUIDE_EXTERNAL_GAME_ICONS[iconName];
  if (gameIconName) {
    wrapper.dataset.asset = gameIconName;
    const image = guideElement("img", "guide-game-icon");
    image.src = `/game-store/assets/game-icons/${gameIconName}.svg`;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.setAttribute("aria-hidden", "true");
    wrapper.append(image);
  }
  return wrapper;
}

function guideHeroRole(name, candidates = []) {
  const candidate = candidates.find((hero) => (hero?.name || hero?.slug) === name);
  const roles = Array.isArray(candidate?.roles) ? candidate.roles : GUIDE_HERO_METADATA[name]?.roles;
  return Array.isArray(roles) ? roles.find((role) => GUIDE_ROLE_ART[role]) || "" : "";
}

function guideHeroAvatar(name, className = "guide-avatar", role = "", candidates = []) {
  const avatar = guideElement("span", className);
  const resolvedRole = GUIDE_ROLE_ART[role] ? role : guideHeroRole(name, candidates);
  const art = GUIDE_ROLE_ART[resolvedRole] || { icon: "crossed-swords", hue: 214 };
  avatar.dataset.hero = name;
  if (resolvedRole) avatar.dataset.role = resolvedRole;
  avatar.setAttribute("style", `--avatar-hue:${art.hue}`);
  avatar.setAttribute("aria-hidden", "true");
  const icon = guideElement("img", "guide-avatar__icon");
  icon.src = `/game-store/assets/game-icons/${art.icon}.svg`;
  icon.alt = "";
  icon.loading = "lazy";
  icon.decoding = "async";
  icon.setAttribute("aria-hidden", "true");
  avatar.append(icon);
  return avatar;
}

function guideItemIconName(category, name) {
  const normalized = String(category || "").toLowerCase();
  if (normalized === "magic") return "item-magic";
  if (normalized === "defense") return "item-defense";
  if (normalized === "movement") return "item-movement";
  if (normalized === "jungling") return "item-jungling";
  if (normalized === "roaming") return "item-roaming";
  if (normalized === "attack") return String(name || "").toLowerCase().includes("bow") ? "emblem-marksman" : "item-attack";
  return "item-magic";
}

function guideEmblemIconName(name) {
  const normalized = String(name || "").toLowerCase();
  return GUIDE_ICON_SHAPES[`emblem-${normalized}`] ? `emblem-${normalized}` : "emblem-tank";
}

function setGuideRoute(route) {
  activeGuideRoute = route;
  auxiliaryDialog.dataset.guidePage = route.id;
  if (route.view) {
    auxiliaryDialog.dataset.guideLocale = route.view.locale || "";
  } else if (!["tool:item-list", "tool:emblems-talents"].includes(route.id)) {
    auxiliaryDialog.dataset.guideLocale = "";
  }
  auxiliaryTitle.textContent = route.title;
  const parent = guideRouteStack[guideRouteStack.length - 1];
  auxiliaryClose.setAttribute("aria-label", parent ? `Back to ${parent.title}` : "Close page");
  if (route.view) {
    auxiliaryTitle.textContent = route.view.title;
    auxiliaryCopy.textContent = route.view.copy;
    auxiliaryStatus.textContent = route.view.status;
    auxiliaryHeaderActions.replaceChildren(...route.view.headerActions);
    auxiliaryContent.replaceChildren(...route.view.children);
  } else {
    auxiliaryHeaderActions.replaceChildren();
    route.render();
  }
  auxiliaryDialog.scrollTop = 0;
  const scroller = document.querySelector(".auxiliary-dialog__body");
  if (scroller) scroller.scrollTop = route.view?.scrollTop || 0;
}

function openGuideRoute(route) {
  if (activeGuideRoute) {
    activeGuideRoute.view = {
      scrollTop: document.querySelector(".auxiliary-dialog__body")?.scrollTop || 0,
      title: auxiliaryTitle.textContent,
      copy: auxiliaryCopy.textContent,
      status: auxiliaryStatus.textContent,
      locale: auxiliaryDialog.dataset.guideLocale || "",
      headerActions: [...auxiliaryHeaderActions.children],
      children: [...auxiliaryContent.children],
    };
    guideRouteStack.push(activeGuideRoute);
  }
  setGuideRoute(route);
}

function closeGuideRoute() {
  const previous = guideRouteStack.pop();
  if (previous) {
    setGuideRoute(previous);
    return;
  }
  auxiliaryDialog.close();
}

function resetGuideRoutes() {
  auxiliaryDialog.scrollTop = 0;
  const scroller = document.querySelector(".auxiliary-dialog__body");
  if (scroller) scroller.scrollTop = 0;
  guideRouteStack = [];
  activeGuideRoute = null;
  auxiliaryDialog.dataset.guidePage = "";
  auxiliaryHeaderActions.replaceChildren();
  auxiliaryClose.setAttribute("aria-label", "Close page");
}

function createGuideLanguageControl(locale, onChange) {
  const group = guideElement("div", "guide-segmented guide-segmented--locale");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Guide language");
  const controls = new Map();
  for (const [value, label] of [["en", "Eng"], ["my", "My"]]) {
    const button = guideElement("button", value === locale ? "is-selected" : "", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(value === locale));
    button.setAttribute("aria-label", value === "en" ? "English" : "Myanmar");
    button.addEventListener("click", () => onChange(value));
    controls.set(value, button);
    group.append(button);
  }
  return {
    element: group,
    setLocale(value) {
      controls.forEach((button, language) => {
        const selected = language === value;
        button.className = selected ? "is-selected" : "";
        button.setAttribute("aria-pressed", String(selected));
      });
    },
  };
}

function guideToolTitle(toolId) {
  return GUIDE_TOOL_TITLES[toolId] || "MLBB Hero Guide";
}

function guideFilterRow(label, values, selected, onSelect) {
  const section = guideElement("section", "guide-filter-group");
  section.append(guideElement("h3", "guide-filter-title", label));
  const row = guideElement("div", "guide-filter-row");
  values.forEach((value, index) => {
    const isSelected = Boolean(selected) && selected === value;
    const button = guideElement("button", isSelected ? "guide-filter-chip is-selected" : "guide-filter-chip", value);
    button.type = "button";
    button.setAttribute("aria-pressed", String(isSelected));
    button.addEventListener("click", () => onSelect(value));
    row.append(button);
  });
  section.append(row);
  return section;
}

async function hydrateGuide(slug, item, renderer) {
  try {
    const response = await fetch(`/api/v1/strategy-guides/${slug}`, { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const payload = await response.json();
    if (!payload || payload.slug !== slug) return;
    guidePayloadCache.set(slug, payload);
    const route = [activeGuideRoute, ...guideRouteStack].find((candidate) => candidate?.id === slug);
    if (route) {
      route.render = () => renderer(guidePayloadCache.get(slug));
      if (route.view?.copy?.startsWith("Loading the local")) route.view = null;
    }
    if (activeGuideRoute?.id === slug) {
      renderer(payload);
    }
  } catch (_error) {
    // The static screenshot-derived reference remains available when the local
    // guide API or an operator snapshot is not present.
  }
}

function renderHeroGuide(item, payload = null) {
  payload = payload || guidePayloadCache.get("mlbb") || null;
  auxiliaryTitle.textContent = item.label;
  auxiliaryCopy.textContent = payload
    ? "Browse hero roles and lanes. Ranked data is paused while source terms and freshness are reviewed."
    : "Loading the local, attributed strategy snapshot. No player, match, or account data is queried.";
  auxiliaryContent.replaceChildren();
  const search = guideElement("input", "guide-search");
  search.type = "search";
  search.placeholder = "Search guide tools…";
  search.setAttribute("aria-label", "Search guide");
  auxiliaryContent.append(search);
  const rankingsFreshness = mlbbRankingsFreshnessPresentation(payload?.rankings_freshness);
  const freshnessNote = guideElement("p", "guide-rankings-freshness", rankingsFreshness.copy);
  freshnessNote.dataset.rankingsFreshness = rankingsFreshness.status;
  freshnessNote.setAttribute("role", "status");
  auxiliaryContent.append(freshnessNote);

  const toolsHeading = guideElement("h3", "guide-section-heading", "Tactical Tools");
  const tools = guideElement("div", "guide-tool-grid");
  GUIDE_TOOLS.forEach((tool) => {
    const card = guideElement("button", `guide-tool-card guide-tool-card--${tool.tone}`);
    card.type = "button";
    card.dataset.search = `${tool.title} ${tool.copy}`.toLocaleLowerCase();
    card.append(guideSvgIcon(tool.id, "guide-tool-icon"), guideElement("strong", "", tool.title), guideElement("small", "", tool.copy));
    card.addEventListener("click", () => openGuideRoute({
      id: `tool:${tool.id}`,
      title: guideToolTitle(tool.id),
      render: () => renderGuideTool(tool.id, item, guidePayloadCache.get("mlbb") || payload),
    }));
    tools.append(card);
  });
  auxiliaryContent.append(toolsHeading, tools, guideElement("h3", "guide-section-heading", "Game Wiki"));
  const wiki = guideElement("div", "guide-wiki-list");
  GUIDE_WIKI.forEach((entry) => {
    const row = guideElement("button", "guide-wiki-row");
    row.type = "button";
    row.dataset.search = `${entry.title} ${entry.copy}`.toLocaleLowerCase();
    row.append(guideSvgIcon(entry.icon, "guide-wiki-icon"), guideElement("span", "guide-wiki-copy"));
    row.children[1].append(guideElement("strong", "", entry.title), guideElement("small", "", entry.copy));
    row.append(guideElement("span", "guide-wiki-arrow", "›"));
    row.addEventListener("click", () => openGuideRoute({
      id: `tool:${entry.id}`,
      title: guideToolTitle(entry.id),
      render: () => renderGuideTool(entry.id, item, guidePayloadCache.get("mlbb") || payload),
    }));
    wiki.append(row);
  });
  auxiliaryContent.append(wiki);
  const attribution = guideElement("p", "guide-source-note", "Hero and item references: Mobile Legends Wiki · CC BY-SA. Portraits are omitted; role emblems use separate licenses.");
  const sourceLink = guideElement("a", "guide-source-link", "Data source & license");
  sourceLink.href = "https://mlbbdex.com/en/api-doc";
  sourceLink.target = "_blank";
  sourceLink.rel = "noreferrer noopener";
  attribution.append(sourceLink);
  const artLink = guideElement("a", "guide-source-link", "Icon credits");
  artLink.href = "/game-store/assets/game-icons/ATTRIBUTION.txt";
  artLink.target = "_blank";
  artLink.rel = "noreferrer noopener";
  attribution.append(artLink);
  auxiliaryContent.append(attribution);
  search.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    [...tools.children, ...wiki.children].forEach((entry) => {
      const searchable = entry.dataset.search || entry.textContent.toLocaleLowerCase();
      entry.hidden = Boolean(query) && !searchable.includes(query);
    });
  });
  const snapshotDate = guideSnapshotDate(payload?.snapshot?.captured_at);
  auxiliaryStatus.textContent = payload
    ? `MLBB reference${snapshotDate ? ` · snapshot ${snapshotDate}` : ""} · no player data queried.`
    : "MLBB guide · waiting for the local, attributed strategy snapshot.";
  if (!payload) void hydrateGuide("mlbb", item, (nextPayload) => renderHeroGuide(item, nextPayload));
}

function renderMyanmarGameGuide(item, guideSlug = "pubg-mobile-myanmar", payload = null) {
  payload = payload || guidePayloadCache.get(guideSlug) || null;
  const isFreeFire = guideSlug === "free-fire-myanmar";
  const gameName = payload?.game || (isFreeFire ? "Garena Free Fire" : "PUBG Mobile");
  auxiliaryDialog.dataset.guidePage = guideSlug;
  auxiliaryTitle.textContent = isFreeFire ? "Free Fire Myanmar Guide" : "PUBG Myanmar Guide";
  auxiliaryCopy.textContent = "";
  auxiliaryContent.replaceChildren();
  const banner = guideElement("section", "myanmar-guide-banner");
  const badge = guideElement("span", "myanmar-guide-badge");
  badge.setAttribute("aria-hidden", "true");
  badge.append(guideSvgIcon("myanmar-guide", "myanmar-guide-badge__icon"));
  banner.append(
    badge,
    guideElement("div", "myanmar-guide-banner__copy"),
  );
  banner.children[1].append(
    guideElement("strong", "", isFreeFire ? "Garena Free Fire" : "PUBG Mobile"),
    guideElement("small", "", "မြန်မာလို အဖွဲ့ကစားနည်းနှင့် connection checklist"),
  );
  auxiliaryContent.append(banner);
  const gameSwitch = guideElement("div", "guide-segmented myanmar-guide-games");
  gameSwitch.setAttribute("role", "group");
  gameSwitch.setAttribute("aria-label", "Choose game guide");
  [
    ["pubg-mobile-myanmar", "PUBG Mobile"],
    ["free-fire-myanmar", "Free Fire"],
  ].forEach(([slug, label]) => {
    const button = guideElement("button", slug === guideSlug ? "is-selected" : "", label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(slug === guideSlug));
    button.addEventListener("click", () => {
      if (slug === guideSlug) return;
      if (activeGuideRoute) {
        activeGuideRoute.id = slug;
        activeGuideRoute.render = () => renderMyanmarGameGuide(item, slug, guidePayloadCache.get(slug) || null);
      }
      renderMyanmarGameGuide(item, slug, guidePayloadCache.get(slug) || null);
    });
    gameSwitch.append(button);
  });
  auxiliaryContent.append(gameSwitch);
  const patchEffectiveOn = payload?.patch_changes_effective_on || "2026-10-01";
  const patchStatus = payload?.patch_status || "unknown";
  const patchStatusMessage = patchStatus === "scheduled_unverified"
    ? `changes are scheduled for ${patchEffectiveOn}; verify after rollout`
    : patchStatus === "verification_required"
      ? `the scheduled date ${patchEffectiveOn} has passed; verify the current game client`
      : "patch availability is unknown; verify the current game client";
  const noteText = isFreeFire
    ? `OB55 reference · reviewed ${payload?.reviewed_on || "2026-09-24"}; ${patchStatusMessage}.`
    : "Offline strategy reference · no player stats.";
  const note = guideElement("p", "guide-source-note", noteText);
  auxiliaryContent.append(note);
  const sections = Array.isArray(payload?.sections) && payload.sections.length
    ? payload.sections.map((section) => ({
      title: section.title || section.title_en || "Guide section",
      titleEn: section.title_en || "Reference section",
      cards: Array.isArray(section.cards)
        ? section.cards.map((card) => [card.title || "Tip", card.body || "Reference guidance not captured."])
        : [],
    }))
    : isFreeFire ? [] : MYANMAR_GUIDE_SECTIONS;
  if (sections.length === 0) {
    auxiliaryContent.append(guideElement(
      "p",
      "guide-analysis-empty",
      payload ? "Guide content is not available in this snapshot." : "Loading the local, offline guide. No external player-data request is made.",
    ));
  }
  sections.forEach((section) => {
    const panel = guideElement("section", "myanmar-guide-section");
    const heading = guideElement("div", "myanmar-guide-section__heading");
    const title = String(section.title || "Guide section");
    const titleEn = String(section.titleEn || "Reference section");
    heading.append(guideElement("h3", "", title));
    if (titleEn.trim().toLocaleLowerCase() !== title.trim().toLocaleLowerCase()) {
      heading.append(guideElement("span", "", titleEn));
    }
    const cards = guideElement("div", "myanmar-guide-cards");
    section.cards.forEach(([title, body]) => {
      const card = guideElement("article", "myanmar-guide-card");
      card.append(guideElement("strong", "", title), guideElement("p", "", body));
      cards.append(card);
    });
    panel.append(heading, cards);
    auxiliaryContent.append(panel);
  });
  const footer = guideElement("p", "guide-source-note", isFreeFire
    ? "General practice only · no current build or rank claims. Use the official client and follow anti-cheat rules."
    : "Strategy advice is a local reference, not a guarantee of rank, ping, or match outcome.");
  if (isFreeFire) {
    const patchNotesLink = guideElement("a", "guide-source-link", "Official OB55 patch notes");
    patchNotesLink.href = "https://ff.garena.com/en/article/1712/";
    patchNotesLink.target = "_blank";
    patchNotesLink.rel = "noreferrer noopener";
    const sourceLink = guideElement("a", "guide-source-link", "Garena Free Fire official support");
    sourceLink.href = "https://ff.garena.com/en/support/";
    sourceLink.target = "_blank";
    sourceLink.rel = "noreferrer noopener";
    footer.append(" ", patchNotesLink, " · ", sourceLink);
  }
  auxiliaryContent.append(footer);
  auxiliaryStatus.textContent = payload
    ? `${gameName} guide · ${payload.patch_version || payload.freshness || "offline reference"}${payload.reviewed_on ? ` · reviewed ${payload.reviewed_on}` : ""} · no player data queried.`
    : `${gameName} guide · offline reference; no player data queried.`;
  if (!payload) void hydrateGuide(guideSlug, item, (nextPayload) => renderMyanmarGameGuide(item, guideSlug, nextPayload));
}

function renderGuideTool(toolId, item, payload = null) {
  payload = payload || guidePayloadCache.get("mlbb") || null;
  auxiliaryContent.replaceChildren();
  auxiliaryTitle.textContent = guideToolTitle(toolId);
  const payloadSections = Array.isArray(payload?.sections) ? payload.sections : [];
  const payloadHeroSection = payloadSections.find((section) => section.id === "hero-tiers");
  const payloadItemSection = payloadSections.find((section) => section.id === "items");
  if (toolId === "hero-tier-list") {
    const payloadHeroes = Array.isArray(payloadHeroSection?.heroes) ? payloadHeroSection.heroes : [];
    const heroes = payloadHeroes.length
      ? payloadHeroes
      : Object.entries(GUIDE_HERO_METADATA).map(([name, metadata]) => ({ name, ...metadata }));
    const roleValues = payloadHeroSection?.filters?.roles || GUIDE_ROLES;
    const laneValues = payloadHeroSection?.filters?.lanes || GUIDE_LANES;
    let selectedRole = "";
    let selectedLane = "";
    let searchQuery = "";
    let visibleHeroCount = 24;
    const filters = guideElement("div", "guide-filters");
    const search = guideElement("input", "guide-search guide-hero-directory__search");
    search.type = "search";
    search.placeholder = "Search heroes by name…";
    search.setAttribute("aria-label", "Search heroes by name");
    search.addEventListener("input", () => {
      searchQuery = search.value.trim().toLocaleLowerCase();
      visibleHeroCount = 24;
      renderHeroes();
    });
    const count = guideElement("p", "guide-directory-count", "");
    count.setAttribute("role", "status");
    count.setAttribute("aria-live", "polite");
    const snapshotDate = guideSnapshotDate(payload?.snapshot?.captured_at);
    const freshness = guideElement("p", "guide-directory-note",
      `Roles and lanes are from a saved snapshot${snapshotDate ? ` · ${snapshotDate}` : ""}; verify changes in-game.`);
    const heroGrid = guideElement("div", "guide-hero-directory");
    const showMore = guideElement("button", "guide-directory-more", "Show 24 more heroes");
    showMore.type = "button";
    showMore.addEventListener("click", () => {
      visibleHeroCount += 24;
      renderHeroes();
    });
    const heroMetadata = new Map();
    heroes.forEach((hero) => {
      const name = hero?.name || hero?.slug;
      if (typeof name === "string") heroMetadata.set(name, hero);
    });
    const renderHeroes = () => {
      filters.replaceChildren(
        guideFilterRow("Filter by Roles", roleValues, selectedRole, (value) => {
          selectedRole = selectedRole === value ? "" : value;
          visibleHeroCount = 24;
          renderHeroes();
        }),
        guideFilterRow("Filter by Lanes", laneValues, selectedLane, (value) => {
          selectedLane = selectedLane === value ? "" : value;
          visibleHeroCount = 24;
          renderHeroes();
        }),
      );
      const filtered = heroes.filter((entry) => {
        const name = entry?.name || entry?.slug || "";
        const metadata = heroMetadata.get(name) || {};
        const roles = Array.isArray(metadata.roles) ? metadata.roles : [];
        const lanes = Array.isArray(metadata.lanes) ? metadata.lanes : [];
        const searchable = `${name} ${roles.join(" ")} ${lanes.join(" ")}`.toLocaleLowerCase();
        return (!selectedRole || roles.includes(selectedRole))
          && (!selectedLane || lanes.includes(selectedLane))
          && (!searchQuery || searchable.includes(searchQuery));
      });
      count.textContent = `Showing ${Math.min(filtered.length, visibleHeroCount)} of ${filtered.length} ${filtered.length === 1 ? "hero" : "heroes"}`;
      heroGrid.replaceChildren();
      if (!filtered.length) {
        heroGrid.append(guideElement("p", "guide-analysis-empty", "No heroes match those filters."));
        showMore.hidden = true;
        return;
      }
      filtered.slice(0, visibleHeroCount).forEach((entry) => {
        const hero = entry.name || entry.slug;
        if (typeof hero !== "string" || !hero) return;
        const metadata = heroMetadata.get(hero) || {};
        const roles = Array.isArray(metadata.roles) ? metadata.roles : [];
        const lanes = Array.isArray(metadata.lanes) ? metadata.lanes : [];
        const role = roles[0] || "";
        const descriptor = [roles.join(" · "), lanes.join(" · ")].filter(Boolean).join("  /  ") || "Details not in this snapshot";
        const card = guideElement("button", "guide-hero-card");
        card.type = "button";
        card.dataset.hero = hero;
        card.setAttribute("aria-label", `${hero}${roles.length ? `, roles ${roles.join(", ")}` : ""}${lanes.length ? `, lanes ${lanes.join(", ")}` : ""}`);
        card.append(
          guideHeroAvatar(hero, "guide-avatar", role, heroes),
          guideElement("span", "guide-hero-card__name", hero),
          guideElement("span", "guide-hero-card__stats", descriptor),
        );
        card.addEventListener("click", () => openGuideRoute({
          id: `hero:${hero}`,
          title: hero,
          render: () => renderHeroReferenceDetail(item, guidePayloadCache.get("mlbb") || payload, hero),
        }));
        heroGrid.append(card);
      });
      showMore.hidden = visibleHeroCount >= filtered.length;
    };
    auxiliaryContent.append(search, count, freshness, filters, heroGrid, showMore);
    renderHeroes();
    auxiliaryStatus.textContent = "Hero roles and lanes are reference data. Rankings are paused pending source-term and freshness review.";
    return;
  }
  if (toolId === "item-list") {
    let locale = "en";
    auxiliaryDialog.dataset.guideLocale = locale;
    const copy = GUIDE_LOCALE_TEXT[locale];
    const language = createGuideLanguageControl(locale, (nextLocale) => {
      locale = nextLocale;
      auxiliaryDialog.dataset.guideLocale = locale;
      language.setLocale(locale);
      applyLocale();
      render(search.value);
      auxiliaryTitle.textContent = GUIDE_LOCALE_TEXT[locale].itemList;
      updateStatus();
    });
    const search = guideElement("input", "guide-search");
    search.type = "search";
    search.placeholder = copy.searchItems;
    const categories = guideElement("div", "guide-filter-row");
    const itemCategories = payloadItemSection?.categories || ["All", "Attack", "Attack & Magic", "Attack, Magic & Defense"];
    const categoryValues = itemCategories.includes("All") ? itemCategories : ["All", ...itemCategories];
    categoryValues.forEach((value, index) => {
      const button = guideElement("button", index === 0 ? "guide-filter-chip is-selected" : "guide-filter-chip", copy[valueKey(value)] || value);
      button.type = "button";
      button.dataset.category = value;
      button.setAttribute("aria-pressed", String(index === 0));
      button.addEventListener("click", () => {
        selectedCategory = value === "All" ? "" : value;
        categories.querySelectorAll("button").forEach((sibling) => {
          const selected = sibling === button;
          sibling.className = selected ? "guide-filter-chip is-selected" : "guide-filter-chip";
          sibling.setAttribute("aria-pressed", String(selected));
        });
        render(search.value);
      });
      categories.append(button);
    });
    const sort = guideElement("select", "guide-sort");
    [["name", copy.sortName], ["price", copy.sortPrice]].forEach(([value, label]) => {
      const option = guideElement("option", "", label);
      option.value = value;
      sort.append(option);
    });
    const list = guideElement("div", "guide-item-list");
    const payloadItems = Array.isArray(payloadItemSection?.items)
      ? payloadItemSection.items.map((entry) => ({
        name: entry.name || entry.slug || "Item",
        category: entry.category || "Attack",
        price: entry.price === null || entry.price === undefined ? "—" : String(entry.price),
        stats: Array.isArray(entry.stats) && entry.stats.length ? entry.stats.join("  ") : entry.bonus || "Stats are not present in this snapshot",
        summary: entry.summary || "",
        detail: [entry.unique, entry.passive, entry.active].filter((value) => typeof value === "string" && value.length).join(" "),
        recipe: Array.isArray(entry.recipe) ? entry.recipe.filter((value) => typeof value === "string") : [],
        bestFor: entry.best_for || "",
      }))
      : GUIDE_ITEMS.map((entry) => ({ ...entry, summary: "", recipe: [], bestFor: "", category: entry.category || "Attack" }));
    const items = payloadItems.length ? payloadItems : GUIDE_ITEMS.map((entry) => ({ ...entry, summary: "", recipe: [], bestFor: "", category: entry.category || "Attack" }));
    let selectedCategory = "";
    let sortMode = "name";
    function valueKey(value) {
      return ({
        "Attack & Magic": "attackMagic",
        "Attack, Magic & Defense": "attackMagicDefense",
      })[value] || value.toLowerCase();
    }
    function applyLocale() {
      const labels = GUIDE_LOCALE_TEXT[locale];
      search.placeholder = labels.searchItems;
      categories.querySelectorAll("button").forEach((button) => {
        button.textContent = labels[valueKey(button.dataset.category)] || button.dataset.category;
      });
      if (sort.children[0]) sort.children[0].textContent = labels.sortName;
      if (sort.children[1]) sort.children[1].textContent = labels.sortPrice;
    }
    function updateStatus() {
      const labels = GUIDE_LOCALE_TEXT[locale];
      auxiliaryStatus.textContent = payloadItemSection?.items?.length
        ? `${labels.itemCountPrefix} ${payloadItemSection.items.length} · ${labels.gameDataNote} ${labels.snapshotNote}${guideSnapshotDate(payload?.snapshot?.captured_at) ? ` · snapshot ${guideSnapshotDate(payload.snapshot.captured_at)}` : ""}`
        : `${labels.gameDataNote} ${labels.referenceSampleNote}`;
    }
    const render = (query = "") => {
      list.replaceChildren();
      const visibleItems = items
        .filter((entry) => !selectedCategory || entry.category === selectedCategory)
        .filter((entry) => entry.name.toLowerCase().includes(query.toLowerCase()))
        .sort((left, right) => sortMode === "price"
          ? (Number.parseInt(left.price, 10) || 0) - (Number.parseInt(right.price, 10) || 0)
          : left.name.localeCompare(right.name));
      visibleItems.forEach((entry) => {
        const card = guideElement("article", "guide-item-card");
        const copy = guideElement("div", "guide-item-copy");
        copy.append(guideElement("strong", "", entry.name), guideElement("p", "guide-item-stats", entry.stats));
        if (entry.summary) copy.append(guideElement("p", "guide-item-summary", entry.summary));
        if (entry.detail) copy.append(guideElement("p", "guide-item-passive", entry.detail));
        const labels = GUIDE_LOCALE_TEXT[locale];
        if (entry.recipe.length) copy.append(guideElement("small", "guide-item-recipe", `${labels.buildsFrom} · ${entry.recipe.join(" + ")}`));
        if (entry.bestFor) copy.append(guideElement("small", "guide-item-best-for", `${labels.usefulFor} · ${entry.bestFor}`));
        card.append(guideSvgIcon(guideItemIconName(entry.category, entry.name), "guide-item-orb"), copy, guideElement("b", "guide-item-price", `◉ ${entry.price}`));
        list.append(card);
      });
    };
    search.addEventListener("input", () => render(search.value));
    sort.addEventListener("change", () => {
      sortMode = sort.value === "price" ? "price" : "name";
      render(search.value);
    });
    render(search.value);
    auxiliaryHeaderActions.append(language.element);
    auxiliaryContent.append(search, categories, sort, list);
    updateStatus();
    return;
  }
  if (toolId === "emblems-talents") {
    let locale = "en";
    auxiliaryDialog.dataset.guideLocale = locale;
    const language = createGuideLanguageControl(locale, (nextLocale) => {
      locale = nextLocale;
      auxiliaryDialog.dataset.guideLocale = locale;
      language.setLocale(locale);
      auxiliaryTitle.textContent = GUIDE_LOCALE_TEXT[locale].emblems;
      title.textContent = GUIDE_LOCALE_TEXT[locale].mainEmblems;
      auxiliaryStatus.textContent = GUIDE_LOCALE_TEXT[locale].emblemsNote;
    });
    const title = guideElement("h3", "guide-section-heading", GUIDE_LOCALE_TEXT[locale].mainEmblems);
    const grid = guideElement("div", "guide-emblem-grid");
    GUIDE_EMBLEMS.forEach((entry) => {
      const card = guideElement("article", `guide-emblem-card guide-emblem-card--${entry.tone}`);
      card.append(guideSvgIcon(guideEmblemIconName(entry.name), "guide-emblem-orb"), guideElement("h4", "", entry.name));
      const stats = guideElement("ul", "");
      entry.stats.forEach((stat) => stats.append(guideElement("li", "", stat)));
      card.append(stats);
      grid.append(card);
    });
    auxiliaryHeaderActions.append(language.element);
    auxiliaryContent.append(title, grid);
    auxiliaryStatus.textContent = GUIDE_LOCALE_TEXT[locale].emblemsNote;
    return;
  }
  if (toolId === "counter-picks") {
    renderDraftTeamSurface("Check enemy lane coverage", true, item, payloadHeroSection?.heroes, payload);
    return;
  }
  if (toolId === "draft-strategy") {
    renderDraftTeamSurface("Analyze lane coverage", false, item, payloadHeroSection?.heroes, payload);
    return;
  }
  const empty = guideElement("div", "guide-analysis-empty", "Select heroes to generate reference insights and recommendations.");
  auxiliaryContent.append(empty);
  auxiliaryStatus.textContent = "Reference tool shell only · no game account or match data is queried.";
}

function renderHeroReferenceDetail(item, payload, heroName) {
  payload = payload || guidePayloadCache.get("mlbb") || null;
  auxiliaryTitle.textContent = heroName;
  auxiliaryCopy.textContent = "Reference hero detail · no player, match, or account data is queried.";
  auxiliaryContent.replaceChildren();
  const payloadHeroes = Array.isArray(payload?.sections)
    ? payload.sections.find((section) => section.id === "hero-tiers")?.heroes || []
    : [];
  const snapshot = payloadHeroes.find((hero) => (hero?.name || hero?.slug) === heroName) || {};
  const staticMetadata = GUIDE_HERO_METADATA[heroName] || {};
  const roles = Array.isArray(snapshot.roles) && snapshot.roles.length ? snapshot.roles : staticMetadata.roles || [];
  const lanes = Array.isArray(snapshot.lanes) && snapshot.lanes.length ? snapshot.lanes : staticMetadata.lanes || [];
  const panel = guideElement("section", "guide-hero-detail");
  const identity = guideElement("div", "guide-hero-detail__identity");
  identity.append(
    guideHeroAvatar(heroName, "guide-hero-detail__orb", roles[0] || "", payloadHeroes),
    guideElement("div", "guide-hero-detail__name", heroName),
  );
  const tags = guideElement("div", "guide-hero-detail__tags");
  [...roles, ...lanes].forEach((tag) => tags.append(guideElement("span", "guide-hero-detail__tag", tag)));
  const style = guideElement("p", "guide-hero-detail__style", roles.length ? `Roles · ${roles.join(" · ")}` : "Role details are not in this snapshot.");
  const rankingNote = guideElement("p", "guide-hero-detail__score", "Rankings and match statistics are paused until source terms and current data are verified.");
  const officialLink = guideElement("a", "guide-source-link", "Check the official hero list");
  officialLink.href = "https://www.mobilelegends.com/hero/list";
  officialLink.target = "_blank";
  officialLink.rel = "noreferrer noopener";
  panel.append(identity, tags, style, rankingNote, officialLink);
  auxiliaryContent.append(panel);
  auxiliaryStatus.textContent = "Hero role reference · no ranking or match data is displayed.";
}

function renderDraftTeamSurface(actionLabel, hasSelections, item, payloadHeroes = [], payload = null) {
  const payloadNames = Array.isArray(payloadHeroes)
    ? payloadHeroes.map((hero) => hero?.name || hero?.slug).filter((name) => typeof name === "string")
    : [];
  const pickerHeroes = [...new Set(payloadNames)].slice(0, 200);
  const state = {
    activeTeam: hasSelections ? "enemy" : "ally",
    ally: [],
    enemy: [],
    suggestionsVisible: false,
    revision: 0,
  };
  let searchQuery = "";
  let selectedRole = "";
  let selectedLane = "";
  let visibleHeroCount = 24;
  const shell = guideElement("div", "guide-draft-shell");
  const teams = guideElement("div", "guide-draft-teams");
  const picker = guideElement("section", "guide-draft-picker");
  const pickerTitle = guideElement("div", "guide-draft-picker__header");
  pickerTitle.append(guideElement("strong", "", "Choose Heroes"), guideElement("span", "", "Select up to five per team"));
  const search = guideElement("input", "guide-search");
  search.type = "search";
  search.placeholder = "Search heroes by name…";
  search.setAttribute("aria-label", "Search draft heroes by name");
  search.addEventListener("input", () => {
    searchQuery = search.value.trim().toLocaleLowerCase();
    visibleHeroCount = 24;
    renderPicker();
  });
  const filters = guideElement("div", "guide-draft-filters");
  const filterValues = (values, label) => {
    const select = guideElement("select", "guide-sort");
    select.setAttribute("aria-label", label);
    const allOption = guideElement("option", "", `All ${label.toLowerCase()}`);
    allOption.value = "";
    select.append(allOption);
    values.forEach((value) => {
      const option = guideElement("option", "", value);
      option.value = value;
      select.append(option);
    });
    return select;
  };
  const heroFilters = payload?.sections?.find((section) => section.id === "hero-tiers")?.filters || {};
  const roleFilter = filterValues(Array.isArray(heroFilters.roles) ? heroFilters.roles : GUIDE_ROLES, "Roles");
  const laneFilter = filterValues(Array.isArray(heroFilters.lanes) ? heroFilters.lanes : GUIDE_LANES, "Lanes");
  [roleFilter, laneFilter].forEach((select) => select.addEventListener("change", () => {
    selectedRole = roleFilter.value;
    selectedLane = laneFilter.value;
    visibleHeroCount = 24;
    renderPicker();
  }));
  filters.append(roleFilter, laneFilter);
  const count = guideElement("p", "guide-directory-count", "");
  count.setAttribute("role", "status");
  count.setAttribute("aria-live", "polite");
  const grid = guideElement("div", "guide-draft-picker__grid");
  const showMore = guideElement("button", "guide-directory-more", "Show 24 more heroes");
  showMore.type = "button";
  showMore.addEventListener("click", () => {
    visibleHeroCount += 24;
    renderPicker();
  });
  picker.append(pickerTitle, search, filters, count, grid, showMore);
  const analysis = guideElement("div", "guide-draft-analysis");
  const snapshotDate = guideSnapshotDate(payload?.snapshot?.captured_at);
  const analysisScope = `Lane coverage only${snapshotDate ? ` · roster snapshot ${snapshotDate}` : ""}. No counter, synergy, win-rate, or pick recommendation is generated.`;
  function renderPicker() {
    const heroes = pickerHeroes.filter((name) => {
      const entry = payloadHeroes.find((hero) => (hero?.name || hero?.slug) === name);
      const roles = Array.isArray(entry?.roles) ? entry.roles : [];
      const lanes = Array.isArray(entry?.lanes) ? entry.lanes : [];
      return (!selectedRole || roles.includes(selectedRole))
        && (!selectedLane || lanes.includes(selectedLane))
        && (!searchQuery || `${name} ${roles.join(" ")} ${lanes.join(" ")}`.toLocaleLowerCase().includes(searchQuery));
    });
    count.textContent = `Showing ${Math.min(heroes.length, visibleHeroCount)} of ${heroes.length} ${heroes.length === 1 ? "hero" : "heroes"}`;
    grid.replaceChildren();
    if (!heroes.length) grid.append(guideElement("p", "guide-analysis-empty", "No heroes match those filters."));
    heroes.slice(0, visibleHeroCount).forEach((hero) => {
      const selected = state.ally.includes(hero) || state.enemy.includes(hero);
      const card = guideElement("button", selected ? "guide-draft-hero is-selected" : "guide-draft-hero");
      card.type = "button";
      card.dataset.hero = hero;
      card.setAttribute("aria-pressed", String(selected));
      card.setAttribute("aria-label", `${hero}${selected ? ", selected" : ""}`);
      card.append(guideHeroAvatar(hero, "guide-avatar", "", payloadHeroes), guideElement("span", "guide-draft-hero__name", hero));
      card.addEventListener("click", () => {
        const team = state[state.activeTeam];
        if (team.includes(hero)) state[state.activeTeam] = team.filter((candidate) => candidate !== hero);
        else if (team.length < 5 && !selected) state[state.activeTeam] = [...team, hero];
        render();
      });
      grid.append(card);
    });
    showMore.hidden = visibleHeroCount >= heroes.length;
  }
  const render = () => {
    state.revision += 1;
    teams.replaceChildren();
    const teamDefinitions = hasSelections
      ? [{ key: "enemy", label: "Enemy Team", tone: "enemy" }]
      : [{ key: "ally", label: "Ally Team", tone: "ally" }, { key: "enemy", label: "Enemy Team", tone: "enemy" }];
    teamDefinitions.forEach(({ key, label, tone }) => {
      const team = guideElement("section", `guide-draft-team guide-draft-team--${tone}`);
      const title = guideElement("div", "guide-draft-team__header");
      const selectTeam = guideElement("button", key === state.activeTeam ? "guide-draft-team__select is-selected" : "guide-draft-team__select", label);
      selectTeam.type = "button";
      selectTeam.addEventListener("click", () => {
        state.activeTeam = key;
        render();
      });
      const reset = guideElement("button", "guide-reset", "Reset");
      reset.type = "button";
      reset.addEventListener("click", () => {
        state[key] = [];
        state.suggestionsVisible = false;
        render();
      });
      title.append(selectTeam, reset);
      const slots = guideElement("div", "guide-draft-slots");
      for (let index = 0; index < 5; index += 1) {
        const hero = state[key][index];
        const slot = guideElement("button", hero ? "guide-draft-slot is-filled" : "guide-draft-slot", hero ? "" : "+");
        slot.type = "button";
        if (hero) {
          slot.dataset.hero = hero;
          slot.setAttribute("aria-label", `Remove ${hero} from ${label}`);
          slot.append(guideHeroAvatar(hero, "guide-avatar", "", payloadHeroes), guideElement("span", "guide-draft-slot__name", hero));
        } else {
          slot.setAttribute("aria-label", `Empty ${label} slot ${index + 1}`);
        }
        if (hero) slot.addEventListener("click", () => {
          state[key] = state[key].filter((candidate) => candidate !== hero);
          render();
        });
        slots.append(slot);
      }
      team.append(title, slots);
      teams.append(team);
    });
    pickerTitle.lastChild.textContent = `Editing ${state.activeTeam === "enemy" ? "Enemy" : "Ally"} Team`;
    renderPicker();
    analysis.replaceChildren();
    analysis.append(guideElement("h3", "guide-section-heading", "Lane coverage"),
      guideElement("p", "guide-analysis-empty", `Assigns lanes recorded for selected heroes. ${analysisScope}`));
    const action = guideElement("button", "guide-primary-action", actionLabel);
    action.type = "button";
    action.disabled = state.ally.length + state.enemy.length === 0;
    action.addEventListener("click", async () => {
      const revision = state.revision;
      const selections = Object.fromEntries(["ally", "enemy"].map((side) => [side,
        state[side].map((name) => payloadHeroes.find((hero) => (hero?.name || hero?.slug) === name)?.slug).filter(Boolean)]));
      action.disabled = true;
      action.textContent = "Analyzing…";
      auxiliaryStatus.textContent = "Checking lane assignments against the stored roster…";
      try {
        const response = await fetch("/api/v1/strategy-guides/mlbb/draft-analysis", {
          method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(selections),
        });
        if (!response.ok) throw new Error("analysis unavailable");
        const result = await response.json();
        if (revision !== state.revision || !action.isConnected || !auxiliaryDialog.open) return;
        if (!result.teams || !result.roster_revision) throw new Error("invalid analysis");
        const details = guideElement("section", "guide-analysis-result");
        for (const side of ["ally", "enemy"]) {
          const team = result.teams[side];
          if (!team || !Array.isArray(team.assignments) || !Array.isArray(team.open_lanes)) throw new Error("invalid analysis");
          details.append(guideElement("h4", "", side === "ally" ? "Ally team" : "Enemy team"));
          details.append(guideElement("p", "", team.selected_count
            ? team.assignments.map((row) => {
              const hero = payloadHeroes.find((entry) => entry.slug === row.hero_slug);
              return `${hero?.name || row.hero_slug}: ${row.lane}`;
            }).join(" · ") || "No recorded lane assignment."
            : "No heroes selected."));
          details.append(guideElement("p", "", `Open lanes: ${team.open_lanes.join(", ") || "None"}`));
        }
        analysis.querySelectorAll(".guide-analysis-result").forEach((node) => node.remove());
        analysis.append(details);
        auxiliaryStatus.textContent = result.notice;
      } catch {
        if (revision === state.revision && action.isConnected) {
          auxiliaryStatus.textContent = "Analysis unavailable. Your picks are kept; try again.";
        }
      } finally {
        if (revision === state.revision && action.isConnected) {
          action.disabled = false;
          action.textContent = actionLabel;
        }
      }
    });
    analysis.append(action);
  };
  shell.append(teams, picker, analysis);
  auxiliaryContent.append(shell);
  render();
  auxiliaryStatus.textContent = `Recorded community roster · not independently verified for the current patch. ${analysisScope} Analysis uses the local backend; no player account is queried.`;
}

function renderRegionChecker(item) {
  auxiliaryTitle.textContent = item.label;
  auxiliaryCopy.textContent = "Enter the Account ID and Zone ID shown in your in-game profile. No password is needed.";
  auxiliaryContent.replaceChildren();
  const form = guideElement("form", "region-checker");
  form.append(guideElement("h3", "", "Enter account details"));
  const account = guideElement("input", "guide-input");
  account.type = "text";
  account.inputMode = "numeric";
  account.maxLength = 20;
  account.required = true;
  account.autocomplete = "off";
  account.placeholder = "Account ID";
  account.setAttribute("aria-label", "Account ID");
  account.setAttribute("list", "mlbb-account-history");
  const accountOptions = document.createElement("datalist");
  accountOptions.id = "mlbb-account-history";
  const accountHelp = guideElement("p", "guide-form-status", "Saved Account IDs stay in this browser.");
  const historyKey = MLBB_ACCOUNT_HISTORY_KEY;
  const validAccount = MLBB_ACCOUNT_ID_PATTERN;
  let accountHistory = [];
  function readHistory() {
    try {
      const saved = JSON.parse(globalThis.localStorage?.getItem(historyKey) || "null");
      if (Array.isArray(saved)) {
        return [...new Set(saved.filter((id) => typeof id === "string" && validAccount.test(id)))];
      }
    } catch { /* Browser storage may be unavailable. */ }
    return accountHistory;
  }
  accountHistory = readHistory();
  account.value = accountHistory[0] || "";
  const clearHistory = guideElement("button", "detail-actions__clear", "Clear saved Account IDs");
  clearHistory.type = "button";
  function showHistory() {
    accountOptions.replaceChildren();
    for (const id of accountHistory) {
      const option = document.createElement("option");
      option.value = id;
      accountOptions.append(option);
    }
    clearHistory.hidden = accountHistory.length === 0;
    accountHelp.textContent = accountHistory.length
      ? "Recent Account IDs are saved in this browser. Clear them here whenever you want."
      : "Your last searched Account ID will be saved in this browser for next time.";
  }
  function saveHistory() {
    try { globalThis.localStorage?.setItem(historyKey, JSON.stringify(accountHistory)); }
    catch { /* Keep suggestions available in this form when storage fails. */ }
    showHistory();
  }
  function rememberAccount() {
    const id = account.value.trim();
    if (!validAccount.test(id)) return;
    accountHistory = [id, ...readHistory().filter((previous) => previous !== id)];
    saveHistory();
  }
  account.addEventListener("change", rememberAccount);
  clearHistory.addEventListener("click", () => {
    accountHistory = [];
    account.value = "";
    saveHistory();
    status.textContent = "Saved Account IDs cleared.";
    account.focus();
  });
  showHistory();
  const zone = guideElement("input", "guide-input");
  zone.type = "text";
  zone.maxLength = 120;
  zone.required = true;
  zone.autocomplete = "off";
  zone.placeholder = "Zone ID from your profile";
  zone.setAttribute("aria-label", "Zone ID from your MLBB profile");
  zone.setAttribute("list", "mlbb-zone-options");
  zone.setAttribute("aria-describedby", "mlbb-zone-help");
  const zoneOptions = document.createElement("datalist");
  zoneOptions.id = "mlbb-zone-options";
  const zoneLabel = guideElement("label", "", "Zone ID");
  const clearZoneHistory = guideElement("button", "detail-actions__clear", "Clear saved zones");
  clearZoneHistory.type = "button";
  const zoneHelp = guideElement("p", "guide-form-status", "Enter the numeric Zone ID shown in your MLBB profile.");
  zoneHelp.id = "mlbb-zone-help";
  let zoneDirectory = [];
  function showZoneOptions() {
    zoneOptions.replaceChildren(...zoneDirectory.map((entry) => {
      const option = document.createElement("option");
      option.value = `${entry.name} (${entry.id})`;
      option.label = `${entry.name} · ID ${entry.id}`;
      return option;
    }), ...recentMlbbZoneIds().filter((id) => !zoneDirectory.some((entry) => entry.id === id)).map((id) => {
      const option = document.createElement("option");
      option.value = id;
      option.label = `Recently used · Zone ID ${id}`;
      return option;
    }));
    clearZoneHistory.hidden = recentMlbbZoneIds().length === 0;
    zone.placeholder = zoneDirectory.length ? "Zone ID or zone name" : "Zone ID from your profile";
    zone.setAttribute("aria-label", zoneDirectory.length ? "Zone ID or name" : "Zone ID from your MLBB profile");
    zoneLabel.textContent = zoneDirectory.length ? "Zone ID or name" : "Zone ID";
    zoneHelp.textContent = zoneDirectory.length
      ? "Choose a zone name with its ID, or enter the numeric Zone ID from your profile."
      : recentMlbbZoneIds().length
        ? "Official zone names are unavailable. Choose a recently used ID or enter the numeric Zone ID from your profile."
        : "Zone names are unavailable. Enter the numeric Zone ID from your game profile.";
  }
  fetch("/api/v1/games/mlbb/zones", { headers: { Accept: "application/json" } })
    .then((response) => response.ok ? response.json() : null)
    .then((directory) => {
      zoneDirectory = (directory?.zones || []).filter((entry) => typeof entry?.name === "string"
        && typeof entry?.id === "string" && MLBB_ZONE_ID_PATTERN.test(entry.id));
      showZoneOptions();
    }).catch(() => {
      zoneDirectory = [];
      showZoneOptions();
    });
  const submit = guideElement("button", "guide-primary-action", "Check Account");
  submit.type = "submit";
  const status = guideElement("p", "guide-form-status");
  status.setAttribute("role", "status");
  clearZoneHistory.addEventListener("click", () => {
    const cleared = clearMlbbZoneIds();
    showZoneOptions();
    status.textContent = cleared ? "Saved Zone IDs cleared from this browser." : "This browser could not clear saved Zone IDs.";
    zone.focus();
  });
  form.append(guideElement("label", "", "Account ID"), account, accountOptions, accountHelp,
    zoneLabel, zone, zoneOptions, zoneHelp, clearZoneHistory, submit, status, clearHistory);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const playerId = account.value.trim();
    const zoneId = zone.value.trim();
    rememberAccount();
    if (!validAccount.test(playerId) || !zoneId) {
      status.textContent = "Enter a numeric Account ID and your zone.";
      return;
    }
    submit.disabled = account.disabled = zone.disabled = clearHistory.disabled = true;
    status.textContent = "Checking account…";
    try {
      const result = await recipientLookupRequest({ player_id: playerId, server_id: zoneId }, "mobile-legends");
      if (typeof result.player_name !== "string" || typeof result.region !== "string") {
        throw new Error("The provider did not confirm a player name and region.");
      }
      rememberMlbbZoneId(zoneId);
      showZoneOptions();
      status.textContent = `${result.player_name} · Account region: ${result.region}`;
    } catch (error) {
      status.textContent = error.status ? error.message : "Account check is unavailable. Try again shortly.";
    } finally {
      submit.disabled = account.disabled = zone.disabled = clearHistory.disabled = false;
    }
  });
  for (const input of [account, zone]) input.addEventListener("input", () => { status.textContent = ""; });
  auxiliaryContent.append(form);
  auxiliaryStatus.textContent = "";
}

const NOTICE_SEEN_KEY = "aurix-store-seen-notices-v1";
function seenNoticeIds() {
  try {
    const value = JSON.parse(localStorage.getItem(NOTICE_SEEN_KEY) || "[]");
    return new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string") : []);
  } catch { return new Set(); }
}

async function getStoreNotices() {
  const response = await fetch("/api/v1/store/notices", {
    headers: { Accept: "application/json" }, cache: "no-store", signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("Store notices could not be loaded");
  const payload = await response.json();
  if (!Array.isArray(payload.notices) || payload.notices.some((notice) => !notice
      || typeof notice.notice_id !== "string" || typeof notice.title !== "string"
      || typeof notice.body !== "string" || typeof notice.updated_at !== "string"
      || !["info", "success", "warning"].includes(notice.severity))) {
    throw new Error("Store notices did not match the expected format");
  }
  return payload.notices;
}

function noticeKey(notice) { return `${notice.notice_id}:${notice.updated_at || notice.created_at}`; }

function updateNoticeBadge(notices) {
  const seen = seenNoticeIds();
  const unread = notices.filter((notice) => !seen.has(noticeKey(notice))).length;
  notificationCount.hidden = unread === 0;
  notificationCount.textContent = unread > 9 ? "9+" : String(unread);
  notificationOpen.setAttribute("aria-label", unread ? `Open store notices, ${unread} unread` : "Open store notices");
}

function noticeCard(notice) {
  const article = document.createElement("article");
  article.className = "store-notice";
  article.dataset.severity = notice.severity;
  const meta = document.createElement("div");
  meta.className = "store-notice__meta";
  const severity = document.createElement("span");
  severity.className = "store-notice__severity";
  severity.textContent = notice.severity === "warning" ? "Important" : notice.severity === "success" ? "Good news" : "Update";
  const date = new Date(notice.updated_at || notice.created_at);
  const time = document.createElement("time");
  if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
  time.textContent = Number.isNaN(date.getTime()) ? "Recently updated" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
  meta.append(severity, time);
  const heading = document.createElement("h3");
  heading.textContent = notice.title;
  const body = document.createElement("p");
  body.textContent = notice.body;
  article.append(meta, heading, body);
  return article;
}

async function refreshNoticeBadge() {
  try { updateNoticeBadge(await getStoreNotices()); }
  catch { notificationCount.hidden = true; }
}

async function renderNoticeCenter() {
  auxiliaryContent.replaceChildren();
  auxiliaryContent.setAttribute("aria-busy", "true");
  auxiliaryStatus.textContent = "Loading store updates…";
  try {
    const notices = await getStoreNotices();
    if (auxiliaryDialog.dataset.surface !== "notice_center") return;
    const seen = seenNoticeIds();
    for (const notice of notices) seen.add(noticeKey(notice));
    try { localStorage.setItem(NOTICE_SEEN_KEY, JSON.stringify([...seen].slice(-200))); } catch { /* Reading notices still works without local storage. */ }
    updateNoticeBadge(notices);
    if (!notices.length) {
      const empty = document.createElement("section");
      empty.className = "store-notice-empty";
      empty.setAttribute("role", "status");
      const heading = document.createElement("strong");
      heading.textContent = "You’re all caught up";
      const copy = document.createElement("p");
      copy.textContent = "New store updates will appear here.";
      empty.append(heading, copy);
      auxiliaryContent.append(empty);
      auxiliaryStatus.textContent = "";
      return;
    }
    const feed = document.createElement("div");
    feed.className = "store-notice-feed";
    notices.forEach((notice) => feed.append(noticeCard(notice)));
    auxiliaryContent.append(feed);
    auxiliaryStatus.textContent = `${notices.length} current update${notices.length === 1 ? "" : "s"}`;
  } catch {
    if (auxiliaryDialog.dataset.surface !== "notice_center") return;
    auxiliaryContent.replaceChildren();
    const message = document.createElement("section");
    message.className = "store-notice-empty store-notice-empty--error";
    message.setAttribute("role", "alert");
    const heading = document.createElement("strong");
    heading.textContent = "Updates couldn’t load";
    const copy = document.createElement("p");
    copy.textContent = "Check your connection and retry.";
    message.append(heading, copy);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "store-notice-retry";
    retry.textContent = "Retry";
    retry.addEventListener("click", () => void renderNoticeCenter());
    auxiliaryContent.append(message, retry);
    auxiliaryStatus.textContent = "";
  } finally {
    if (auxiliaryDialog.dataset.surface === "notice_center") auxiliaryContent.setAttribute("aria-busy", "false");
  }
}

function renderLeaderboardEntries(entries, list, limit = 20) {
  list.replaceChildren();
  for (const entry of entries.slice(0, limit)) {
    const row = document.createElement("li");
    row.className = "store-leaderboard-row";
    row.dataset.rank = String(entry.rank);
    const rank = document.createElement("span");
    rank.className = "store-leaderboard-row__rank";
    rank.textContent = `#${entry.rank}`;
    const name = document.createElement("strong");
    name.textContent = entry.display_name;
    const achievement = document.createElement("span");
    achievement.className = "store-leaderboard-row__achievement";
    achievement.textContent = entry.achievement;
    const detail = document.createElement("small");
    const next = entry.orders_to_next
      ? `${entry.orders_to_next} to ${entry.next_achievement}`
      : "highest badge";
    detail.textContent = `${entry.completed_orders} delivered · ${next}`;
    row.append(rank, name, achievement, detail);
    list.append(row);
  }
}

async function fetchLeaderboard() {
  const response = await fetch("/api/v1/store/leaderboard", {
    headers: { Accept: "application/json" }, cache: "no-store", signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("Leaderboard unavailable");
  const payload = await response.json();
  if (payload.period !== "rolling_30_days" || typeof payload.as_of !== "string"
      || !Number.isFinite(Date.parse(payload.as_of)) || !Array.isArray(payload.entries)
      || payload.entries.length > 20
      || payload.entries.some((entry) => !entry || !Number.isSafeInteger(entry.rank)
        || entry.rank < 1 || entry.rank > 20
        || typeof entry.display_name !== "string" || entry.display_name.length < 2 || entry.display_name.length > 24
        || !Number.isSafeInteger(entry.completed_orders) || entry.completed_orders < 1
        || !["Starter", "Rising", "Elite", "Legend"].includes(entry.achievement)
        || !(entry.next_achievement === null || ["Rising", "Elite", "Legend"].includes(entry.next_achievement))
        || !Number.isSafeInteger(entry.orders_to_next) || entry.orders_to_next < 0)) {
    throw new Error("Leaderboard did not match the expected format");
  }
  return payload.entries;
}

async function loadLeaderboardPreview() {
  try {
    const entries = await fetchLeaderboard();
    renderLeaderboardEntries(entries, leaderboardPreview, 3);
    leaderboardStatus.textContent = entries.length
      ? "Completed orders · rolling 30 days"
      : "No opted-in entries in the last 30 days.";
  } catch {
    leaderboardPreview.replaceChildren();
    leaderboardStatus.textContent = "Leaderboard is temporarily unavailable.";
  }
}

async function renderLeaderboardCenter() {
  auxiliaryContent.replaceChildren();
  const list = document.createElement("ol");
  list.className = "store-leaderboard-list";
  const status = document.createElement("p");
  status.className = "store-leaderboard-status";
  status.setAttribute("role", "status");
  status.textContent = "Loading completed orders…";
  auxiliaryContent.append(status, list);
  try {
    const entries = await fetchLeaderboard();
    if (auxiliaryDialog.dataset.surface !== "leaderboard") return;
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "auxiliary-empty";
      empty.textContent = "No buyers have opted in yet.";
      const icon = document.querySelector(".home-leaderboard__icon")?.cloneNode(true);
      if (icon) empty.prepend(icon);
      auxiliaryContent.replaceChildren(empty);
      auxiliaryStatus.textContent = "";
    } else {
      renderLeaderboardEntries(entries, list);
      status.textContent = "Delivered top-ups · rolling 30 days · tied buyers share a rank.";
      auxiliaryStatus.textContent = "Only opted-in display names appear. Order IDs, game IDs, and payment amounts stay private.";
    }
    const join = document.createElement("button");
    join.type = "button";
    join.className = "store-leaderboard-join";
    join.textContent = "Choose my leaderboard name";
    join.addEventListener("click", () => {
      auxiliaryDialog.close();
      if (globalThis.AurixCustomer) void globalThis.AurixCustomer.openProfile();
      else document.querySelector("#account-open")?.click();
    });
    auxiliaryContent.append(join);
  } catch {
    status.textContent = "Leaderboard is temporarily unavailable. Try again shortly.";
    auxiliaryStatus.textContent = "Could not refresh the ranking.";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "store-leaderboard-join";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => void renderLeaderboardCenter());
    auxiliaryContent.append(retry);
  }
}

function openAuxiliary(item) {
  auxiliaryDialog.dataset.surface = item.surface || "reference";
  auxiliaryDialog.dataset.guideLocale = "";
  resetGuideRoutes();
  auxiliaryContent.setAttribute("aria-busy", "false");
  auxiliaryTitle.textContent = item.label;
  auxiliaryClose.setAttribute("aria-label", item.surface === "notice_center" ? "Close notices" : "Close page");
  auxiliaryClose.textContent = item.surface === "notice_center" ? "" : "×";
  auxiliaryCopy.textContent = item.surface === "notice_center" ? "" : item.notice || "This reference surface is read-only in the preview.";
  auxiliaryStatus.textContent = "";
  if (item.surface === "hero_guide") {
    setGuideRoute({
      id: "mlbb",
      title: item.label,
      render: () => renderHeroGuide(item, guidePayloadCache.get("mlbb") || null),
    });
  } else if (item.surface === "strategy_guide") {
    const guideSlug = typeof item.guide_slug === "string" ? item.guide_slug : "pubg-mobile-myanmar";
    setGuideRoute({
      id: guideSlug,
      title: item.label,
      render: () => renderMyanmarGameGuide(item, guideSlug, guidePayloadCache.get(guideSlug) || null),
    });

  } else if (item.surface === "support_chat") {
    renderSupportChat(item);
  } else if (item.surface === "region_checker") {
    renderRegionChecker(item);
  } else if (item.surface === "empty_state") {
    renderAuxiliaryEmptyState(item);
  } else if (item.surface === "reference_feed") {
    renderReferenceFeed(item);
  } else if (item.surface === "notice_center") {
    void renderNoticeCenter();
  } else if (item.surface === "leaderboard") {
    void renderLeaderboardCenter();
  } else {
    auxiliaryContent.replaceChildren();
    const notice = document.createElement("p");
    notice.className = "auxiliary-placeholder";
    notice.textContent = item.notice || "Reference content was not captured.";
    auxiliaryContent.append(notice);
    auxiliaryStatus.textContent = ["disabled", "unavailable"].includes(item.status)
      ? "Unavailable in this preview."
      : "Preview only · no action is available.";
  }
  auxiliaryDialog.showModal();
}

function openNavigationItem(item) {
  navigationDialog.close();
  const customer = globalThis.AurixCustomer;
  if (customer && ["order-history", "product-order-history", "top-up-history"].includes(item.id)) {
    void customer.openHistory(item.id === "top-up-history" ? "topups" : "orders");
    return;
  }
  if (customer && ["update-profile", "profile"].includes(item.id)) {
    void customer.openProfile();
    return;
  }
  if (customer && item.id === "logout") {
    void customer.openProfile();
    return;
  }
  if (item.id === "home") {
    auxiliaryDialog.close();
    return;
  }
  if (item.id === "top-up") {
    openWallet();
    return;
  }
  openAuxiliary(item);
}

notificationOpen.addEventListener("click", () => openAuxiliary({
  id: "store-notices", label: "Store notices", surface: "notice_center",
  notice: "Updates from the AuriX Game Store.",
}));
document.querySelector("#leaderboard-open").addEventListener("click", () => openAuxiliary({
  id: "top-buyers", label: "Most active buyers", surface: "leaderboard",
  notice: "Delivered top-ups · rolling 30 days. Names appear only by choice; no prize is active.",
}));

function openWallet() {
  setStoreTab("wallet");
  if (globalThis.AurixCustomer?.openWallet) void globalThis.AurixCustomer.openWallet();
  else document.querySelector("#account-open")?.click();
}

async function loadNavigation() {
  try {
    const response = await fetch("/api/v1/navigation", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`Navigation request failed (${response.status})`);
    const payload = await response.json();
    if (payload.checkout_enabled !== false && payload.checkout_enabled !== true || !Array.isArray(payload.items)) {
      throw new Error("Navigation response did not match the preview contract");
    }
    renderNavigation(payload.items);
  } catch (error) {
    navigationSummary.textContent = "Navigation is temporarily unavailable.";
    navigationList.replaceChildren();
    const notice = document.createElement("p");
    notice.className = "auxiliary-placeholder";
    notice.textContent = "The read-only navigation manifest could not be loaded.";
    navigationList.append(notice);
  }
}

function addCategoryButton(category) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "category-chip";
  const categoryLabel = `${category.id} ${category.name}`.replace(/[-_]+/g, " ");
  const isCurrentListings = /\b(?:current\s+listings?|live\s+(?:offers?|listings?))\b/i.test(categoryLabel);
  const categoryKind = isCurrentListings ? "current"
    : /\bpc\b|computer/i.test(categoryLabel) ? "pc"
      : /gift|card|redeem/i.test(categoryLabel) ? "gift"
        : /mobile|app/i.test(categoryLabel) ? "mobile" : "category";
  button.textContent = category.name;
  button.dataset.categoryKind = categoryKind;
  if (category.disabled) {
    button.disabled = true;
    button.title = "No verified live listings are available yet.";
    button.setAttribute("aria-label", "Current listings are not available yet");
  }
  button.dataset.category = category.id;
  button.setAttribute("aria-pressed", String(category.id === activeCategory));
  button.addEventListener("click", () => {
    activeCategory = category.id === activeCategory ? "" : category.id;
    refreshCategoryButtons();
    loadCatalog();
  });
  categoryList.append(button);
}

function addAllCategoryButton(label = "All Games") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "category-chip";
  button.textContent = label;
  button.dataset.categoryKind = "all";
  button.dataset.category = "";
  button.setAttribute("aria-pressed", String(activeCategory === ""));
  button.addEventListener("click", () => {
    activeCategory = "";
    refreshCategoryButtons();
    loadCatalog();
  });
  categoryList.append(button);
}

function refreshCategoryButtons() {
  for (const button of categoryList.querySelectorAll(".category-chip[data-category]")) {
    button.setAttribute("aria-pressed", String((button.dataset.category || "") === activeCategory));
  }
}

function productCard(product, catalogIndex) {
  const card = document.createElement("article");
  card.className = "product-card";
  card.tabIndex = 0;
  card.dataset.slug = product.slug || "game";
  card.dataset.catalogIndex = String(catalogIndex);
  card.setAttribute("aria-label", `${product.name} game product`);

  // The reference app makes the whole game tile feel tappable. Keep the
  // explicit button for keyboard/accessibility users, while allowing a
  // direct card tap to enter the same detail surface.
  card.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    showProduct(product);
  });
  card.addEventListener("keydown", (event) => {
    if (event.target !== card || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    showProduct(product);
  });

  const icon = document.createElement("div");
  icon.className = "product-card__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = product.name.slice(0, 1).toUpperCase();

  const cover = document.createElement("div");
  cover.className = "product-card__cover";
  cover.setAttribute("aria-hidden", "true");
  const monogram = document.createElement("strong");
  monogram.className = "product-card__monogram";
  monogram.textContent = ({ "mobile-legends": "MLBB", "pubg-mobile": "PUBG", "genshin-impact": "GI", freefire: "FF" })[product.slug]
    || product.name.split(/\s+/).slice(0, 3).map((word) => word[0]).join("").toUpperCase();
  cover.append(monogram);
  const coverLabel = document.createElement("span");
  coverLabel.className = "product-card__cover-label";
  coverLabel.textContent = "GAME ITEMS";
  cover.append(coverLabel);
  const pinBadge = document.createElement("span");
  pinBadge.className = "product-card__pin-badge";
  pinBadge.setAttribute("aria-hidden", "true");
  pinBadge.textContent = "PINNED FIRST";
  pinBadge.hidden = true;
  cover.append(pinBadge);
  attachGameArt(cover, product);

  const category = document.createElement("p");
  category.className = "product-card__category";
  category.textContent = product.supplier_listed === true ? "FazerCards catalog" : product.category;
  category.hidden = product.live === true;

  const title = document.createElement("h3");
  title.textContent = product.name;

  const badges = document.createElement("span");
  badges.className = "product-card__sample-badges";
  if (Array.isArray(product.reference_badges) && product.reference_badges.length > 0) {
    badges.textContent = `Reference label: ${product.reference_badges.join(", ")}`;
  }

  const status = document.createElement("span");
  status.className = "product-card__status";
  status.textContent = product.cached_listing
    ? "Live status unverified"
    : product.supplier_listed === true
    ? Number.isSafeInteger(product.preview_package_count) && product.preview_package_count > 0
      ? `${product.preview_package_count} package options · snapshot`
      : "Supplier-listed · review pending"
    : product.live
    ? `${Array.isArray(product.offers) ? product.offers.length : 0} packages listed`
    : product.preview_package_count ? `${product.preview_package_count} packages · preview` : "In review";

  const statusRow = document.createElement("span");
  statusRow.className = "product-card__status-row";
  statusRow.hidden = product.cached_listing === true;
  const statusDot = document.createElement("i");
  statusDot.className = product.live ? "product-card__status-dot product-card__status-dot--live" : "product-card__status-dot";
  statusDot.setAttribute("aria-hidden", "true");
  statusRow.append(statusDot, status);

  let priceRow = null;
  const priceSummary = liveProductPriceSummary(product);
  if (priceSummary) {
    priceRow = document.createElement("span");
    priceRow.className = "product-card__price-row";
    const price = document.createElement("strong");
    price.className = "product-card__price";
    price.textContent = `from ${formatMinorAmount(priceSummary.lowest.price, priceSummary.lowest.currency)}`;
    priceRow.append(price);
    if (priceSummary.promo) {
      const promo = document.createElement("span");
      promo.className = "product-card__promo";
      promo.textContent = `save ${formatMinorAmount(priceSummary.promo.promo, priceSummary.promo.currency)}`;
      priceRow.append(promo);
    }
    card.setAttribute("aria-label", `${product.name} game product · ${price.textContent}`);
  }

  const details = document.createElement("button");
  details.type = "button";
  details.className = "product-card__details";
  const action = product.cached_listing ? "Check availability"
    : product.supplier_listed === true
      ? Number.isSafeInteger(product.preview_package_count) && product.preview_package_count > 0 ? "Browse packages" : "View listing"
      : product.live || product.reference_detail_available === true ? "View packages" : "View status";
  details.textContent = `${action} ›`;
  details.setAttribute("aria-label", `${action} for ${product.name}`);
  details.addEventListener("click", () => showProduct(product));

  const favorite = document.createElement("button");
  favorite.type = "button";
  favorite.className = "product-card__favorite";
  const favoriteIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  favoriteIcon.setAttribute("class", "product-card__favorite-icon");
  favoriteIcon.setAttribute("viewBox", "0 0 24 24");
  favoriteIcon.setAttribute("aria-hidden", "true");
  const favoriteStar = document.createElementNS("http://www.w3.org/2000/svg", "path");
  favoriteStar.setAttribute("d", "m12 2.75 2.85 5.78 6.38.93-4.62 4.5 1.09 6.35L12 17.31l-5.7 3 1.09-6.35-4.62-4.5 6.38-.93L12 2.75Z");
  favoriteIcon.append(favoriteStar);
  favorite.append(favoriteIcon);
  favorite.setAttribute("aria-hidden", "false");
  const updatePin = () => {
    const pinned = pinnedProductSlugs.has(product.slug);
    card.dataset.pinned = String(pinned);
    pinBadge.hidden = !pinned;
    favorite.className = pinned ? "product-card__favorite is-pinned" : "product-card__favorite";
    favorite.setAttribute("aria-label", pinned ? `Unpin ${product.name}` : `Pin ${product.name}`);
    favorite.setAttribute("aria-pressed", String(pinned));
    favorite.title = `${pinned ? "Pinned first" : "Pin to top"} · saved in this browser when storage is available`;
  };
  favorite.addEventListener("click", () => {
    if (pinnedProductSlugs.has(product.slug)) pinnedProductSlugs.delete(product.slug);
    else pinnedProductSlugs.add(product.slug);
    try { globalThis.localStorage?.setItem("aurix-favorite-games", JSON.stringify([...pinnedProductSlugs].slice(0, 200))); } catch { /* Browser storage may be disabled. */ }
    updatePin();
    refreshSavedButton();
    if (savedOnly) void loadCatalog();
    else sortProductCards();
  });
  updatePin();

  const ribbon = document.createElement("span");
  ribbon.className = "product-card__ribbon";
  ribbon.textContent = Array.isArray(product.reference_badges) && product.reference_badges.includes("Pre-order")
    ? "Reference"
    : "";
  ribbon.hidden = !ribbon.textContent;

  card.append(cover, icon, favorite, ribbon, category, title, badges, statusRow);
  if (priceRow) card.append(priceRow);
  card.append(details);
  return card;
}

function sortProductCards() {
  productGrid.replaceChildren(...Array.from(productGrid.children).sort((left, right) =>
    Number(right.dataset.pinned === "true") - Number(left.dataset.pinned === "true") ||
    Number(left.dataset.catalogIndex) - Number(right.dataset.catalogIndex)));
}

function refreshSavedButton() {
  if (!savedGamesButton) return;
  savedGamesButton.setAttribute("aria-pressed", String(savedOnly));
  savedGamesButton.textContent = pinnedProductSlugs.size ? `Saved (${pinnedProductSlugs.size})` : "Saved";
}

function safeCatalogProduct(product) {
  if (!product || typeof product !== "object" || typeof product.slug !== "string"
      || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(product.slug)
      || typeof product.name !== "string" || !product.name.trim() || product.name.length > 120
      || /[\u0000-\u001f\u007f]/.test(product.name)) return null;
  const categoryIds = Array.isArray(product.category_ids)
    ? [...new Set(product.category_ids.filter((id) => typeof id === "string" && /^[a-z0-9][a-z0-9-]{0,79}$/.test(id)))].slice(0, 12)
    : [];
  return {
    slug: product.slug,
    name: product.name.trim(),
    category: typeof product.category === "string" && product.category.length <= 80 ? product.category : "Game top-up",
    category_ids: categoryIds,
    ...(product.supplier_listed === true ? { supplier_listed: true } : {}),
    ...(product.status === "supplier_review" ? { status: "supplier_review" } : {}),
    ...(Number.isSafeInteger(product.preview_package_count) && product.preview_package_count > 0
      && product.preview_package_count <= 20_000 ? { preview_package_count: product.preview_package_count } : {}),
    ...(Number.isSafeInteger(product.preview_region_count) && product.preview_region_count > 0
      && product.preview_region_count <= 300 ? { preview_region_count: product.preview_region_count } : {}),
    ...(typeof product.artwork_slug === "string" && /^[a-z0-9][a-z0-9-]{0,79}$/.test(product.artwork_slug)
      ? { artwork_slug: product.artwork_slug } : {}),
  };
}

function safeCatalogCategories(categories) {
  if (!Array.isArray(categories)) return [];
  return categories.slice(0, 50).flatMap((category) => {
    if (!category || typeof category.id !== "string" || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(category.id)
        || typeof category.name !== "string" || !category.name.trim() || category.name.length > 80) return [];
    return [{ id: category.id, name: category.name.trim() }];
  });
}

function readCatalogCache() {
  try {
    const saved = JSON.parse(globalThis.localStorage?.getItem(GAME_LIST_CACHE_KEY) || "null");
    if (!saved || saved.version !== 2 || !Number.isSafeInteger(saved.saved_at)
        || Date.now() - saved.saved_at < 0 || Date.now() - saved.saved_at > GAME_LIST_CACHE_MAX_AGE
        || !Array.isArray(saved.products) || saved.products.length > 300) return null;
    return {
      savedAt: saved.saved_at,
      source: ["live", "supplier"].includes(saved.source) ? saved.source : "reference",
      products: saved.products.map(safeCatalogProduct).filter(Boolean),
      categories: safeCatalogCategories(saved.categories),
    };
  } catch { return null; }
}

function saveCatalogCache(products, categories, source = "reference") {
  const safeProducts = Array.isArray(products) ? products.slice(0, 300).map(safeCatalogProduct).filter(Boolean) : [];
  catalogCache = { savedAt: Date.now(), source, products: safeProducts, categories: safeCatalogCategories(categories) };
  try {
    globalThis.localStorage?.setItem(GAME_LIST_CACHE_KEY, JSON.stringify({
      version: 2, saved_at: catalogCache.savedAt, source, products: catalogCache.products, categories: catalogCache.categories,
    }));
  } catch { /* Keep the in-memory catalog when browser storage is unavailable. */ }
}

function renderCatalogProducts(products, categories, { liveMode = false, totalCount = products.length, cached = false } = {}) {
  if (catalogSection) {
    catalogSection.dataset.catalogMode = cached
      ? catalogCache?.source === "live" ? "live" : catalogCache?.source === "supplier" ? "supplier" : "reference"
      : liveMode ? "live" : "reference";
  }
  productGrid.replaceChildren();
  const visibleProducts = products.map((product, index) => ({ product, index }))
    .filter(({ product }) => !savedOnly || pinnedProductSlugs.has(product.slug))
    .sort((left, right) => Number(pinnedProductSlugs.has(right.product.slug)) - Number(pinnedProductSlugs.has(left.product.slug)) || left.index - right.index);
  availableLiveCategoryIds = liveMode && !activeCategory && !searchInput.value.trim()
    ? new Set(products.flatMap((product) => Array.isArray(product.category_ids) ? product.category_ids : []))
    : null;
  const visibleCategories = liveMode
    ? categories.filter((item) => item.id === "live-offers" || availableLiveCategoryIds?.has(item.id) || item.id === activeCategory)
    : categories;
  const currentListingsCategory = visibleCategories.find((item) => item.id === "live-offers");
  const canShowGlobalCounts = liveMode && !cached && !activeCategory && !searchInput.value.trim();
  const orderableCount = canShowGlobalCounts ? products.filter((product) => product.live === true).length : null;
  const supplierReviewCount = canShowGlobalCounts ? products.filter((product) => product.supplier_listed === true).length : null;
  const allListingsLabel = canShowGlobalCounts ? `All Games (${totalCount})` : "All Games";
  const orderedCategories = [{ ...(currentListingsCategory || { id: "live-offers", name: "Current Listings" }),
    name: canShowGlobalCounts ? `Ready to order (${orderableCount})` : (currentListingsCategory?.name || "Current Listings"),
    disabled: !liveMode || cached || !currentListingsCategory },
    ...visibleCategories.filter((item) => item.id !== "live-offers").map((item) => item.id === "supplier-review" && canShowGlobalCounts
      ? { ...item, name: `In review (${supplierReviewCount})` } : item)];
  categoryList.hidden = false;
  const categoryIds = JSON.stringify(orderedCategories.map(({ id, name, disabled }) => [id, name, Boolean(disabled)]));
  if (categoryList.dataset.catalogIds !== categoryIds) {
    categoryList.replaceChildren();
    addAllCategoryButton(allListingsLabel);
    categoryList.append(savedGamesButton);
    orderedCategories.forEach(addCategoryButton);
    categoryList.dataset.catalogIds = categoryIds;
  }
  refreshCategoryButtons();
  updateCatalogContext(cached ? "cached" : liveMode ? "live" : "preview");
  catalogRenderMode = cached ? "cached" : "fresh";
  cachedRenderKey = cached ? `${activeCategory}\u001f${searchInput.value.trim().toLowerCase()}\u001f${savedOnly}` : "";
  if (visibleProducts.length === 0) {
    setStatus(cached
      ? savedOnly ? "No saved games in this list yet." : "No game in this list matches."
      : savedOnly ? "No saved games match. Star a game to keep it here." : liveMode ? "No live offers available in this filter." : "No games match. Try a different search or category.", cached ? "cached" : "empty");
    if (catalogReset) catalogReset.hidden = false;
    return;
  }
  visibleProducts.forEach(({ product, index }) => productGrid.append(productCard(product, index)));
  const supplierTitleCount = visibleProducts.filter(({ product }) => product.supplier_listed === true).length;
  const approvedListingCount = visibleProducts.filter(({ product }) => product.live === true).length;
  setStatus(cached
    ? `${visibleProducts.length} cached title${visibleProducts.length === 1 ? "" : "s"} · checking availability`
    : savedOnly ? `${visibleProducts.length} saved game${visibleProducts.length === 1 ? "" : "s"} · on this device` : liveMode
      ? activeCategory === "supplier-review"
        ? `${totalCount} supplier-listed title${totalCount === 1 ? "" : "s"} · packages are browse-only`
        : activeCategory === "live-offers"
          ? `${totalCount} current listings · prices from the latest approved snapshot`
          : supplierTitleCount
            ? `${supplierTitleCount} supplier-listed title${supplierTitleCount === 1 ? "" : "s"} · ${approvedListingCount} orderable now`
            : `${totalCount} current listings · prices from the latest approved snapshot`
      : `${totalCount} titles · checkout unavailable`, cached ? "cached" : "ready");
  if (catalogReset) catalogReset.hidden = !(savedOnly || activeCategory || searchInput.value.trim());
}

function renderCachedCatalog() {
  if (!catalogCache) return false;
  const renderKey = `${activeCategory}\u001f${searchInput.value.trim().toLowerCase()}\u001f${savedOnly}`;
  if (catalogRenderMode === "cached" && cachedRenderKey === renderKey) {
    updateCatalogContext("cached");
    return true;
  }
  const query = searchInput.value.trim().toLowerCase();
  const products = catalogCache.products
    .filter((product) => (!query || `${product.name} ${product.category}`.toLowerCase().includes(query))
      && (!activeCategory || product.category_ids.includes(activeCategory)))
    .map((product) => ({ ...product, live: false, offers: [], purchase_enabled: false, cached_listing: true }));
  renderCatalogProducts(products, catalogCache.categories, { cached: true, totalCount: products.length });
  return true;
}

async function fetchCatalogDiscovery(signal) {
  const response = await fetch("/api/v1/catalog/discovery", {
    headers: { Accept: "application/json" }, signal, cache: "no-store",
  });
  if (!response.ok) return null;
  const payload = await response.json();
  if (payload?.schema_version !== 1 || payload.status !== "cached" || payload.checkout_enabled !== false
      || !Array.isArray(payload.products) || !Array.isArray(payload.categories)
      || !Number.isInteger(payload.total_count) || payload.total_count !== payload.products.length) return null;
  const products = payload.products.map(safeCatalogProduct).filter(Boolean);
  return { products, categories: safeCatalogCategories(payload.categories) };
}

function renderCatalogSkeleton() {
  if (productGrid.childElementCount) return;
  productGrid.replaceChildren();
  for (let index = 0; index < 3; index += 1) {
    const card = document.createElement("article");
    card.className = "product-card product-card--skeleton";
    card.setAttribute("aria-hidden", "true");
    for (const [name, extra] of [["cover", ""], ["line", ""], ["line", "product-card--skeleton__line--short"]]) {
      const shape = document.createElement("span");
      shape.className = `product-card--skeleton__${name}${extra ? ` ${extra}` : ""}`;
      card.append(shape);
    }
    productGrid.append(card);
  }
}

async function loadCatalog({ refreshDiscovery = false } = {}) {
  activeLoadController?.abort();
  const controller = new AbortController();
  activeLoadController = controller;
  const hasCachedList = renderCachedCatalog();
  if (hasCachedList) updateCatalogContext("refreshing");
  if (!hasCachedList || productGrid.childElementCount) {
    setStatus(hasCachedList ? "Cached titles · checking availability…" : "Loading games…", hasCachedList ? "cached" : "loading");
  }
  if (storeStage && !hasCachedList) storeStage.textContent = "Loading games";
  if (!hasCachedList) {
    productGrid.replaceChildren();
    catalogRenderMode = "";
    renderCatalogSkeleton();
  }
  productGrid.inert = !hasCachedList;
  productGrid.setAttribute("aria-busy", "true");
  if (catalogRetry) catalogRetry.hidden = true;
  refreshSavedButton();
  if (searchClear) searchClear.hidden = !searchInput.value;
  const parameters = new URLSearchParams();
  const query = searchInput.value.trim();
  if (catalogReset) catalogReset.hidden = !(savedOnly || activeCategory || query);
  if (activeCategory) parameters.set("category", activeCategory);
  if (query) parameters.set("q", query);

  try {
    if (refreshDiscovery || Date.now() - catalogDiscoveryAttemptedAt >= CATALOG_DISCOVERY_REFRESH_INTERVAL) {
      let discovery = null;
      try { discovery = await fetchCatalogDiscovery(controller.signal); }
      catch (discoveryError) { if (discoveryError.name === "AbortError") throw discoveryError; }
      if (controller !== activeLoadController) return;
      catalogDiscoveryAttemptedAt = Date.now();
      if (discovery && (discovery.products.length > 0 || !catalogCache)) {
        saveCatalogCache(discovery.products, discovery.categories, "supplier");
        renderCachedCatalog();
        updateCatalogContext("refreshing");
        setStatus("Cached titles · checking availability…", "cached");
        productGrid.inert = false;
      }
    }
    const products = [];
    let firstPage = null;
    let offset = 0;
    let liveMode = false;
    const liveParameters = new URLSearchParams(parameters);
    liveParameters.set("limit", "50");
    liveParameters.set("offset", "0");
    try {
      const liveResponse = await fetch(`/api/v1/live-catalog?${liveParameters}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (liveResponse.ok) {
        const livePage = await liveResponse.json();
        // Once operator controls apply, an empty live projection is intentional.
        const liveCategoryRequested = activeCategory === "live-offers";
        const acceptedCatalogStatuses = new Set(["live", "supplier_listed"]);
        if (acceptedCatalogStatuses.has(livePage.status) && typeof livePage.checkout_enabled === "boolean" &&
            Array.isArray(livePage.categories) && Array.isArray(livePage.products) &&
            Number.isInteger(livePage.total_count) &&
            (livePage.total_count > 0 || liveCategoryRequested || livePage.catalog_controls_active === true || livePage.status === "supplier_listed")) {
          liveMode = true;
          firstPage = livePage;
          products.push(...livePage.products);
          while (livePage.next_offset !== null) {
            if (!Number.isInteger(livePage.next_offset) || livePage.next_offset <= offset) {
              throw new Error("Live catalog pagination did not advance");
            }
            offset = livePage.next_offset;
            const nextParameters = new URLSearchParams(parameters);
            nextParameters.set("limit", "50");
            nextParameters.set("offset", String(offset));
            const nextResponse = await fetch(`/api/v1/live-catalog?${nextParameters}`, {
              headers: { Accept: "application/json" },
              signal: controller.signal,
            });
            if (!nextResponse.ok) throw new Error("Live catalog page failed");
            const nextPage = await nextResponse.json();
            if (nextPage.status !== livePage.status || !Array.isArray(nextPage.products)) {
              throw new Error("Live catalog response did not match the contract");
            }
            products.push(...nextPage.products);
            livePage.next_offset = nextPage.next_offset;
          }
        }
      }
    } catch (liveError) {
      if (liveError.name === "AbortError") throw liveError;
      liveMode = false;
      products.length = 0;
      firstPage = null;
      offset = 0;
    }
    while (!liveMode) {
      const pageParameters = new URLSearchParams(parameters);
      pageParameters.set("limit", "50");
      pageParameters.set("offset", String(offset));
      const response = await fetch(`/api/v1/catalog/products?${pageParameters}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Catalog request failed (${response.status})`);
      const page = await response.json();
      if (!Array.isArray(page.categories) || !Array.isArray(page.products) ||
          typeof page.checkout_enabled !== "boolean" || !Number.isInteger(page.total_count)) {
        throw new Error("Catalog response did not match the preview contract");
      }
      if (!firstPage) firstPage = page;
      products.push(...page.products);
      if (page.next_offset === null) break;
      if (!Number.isInteger(page.next_offset) || page.next_offset <= offset) {
        throw new Error("Catalog pagination did not advance");
      }
      offset = page.next_offset;
    }
    if (controller !== activeLoadController) return;
    if (!activeCategory && !query) {
      const source = liveMode && firstPage.status === "supplier_listed" ? "supplier"
        : liveMode ? "live" : "reference";
      saveCatalogCache(products, firstPage.categories, source);
    }
    renderCatalogProducts(products, firstPage.categories, { liveMode, totalCount: firstPage.total_count });
  } catch (error) {
    if (error.name === "AbortError" || controller !== activeLoadController) return;
    if (renderCachedCatalog()) {
      const savedCount = productGrid.childElementCount;
      if (savedCount) setStatus(`${savedCount} cached title${savedCount === 1 ? "" : "s"} · could not check offers`, "cached");
    } else {
      productGrid.replaceChildren();
      catalogRenderMode = "";
      setStatus("Current offers and prices are unavailable right now. No order or payment has started; please check back later.", "error");
      updateCatalogContext("error");
    }
    if (catalogRetry) catalogRetry.hidden = false;
  } finally {
    if (controller === activeLoadController) {
      productGrid.inert = false;
      productGrid.setAttribute("aria-busy", "false");
    }
  }
}

async function showProduct(product) {
  activeProduct = product;
  activeProductSlug = product.slug || "";
  setDetailView("packages", false, false);
  updateDetailFavorite();
  const liveProduct = product.live === true;
  const cachedListing = product.cached_listing === true;
  const title = document.querySelector("#dialog-title");
  const copy = document.querySelector("#dialog-copy");
  activeDetailController?.abort();
  const controller = new AbortController();
  activeDetailController = controller;
  setDetailArt(product);
  title.textContent = product.name;
  detailHeroMeta.textContent = "Checking current package listings…";
  detailStockHeading.textContent = "Checking current listings";
  detailStockCopy.textContent = "Current package availability is being checked.";
  detailStockRegions.replaceChildren();
  copy.textContent = "Loading current product status…";
  resetProductDetail();
  const detailScroller = document.querySelector(".product-detail__body");
  if (detailScroller) detailScroller.scrollTop = 0;
  dialog.showModal();
  try {
    const response = await fetch(`/api/v1/catalog/products/${encodeURIComponent(product.slug)}`, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Product status unavailable");
    const detail = await response.json();
    if (controller !== activeDetailController || controller.signal.aborted) return;
    if (typeof detail.checkout_enabled !== "boolean" || !detail.product || !Array.isArray(detail.offers)) {
      throw new Error("Product response did not match the preview contract");
    }
    if (detail.status === "live") {
      renderLiveProduct({ ...detail.product, live: true, offers: detail.offers,
        purchase_enabled: detail.purchase_enabled === true && detail.product.purchase_enabled === true });
      if (detailScroller) detailScroller.scrollTop = 0;
      return;
    }
    title.textContent = detail.product.name;
    if (detail.status === "supplier_review") {
      const regions = detail.supplier_preview?.regions;
      const hasPreview = Array.isArray(regions) && regions.length > 0;
      const packageCount = hasPreview
        ? regions.reduce((count, region) => count + (Array.isArray(region.offers) ? region.offers.length : 0), 0)
        : 0;
      copy.hidden = false;
      copy.textContent = hasPreview
        ? `FazerCards lists ${packageCount} package option${packageCount === 1 ? "" : "s"}. AuriX is reviewing region support, required account fields, MMK price, and delivery. You can inspect this dated supplier snapshot, but ordering remains closed.`
        : "FazerCards lists this game, but package options have not been confirmed. AuriX is reviewing region support, required account fields, MMK price, and delivery; ordering remains closed.";
      setDetailLiveStatus("");
      detailHeroMeta.textContent = hasPreview
        ? `${packageCount} supplier package option${packageCount === 1 ? "" : "s"} · snapshot only`
        : "Supplier listing · review pending";
      detailSelection.textContent = "Packages not approved for sale yet";
      detailBuy.disabled = true;
      detailBuy.textContent = "Not for sale yet";
      detailBuy.setAttribute("aria-disabled", "true");
      detailClear.hidden = true;
      detailRetry.hidden = true;
      dialog.dataset.offerState = "supplier-review";
      detailStockHeading.textContent = hasPreview ? `${packageCount} supplier package options` : "Supplier review pending";
      detailStockCopy.textContent = hasPreview
        ? "Supplier snapshot only—not current stock. Exact quantities are not reported. Region, account fields, MMK price and delivery still need approval."
        : "No package snapshot is available yet. Region, account fields, MMK price and delivery still need approval.";
      if (hasPreview) {
        renderSnapshotStock(regions, true);
        setDetailView("stock", false, false);
      } else {
        clearStockRegions();
        appendStockEmpty("Packages, supported regions, pricing, and account requirements still need verification. No order or payment has started.");
      }
    } else if (Array.isArray(detail.supplier_preview?.regions) && detail.supplier_preview.regions.length) {
      copy.textContent = "Browse supplier packages in this preview. Prices and account eligibility are pending.";
      setDetailLiveStatus("Supplier package preview · prices and eligibility pending");
      renderReferenceSnapshot(detail.supplier_preview, true);
    } else {
      copy.textContent = "No packages available yet.";
      copy.hidden = true;
      setDetailLiveStatus("No packages are listed right now.");
      detailSelection.textContent = "";
      detailClear.hidden = true;
      detailRetry.hidden = false;
      dialog.dataset.offerState = "unavailable";
      detailHeroMeta.textContent = "No approved listings";
      renderUnavailableStock("No approved packages are currently listed for this game.", true);
    }
    if (detailScroller) detailScroller.scrollTop = 0;
  } catch (error) {
    if (error.name === "AbortError" || controller !== activeDetailController) return;
    dialog.dataset.offerState = "unavailable";
    copy.hidden = cachedListing;
    copy.textContent = cachedListing
      ? "Saved title for browsing only."
      : liveProduct
      ? "Current packages could not be refreshed."
      : "Product status could not be loaded.";
    setDetailLiveStatus(cachedListing
      ? "Current offers are unavailable. No order or payment has started."
      : liveProduct
      ? "Try again before choosing a package."
      : "No purchase action is available in this preview.");
    detailRetry.hidden = false;
    detailHeroMeta.textContent = "Listings unavailable";
    renderUnavailableStock("Current listings could not be verified. Try again before ordering.");
  }
}

function setDetailArt(product) {
  if (!detailArt) return;
  const slug = typeof product?.slug === "string" ? product.slug : "game";
  detailArt.dataset.slug = slug;
  dialog.dataset.game = slug;
  const label = document.querySelector("#detail-art-label");
  if (label) label.textContent = ({ "mobile-legends": "MLBB", "pubg-mobile": "PUBG", freefire: "FF" })[slug] || product.name || "GAME";
  const art = GAME_ART[slug] || GAME_ART[product?.artwork_slug];
  if (detailArtImage) {
    if (!detailArtImage.dataset.handlersAttached) {
      detailArtImage.addEventListener("load", () => {
        if (detailArtImage.dataset.slug === detailArt.dataset.slug) {
          detailArtImage.hidden = false;
          detailArt.dataset.artState = "loaded";
        } else {
          detailArtImage.hidden = true;
        }
      });
      detailArtImage.addEventListener("error", () => {
        detailArtImage.hidden = true;
        if (detailArtImage.dataset.slug === detailArt.dataset.slug) detailArt.dataset.artState = "fallback";
      });
      detailArtImage.dataset.handlersAttached = "true";
    }
    detailArtImage.hidden = !art;
    detailArtImage.dataset.slug = slug;
    detailArtImage.dataset.source = art?.source || "";
    if (art && detailArtImage.src !== art.src) detailArtImage.src = art.src;
    if (!art) detailArt.dataset.artState = "fallback";
  }
}

function setDetailLiveStatus(message) {
  detailLiveStatus.textContent = message;
  detailLiveState.hidden = !message;
}

function setDetailHeroSummary(label, packageCount = null, regionCount = null) {
  if (!detailHeroMeta) return;
  if (packageCount === null) {
    detailHeroMeta.textContent = label;
    return;
  }
  detailHeroMeta.textContent = `${packageCount} package${packageCount === 1 ? "" : "s"}${regionCount ? ` · ${regionCount} region${regionCount === 1 ? "" : "s"}` : ""}`;
}

function clearStockRegions() {
  detailStockRegions.replaceChildren();
}

function appendStockEmpty(message) {
  const empty = document.createElement("p");
  empty.className = "detail-stock-empty";
  empty.textContent = message;
  detailStockRegions.append(empty);
}

function appendStockRegion(name, count, note, observedAt = "") {
  const item = document.createElement("article");
  item.className = "detail-stock-region";
  item.setAttribute("role", "listitem");
  const heading = document.createElement("div");
  heading.className = "detail-stock-region__heading";
  const title = document.createElement("strong");
  title.textContent = name;
  const total = document.createElement("span");
  total.textContent = `${count} package${count === 1 ? "" : "s"}`;
  heading.append(title, total);
  const detail = document.createElement("p");
  detail.textContent = note;
  item.append(heading, detail);
  if (observedAt) {
    const stamp = document.createElement("small");
    stamp.textContent = `Snapshot · ${observedAt}`;
    item.append(stamp);
  }
  detailStockRegions.append(item);
  return item;
}

function formatStockObservation(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "time unavailable"
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeZone: "Asia/Yangon" }).format(date);
}

function renderUnavailableStock(message, verified = false) {
  detailStockHeading.textContent = verified ? "No approved listings" : "Listings unavailable";
  detailStockCopy.textContent = verified
    ? "No current package listing is approved for sale. Supplier quantities are not displayed."
    : "Stock is not confirmed and no order or payment has started.";
  clearStockRegions();
  appendStockEmpty(message);
}

function renderLiveStock() {
  const available = accountEligibleLiveOffers();
  const offers = activeLiveRegion
    ? available.filter((offer) => liveRegionKey(offer.region) === activeLiveRegion)
    : available;
  clearStockRegions();
  if (!offers.length) {
    detailStockHeading.textContent = "No approved listings";
    detailStockCopy.textContent = playerAccountCheck?.eligible_offer_ids
      ? "No approved package matches the checked account region. Unit stock counts are not reported."
      : "No approved package is currently listed. Supplier unit stock counts are not reported.";
    appendStockEmpty(playerAccountCheck?.eligible_offer_ids
      ? "Check the account details or choose another game."
      : "Try again later to check for current listings.");
    setDetailHeroSummary("No current packages");
    return;
  }
  const regions = new Map();
  for (const offer of offers) {
    const key = liveRegionKey(offer.region) || "region:unreported";
    const name = key === "region:unreported" ? "Region not reported" : liveRegionLabel(offer.region);
    const region = regions.get(key) || { name, count: 0 };
    region.count += 1;
    regions.set(key, region);
  }
  detailStockHeading.textContent = "Current approved listings";
  detailStockCopy.textContent = "Package listings are current; FazerCards does not report unit quantities. Check your player to confirm the matching region.";
  for (const region of regions.values()) {
    appendStockRegion(region.name, region.count, "Listed package options · unit quantity unreported");
  }
  setDetailHeroSummary("Current packages", offers.length, regions.size);
}

function renderSnapshotStock(regions, supplierPreview) {
  clearStockRegions();
  const packageCount = regions.reduce((count, region) => count + (Array.isArray(region.offers) ? region.offers.length : 0), 0);
  detailStockHeading.textContent = supplierPreview
    ? packageCount ? `${packageCount} supplier package options` : "Supplier package snapshot"
    : "Reference only · not live stock";
  detailStockCopy.textContent = supplierPreview
    ? "Supplier snapshot only—not current stock. Exact quantities are not reported. Region, account fields, MMK price and delivery still need approval."
    : "Historical package examples only. They do not show current stock or offers for sale.";
  for (const region of regions) {
    const offers = Array.isArray(region.offers) ? region.offers : [];
    const item = appendStockRegion(region.name, offers.length,
      supplierPreview ? "Supplier package entries · not available to order" : "Historical examples · current stock not checked",
      region.observed_at ? formatStockObservation(region.observed_at) : "");
    const rows = document.createElement("div");
    rows.className = "detail-stock-region__offers";
    for (const offer of offers) {
      const row = document.createElement("div");
      row.className = "detail-stock-offer";
      const name = document.createElement("span");
      name.textContent = offer.name;
      const status = document.createElement("small");
      status.textContent = supplierPreview && offer.stock_label ? offer.stock_label : "Quantity not reported";
      row.append(name, status);
      rows.append(row);
    }
    item.append(rows);
  }
  if (!regions.length) appendStockEmpty("No stock snapshot is available.");
  setDetailHeroSummary(supplierPreview ? "Supplier snapshot" : "Reference listings", packageCount, regions.length);
}

function renderLiveProduct(product) {
  const title = document.querySelector("#dialog-title");
  const copy = document.querySelector("#dialog-copy");
  title.textContent = product.name;
  copy.textContent = "Confirm the returned player and region.";
  setDetailLiveStatus("Checking account status…");
  referenceDetail.hidden = false;
  referenceDetail.setAttribute("aria-label", "Available game packages and account details");
  offerList.setAttribute("aria-label", "Available game packages");
  detailTabs.hidden = true;
  pinnedTitle.hidden = true;
  pinnedOfferList.hidden = true;
  referenceNote.hidden = false;
  referenceNote.textContent = "MMK pricing · supplier stock unreported";
  activeLivePurchaseEnabled = product.purchase_enabled === true;
  activeLiveOffers = Array.isArray(product.offers) ? product.offers : [];
  setDetailHeroSummary("Current packages", activeLiveOffers.length);
  activeLiveRegion = "";
  activeReferenceRegion = "live";
  detailPackageStep.before(recipientSection);
  const lookupOffer = activeLiveOffers.find((offer) => offer.fulfillment_kind === "direct_topup");
  if (lookupOffer) renderRecipientPreview(lookupOffer);
  renderLiveOffers();
  const checkoutController = activeDetailController;
  fetch("/api/v1/checkout/config", { headers: { Accept: "application/json" } })
    .then((response) => response.ok ? response.json() : null)
    .then((config) => {
      if (checkoutController !== activeDetailController || activeProductSlug !== product.slug) return;
      checkoutConfigLoaded = true;
      accountCheckEnabled = config?.account_check_enabled === true;
      recipientCheckoutEnabled = config?.recipient_review_enabled === true;
      checkoutPaymentsEnabled = config?.payments_enabled === true;
      checkoutPaymentRails = checkoutPaymentsEnabled && Array.isArray(config?.payment_rails)
        ? [...new Set(config.payment_rails.filter((rail) => typeof rail === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(rail)))]
        : [];
      setDetailLiveStatus(accountCheckEnabled ? "" : "Account checks unavailable.");
      if (!recipientSection.hidden) recipientStatus.textContent = accountCheckEnabled ? "" : "Account checks unavailable.";
      updateCheckoutAction();
    }).catch(() => {
      if (checkoutController !== activeDetailController || activeProductSlug !== product.slug) return;
      checkoutConfigLoaded = true;
      setDetailLiveStatus("Account status unavailable. Reopen and retry.");
      updateCheckoutAction();
    });
}

function liveRegionKey(region) {
  if (!region || typeof region !== "object") return "";
  const type = typeof region.type === "string" ? region.type : "";
  const label = typeof region.label === "string" ? region.label : "";
  return `${type}:${label}`;
}

function liveRegionLabel(region) {
  if (!region || typeof region !== "object") return "Unknown region";
  const label = typeof region.label === "string" ? region.label.trim() : "";
  if (label && label.toLocaleLowerCase() !== "supported account regions") return label;
  const type = typeof region.type === "string" ? region.type.trim() : "";
  if (type.toLocaleLowerCase() === "global") return "Global";
  return type ? type.replaceAll("_", " ") : "Unknown region";
}

function accountEligibleLiveOffers() {
  const eligibleIds = playerAccountCheck?.eligible_offer_ids;
  return Array.isArray(eligibleIds)
    ? activeLiveOffers.filter((offer) => eligibleIds.includes(offer.offer_id))
    : activeLiveOffers;
}

function renderLiveRegions() {
  regionList.replaceChildren();
  detailRegionLabel.hidden = true;
  detailRegionLabel.textContent = "";
  const regions = [];
  const seen = new Set();
  for (const offer of accountEligibleLiveOffers()) {
    const key = liveRegionKey(offer.region);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    regions.push({ key, value: offer.region });
  }
  if (regions.length === 0) {
    detailTabs.hidden = true;
    return;
  }
  detailTabs.hidden = false;
  if (regions.length === 1) {
    const label = document.createElement("span");
    label.className = "detail-region detail-region--static";
    label.textContent = `Region · ${liveRegionLabel(regions[0].value)}`;
    regionList.append(label);
    return;
  }
  detailRegionLabel.hidden = false;
  detailRegionLabel.textContent = "Available regions";
  const all = document.createElement("button");
  all.type = "button";
  all.className = "detail-region";
  all.disabled = !playerAccountIsCurrent();
  all.setAttribute("aria-pressed", String(activeLiveRegion === ""));
  all.textContent = "All regions";
  all.addEventListener("click", () => {
    activeLiveRegion = "";
    selectedReferenceOffer = null;
    detailSelection.textContent = "Select a package";
    detailActionStatus.textContent = "";
      if (activeProductSlug !== "mobile-legends") clearRecipientPreview();
    renderLiveOffers();
  });
  regionList.append(all);
  for (const region of regions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "detail-region";
    button.disabled = !playerAccountIsCurrent();
    button.setAttribute("aria-pressed", String(region.key === activeLiveRegion));
    button.textContent = liveRegionLabel(region.value);
    button.addEventListener("click", () => {
      activeLiveRegion = region.key;
      selectedReferenceOffer = null;
      detailSelection.textContent = "Select a package";
      detailActionStatus.textContent = `Showing ${liveRegionLabel(region.value)} packages.`;
      if (activeProductSlug !== "mobile-legends") clearRecipientPreview();
      renderLiveOffers();
    });
    regionList.append(button);
  }
}

function updateOfferSelection(selectedButton) {
  for (const list of [pinnedOfferList, offerList]) {
    for (const button of list.querySelectorAll("button")) {
      const selected = button === selectedButton;
      button.setAttribute("aria-pressed", String(selected));
      const marker = Array.from(button.children).find((child) => child.className === "detail-offer__transfer");
      if (marker) marker.textContent = selected ? "✓" : "+";
    }
  }
}

function renderLiveOffers() {
  renderLiveRegions();
  renderLiveStock();
  pinnedTitle.hidden = true;
  pinnedOfferList.hidden = true;
  pinnedOfferList.replaceChildren();
  offerList.replaceChildren();
  const eligibleOffers = accountEligibleLiveOffers();
  const offers = activeLiveRegion
    ? eligibleOffers.filter((offer) => liveRegionKey(offer.region) === activeLiveRegion)
    : eligibleOffers;
  emptyOffers.hidden = offers.length > 0;
  if (offers.length === 0) {
    emptyOffers.textContent = Array.isArray(playerAccountCheck?.eligible_offer_ids)
      ? "No current package supports the detected account region. Check the account details or choose another game."
      : "No live package is currently available.";
    return;
  }
  for (const offer of offers) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "detail-offer";
    button.disabled = !playerAccountIsCurrent();
    if (button.disabled) button.title = "Check and confirm your player account before choosing a package.";
    button.setAttribute("aria-pressed", String(offer.offer_id === selectedReferenceOffer?.offer_id));
    const icon = document.createElement("span");
    icon.className = "detail-offer__icon";
    icon.setAttribute("aria-hidden", "true");
    attachDetailOfferIcon(icon, offer.name);
    const name = document.createElement("span");
    name.className = "detail-offer__name";
    name.textContent = offer.name;
    const price = document.createElement("span");
    price.className = "detail-offer__price";
    price.textContent = formatMinorAmount(offer.price_minor, offer.currency);
    const transfer = document.createElement("span");
    transfer.className = "detail-offer__transfer";
    transfer.setAttribute("aria-hidden", "true");
    transfer.textContent = offer.offer_id === selectedReferenceOffer?.offer_id ? "✓" : "+";
    const promo = Number(offer.promo_discount_minor || 0);
    if (Number.isFinite(promo) && promo > 0) {
      const badge = document.createElement("span");
      badge.className = "detail-offer__promo";
      badge.textContent = `save ${formatMinorAmount(promo, offer.currency)}`;
      button.append(icon, name, badge, price, transfer);
    } else {
      button.append(icon, name, price, transfer);
    }
    button.addEventListener("click", () => {
      if (selectedReferenceOffer === offer) return;
      selectedReferenceOffer = offer;
      detailSelection.textContent = `${offer.name} · ${price.textContent}`;
      detailActionStatus.textContent = "Package selected.";
      updateOfferSelection(button);
      updateCheckoutAction();
    });
    offerList.append(button);
  }
}

function clearPlayerAccountVerification(message = "") {
  accountCheckRevision += 1;
  playerAccountCheck = null;
  playerAccountConfirmed = false;
  accountCheckReview.hidden = true;
  accountCheckConfirm.checked = false;
  selectedReferenceOffer = null;
  if (activeLiveOffers.length) {
    detailSelection.textContent = "Check your player account first";
    detailActionStatus.textContent = message;
    renderLiveOffers();
  }
  updateCheckoutAction();
}

function renderRecipientPreview(offer) {
  invalidateCheckoutReview();
  recipientFields.replaceChildren();
  accountCheckButton.hidden = false;
  accountCheckReview.hidden = true;
  accountCheckConfirm.checked = false;
  playerAccountCheck = null;
  playerAccountConfirmed = false;
  const labels = Array.isArray(offer?.required_input_labels)
    ? [...new Set(offer.required_input_labels.filter((label) => typeof label === "string" && label.trim()))]
    : [];
  recipientSection.hidden = labels.length === 0;
  recipientStatus.textContent = labels.length
    ? accountCheckEnabled
      ? `Enter ${labels.map((label) => label.replaceAll("_", " ")).join(" and ")}, then check the account. No AuriX sign-in is needed.`
      : "Account checks are loading or unavailable. No sign-in is needed to check an account."
    : "";
  for (const labelText of labels) {
    const wrapper = document.createElement("div");
    wrapper.className = "detail-recipient__field";
    const label = document.createElement("span");
    const displayLabel = labelText === "player_id" ? "Player ID"
      : labelText === "server_id" ? (activeProductSlug === "mobile-legends" ? "Zone ID" : "Server ID")
        : labelText === "zone_id" ? "Zone ID"
          : labelText.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
    label.textContent = displayLabel;
    const inputId = `recipient-${labelText.replace(/[^a-z0-9-]/gi, "-")}`;
    label.setAttribute("for", inputId);
    const input = document.createElement("input");
    input.id = inputId;
    input.type = "text";
    input.maxLength = 2048;
    input.autocomplete = "off";
    input.placeholder = activeProductSlug === "mobile-legends" && ["server_id", "zone_id"].includes(labelText)
      ? "Zone ID or configured name" : `Enter ${displayLabel}`;
    input.setAttribute("aria-label", displayLabel);
    input.dataset.fieldKey = labelText;
    wrapper.append(label, input);
    if (labelText === "player_id") {
      const history = document.createElement("datalist");
      history.id = `game-account-history-${activeProductSlug}`;
      const savedIds = recentPlayerIds();
      input.value = savedIds[0] || "";
      const hint = document.createElement("small");
      hint.className = "detail-recipient__hint";
      hint.id = `game-account-history-help-${activeProductSlug}`;
      input.setAttribute("list", history.id);
      input.setAttribute("aria-describedby", hint.id);
      const quickSelect = document.createElement("div");
      quickSelect.className = "detail-recipient__quick-select";
      quickSelect.hidden = true;
      const quickHeading = document.createElement("div");
      quickHeading.className = "detail-recipient__quick-heading";
      const quickTitle = document.createElement("strong");
      quickTitle.textContent = "Recent IDs";
      const quickLocal = document.createElement("small");
      quickLocal.textContent = "This device only";
      quickHeading.append(quickTitle, quickLocal);
      const quickList = document.createElement("div");
      quickList.className = "detail-recipient__quick-list";
      quickSelect.append(quickHeading, quickList);
      const clearHistory = document.createElement("button");
      clearHistory.type = "button";
      clearHistory.className = "detail-recipient__clear-history";
      clearHistory.textContent = "Clear saved IDs";
      const refreshHistory = (saved = true) => {
        const ids = recentPlayerIds();
        history.replaceChildren(...ids.map((id) => {
          const option = document.createElement("option");
          option.value = id;
          return option;
        }));
        quickList.replaceChildren(...ids.slice(0, 4).map((id) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "detail-recipient__quick-chip";
          button.textContent = id;
          button.setAttribute("aria-label", `Use recent Player ID ${id}`);
          button.addEventListener("click", () => {
            input.value = id;
            const result = rememberPlayerId(id);
            refreshHistory(result.saved);
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.focus();
          });
          return button;
        }));
        quickSelect.hidden = ids.length === 0;
        clearHistory.hidden = ids.length === 0;
        hint.textContent = !saved ? "Saving unavailable in this browser."
          : "Recent IDs stay on this device.";
        hint.hidden = saved && ids.length > 0;
      };
      input.addEventListener("change", () => refreshHistory(rememberPlayerId(input.value).saved));
      clearHistory.addEventListener("click", () => {
        const cleared = clearPlayerIds();
        input.value = "";
        refreshHistory(cleared);
        invalidateCheckoutReview();
        clearPlayerAccountVerification("Enter your player details and check the account again.");
        recipientStatus.textContent = cleared ? "Saved Account IDs cleared from this browser." : "This browser could not clear saved Account IDs.";
        input.focus();
      });
      refreshHistory();
      wrapper.append(history, hint, quickSelect, clearHistory);
    }
    if (activeProductSlug === "mobile-legends" && ["server_id", "zone_id"].includes(labelText)) {
      const options = document.createElement("datalist");
      options.id = "mlbb-checkout-zone-options";
      const hint = document.createElement("small");
      hint.className = "detail-recipient__hint";
      hint.id = "mlbb-checkout-zone-help";
      hint.textContent = "Loading available zone names…";
      input.setAttribute("list", options.id);
      input.setAttribute("aria-describedby", hint.id);
      wrapper.append(options, hint);
      const clearZones = document.createElement("button");
      clearZones.type = "button";
      clearZones.className = "detail-recipient__clear-history";
      clearZones.textContent = "Clear saved zones";
      clearZones.hidden = recentMlbbZoneIds().length === 0;
      clearZones.addEventListener("click", () => {
        const cleared = clearMlbbZoneIds();
        clearZones.hidden = recentMlbbZoneIds().length === 0;
        void loadMlbbCheckoutZones().then((zones) => {
          options.replaceChildren(...zones.map((zone) => {
            const option = document.createElement("option");
            option.value = `${zone.name} (${zone.id})`;
            option.label = `${zone.name} · ID ${zone.id}`;
            return option;
          }));
        });
        hint.textContent = cleared ? "Saved Zone IDs cleared from this browser." : "This browser could not clear saved Zone IDs.";
      });
      wrapper.append(clearZones);
      void loadMlbbCheckoutZones().then((zones) => {
        if (!input.isConnected) return;
        const approvedOptions = zones.map((zone) => {
          const option = document.createElement("option");
          option.value = `${zone.name} (${zone.id})`;
          option.label = `${zone.name} · ID ${zone.id}`;
          return option;
        });
        const knownIds = new Set(zones.map((zone) => zone.id));
        const historyOptions = recentMlbbZoneIds().filter((id) => !knownIds.has(id)).map((id) => {
          const option = document.createElement("option");
          option.value = id;
          option.label = `Recently used · Zone ID ${id}`;
          return option;
        });
        options.replaceChildren(...approvedOptions, ...historyOptions);
        hint.textContent = zones.length
          ? "Choose a zone or enter its numeric ID."
          : recentMlbbZoneIds().length
            ? "Use a saved Zone ID or enter it from your profile."
            : "Enter the numeric Zone ID from your profile.";
      });
    }
    input.addEventListener("input", () => {
      invalidateCheckoutReview();
      clearPlayerAccountVerification("Details changed. Check the account again.");
    });
    recipientFields.append(wrapper);
  }
  accountCheckButton.textContent = "Check player ID";
  accountCheckButton.disabled = true;
  detailSelection.textContent = "Player check required";
  updateCheckoutAction();
}

function clearRecipientPreview() {
  invalidateCheckoutReview();
  accountCheckReview.hidden = true;
  accountCheckConfirm.checked = false;
  accountCheckButton.hidden = true;
  document.querySelector("#recipient-checkout-payment")?.remove();
  recipientSection.hidden = true;
  recipientFields.replaceChildren();
  recipientStatus.textContent = "";
}

function resetProductDetail() {
  setDetailView("packages", false, false);
  accountCheckRevision += 1;
  accountCheckBusy = false;
  accountCheckEnabled = false;
  playerAccountCheck = null;
  playerAccountConfirmed = false;
  detailPackageStep.hidden = false;
  detailPackageStep.before(recipientSection);
  recipientCheckoutEnabled = false;
  checkoutPaymentsEnabled = false;
  checkoutPaymentRails = [];
  checkoutConfigLoaded = false;
  checkoutOrder = null;
  checkoutPaymentAttemptRail = "";
  activeReferenceRegion = "";
  activeReferenceRegions = [];
  activeSupplierPreview = false;
  activeLiveOffers = [];
  activeLiveRegion = "";
  activeLivePurchaseEnabled = false;
  selectedReferenceOffer = null;
  referenceNote.hidden = true;
  referenceNote.textContent = "";
  referenceDetail.hidden = true;
  detailTabs.hidden = true;
  dialog.dataset.referenceSnapshot = "false";
  referenceDescription.hidden = true;
  referenceDescription.textContent = "";
  packageNotes.hidden = true;
  packageNotes.open = false;
  detailRegionLabel.hidden = true;
  detailRegionLabel.textContent = "";
  regionList.replaceChildren();
  pinnedTitle.hidden = true;
  pinnedOfferList.hidden = true;
  pinnedOfferList.replaceChildren();
  offerTitle.textContent = "Choose a package";
  offerList.replaceChildren();
  emptyOffers.hidden = true;
  emptyOffers.textContent = "";
  clearRecipientPreview();
  detailHelpCopy.hidden = true;
  detailHelpCopy.textContent = "";
  document.querySelector("#dialog-copy").hidden = false;
  detailSelection.textContent = "Player check required";
  detailClear.hidden = true;
  dialog.dataset.offerState = "ready";
  detailActionStatus.textContent = "";
  setDetailLiveStatus("");
  detailRetry.hidden = true;
  detailHeroMeta.textContent = "";
  detailStockHeading.textContent = "Checking current listings";
  detailStockCopy.textContent = "Current package availability is being checked.";
  clearStockRegions();
  detailBuy.disabled = true;
  detailBuy.textContent = "Select a package";
  detailBuy.setAttribute("aria-disabled", "true");
  detailClear.disabled = true;
}

function renderReferenceSnapshot(snapshot, supplierPreview = false) {
  if (!Array.isArray(snapshot.regions) || snapshot.regions.length === 0) return;
  referenceDetail.hidden = false;
  referenceDetail.setAttribute("aria-label", supplierPreview ? "Supplier package listings" : "Historical example packages");
  offerList.setAttribute("aria-label", supplierPreview ? "Supplier package listings" : "Historical example packages");
  detailTabs.hidden = false;
  dialog.dataset.referenceSnapshot = "true";
  referenceNote.hidden = false;
  referenceNote.textContent = supplierPreview
    ? "Packages listed by the supplier. Pricing and eligibility are pending."
    : `Example packages · captured ${snapshot.observed_on}. Historical prices; purchases unavailable.`;
  referenceDescription.hidden = typeof snapshot.description !== "string" || !snapshot.description.trim();
  referenceDescription.textContent = referenceDescription.hidden ? "" : snapshot.description;
  packageNotes.hidden = referenceDescription.hidden;
  if (packageNotes.hidden) packageNotes.open = false;
  document.querySelector("#dialog-copy").hidden = true;
  offerTitle.textContent = "Choose a package";
  activeReferenceRegions = snapshot.regions.map((region) => ({
    ...region,
    description: typeof region.description === "string" ? region.description : snapshot.description,
  }));
  activeSupplierPreview = supplierPreview;
  activeReferenceRegion = snapshot.regions[0].id;
  setDetailHeroSummary(supplierPreview ? "Supplier review pending" : "Reference listings",
    snapshot.regions.reduce((count, region) => count + (Array.isArray(region.offers) ? region.offers.length : 0), 0), snapshot.regions.length);
  renderReferenceRegions();
  renderReferenceRegion(activeReferenceRegions[0]);
  updateCheckoutAction();
}

function renderReferenceRegion(region) {
  if (region.observed_at) {
    referenceNote.textContent = `Supplier listing checked ${new Date(region.observed_at).toLocaleString()} · purchases unavailable`;
  }
  referenceDescription.hidden = typeof region.description !== "string" || !region.description.trim();
  referenceDescription.textContent = referenceDescription.hidden ? "" : region.description;
  packageNotes.hidden = referenceDescription.hidden;
  if (packageNotes.hidden) packageNotes.open = false;
  renderSnapshotStock([region], activeSupplierPreview);
  renderReferenceOffers(region);
}

function renderReferenceRegions() {
  regionList.replaceChildren();
  detailRegionLabel.hidden = activeReferenceRegions.length <= 1;
  detailRegionLabel.textContent = activeReferenceRegions.length > 1 ? "Available regions" : "";
  if (activeReferenceRegions.length === 1) {
    const label = document.createElement("span");
    label.className = "detail-region detail-region--static";
    label.textContent = `Region · ${activeReferenceRegions[0].name}`;
    regionList.append(label);
    return;
  }
  for (const region of activeReferenceRegions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "detail-region";
    button.setAttribute("aria-pressed", String(region.id === activeReferenceRegion));
    button.setAttribute("aria-label", region.name);
    button.textContent = region.name;
    button.addEventListener("click", () => {
      activeReferenceRegion = region.id;
      selectedReferenceOffer = null;
      detailSelection.textContent = "Select a package";
      detailActionStatus.textContent = "";
      renderReferenceRegions();
      const selectedRegion = activeReferenceRegions.find((candidate) => candidate.id === region.id);
      if (selectedRegion) renderReferenceRegion(selectedRegion);
    });
    regionList.append(button);
  }
}

function renderReferenceOffers(region) {
  pinnedOfferList.replaceChildren();
  offerList.replaceChildren();
  const offers = Array.isArray(region.offers) ? region.offers : [];
  emptyOffers.hidden = offers.length > 0;
  const pinnedOffers = offers.filter((offer) => offer.pinned === true);
  const regularOffers = offers.filter((offer) => offer.pinned !== true);
  pinnedTitle.hidden = pinnedOffers.length === 0;
  pinnedOfferList.hidden = pinnedOffers.length === 0;
  if (offers.length === 0) {
    emptyOffers.textContent = "No package list was captured for this region. AuriX has no current offer for sale.";
    return;
  }

  const renderOffer = (offer, target) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = offer.pinned === true ? "detail-offer detail-offer--pinned" : "detail-offer";
    button.setAttribute("aria-pressed", String(offer.id === selectedReferenceOffer?.id));
    const icon = document.createElement("span");
    icon.className = "detail-offer__icon";
    icon.setAttribute("aria-hidden", "true");
    const referenceIconKind = detailArt?.dataset?.slug === "pubg-mobile" ? "coins" : "";
    attachDetailOfferIcon(icon, offer.name, referenceIconKind);
    const name = document.createElement("span");
    name.className = "detail-offer__name";
    name.textContent = offer.name;
    const price = document.createElement("span");
    price.className = "detail-offer__price";
    price.textContent = offer.stock_label || offer.observed_price;
    if (offer.stock_label) {
      const row = document.createElement("div");
      row.className = `${button.className} detail-offer--preview`;
      row.append(icon, name, price);
      target.append(row);
      return;
    }
    const transfer = document.createElement("span");
    transfer.className = "detail-offer__transfer";
    transfer.setAttribute("aria-hidden", "true");
    transfer.textContent = offer.id === selectedReferenceOffer?.id ? "✓" : "+";
    button.append(icon, name, price, transfer);
    button.addEventListener("click", () => {
      if (selectedReferenceOffer === offer) return;
      selectedReferenceOffer = offer;
      detailSelection.textContent = `${offer.name} · ${offer.observed_price}`;
      detailActionStatus.textContent = "Reference selection only; no AuriX offer is approved. Use the MLBB Region Checker under Game tools to check an account.";
      clearRecipientPreview();
      updateOfferSelection(button);
    });
    target.append(button);
  };
  pinnedOffers.forEach((offer) => renderOffer(offer, pinnedOfferList));
  regularOffers.forEach((offer) => renderOffer(offer, offerList));
}

function clearSearchTimer() {
  if (searchTimer !== null) globalThis.clearTimeout?.(searchTimer);
  searchTimer = null;
}
savedGamesButton?.addEventListener("click", () => {
  clearSearchTimer();
  savedOnly = !savedOnly;
  void loadCatalog();
});
searchInput.addEventListener("input", () => {
  clearSearchTimer();
  if (searchClear) searchClear.hidden = !searchInput.value;
  if (typeof globalThis.setTimeout === "function") searchTimer = globalThis.setTimeout(() => void loadCatalog(), 180);
  else void loadCatalog();
});
searchClear?.addEventListener("click", () => {
  clearSearchTimer();
  searchInput.value = "";
  searchInput.focus();
  void loadCatalog();
});
catalogReset?.addEventListener("click", () => {
  clearSearchTimer();
  savedOnly = false;
  activeCategory = "";
  searchInput.value = "";
  void loadCatalog();
});
catalogRetry?.addEventListener("click", () => { clearSearchTimer(); void loadCatalog({ refreshDiscovery: true }); });

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  clearSearchTimer();
  loadCatalog();
});
searchToggle.addEventListener("click", () => {
  const isOpen = searchForm.dataset.mobileOpen !== "true";
  searchForm.dataset.mobileOpen = String(isOpen);
  searchToggle.setAttribute("aria-expanded", String(isOpen));
  searchToggle.setAttribute("aria-label", isOpen ? "Hide game search" : "Search games");
  searchToggleLabel.textContent = isOpen ? "Hide" : "Search";
  if (isOpen && typeof searchInput.focus === "function") searchInput.focus({ preventScroll: true });
});

document.querySelector("#dialog-close").addEventListener("click", () => dialog.close());
document.querySelector("#nav-open").addEventListener("click", () => navigationDialog.showModal());
document.querySelector("#nav-close").addEventListener("click", () => navigationDialog.close());
auxiliaryClose.addEventListener("click", () => {
  if (activeGuideRoute) closeGuideRoute();
  else auxiliaryDialog.close();
});
auxiliaryDialog.addEventListener("cancel", (event) => {
  if (activeGuideRoute && guideRouteStack.length) {
    event.preventDefault();
    closeGuideRoute();
  }
});
document.querySelector("#wallet-open").addEventListener("click", openWallet);
walletClose.addEventListener("click", () => {
  if (!walletConfirmationPreview.hidden) {
    setWalletStep(1);
    walletStatus.textContent = "Returned to amount and method selection. Nothing was sent.";
    return;
  }
  walletDialog.close();
});
document.querySelector("#wallet-clear").addEventListener("click", () => {
  selectedWalletAmount = "";
  selectedWalletMethod = "";
  walletCustomAmount.value = "";
  walletCustomAmount.setAttribute("aria-invalid", "false");
  walletCustomAmountHint.textContent = WALLET_AMOUNT_HINT;
  setWalletStep(1);
  updateWalletSelection();
  walletStatus.textContent = "Selection cleared.";
});
walletNext.addEventListener("click", () => {
  if (walletNext.disabled) return;
  setWalletStep(2);
  walletStatus.textContent = "Preview only — no transfer account is configured and no payment was initiated.";
});
document.querySelector("#detail-clear").addEventListener("click", () => {
  selectedReferenceOffer = null;
  detailSelection.textContent = playerAccountIsCurrent() ? "Select a package" : "Check your player account first";
  detailActionStatus.textContent = "Selection cleared.";
  if (activeLiveOffers.length) {
    renderLiveOffers();
    updateCheckoutAction();
  } else clearRecipientPreview();
  if (!referenceDetail.hidden) {
    if (activeLiveOffers.length > 0) {
      renderLiveOffers();
      return;
    }
    const activeRegion = activeReferenceRegions.find((region) => region.id === activeReferenceRegion);
    if (activeRegion) renderReferenceOffers(activeRegion);
  }
});

accountCheckButton.addEventListener("click", async () => {
  if (accountCheckButton.disabled || accountCheckBusy || !accountCheckEnabled) return;
  const fields = recipientFieldValues();
  const playerId = fields.player_id || "";
  const zoneId = fields.server_id || fields.zone_id || "";
  if (Object.values(fields).some((value) => !value)
      || (activeProductSlug === "mobile-legends" && (!MLBB_ACCOUNT_ID_PATTERN.test(playerId) || !zoneId))) {
    recipientStatus.textContent = activeProductSlug === "mobile-legends"
      ? "Enter your numeric Player ID and Zone ID first."
      : "Enter all requested player account details first.";
    return;
  }
  rememberPlayerId(playerId);
  if (activeProductSlug === "mobile-legends") rememberMlbbZoneId(zoneId);
  accountCheckBusy = true;
  const requestRevision = ++accountCheckRevision;
  selectedReferenceOffer = null;
  playerAccountCheck = null;
  playerAccountConfirmed = false;
  accountCheckReview.hidden = true;
  accountCheckConfirm.checked = false;
  detailSelection.textContent = "Checking player account…";
  accountCheckButton.textContent = "Checking account…";
  for (const control of recipientFields.querySelectorAll("input, button")) control.disabled = true;
  recipientStatus.textContent = "Checking your game account. AuriX sign-in is not required.";
  renderLiveOffers();
  updateCheckoutAction();
  try {
    const result = await recipientLookupRequest(fields, activeProductSlug);
    if (requestRevision !== accountCheckRevision) return;
    if (typeof result.player_name !== "string" || !result.player_name.trim()
        || (result.region !== null && result.region !== undefined
          && (typeof result.region !== "string" || !result.region.trim()))) {
      throw new Error("The supplier did not confirm this game account.");
    }
    const verifiedFields = result.verified_fields && typeof result.verified_fields === "object"
      && !Array.isArray(result.verified_fields) ? result.verified_fields : fields;
    if (Object.keys(verifiedFields).length !== Object.keys(fields).length
        || Object.keys(fields).some((key) => typeof verifiedFields[key] !== "string")) {
      throw new Error("The verified account details were incomplete.");
    }
    for (const input of recipientFields.querySelectorAll("input")) {
      input.value = verifiedFields[input.dataset.fieldKey];
    }
    const eligibleIds = Array.isArray(result.eligible_offer_ids)
      ? [...new Set(result.eligible_offer_ids.filter((id) => typeof id === "string"))] : undefined;
    playerAccountCheck = { fields: verifiedFields, player_name: result.player_name.trim(),
      region: typeof result.region === "string" ? result.region.trim() : null,
      ...(eligibleIds ? { eligible_offer_ids: eligibleIds } : {}) };
    playerAccountConfirmed = false;
    accountCheckName.textContent = result.player_name.trim();
    accountCheckRegion.textContent = `Account region: ${playerAccountCheck.region || "Not reported"}`;
    accountCheckReview.hidden = false;
    accountCheckConfirm.checked = false;
    rememberPlayerId(playerId);
    if (activeProductSlug === "mobile-legends") rememberMlbbZoneId(verifiedFields.server_id || verifiedFields.zone_id);
    recipientStatus.textContent = "Account checked. Confirm the player to continue.";
    detailSelection.textContent = "Confirm your player above";
  } catch (error) {
    if (requestRevision !== accountCheckRevision) return;
    playerAccountCheck = null;
    playerAccountConfirmed = false;
    accountCheckReview.hidden = true;
    accountCheckConfirm.checked = false;
    recipientStatus.textContent = error.status === 400 || error.status === 429
      ? error.message
      : "Player check is temporarily unavailable. Your details stay on this device; try again shortly.";
    detailSelection.textContent = "Player check required";
  } finally {
    if (requestRevision !== accountCheckRevision) return;
    accountCheckBusy = false;
    accountCheckButton.textContent = "Check player ID";
    for (const control of recipientFields.querySelectorAll("input, button")) control.disabled = false;
    renderLiveOffers();
    updateCheckoutAction();
  }
});

accountCheckConfirm.addEventListener("change", () => {
  playerAccountConfirmed = accountCheckConfirm.checked && playerAccountDetailsMatch();
  if (!playerAccountConfirmed) selectedReferenceOffer = null;
  recipientStatus.textContent = playerAccountConfirmed
    ? "Player confirmed. Choose a package below."
    : "Confirm this player before choosing a package.";
  detailSelection.textContent = playerAccountConfirmed ? "Select a package" : "Confirm your player above";
  renderLiveOffers();
  updateCheckoutAction();
});

document.querySelector("#detail-buy").addEventListener("click", async () => {
  if (detailBuy.disabled || checkoutBusy) {
    if (!recipientCheckoutEnabled) detailActionStatus.textContent = "Account checks are unavailable for this game right now. No order or payment was started.";
    return;
  }
  const revision = checkoutRevision;
  const confirmingOrder = Boolean(checkoutReview);
  checkoutBusy = true;
  updateCheckoutAction();
  try {
    if (checkoutReview) {
      const result = await checkoutRequest("/api/v1/checkout/confirm", { quote_id: checkoutReview.quote_id, confirmed: true });
      if (!result.order?.order_id) throw new Error("Order confirmation was incomplete. Check your order history before retrying.");
      if (revision !== checkoutRevision) return;
      checkoutOrder = { orderId: result.order.order_id,
        amountMinor: result.order.amount_minor ?? checkoutReview.amount_minor,
        currency: result.order.currency ?? checkoutReview.currency };
      invalidateCheckoutReview();
      recipientFields.replaceChildren();
      recipientStatus.textContent = "Recipient confirmed and order created.";
      renderCheckoutPaymentOptions();
      recipientCheckoutEnabled = false;
    } else {
      const fields = Object.fromEntries([...recipientFields.querySelectorAll("input")]
        .map((input) => [input.dataset.fieldKey, input.value.trim()]));
      if (activeProductSlug === "mobile-legends") {
        if (fields.player_id) rememberPlayerId(fields.player_id);
        const zoneKey = ["server_id", "zone_id"].find((key) => Object.prototype.hasOwnProperty.call(fields, key));
        if (zoneKey) {
          const zones = await loadMlbbCheckoutZones();
          const zoneId = resolveMlbbCheckoutZone(fields[zoneKey], zones);
          if (!zoneId) throw new Error("Choose a zone name with its ID, or enter the numeric Zone ID from your game profile.");
          fields[zoneKey] = zoneId;
        }
      }
      const result = await checkoutRequest("/api/v1/checkout/review", { offer_id: selectedReferenceOffer.offer_id, fields });
      if (revision !== checkoutRevision) return;
      const quote = result.quote;
      if (!quote || typeof quote.quote_id !== "string" || typeof quote.player_name !== "string"
          || !Number.isSafeInteger(quote.amount_minor) || quote.amount_minor <= 0 || typeof quote.currency !== "string") {
        throw new Error("Account review was incomplete. Please check again.");
      }
      if (activeProductSlug === "mobile-legends") {
        const zoneKey = ["server_id", "zone_id"].find((key) => Object.prototype.hasOwnProperty.call(fields, key));
        if (zoneKey) rememberMlbbZoneId(fields[zoneKey]);
      }
      checkoutReview = quote;
      const review = document.createElement("section");
      review.id = "recipient-checkout-review";
      review.className = "recipient-checkout-review";
      const title = document.createElement("h3");
      title.textContent = quote.player_name;
      const summary = document.createElement("p");
      summary.textContent = `${quote.package} · ${formatMinorAmount(quote.amount_minor, quote.currency)} · Account region: ${quote.region || "not reported"}`;
      const label = document.createElement("label");
      const confirm = document.createElement("input");
      confirm.type = "checkbox";
      confirm.id = "recipient-checkout-confirmed";
      confirm.addEventListener("change", updateCheckoutAction);
      const text = document.createElement("span");
      text.textContent = "I confirm this is the correct player and package.";
      label.append(confirm, text);
      review.append(title, summary, label);
      recipientSection.append(review);
      recipientStatus.textContent = "Account checked. Confirm these details before creating your order. Review expires shortly.";
      confirm.focus();
    }
  } catch (error) {
    if (revision === checkoutRevision) {
      if (error.status === 400) invalidateCheckoutReview();
      detailActionStatus.textContent = error.status === 401
        ? confirmingOrder
          ? "Sign in to finish creating this order. Your checked player, package, and review are kept; then confirm the order again."
          : "Sign in to continue ordering. Your checked player and package are kept; after signing in, tap Continue to order again."
        : confirmingOrder && (error.status === 0 || error.status >= 500)
          ? "Order confirmation could not be verified. Check Order History before retrying."
        : !confirmingOrder && (error.status >= 500 || !error.status)
          ? "We couldn’t verify this account right now. No order or payment was started. Please try again later."
          : error.message || "Account verification is unavailable.";
    }
  } finally {
    checkoutBusy = false;
    updateCheckoutAction();
  }
});

function renderCheckoutPaymentOptions() {
  document.querySelector("#recipient-checkout-payment")?.remove();
  const panel = document.createElement("section");
  panel.id = "recipient-checkout-payment";
  panel.className = "recipient-checkout-payment";
  const title = document.createElement("h3");
  title.textContent = "Payment";
  const summary = document.createElement("p");
  summary.textContent = `Order ${checkoutOrder.orderId} · ${formatMinorAmount(checkoutOrder.amountMinor, checkoutOrder.currency)}`;
  panel.append(title, summary);
  const methods = document.createElement("div");
  methods.className = "recipient-checkout-payment__methods";
  const status = document.createElement("p");
  status.className = "recipient-checkout-payment__status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const walletStatus = document.createElement("p");
  walletStatus.className = "recipient-checkout-payment__status";
  walletStatus.setAttribute("role", "status");
  walletStatus.setAttribute("aria-live", "polite");
  if (!checkoutPaymentsEnabled || checkoutPaymentRails.length === 0) {
    status.textContent = "Payment is not enabled yet. Do not transfer money; check Order History for updates.";
  } else {
    status.textContent = "Choose a method to request the saved transfer instructions. Nothing is charged by this action.";
    void addWalletPaymentOption(checkoutOrder, methods, walletStatus);
    for (const rail of checkoutPaymentRails) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "recipient-checkout-payment__method";
      button.dataset.rail = rail;
      button.textContent = checkoutPaymentAttemptRail
        ? rail === checkoutPaymentAttemptRail ? `Retry ${paymentRailLabel(rail)}` : paymentRailLabel(rail)
        : paymentRailLabel(rail);
      button.disabled = checkoutBusy || Boolean(checkoutPaymentAttemptRail && rail !== checkoutPaymentAttemptRail);
      button.addEventListener("click", () => void openCheckoutPayment(rail, status, methods));
      methods.append(button);
    }
  }
  panel.append(methods, status, walletStatus);
  recipientSection.append(panel);
  detailActionStatus.textContent = `Order ${checkoutOrder.orderId} created. ${checkoutPaymentsEnabled ? "No payment was collected by creating the order." : "Payment is not enabled; no payment was collected."}`;
}

async function addWalletPaymentOption(order, methods, status) {
  try {
    const response = await fetch("/api/v1/wallet", { credentials: "same-origin",
      headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const payload = await response.json();
    const wallet = payload?.wallet;
    if (!Number.isSafeInteger(wallet?.available_minor) || wallet.available_minor <= 0
        || wallet.currency !== order.currency || !methods.isConnected) return;
    if (wallet.available_minor < order.amountMinor) {
      status.textContent = `Wallet is short by ${formatMinorAmount(order.amountMinor - wallet.available_minor, order.currency)}. Wallet payment requires verified funds for the full order total; choose a transfer method.`;
      return;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "recipient-checkout-payment__method";
    button.dataset.walletPayment = "true";
    button.textContent = `Use wallet · ${formatMinorAmount(order.amountMinor, order.currency)}`;
    button.disabled = Boolean(checkoutBusy || checkoutPaymentAttemptRail);
    button.addEventListener("click", async () => {
      if (checkoutBusy || button.disabled) return;
      checkoutBusy = true;
      button.disabled = true;
      for (const control of methods.querySelectorAll("button")) control.disabled = true;
      status.textContent = "Confirming wallet payment…";
      try {
        const result = await checkoutRequest(`/api/v1/orders/${encodeURIComponent(order.orderId)}/wallet-payment`, { confirmed: true });
        if (result.order?.state !== "paid_reserved" && !result.replayed) {
          throw new Error("Wallet payment was not confirmed. Check Order History before retrying.");
        }
        status.textContent = "Wallet payment reserved. Follow delivery in Order History.";
        detailActionStatus.textContent = status.textContent;
        dialog.close();
        await globalThis.AurixCustomer?.openOrder(order.orderId);
      } catch (error) {
        const shortfall = error.details?.shortfall_minor;
        status.textContent = Number.isSafeInteger(shortfall) && shortfall > 0
          ? `Wallet is short by ${formatMinorAmount(shortfall, error.details.currency || order.currency)}. Choose a transfer method to pay the full order total.`
          : error.status === 0 || error.status >= 500
            ? "Wallet payment status could not be verified. Check Order History before retrying."
            : error.message || "Wallet payment could not be completed. Reload this order before retrying.";
        button.disabled = false;
        for (const control of methods.querySelectorAll("button")) control.disabled = false;
      } finally {
        checkoutBusy = false;
      }
    });
    methods.prepend(button);
    status.textContent = `Wallet balance ${formatMinorAmount(wallet.available_minor, order.currency)}. You can pay this order from your wallet or choose a transfer method.`;
  } catch {
    // Keep bank transfer options usable when the private wallet read is unavailable.
  }
}

function paymentRailLabel(rail) {
  return ({ kpay: "KPay", wavepay: "WavePay", aya: "AYA Pay", uab: "UABPay", cb: "CB Pay" })[rail] || rail.toUpperCase();
}

async function openCheckoutPayment(rail, status, methods) {
  if (!checkoutOrder || checkoutBusy || (checkoutPaymentAttemptRail && checkoutPaymentAttemptRail !== rail)) return;
  checkoutPaymentAttemptRail = rail;
  checkoutBusy = true;
  for (const button of methods.querySelectorAll("button")) button.disabled = true;
  status.textContent = `Requesting ${paymentRailLabel(rail)} instructions…`;
  try {
    const result = await checkoutRequest("/api/v1/checkout/payment", { order_id: checkoutOrder.orderId, rail });
    const payment = result.payment;
    if (!payment || payment.order_id !== checkoutOrder.orderId
        || payment.rail !== rail || !Number.isSafeInteger(payment.amount_minor)
        || payment.amount_minor !== checkoutOrder.amountMinor || payment.currency !== checkoutOrder.currency
        || typeof payment.expires_at !== "string") {
      throw new Error("Payment instructions could not be verified. Check Order History before retrying.");
    }
    if (typeof globalThis.AurixCustomer?.openOrder === "function") {
      const savedOrderId = checkoutOrder.orderId;
      dialog.close();
      await globalThis.AurixCustomer.openOrder(savedOrderId);
      return;
    }
    methods.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = payment.order_state === "awaiting_payment" && !payment.expired
      ? "Transfer the exact amount to this account"
      : payment.order_state === "awaiting_payment" ? "Payment window expired" : "Payment already matched";
    methods.append(heading);
    const amount = document.createElement("p");
    amount.textContent = formatMinorAmount(payment.amount_minor, payment.currency);
    methods.append(amount);
    if (payment.order_state === "awaiting_payment" && !payment.expired) {
      for (const [label, value] of [["Method", paymentRailLabel(rail)], ["Account name", payment.account_name], ["Account", payment.account]]) {
        if (typeof value !== "string" || !value.trim()) continue;
        const row = document.createElement("p");
        row.textContent = `${label}: ${value}`;
        methods.append(row);
      }
      if (typeof payment.qr_asset === "string") {
        const qr = document.createElement("img");
        qr.className = "payment-receiver-qr";
        qr.alt = `${paymentRailLabel(rail)} receiver QR code`;
        qr.loading = "lazy";
        qr.decoding = "async";
        qr.referrerPolicy = "no-referrer";
        qr.addEventListener("error", () => {
          qr.remove();
          const unavailable = document.createElement("p");
          unavailable.textContent = "QR code unavailable. Use the listed account details or contact support.";
          methods.append(unavailable);
        });
        qr.src = `/api/v1/orders/${encodeURIComponent(checkoutOrder.orderId)}/payment-qr`;
        methods.append(qr);
      }
    }
    status.textContent = payment.expired && payment.order_state === "awaiting_payment"
      ? "These saved instructions have expired. Do not transfer; open Order History or contact support."
      : payment.order_state === "awaiting_payment"
        ? `Instructions expire ${formatPaymentExpiry(payment.expires_at)}. Save this order number for support. No transfer has been verified yet.`
        : "The order has moved past payment. Do not send another transfer.";
    const followOrder = document.createElement("button");
    followOrder.type = "button";
    followOrder.className = "recipient-checkout-payment__method";
    followOrder.textContent = "Submit receipt / follow order";
    const savedOrderId = checkoutOrder.orderId;
    followOrder.addEventListener("click", () => {
      dialog.close();
      void globalThis.AurixCustomer?.openOrder(savedOrderId);
    });
    methods.append(followOrder);
  } catch (error) {
    status.textContent = error.status === 0 || error.status >= 500
      ? "Payment instructions could not be verified. Check Order History before retrying."
      : error.message || "Payment instructions could not be loaded. Check Order History before retrying.";
    for (const button of methods.querySelectorAll("button")) button.disabled = button.dataset.rail !== rail;
  } finally {
    checkoutBusy = false;
  }
}

function formatPaymentExpiry(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "time unavailable"
    : `${new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Yangon" }).format(date)} MMT`;
}


detailHelp.addEventListener("click", () => {
  detailHelpCopy.hidden = !detailHelpCopy.hidden;
  detailHelpCopy.textContent = dialog.dataset.offerState === "unavailable"
    ? "No packages are available right now. Retry later or go back to choose another game."
    : dialog.dataset.referenceSnapshot === "true"
      ? "These package details are a reference only. Pricing and eligibility are pending, so they cannot start an order or payment."
      : "Choose a package for your account region. Check the returned player name before ordering; do not pay if the account details do not match.";
});
detailRetry.addEventListener("click", () => {
  if (activeProduct) void showProduct(activeProduct);
});
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});
navigationDialog.addEventListener("click", (event) => {
  if (event.target === navigationDialog) navigationDialog.close();
});
auxiliaryDialog.addEventListener("click", (event) => {
  if (event.target === auxiliaryDialog) auxiliaryDialog.close();
});

floatingWallet.addEventListener("click", openWallet);
function navigateStorePage(tab) {
  if (tab === activeStoreTab) {
    if (tab === "home") window.scrollTo({ top: 0, behavior: "smooth" });
    else if (tab === "games") document.querySelector("#catalog-title").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const update = () => {
    setStoreTab(tab);
    if (typeof storeScrollPositions[tab] === "number") {
      window.scrollTo({ top: storeScrollPositions[tab], behavior: "instant" });
    } else if (tab === "home") window.scrollTo({ top: 0, behavior: "instant" });
    else if (tab === "games") {
      document.querySelector("#catalog-title").scrollIntoView({ behavior: "instant", block: "start" });
    }
    if (tab === "games") void loadCatalog();
  };
  if (globalThis.AurixPager) globalThis.AurixPager.move(update, tab === "games" ? 1 : -1);
  else update();
}
document.querySelector("#bottom-home").addEventListener("click", () => navigateStorePage("home"));
document.querySelector("#bottom-games").addEventListener("click", () => navigateStorePage("games"));
document.querySelector("#bottom-history").addEventListener("click", () => {
  if (globalThis.AurixCustomer) void globalThis.AurixCustomer.openHistory("orders");
  else openNavigationById("order-history");
});
document.querySelector("#bottom-profile").addEventListener("click", () => {
  if (globalThis.AurixCustomer) void globalThis.AurixCustomer.openProfile();
  else openNavigationById("update-profile");
});
document.querySelector("#footer-guides-open")?.addEventListener("click", () => document.querySelector("#nav-open")?.click());

function setStoreTab(tab) {
  if (["home", "games", "wallet"].includes(tab) && tab !== activeStoreTab) {
    storeScrollPositions[activeStoreTab] = window.scrollY;
    activeStoreTab = tab;
  }
  if (["home", "games", "wallet"].includes(tab)) document.querySelector("main").dataset.storeView = tab;
  const catalogTitle = document.querySelector("#catalog-title");
  if (catalogTitle && (tab === "home" || tab === "games")) {
    catalogTitle.textContent = tab === "games" ? "All games" : "Choose your game";
  }
  for (const name of ["home", "games", "wallet", "history", "profile"]) {
    const button = document.querySelector(`#bottom-${name}`);
    if (!button) continue;
    button.className = `bottom-navigation__item${name === tab ? " bottom-navigation__item--active" : ""}`;
    button.setAttribute("aria-current", name === tab ? "page" : "false");
  }
}
function showWalletHome() {
  const update = () => {
    setStoreTab("wallet");
    const card = document.querySelector("#home-wallet-card");
    if (card && !card.hidden) card.scrollIntoView({ behavior: "smooth", block: "center" });
    else window.scrollTo({ top: 0, behavior: "smooth" });
  };
  if (globalThis.AurixPager) globalThis.AurixPager.move(update, -1);
  else update();
}
globalThis.AurixStore = Object.freeze({ resetAuxiliary: resetGuideRoutes, setTab: setStoreTab, showWallet: showWalletHome });

updateConnectionState(typeof navigator === "undefined" ? true : navigator.onLine !== false);
if (typeof globalThis.addEventListener === "function") {
  globalThis.addEventListener("online", () => updateConnectionState(true));
  globalThis.addEventListener("offline", () => updateConnectionState(false));
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { void loadCatalog(); void refreshNoticeBadge(); }
});
window.addEventListener("focus", () => void refreshNoticeBadge());
if (typeof navigator !== "undefined" && navigator.serviceWorker && typeof navigator.serviceWorker.register === "function") {
  navigator.serviceWorker.register("/game-store/service-worker.js", { scope: "/game-store/" }).catch(() => {
    // The storefront remains fully usable when the browser disallows PWA registration.
  });
}

renderWalletChoices();
setWalletStep(1);
updateWalletSelection();
loadNavigation();
catalogCache = readCatalogCache();
if (catalogCache) renderCachedCatalog();
loadCatalog();
loadLeaderboardPreview();
refreshNoticeBadge();
