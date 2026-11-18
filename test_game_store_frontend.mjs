import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

class ElementStub {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.open = false;
    this.hidden = false;
    this.disabled = false;
    this.isConnected = true;
    this.classes = new Set();
    this.classList = {
      toggle: (name, force) => {
        const next = force ?? !this.classes.has(name);
        if (next) this.classes.add(name);
        else this.classes.delete(name);
        return next;
      },
      contains: (name) => this.classes.has(name),
    };
  }

  get childElementCount() {
    return this.children.length;
  }

  get lastChild() {
    return this.children.at(-1) || null;
  }

  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }

  append(...children) {
    children.forEach((child) => {
      if (child && typeof child === "object") {
        if (child.parent) child.parent.children = child.parent.children.filter((item) => item !== child);
        child.parent = this;
      } else if (child === null || child === undefined) return;
      this.children.push(child);
    });
  }

  prepend(...children) {
    children.reverse().forEach((child) => {
      if (child && typeof child === "object") child.parent = this;
      this.children.unshift(child);
    });
  }

  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  before() {}
  focus() { this.focused = true; }
  scrollIntoView() {}

  replaceChildren(...children) {
    this.children.forEach((child) => { if (child.parent === this) child.parent = null; });
    this.children = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  removeAttribute(name) { delete this.attributes[name]; }

  querySelectorAll(selector) {
    if (selector === "input") return this.children.flatMap((child) => [
      ...(child.tagName === "input" ? [child] : []), ...(child.querySelectorAll?.(selector) || []),
    ]);
    return selector === "button"
      ? this.children.filter((child) => child.tagName === "button")
      : [];
  }

  showModal() {
    this.open = true;
  }

  close() {
    this.open = false;
  }

  click() { this.listeners.click?.({ target: this }); }
}

const categories = [
  { id: "mobile-games", name: "Mobile Games" },
  { id: "pc-games", name: "PC Games" },
  { id: "gift-cards", name: "Gift Card" },
  { id: "mobile-apps", name: "Mobile App" },
  { id: "redeem-codes", name: "Redeem Code" },
];

const products = Array.from({ length: 65 }, (_, index) => ({
  slug: index === 0 ? "mobile-legends" : index === 1 ? "pubg-mobile" : `game-${index + 1}`,
  name: index === 0 ? "Mobile Legends" : index === 1 ? "PUBG Mobile" : `Game ${index + 1}`,
  category_ids: ["mobile-games"],
  category: "Mobile Games",
  reference_badges: index < 2 ? ["Pre-order"] : [],
  reference_detail_available: index < 2,
  status: "supplier_review",
  purchase_enabled: false,
}));

function makeResponse(payload) {
  return { ok: true, status: 200, json: async () => payload };
}

function productPage(offset, sourceProducts = products) {
  const page = sourceProducts.slice(offset, offset + 50);
  const nextOffset = offset + page.length;
  return {
    schema_version: 1,
    status: "preview",
    checkout_enabled: false,
    categories,
    products: page,
    result_count: page.length,
    total_count: sourceProducts.length,
    next_offset: nextOffset < sourceProducts.length ? nextOffset : null,
  };
}

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("storefront loads all pages, filters categories, and fetches product status", async () => {
  let controlsActive = false;
  const selectors = [
    "#category-list",
    "#product-grid",
    ".catalog",
    "#catalog-status",
    "#store-stage",
    "#availability-copy",
    "#search-form",
    "#saved-games",
    "#search-toggle",
    "#search-toggle-label",
    "#search",
    "#nav-open",
    "#nav-close",
    "#notification-open",
    "#notification-count",
    "#wallet-balance",
    "#wallet-balance-toggle",
    "#home-wallet-topup",
    "#home-order-history",
    "#home-topup-history",
    "#home-events-news",
    "#home-support-chat",
    "#home-notification",
    "#home-leaderboard-status",
    "#home-leaderboard-preview",
    "#leaderboard-open",
    "#bottom-wallet",
    "#bottom-navigation",
    "#bottom-home",
    "#bottom-games",
    "#bottom-history",
    "#bottom-profile",
    "#navigation-dialog",
    "#navigation-list",
    "#navigation-summary",
    "#auxiliary-dialog",
    "#auxiliary-close",
    "#auxiliary-title",
    "#auxiliary-copy",
    "#auxiliary-content",
    "#auxiliary-header-actions",
    "#auxiliary-status",
    "#product-dialog",
    "#dialog-title",
    "#detail-art",
    "#detail-art-label",
    "#detail-hero-meta", "#detail-favorite", "#detail-notices", "#detail-more",
    "#detail-tab-packages", "#detail-tab-stock", "#detail-tab-info",
    "#detail-panel-packages", "#detail-panel-stock", "#detail-panel-info",
    "#detail-stock-heading", "#detail-stock-copy", "#detail-stock-regions",
    "#dialog-copy",
    "#dialog-close",
    "#detail-reference-note",
    "#detail-reference",
    "#detail-package-step",
    "#purchase-steps",
    "#purchase-step-account",
    "#purchase-step-package",
    "#detail-package-gate",
    ".detail-order-promise",
    ".detail-actions",
    "#detail-tabs",
    "#detail-region-label",
    "#detail-reference-description",
    ".package-notes",
    "#detail-region-list",
    "#detail-pinned-title",
    "#detail-pinned-offers",
    "#detail-offers-title",
    "#detail-offers",
    "#detail-empty-offers",
    "#detail-recipient",
    "#detail-recipient-fields",
    "#detail-account-check",
    "#detail-account-check-review",
    "#detail-account-check-name",
    "#detail-account-check-region",
    "#detail-account-check-confirm",
    "#detail-recipient-status",
    "#detail-live-status",
    "#detail-retry",
    ".dialog-state",
    "#detail-help",
    "#detail-help-copy",
    "#detail-selection",
    "#detail-action-status",
    "#detail-clear",
    "#detail-buy",
    "#wallet-dialog",
    "#wallet-amounts",
    "#wallet-custom-amount",
    "#wallet-custom-amount-hint",
    "#wallet-methods",
    "#wallet-selection",
    "#wallet-next",
    "#wallet-status",
    "#wallet-step-1",
    "#wallet-step-2",
    "#wallet-step-one-amount",
    "#wallet-step-one-method",
    "#wallet-step-one-boundary",
    "#wallet-confirmation-preview",
    "#wallet-transfer-amount",
    "#wallet-transfer-method",
    "#wallet-account-icon",
    "#wallet-account-label",
    "#wallet-open",
    "#wallet-close",
    "#wallet-clear",
  ];
  const elements = new Map(["main", ...selectors].map((selector) => [selector, new ElementStub()]));
  const calls = [];
  const document = {
    querySelector: (selector) => elements.get(selector),
    createElement: (tagName) => new ElementStub(tagName),
    createElementNS: (_namespace, tagName) => new ElementStub(tagName),
    createTextNode: (text) => String(text),
    addEventListener: () => {},
  };
  const fetch = async (requestUrl) => {
    calls.push(requestUrl);
    const url = new URL(requestUrl, "http://aurix.test");
    if (url.pathname === "/api/v1/navigation") {
      return makeResponse({
        schema_version: 1,
        status: "preview",
        checkout_enabled: false,
        items: [
          { id: "home", label: "Home", surface: "catalog", status: "available" },
          {
            id: "top-up",
            label: "Top-up",
            surface: "wallet_preview",
            status: "reference_only",
            notice: "Wallet preview only",
          },
          {
            id: "order-history",
            label: "Order History",
            surface: "empty_state",
            status: "reference_empty",
            tabs: ["Orders", "Pre-orders"],
            empty_label: "No Data",
            notice: "No order data",
          },
          {
            id: "support-chat",
            label: "Support Chat",
            surface: "not_captured",
            status: "unavailable",
            notice: "Support is unavailable",
          },
          {
            id: "events-news",
            label: "Events/News",
            surface: "reference_feed",
            status: "reference_only",
            notice: "Reference feed",
            items: [
              { id: "news-1", title: "JJK Skin Event August 7", date: "Jul 30, 2026", summary: "Two free-token phases." },
              { id: "news-2", title: "August Collector Skin", date: "Jul 28, 2026", summary: "Collector exchange choices." },
            ],
          },
          {
            id: "mbb-hero-guide",
            label: "MLBB Hero Guide",
            surface: "hero_guide",
            status: "reference_only",
            notice: "Dated, attributed guide",
          },
          {
            id: "myanmar-game-guide",
            label: "Myanmar Game Guide",
            surface: "strategy_guide",
            status: "reference_only",
            notice: "Myanmar-localized strategy reference for PUBG Mobile. No player lookup, match history, or account action is enabled.",
            guide_slug: "pubg-mobile-myanmar",
          },
          {
            id: "leaderboard",
            label: "Most active buyers",
            surface: "leaderboard",
            status: "available",
            notice: "Opt-in ranking of delivered top-ups.",
          },
          {
            id: "update-profile",
            label: "Update Profile",
            surface: "profile_reference",
            status: "disabled",
            notice: "Profile reference",
          },
        ],
      });
    }
    if (url.pathname === "/api/v1/strategy-guides/mlbb") {
      return makeResponse({
        schema_version: 1, slug: "mlbb", freshness: "operator_snapshot", player_lookup: false,
        snapshot: { patch: "2.1.90", rankings_measured_at: "2026-09-21T10:00:42.336Z" },
        rankings_freshness: {
          status: "fresh",
          last_measured_at: "2026-09-21T10:00:42.336Z",
          source_reported_measured_date: "2026-09-21",
          last_successful_snapshot_capture_at: "2026-09-24T03:38:04.737906+00:00",
          age_days: 3,
          freshness_window_days: 7,
          time_basis: "UTC calendar date",
          notice: "Snapshot age only; this is not live game data or a provider sync-health check.",
        },
        sections: [
          { id: "hero-tiers", filters: { roles: ["Tank", "Mage", "Assassin"], lanes: ["Roam", "Mid Lane", "Jungle"] }, heroes: [
            { name: "Snapshot Hero", tier: "SS", roles: ["Tank"], lanes: ["Roam"], score: 72.1, win_rate: 51.3, pick_rate: 4.7 },
            { name: "Ranked Mage", tier: "A", roles: ["Mage"], lanes: ["Mid Lane"], win_rate: 50.1, pick_rate: 3.2 },
            { name: "Ranked Assassin", tier: "B", roles: ["Assassin"], lanes: ["Jungle"], win_rate: 49.7, pick_rate: 2.4 },
          ] },
          { id: "items", categories: ["All", "Attack", "Magic", "Defense", "Movement", "Jungling", "Roaming"], items: [
            { name: "Snapshot Item", category: "Attack", price: 123, stats: ["+1 ATK"], passive: "Deter: reference passive", recipe: ["Small Item"] },
            { name: "Snapshot Magic Item", category: "Magic", price: 456, stats: ["+1 Magic"] },
            { name: "Snapshot Defense Item", category: "Defense", price: 789, stats: ["+1 DEF"] },
            { name: "Snapshot Movement Item", category: "Movement", price: 321, stats: ["+1 Speed"] },
            { name: "Snapshot Jungle Item", category: "Jungling", price: 654, stats: ["+1 Jungle"] },
            { name: "Snapshot Roaming Item", category: "Roaming", price: 987, stats: ["+1 Roam"] },
          ] },
        ],
      });
    }
    if (url.pathname === "/api/v1/store/leaderboard") {
      return makeResponse({ period: "rolling_30_days", as_of: "2026-11-16T12:00:00Z", entries: [
        { rank: 1, display_name: "Game Fan", completed_orders: 2, achievement: "Starter", next_achievement: "Rising", orders_to_next: 3 },
      ] });
    }
    if (url.pathname === "/api/v1/store/notices") return makeResponse({ notices: [] });
    if (url.pathname === "/api/v1/strategy-guides/pubg-mobile-myanmar") {
      return makeResponse({
        schema_version: 1, slug: "pubg-mobile-myanmar", freshness: "curated", player_lookup: false,
        sections: [{ title: "မြန်မာ Snapshot", title_en: "Snapshot", cards: [{ title: "Scout", body: "Reference body" }] }],
      });
    }
    if (url.pathname === "/api/v1/strategy-guides/free-fire-myanmar") {
      return makeResponse({
        schema_version: 1, slug: "free-fire-myanmar", game: "Garena Free Fire",
        freshness: "official_patch_snapshot", reviewed_on: "2026-09-24", player_lookup: false,
        patch_version: "OB55", patch_changes_effective_on: "2026-10-01", patch_status: "verification_required",
        source: { patch_notes_url: "https://ff.garena.com/en/article/1712/" },
        sections: [
          { id: "team-play", title: "အဖွဲ့လိုက်ကစားနည်း", title_en: "Squad play", cards: [{ title: "Shot-caller", body: "အဖွဲ့လိုက်ဆုံးဖြတ်ပါ။" }] },
          { id: "myanmar-play-checklist", title: "မြန်မာကစားသမား စစ်ဆေးရန်", title_en: "Myanmar player checklist", cards: [{ title: "လိုင်း", body: "တည်ငြိမ်မှုကိုစစ်ပါ။" }] },
          { id: "patch-watch", title: "OB55 patch ပြင်ဆင်မှု", title_en: "OB55 patch watch", cards: [{ title: "အောက်တိုဘာ ၁ ရက်တွင် စီစဉ်ထားသည်", body: "Update ရပြီးမှ စမ်းပါ။" }] },
        ],
      });
    }
    if (url.pathname === "/api/v1/live-catalog") {
      return makeResponse({
        schema_version: 1, status: "live", checkout_enabled: false,
        catalog_controls_active: controlsActive,
        categories, products: [], result_count: 0, total_count: 0, next_offset: null,
      });
    }
    if (url.pathname.startsWith("/api/v1/catalog/products/")) {
      const requestedSlug = url.pathname.split("/").pop();
      const requestedProduct = products.find((product) => product.slug === requestedSlug) || products[0];
      const referenceSnapshot = requestedSlug === "pubg-mobile"
        ? {
          reference_only: true,
          observed_on: "2026-09-22",
          description: "PUBG reference description",
          regions: [
            {
              id: "pubg-uc",
              name: "PUBG Mobile : UC",
              offers: [
                { id: "uc-60", name: "60 UC", observed_price: "4,500 Ks" },
                { id: "uc-325", name: "325 UC", observed_price: "21,900 Ks", pinned: true },
              ],
            },
            {
              id: "pubg-mobile",
              name: "PUBG Mobile",
              offers: [
                { id: "uc-first-purchase", name: "First Purchase Pack", observed_price: "4,350 Ks", pinned: true },
                { id: "uc-prime-month", name: "Prime (1 Month)", observed_price: "4,350 Ks", pinned: true },
                { id: "pubg-60", name: "60", observed_price: "4,450 Ks" },
                { id: "pubg-weekly-mythic", name: "Weekly Mythic Emblem Value Pack", observed_price: "13,250 Ks" },
                { id: "pubg-firearm-materials", name: "Upgradable Firearm Materials Pack", observed_price: "12,950 Ks" },
              ],
            },
          ],
        }
        : {
          reference_only: true,
          observed_on: "2026-09-21",
          description: "Reference description",
          regions: [
            { id: "global", name: "Mobile Legends (Global)", offers: [] },
            {
              id: "sea",
              name: "SG, Malay, Indo",
              offers: [
                { id: "diamonds-55", name: "Diamonds 55", observed_price: "4,550 Ks" },
                { id: "diamonds-14", name: "Diamonds 14", observed_price: "1,300 Ks" },
              ],
            },
          ],
        };
      return makeResponse({
        schema_version: 1,
        checkout_enabled: false,
        product: requestedProduct,
        regions: [],
        offers: [],
        notice: "Supplier authorization and regional availability are under review.",
        reference_snapshot: referenceSnapshot,
      });
    }
    const category = url.searchParams.get("category");
    const query = (url.searchParams.get("q") || "").trim().toLocaleLowerCase();
    const filtered = products.filter((product) =>
      (!category || product.category_ids.includes(category)) &&
      (!query || product.name.toLocaleLowerCase().includes(query))
    );
    return makeResponse(productPage(Number(url.searchParams.get("offset") || 0), filtered));
  };
  const context = vm.createContext({ document, fetch, URLSearchParams, AbortController, AbortSignal, addEventListener: () => {}, window: { matchMedia: () => ({ matches: true }), addEventListener: () => {} } });
  const source = await readFile(new URL("./web/game-store/app.js", import.meta.url), "utf8");

  vm.runInContext(source, context);
  await settle();

  assert.equal(elements.get("#product-grid").children.length, 65);
  assert.equal(elements.get(".catalog").dataset.catalogMode, "reference");
  assert.equal(elements.get("#home-leaderboard-preview").children.length, 1);
  assert.equal(elements.get("#home-leaderboard-preview").children[0].children[1].textContent, "Game Fan");
  const originalThirdGame = elements.get("#product-grid").children[2];
  const originalThirdFavorite = originalThirdGame.children.find((child) => child.className.includes("product-card__favorite"));
  originalThirdFavorite.listeners.click();
  assert.equal(elements.get("#product-grid").children[0], originalThirdGame, "pinned games move to the front of the catalog");
  assert.ok(originalThirdFavorite.children.some((child) => child.attributes.class === "product-card__favorite-icon"));
  assert.equal(originalThirdGame.children[0].children.find((child) => child.className === "product-card__pin-badge").hidden, false);
  originalThirdFavorite.listeners.click();
  assert.equal(elements.get("#product-grid").children[2], originalThirdGame, "unpinning restores the original catalog position");

  // Auth owns the real account balance; the catalog never invents a sample zero.
  assert.ok(!source.includes('walletBalanceVisible ? "0 Ks"'));
  elements.get("#bottom-history").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Order History");
  assert.ok(!calls.some((url) => String(url).includes("test_token.txt")));
  assert.ok(!calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/orders"));
  elements.get("#auxiliary-close").listeners.click();
  elements.get("#bottom-wallet").listeners.click();
  assert.equal(elements.get("main").dataset.storeView, "wallet", "wallet navigation opens the account wallet view");

  assert.equal(elements.get("#navigation-list").children.length, 3);
  elements.get("#nav-open").listeners.click();
  assert.equal(elements.get("#navigation-dialog").open, true);
  const myanmarGuideButton = elements.get("#navigation-list").children.find((button) => button.textContent === "PUBG Myanmar Guide");
  myanmarGuideButton.listeners.click();
  await settle();
  assert.equal(elements.get("#auxiliary-title").textContent, "PUBG Myanmar Guide");
  assert.equal(elements.get("#auxiliary-content").children[3].children[0].children[0].textContent, "မြန်မာ Snapshot");
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/strategy-guides/pubg-mobile-myanmar"));
  const leaderboardButton = elements.get("#navigation-list").children.find((button) => button.textContent === "Most active buyers");
  assert.ok(leaderboardButton);
  const gameSwitch = elements.get("#auxiliary-content").children[1];
  assert.equal(gameSwitch.attributes["aria-label"], "Choose game guide");
  assert.equal(gameSwitch.children[0].textContent, "PUBG Mobile");
  assert.equal(gameSwitch.children[1].textContent, "Free Fire");
  gameSwitch.children[1].listeners.click();
  await settle();
  assert.equal(elements.get("#auxiliary-title").textContent, "Free Fire Myanmar Guide");
  const freeFireContent = elements.get("#auxiliary-content");
  assert.match(freeFireContent.children[0].children[1].children[0].textContent, /Garena Free Fire/);
  assert.equal(elements.get("#auxiliary-copy").textContent, "");
  assert.match(freeFireContent.children[2].textContent, /OB55 reference · reviewed 2026-09-24/);
  assert.match(freeFireContent.children[2].textContent, /the scheduled date 2026-10-01 has passed; verify the current game client/);
  assert.equal(freeFireContent.children[3].children[0].children[0].textContent, "အဖွဲ့လိုက်ကစားနည်း");
  assert.equal(freeFireContent.children[5].children[0].children[0].textContent, "OB55 patch ပြင်ဆင်မှု");
  const freeFireFooter = freeFireContent.children.at(-1);
  assert.ok(freeFireFooter.children.some((child) => child?.textContent === "Official OB55 patch notes" && child.href === "https://ff.garena.com/en/article/1712/"));
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/strategy-guides/free-fire-myanmar"));
  assert.match(elements.get("#auxiliary-status").textContent, /OB55 · reviewed 2026-09-24 · no player data queried/);
  elements.get("#auxiliary-close").listeners.click();
  elements.get("#nav-open").listeners.click();
  const mlbbGuideButton = elements.get("#navigation-list").children.find((button) => button.textContent === "MLBB Reference Guide");
  mlbbGuideButton.listeners.click();
  await settle();
  assert.ok(elements.get("#auxiliary-content").children.some((child) => child.textContent === "Tactical Tools"));
  assert.match(elements.get("#auxiliary-status").textContent, /MLBB reference.*no player data queried/);
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/strategy-guides/mlbb"));
  const guideSearch = elements.get("#auxiliary-content").children[0];
  const rankingsFreshnessNote = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-rankings-freshness");
  assert.equal(rankingsFreshnessNote.dataset.rankingsFreshness, "withheld");
  assert.match(rankingsFreshnessNote.textContent, /Live rankings are paused until source terms and data freshness are verified/);
  guideSearch.value = "lane";
  guideSearch.listeners.input();
  const guideToolsBeforeClear = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-tool-grid");
  assert.equal(guideToolsBeforeClear.children[0].hidden, false);
  assert.equal(guideToolsBeforeClear.children[1].hidden, false);
  assert.deepEqual(guideToolsBeforeClear.children.map((card) => card.children[0].dataset.icon), [
    "draft-strategy", "counter-picks",
  ]);
  assert.ok(guideToolsBeforeClear.children.every((card) =>
    card.children[0].attributes["aria-hidden"] === "true"
      && card.children[0].children[0].tagName === "svg"
      && card.children[0].children[0].children.length > 0));
  assert.equal(guideToolsBeforeClear.children[0].children[0].children[1].src, "/game-store/assets/kenney/multiplayer.png");
  guideSearch.value = "";
  guideSearch.listeners.input();
  const wikiList = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-wiki-list");
  assert.deepEqual(wikiList.children.map((row) => row.children[0].dataset.icon), [
    "hero-tier-list", "item-list", "emblems-talents",
  ]);
  assert.ok(wikiList.children.every((row) => row.children[0].children[0].children.length > 0));
  wikiList.children[0].listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Hero Reference");
  assert.equal(elements.get("#auxiliary-close").attributes["aria-label"], "Back to MLBB Reference Guide");
  let guideContent = elements.get("#auxiliary-content");
  let heroDirectory = guideContent.children.find((child) => child.className === "guide-hero-directory");
  let guideFilters = guideContent.children.find((child) => child.className === "guide-filters");
  const roleFilter = guideFilters.children[0].children[1].children.find((button) => button.textContent === "Tank");
  roleFilter.listeners.click();
  const activeRoleFilter = guideFilters.children[0].children[1].children.find((button) => button.textContent === "Tank");
  assert.equal(activeRoleFilter.attributes["aria-pressed"], "true");
  assert.equal(
    heroDirectory.children.some((card) => card.dataset.hero === "Snapshot Hero"),
    true,
  );
  const heroCard = heroDirectory.children.find((card) => card.dataset.hero === "Snapshot Hero");
  assert.equal(heroCard.children[0].className, "guide-avatar");
  assert.equal(heroCard.children[0].dataset.role, "Tank");
  assert.equal(heroCard.children[0].children[0].tagName, "img");
  assert.match(heroCard.children[2].textContent, /Tank.*Roam/);
  assert.doesNotMatch(heroCard.textContent, /51\.3%|4\.7%|72\.1/);
  heroCard.listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Snapshot Hero");
  assert.equal(elements.get("#auxiliary-content").children[0].className, "guide-hero-detail");
  assert.match(elements.get("#auxiliary-content").children[0].children[3].textContent, /Rankings and match statistics are paused/);
  elements.get("#auxiliary-close").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Hero Reference");
  guideContent = elements.get("#auxiliary-content");
  heroDirectory = guideContent.children.find((child) => child.className === "guide-hero-directory");
  guideFilters = guideContent.children.find((child) => child.className === "guide-filters");
  assert.equal(guideFilters.children[0].children[1].children.find((button) => button.textContent === "Tank").attributes["aria-pressed"], "true");
  assert.equal(heroDirectory.children[0].dataset.hero, "Snapshot Hero");
  elements.get("#auxiliary-close").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "MLBB Reference Guide");
  const itemWikiList = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-wiki-list");
  itemWikiList.children[1].listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Item Reference");
  const itemContent = elements.get("#auxiliary-content");
  const itemHeaderActions = elements.get("#auxiliary-header-actions");
  const itemLanguage = itemHeaderActions.children.find((child) => child.className.includes("guide-segmented--locale"));
  assert.equal(itemLanguage.attributes["role"], "group");
  assert.equal(itemLanguage.attributes["aria-label"], "Guide language");
  assert.equal(itemContent.children.some((child) => child.className.includes("guide-segmented--locale")), false);
  assert.equal(itemContent.children[0].className, "guide-search");
  const itemSearch = itemContent.children.find((child) => child.className === "guide-search");
  const itemCategories = itemContent.children.find((child) => child.className === "guide-filter-row");
  const itemSort = itemContent.children.find((child) => child.className === "guide-sort");
  const itemList = itemContent.children.find((child) => child.className === "guide-item-list");
  itemLanguage.children.find((button) => button.textContent === "My").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "ပစ္စည်းရည်ညွှန်းစာရင်း");
  assert.equal(itemSearch.placeholder, "ပစ္စည်းအမည် ရှာရန်…");
  assert.equal(itemCategories.children[0].textContent, "အားလုံး");
  assert.equal(itemLanguage.children[1].attributes["aria-pressed"], "true");
  assert.match(elements.get("#auxiliary-status").textContent, /မူရင်းဘာသာဖြင့်သာ ဖော်ပြထားပါသည်/);
  assert.match(elements.get("#auxiliary-status").textContent, /သိမ်းထားသော snapshot.*ပြန်စစ်ပါ/);
  itemLanguage.children.find((button) => button.textContent === "Eng").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "Item Reference");
  const itemCategory = itemCategories.children.find((button) => button.dataset.category === "Attack");
  itemCategory.listeners.click();
  assert.equal(itemList.children[0].children[1].children[0].textContent, "Snapshot Item");
  const attackItemIcon = itemList.children[0].children[0];
  assert.equal(attackItemIcon.dataset.asset, "crossed-swords");
  assert.equal(attackItemIcon.children[1].src, "/game-store/assets/game-icons/crossed-swords.svg");
  itemSort.value = "price";
  itemSort.listeners.change();
  assert.equal(itemList.children[0].children[1].children[0].textContent, "Snapshot Item");
  const magicCategory = itemCategories.children.find((button) => button.dataset.category === "Magic");
  magicCategory.listeners.click();
  const magicItemIcon = itemList.children[0].children[0];
  assert.equal(magicItemIcon.dataset.asset, "spell-book");
  assert.equal(magicItemIcon.children[1].src, "/game-store/assets/game-icons/spell-book.svg");
  for (const [category, iconName] of [["Defense", "shield-reflect"], ["Movement", "boots"], ["Jungling", "jungle"], ["Roaming", "footsteps"]]) {
    itemCategories.children.find((button) => button.dataset.category === category).listeners.click();
    const categoryIcon = itemList.children[0].children[0];
    assert.equal(categoryIcon.dataset.asset, iconName);
    assert.equal(categoryIcon.children[1].src, `/game-store/assets/game-icons/${iconName}.svg`);
  }
  elements.get("#auxiliary-close").listeners.click();
  assert.equal(elements.get("#auxiliary-title").textContent, "MLBB Reference Guide");
  assert.equal(elements.get("#auxiliary-header-actions").children.length, 0);
  const itemGuideWiki = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-wiki-list");
  itemGuideWiki.children[2].listeners.click();
  const emblemContent = elements.get("#auxiliary-content");
  const emblemLanguage = elements.get("#auxiliary-header-actions").children.find((child) => child.className.includes("guide-segmented--locale"));
  const emblemGrid = emblemContent.children.find((child) => child.className === "guide-emblem-grid");
  assert.equal(emblemContent.children.some((child) => child.className.includes("guide-segmented--locale")), false);
  assert.deepEqual(emblemGrid.children.map((card) => [card.children[1].textContent, card.children[0].dataset.asset]), [
    ["Assassin", "assassin-pocket"],
    ["Common", "shield-reflect"],
    ["Fighter", "crossed-swords"],
    ["Mage", "wizard-staff"],
    ["Marksman", "bow-arrow"],
    ["Support", "healing"],
    ["Tank", "shield-reflect"],
  ]);
  emblemLanguage.children.find((button) => button.textContent === "My").listeners.click();
  assert.equal(emblemContent.children.find((child) => child.className === "guide-section-heading").textContent, "အဓိက Emblems");
  assert.equal(elements.get("#auxiliary-title").textContent, "Emblem နမူနာများ");
  assert.match(elements.get("#auxiliary-status").textContent, /အင်္ဂလိပ်ဘာသာဖြင့် ဖော်ပြထားပါသည်/);
  assert.match(elements.get("#auxiliary-status").textContent, /နမူနာသာဖြစ်ပြီး/);
  elements.get("#auxiliary-close").listeners.click();
  const guideTools = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-tool-grid");
  guideTools.children[0].listeners.click();
  const draftShell = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-draft-shell");
  const draftPicker = draftShell.children[1].children.find((child) => child.className === "guide-draft-picker__grid");
  draftPicker.children.find((button) => button.dataset.hero === "Snapshot Hero").listeners.click();
  assert.equal(draftShell.children[0].children[0].children[1].children[0].dataset.hero, "Snapshot Hero");
  draftShell.children[0].children[1].children[0].children[0].listeners.click();
  const enemyDraftPicker = draftShell.children[1].children.find((child) => child.className === "guide-draft-picker__grid");
  enemyDraftPicker.children.find((button) => button.dataset.hero === "Snapshot Hero").listeners.click();
  assert.equal(draftShell.children[0].children[1].children[1].children[0].dataset.hero, undefined, "A hero cannot be selected on both teams");
  await draftShell.children[2].children[2].listeners.click();
  assert.match(elements.get("#auxiliary-status").textContent, /Analysis unavailable|Lane|lane/);
  elements.get("#auxiliary-close").listeners.click();
  elements.get("#auxiliary-content").children.find((child) => child.className === "guide-tool-grid").children[1].listeners.click();
  const counterShell = elements.get("#auxiliary-content").children.find((child) => child.className === "guide-draft-shell");
  assert.equal(counterShell.children[2].children[2].disabled, true);
  assert.equal(counterShell.children[0].children[0].children[1].children[0].dataset.hero, undefined);
  elements.get("#auxiliary-close").listeners.click();
  elements.get("#bottom-wallet").listeners.click();
  assert.equal(elements.get("main").dataset.storeView, "wallet", "wallet navigation opens the account wallet view");

  assert.equal(elements.get("#product-grid").children.length, 65);
  elements.get("#search-toggle").listeners.click();
  assert.equal(elements.get("#search-form").dataset.mobileOpen, "true");
  assert.equal(elements.get("#search-toggle").attributes["aria-expanded"], "true");
  assert.equal(elements.get("#search-toggle-label").textContent, "Hide");
  elements.get("#search-toggle").listeners.click();
  assert.equal(elements.get("#search-form").dataset.mobileOpen, "false");
  assert.equal(elements.get("#search-toggle-label").textContent, "Search");
  assert.equal(elements.get("#catalog-status").textContent, "65 titles · checkout unavailable");
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").searchParams.get("offset") === "50"));

  const pcGames = elements.get("#category-list").children.find((button) => button.dataset.category === "pc-games");
  pcGames.listeners.click();
  await settle();
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").searchParams.get("category") === "pc-games"));

  const mobileButton = elements.get("#category-list").children.find((button) => button.dataset.category === "mobile-games");
  mobileButton.listeners.click();
  await settle();
  const card = elements.get("#product-grid").children.find((item) => item.children.some((child) => child.textContent === "Mobile Legends"));
  const statusButton = card.children.find((child) => child.className === "product-card__details");
  assert.equal(statusButton.attributes["aria-label"], "View packages for Mobile Legends");
  await statusButton.listeners.click();

  assert.equal(elements.get("#product-dialog").open, true);
  assert.ok(calls.some((url) => url.endsWith("/api/v1/catalog/products/mobile-legends")));
  assert.equal(elements.get("#dialog-copy").textContent, "No packages available yet.");
  assert.equal(elements.get("#detail-reference-note").hidden, true);
  assert.equal(elements.get(".package-notes").hidden, true, "empty package notes stay hidden when a listing has no details");
  assert.equal(elements.get("#detail-reference").hidden, true);
  assert.equal(elements.get("#detail-tabs").hidden, true);
  assert.equal(elements.get("#detail-offers").children.length, 0);
  assert.equal(elements.get("#detail-clear").hidden, true);
  assert.equal(elements.get("#detail-buy").disabled, true);
  assert.equal(elements.get("#detail-buy").textContent, "Select a package");
  assert.equal(elements.get("#product-dialog").dataset.offerState, "unavailable");
  assert.equal(elements.get("#detail-tab-packages").attributes["aria-selected"], "true");
  assert.equal(elements.get("#detail-panel-packages").hidden, false);
  assert.equal(elements.get("#detail-panel-stock").hidden, true);
  let detailTransition;
  elements.get("#detail-panel-stock").animate = (_frames, options) => { detailTransition = options; };
  vm.runInContext('window.matchMedia = () => ({matches:false})', context);
  elements.get("#detail-tab-stock").listeners.click();
  assert.equal(detailTransition.duration, 180);
  assert.equal(elements.get("#detail-panel-stock").hidden, false);
  vm.runInContext('window.matchMedia = () => ({matches:true})', context);
  assert.match(elements.get("#detail-stock-heading").textContent, /No approved listings/);
  elements.get("#detail-tab-info").listeners.click();
  assert.equal(elements.get("#detail-panel-info").hidden, false);
  let tabKeyHandled = false;
  elements.get("#detail-tab-info").listeners.keydown({ key: "ArrowLeft", preventDefault() { tabKeyHandled = true; } });
  assert.equal(tabKeyHandled, true);
  assert.equal(elements.get("#detail-tab-stock").attributes["aria-selected"], "true");
  const cardChildren = card.children;
  const cover = cardChildren.find((child) => child.className === "product-card__cover");
  const coverChildren = cover.children;
  const pinButton = cardChildren.find((child) => child.className.includes("product-card__favorite"));
  const asHtmlCollection = (items) => ({
    length: items.length,
    [Symbol.iterator]: function* () { yield* items; },
  });
  card.children = asHtmlCollection(cardChildren);
  cover.children = asHtmlCollection(coverChildren);
  elements.get("#detail-favorite").listeners.click();
  card.children = cardChildren;
  cover.children = coverChildren;
  assert.equal(elements.get("#detail-favorite").attributes["aria-pressed"], "true");
  assert.equal(card.dataset.pinned, "true");
  assert.equal(pinButton.attributes["aria-pressed"], "true");
  elements.get("#detail-favorite").listeners.click();
  assert.equal(elements.get("#detail-favorite").attributes["aria-pressed"], "false");
  assert.equal(card.dataset.pinned, "false");
  assert.equal(pinButton.className, "product-card__favorite");
  assert.equal(pinButton.attributes["aria-pressed"], "false");
  elements.get("#detail-help").listeners.click();
  assert.equal(elements.get("#detail-help-copy").hidden, false);
  assert.ok(!calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/orders"));
  const frontendSource = await readFile(new URL("./web/game-store/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(frontendSource, /test_token\.txt|synthetic-12345/);

  elements.get("#product-dialog").close();
  const pubgCard = elements.get("#product-grid").children.find((item) => item.children.some((child) => child.textContent === "PUBG Mobile"));
  const pubgStatusButton = pubgCard.children.find((child) => child.className === "product-card__details");
  assert.equal(pubgStatusButton.attributes["aria-label"], "View packages for PUBG Mobile");
  await pubgStatusButton.listeners.click();
  assert.equal(elements.get("#dialog-title").textContent, "PUBG Mobile");
  assert.equal(elements.get("#detail-reference").hidden, true);
  assert.equal(elements.get("#detail-offers").children.length, 0);
  assert.equal(elements.get("#detail-buy").disabled, true);

  elements.get("#wallet-open").listeners.click();
  assert.equal(elements.get("main").dataset.storeView, "wallet");
  assert.equal(elements.get("#wallet-dialog").open, false, "wallet action opens the signed-in wallet view, not an unconfigured deposit form");
  assert.ok(!calls.some((url) => /\/api\/v1\/(payments|wallet|orders)/.test(new URL(url, "http://aurix.test").pathname)));

  elements.get("#search").value = "pubg";
  elements.get("#search-form").listeners.submit({ preventDefault() {} });
  await settle();
  assert.equal(elements.get("#product-grid").children.length, 1);
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").searchParams.get("q") === "pubg"));

  controlsActive = true;
  const before = calls.length;
  elements.get("#search-form").listeners.submit({ preventDefault() {} });
  await settle();
  assert.equal(elements.get("#product-grid").children.length, 0);
  assert.match(elements.get("#catalog-status").textContent, /No live offers/);
  assert.ok(!calls.slice(before).some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/catalog/products"));
  elements.get("#nav-open").listeners.click();
  elements.get("#navigation-list").children.find((button) => button.textContent === "Most active buyers").listeners.click();
  await settle();
  assert.equal(elements.get("#auxiliary-title").textContent, "Most active buyers");
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/store/leaderboard"));

  vm.runInContext(`catalogCache = {
    savedAt: Date.now(), source: "live",
    products: [{ slug: "pubg-mobile", name: "PUBG Mobile", category: "Live Offers", category_ids: ["live-offers"] }],
    categories: [{ id: "live-offers", name: "Live Offers" }],
  };`, context);
  let resolveLiveCatalog;
  context.fetch = async (requestUrl) => {
    assert.equal(new URL(requestUrl, "http://aurix.test").pathname, "/api/v1/live-catalog");
    return new Promise((resolve) => { resolveLiveCatalog = resolve; });
  };
  const refresh = vm.runInContext("loadCatalog()", context);
  assert.equal(elements.get("#store-stage").textContent, "Updating listings");
  assert.equal(elements.get("#availability-copy").children[0].textContent, "Checking listings. ");
  resolveLiveCatalog(makeResponse({
    schema_version: 1, status: "live", checkout_enabled: true,
    categories: [{ id: "live-offers", name: "Live Offers" }],
    products: [{ slug: "pubg-mobile", name: "PUBG Mobile", category: "Live Offers", category_ids: ["live-offers"], offers: [], purchase_enabled: false }],
    result_count: 1, total_count: 1, next_offset: null,
  }));
  await refresh;
  assert.equal(elements.get("#store-stage").textContent, "Current listings");

  context.fetch = async () => { throw new Error("synthetic network failure"); };
  await vm.runInContext("loadCatalog()", context);
  assert.equal(elements.get("#store-stage").textContent, "Browse only");
  assert.equal(elements.get("#availability-copy").children[0].textContent, "Browse only. ");
});

test("storefront renders an approved live price and promotion disclosure", async () => {
  const selectors = [
    "#category-list", "#product-grid", ".catalog", "#catalog-status", "#search-form", "#saved-games", "#search-toggle", "#search-toggle-label", "#search",
    "#nav-open", "#nav-close", "#navigation-dialog", "#navigation-list", "#navigation-summary",
    "#notification-open", "#notification-count", "#wallet-balance", "#wallet-balance-toggle", "#home-wallet-topup",
    "#home-order-history", "#home-topup-history", "#home-events-news", "#home-support-chat",
    "#home-notification", "#home-leaderboard-status", "#home-leaderboard-preview", "#leaderboard-open", "#bottom-wallet",
    "#bottom-navigation", "#bottom-home", "#bottom-games", "#bottom-history", "#bottom-profile",
    "#auxiliary-dialog", "#auxiliary-close", "#auxiliary-title", "#auxiliary-copy",
    "#auxiliary-content", "#auxiliary-header-actions", "#auxiliary-status", "#product-dialog", "#dialog-title",
    "#detail-art", "#detail-art-label", "#detail-hero-meta", "#detail-favorite", "#detail-notices", "#detail-more",
    "#detail-tab-packages", "#detail-tab-stock", "#detail-tab-info",
    "#detail-panel-packages", "#detail-panel-stock", "#detail-panel-info",
    "#detail-stock-heading", "#detail-stock-copy", "#detail-stock-regions",
    "#dialog-copy", "#dialog-close", "#detail-reference-note", "#detail-reference", "#detail-package-step",
    "#detail-tabs", "#detail-region-label",
    "#detail-reference-description", ".package-notes", "#detail-region-list", "#detail-pinned-title", "#detail-pinned-offers", "#detail-offers-title", "#detail-offers",
    "#detail-empty-offers", "#detail-live-status", "#detail-retry", ".dialog-state", "#detail-help", "#detail-help-copy",
    "#purchase-steps", "#purchase-step-account", "#purchase-step-package", "#detail-package-gate", ".detail-order-promise", ".detail-actions",
    "#detail-recipient", "#detail-recipient-fields", "#detail-account-check", "#detail-account-check-review",
    "#detail-account-check-name", "#detail-account-check-region", "#detail-account-check-confirm", "#detail-recipient-status",
    "#detail-selection", "#detail-action-status", "#detail-clear", "#detail-buy",
    "#wallet-dialog", "#wallet-amounts", "#wallet-custom-amount", "#wallet-custom-amount-hint", "#wallet-methods", "#wallet-selection",
    "#wallet-next", "#wallet-status", "#wallet-step-1", "#wallet-step-2",
    "#wallet-step-one-amount", "#wallet-step-one-method", "#wallet-step-one-boundary",
    "#wallet-confirmation-preview", "#wallet-transfer-amount", "#wallet-transfer-method",
    "#wallet-account-icon", "#wallet-account-label", "#wallet-open", "#wallet-close", "#wallet-clear",
  ];
  const elements = new Map(["main", ...selectors].map((selector) => [selector, new ElementStub()]));
  const calls = [];
  let checkoutEnabled = false;
  let paymentsEnabled = false;
  let checkoutConfirmFailure = false;
  let checkoutPaymentFailure = false;
  let walletAvailable = 0;
  let productStatusUnavailable = false;
  const commands = [];
  const liveProduct = {
    slug: "mobile-legends", name: "Mobile Legends", category_ids: ["live-offers"],
    category: "Live Offers", reference_badges: [], status: "available", purchase_enabled: true,
    live: true,
    offers: [{
      offer_id: "aurix-mlbb-d55", name: "55 Diamonds", price_minor: 5000,
      currency: "MMK", promo_discount_minor: 300, region: { type: "global", label: "Global" },
      required_input_labels: ["player_id", "server_id"], fulfillment_kind: "direct_topup",
    }, {
      offer_id: "aurix-mlbb-sea-d55", name: "55 Diamonds", price_minor: 5200,
      currency: "MMK", promo_discount_minor: 0, region: { type: "game_server", label: "SEA" },
      required_input_labels: ["player_id", "server_id"], fulfillment_kind: "direct_topup",
    }],
  };
  const document = {
    querySelector: (selector) => elements.get(selector) || [...elements.values()]
      .flatMap((root) => {
        const walk = (node) => node && typeof node === "object" ? [node, ...(node.children || []).flatMap(walk)] : [];
        return walk(root);
      }).find((node) => `#${node.id}` === selector),
    createElement: (tagName) => new ElementStub(tagName),
    createElementNS: (_namespace, tagName) => new ElementStub(tagName),
    addEventListener: () => {},
  };
  const fetch = async (requestUrl, options = {}) => {
    calls.push(requestUrl);
    const url = new URL(requestUrl, "http://aurix.test");
    if (url.pathname === "/api/v1/checkout/config") return makeResponse({ account_check_enabled: checkoutEnabled, recipient_review_enabled: checkoutEnabled, payments_enabled: paymentsEnabled, payment_rails: paymentsEnabled ? ["kpay"] : [] });
    if (url.pathname === "/api/v1/games/mlbb/account-check") {
      const fields = JSON.parse(options.body);
      return makeResponse({ player_name: "Checked player", region: "Myanmar", verified_fields: fields });
    }
    if (url.pathname === "/api/v1/checkout/review") {
      commands.push(JSON.parse(options.body));
      return makeResponse({ quote: { quote_id: "quote-1", player_name: "Checked player", package: "55 Diamonds", amount_minor: 5000, currency: "MMK", region: null } });
    }
    if (url.pathname === "/api/v1/checkout/confirm") {
      commands.push(JSON.parse(options.body));
      if (checkoutConfirmFailure) {
        checkoutConfirmFailure = false;
        return { ok: false, status: 503, json: async () => { throw new SyntaxError("Unexpected token <"); } };
      }
      return makeResponse({ order: { order_id: "game-1", state: "awaiting_payment" } });
    }
    if (url.pathname === "/api/v1/checkout/payment") {
      commands.push(JSON.parse(options.body));
      if (checkoutPaymentFailure) {
        checkoutPaymentFailure = false;
        return { ok: false, status: 503, json: async () => { throw new SyntaxError("Unexpected token <"); } };
      }
      return makeResponse({ payment: { order_id: "game-1", rail: "kpay", amount_minor: 5000,
        currency: "MMK", expires_at: "2026-11-13T12:00:00Z", expired: false,
        order_state: "awaiting_payment", account: "091234", account_name: "AuriX Shop", qr_asset: "kpay-r1.png" } });
    }
    if (url.pathname === "/api/v1/wallet") {
      return makeResponse({ wallet: { available_minor: walletAvailable, currency: "MMK" } });
    }
    if (url.pathname.endsWith("/wallet-payment")) {
      commands.push(JSON.parse(options.body));
      return makeResponse({ order: { state: "paid_reserved" }, payment: { rail: "wallet" }, replayed: false });
    }
    if (url.pathname === "/api/v1/navigation") {
      return makeResponse({ schema_version: 1, status: "preview", checkout_enabled: false, items: [] });
    }
    if (url.pathname === "/api/v1/live-catalog") {
      return makeResponse({
        schema_version: 1, status: "live", checkout_enabled: false,
        categories: [{ id: "live-offers", name: "Live Offers" }], products: [liveProduct],
        result_count: 1, total_count: 1, next_offset: null,
      });
    }
    if (url.pathname === "/api/v1/catalog/products/mobile-legends") {
      if (productStatusUnavailable) return { ok: false, status: 503 };
      return makeResponse({ status: "live", checkout_enabled: false, purchase_enabled: true,
        product: liveProduct, offers: liveProduct.offers });
    }
    throw new Error(`unexpected request: ${url.pathname}`);
  };
  const context = vm.createContext({ document, fetch, URLSearchParams, AbortController, AbortSignal, addEventListener: () => {}, window: { matchMedia: () => ({ matches: true }), addEventListener: () => {} } });
  const source = await readFile(new URL("./web/game-store/app.js", import.meta.url), "utf8");

  vm.runInContext(source, context);
  await settle();

  assert.equal(vm.runInContext('detailOfferIconKind("Weekly Diamond Pass")', context), "gift");
  assert.equal(vm.runInContext('detailOfferIconKind("Diamonds 55")', context), "diamond");
  assert.equal(vm.runInContext('detailOfferIconKind("Magic Spell")', context), "spell");
  assert.equal(vm.runInContext('detailOfferIconKind("325 UC")', context), "coins");
  assert.equal(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"fresh",source_reported_measured_date:"2026-09-21",last_successful_snapshot_capture_at:"2026-09-24T03:38:04.737906+00:00",age_days:3,freshness_window_days:7}).status', context), "withheld");
  assert.match(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"stale",source_reported_measured_date:"2026-09-10",last_successful_snapshot_capture_at:"2026-09-20T00:00:00Z",age_days:10,freshness_window_days:7}).copy', context), /Live rankings are paused until source terms and data freshness are verified/);
  assert.equal(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"fresh",source_reported_measured_date:"2026-09-21",last_successful_snapshot_capture_at:"2026-09-24T03:38:04.737906+00:00",age_days:7,freshness_window_days:7}).status', context), "withheld");
  assert.match(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"unknown"}).copy', context), /Live rankings are paused until source terms and data freshness are verified/);
  assert.match(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"future_dated"}).copy', context), /Live rankings are paused until source terms and data freshness are verified/);
  assert.equal(vm.runInContext('mlbbRankingsFreshnessPresentation({status:"fresh",age_days:3,freshness_window_days:7}).status', context), "withheld");
  assert.equal(elements.get("#product-grid").children.length, 1);
  assert.equal(elements.get(".catalog").dataset.catalogMode, "live");
  assert.equal(elements.get("#catalog-status").textContent, "1 games · prices from the latest approved supplier snapshot");
  const card = elements.get("#product-grid").children[0];
  const livePrice = card.children.find((child) => child.className === "product-card__price-row");
  assert.ok(livePrice);
  assert.match(livePrice.children[0].textContent, /from 5,000 MMK/);
  assert.match(livePrice.children[1].textContent, /save 300 MMK/);
  await card.children.find((child) => child.className === "product-card__details").listeners.click();
  await settle();
  assert.equal(elements.get("#detail-offers").children.length, 2);
  assert.equal(elements.get("#dialog-copy").textContent, "Confirm the returned player and region.");
  assert.equal(elements.get("#detail-reference-note").textContent, "MMK pricing · supplier stock unreported");
  assert.match(elements.get("#detail-stock-heading").textContent, /Current approved listings/);
  assert.match(elements.get("#detail-stock-copy").textContent, /does not report unit quantities/);
  assert.equal(elements.get("#detail-stock-regions").children.length, 2);
  assert.equal(elements.get("#detail-hero-meta").textContent, "2 packages · 2 regions");
  assert.equal(vm.runInContext('formatStockObservation("2026-11-15T00:00:00Z")', context), "15 Nov 2026");
  assert.equal(elements.get(".package-notes").hidden, true, "empty package notes stay out of live details");
  assert.equal(elements.get("#detail-package-gate").hidden, true, "step two already explains the initial package lock");
  assert.equal(elements.get(".detail-order-promise").hidden, false);
  assert.equal(elements.get(".detail-actions").classList.contains("detail-actions--has-offer"), false);
  assert.equal(elements.get("#detail-region-list").children.length, 3);
  assert.equal(elements.get("#detail-region-label").textContent, "Available regions");
  assert.equal(elements.get("#detail-region-label").hidden, false);
  assert.equal(elements.get("#detail-region-list").children[1].textContent, "Global");
  const offer = elements.get("#detail-offers").children[0];
  assert.equal(offer.children[0].dataset.iconKind, "diamond");
  assert.equal(offer.children[0].children[0].src, "/game-store/assets/aurix/item-diamond.svg");
  assert.equal(offer.children[2].className, "detail-offer__promo");
  assert.match(offer.children[2].textContent, /save 300 MMK/);
  assert.match(offer.children[3].textContent, /5,000 MMK/);
  assert.equal(offer.disabled, true, "packages stay locked until the player check is available and complete");
  assert.equal(elements.get("#detail-recipient").hidden, false);
  assert.equal(elements.get("#detail-recipient-fields").children.length, 2);
  assert.equal(elements.get("#detail-account-check").disabled, true);
  assert.equal(elements.get("#detail-live-status").textContent, "Account checks unavailable.");
  assert.equal(elements.get("#detail-live-status").hidden, false);
  assert.equal(elements.get(".dialog-state").hidden, false);
  assert.ok(calls.some((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/catalog/products/mobile-legends"),
    "opening a game refreshes its current product detail");

  checkoutEnabled = true;
  paymentsEnabled = true;
  await card.children.find((child) => child.className === "product-card__details").listeners.click();
  await settle();
  assert.equal(elements.get(".dialog-state").hidden, true, "empty live status row does not occupy space when account checks work");
  assert.equal(elements.get("#detail-live-status").textContent, "");
  const recipientInputs = elements.get("#detail-recipient-fields").querySelectorAll("input");
  const playerInput = recipientInputs[0];
  const zoneInput = recipientInputs[1];
  playerInput.value = "123456789";
  playerInput.listeners.input();
  assert.equal(elements.get("#detail-buy").textContent, "Enter Zone ID above");
  zoneInput.value = "4574";
  zoneInput.listeners.input();
  assert.equal(elements.get("#detail-buy").textContent, "Check your player ID above");
  await elements.get("#detail-account-check").listeners.click();
  await settle();
  assert.equal(elements.get("#detail-account-check-name").textContent, "Checked player");
  assert.equal(elements.get("#detail-package-gate").hidden, false);
  assert.equal(elements.get("#detail-package-gate").textContent, "Confirm player details to unlock packages.");
  elements.get("#detail-account-check-confirm").checked = true;
  elements.get("#detail-account-check-confirm").listeners.change();
  assert.equal(vm.runInContext("playerAccountIsCurrent()", context), true);
  assert.equal(elements.get("#detail-package-gate").hidden, true);
  assert.equal(elements.get("#detail-offers").children[0].disabled, false);
  const firstLiveOffer = elements.get("#detail-offers").children[0];
  firstLiveOffer.listeners.click();
  const secondLiveOffer = elements.get("#detail-offers").children[1];
  secondLiveOffer.listeners.click();
  elements.get("#detail-region-list").children[2].listeners.click();
  assert.equal(elements.get("#detail-offers").children.length, 1);
  assert.match(elements.get("#detail-offers").children[0].children[2].textContent, /5,200 MMK/);
  elements.get("#detail-region-list").children[0].listeners.click();
  assert.equal(elements.get("#detail-offers").children.length, 2);
  elements.get("#detail-offers").children[0].listeners.click();
  assert.equal(elements.get(".detail-actions").classList.contains("detail-actions--has-offer"), true);
  assert.equal(elements.get("#detail-buy").disabled, false);
  await elements.get("#detail-buy").listeners.click();
  assert.equal(document.querySelector("#recipient-checkout-review").children[0].textContent, "Checked player");
  assert.equal(elements.get("#detail-buy").disabled, true, "Player confirmation is mandatory");
  playerInput.value = "987654321";
  playerInput.listeners.input();
  assert.equal(document.querySelector("#recipient-checkout-review"), undefined, "Edited recipient invalidates review");
  assert.equal(elements.get("#detail-buy").disabled, true, "edited identity must be rechecked before checkout");
  playerInput.value = "123456789";
  playerInput.listeners.input();
  await elements.get("#detail-account-check").listeners.click();
  await settle();
  elements.get("#detail-account-check-confirm").checked = true;
  elements.get("#detail-account-check-confirm").listeners.change();
  elements.get("#detail-offers").children[0].listeners.click();
  await elements.get("#detail-buy").listeners.click();
  const confirmation = document.querySelector("#recipient-checkout-confirmed");
  confirmation.checked = true;
  confirmation.listeners.change();
  checkoutConfirmFailure = true;
  await elements.get("#detail-buy").listeners.click();
  assert.match(elements.get("#detail-action-status").textContent, /Order confirmation could not be verified\. Check Order History before retrying\./);
  assert.equal(document.querySelector("#recipient-checkout-payment"), undefined, "uncertain confirmation does not open payment actions");
  await elements.get("#detail-buy").listeners.click();
  assert.match(elements.get("#detail-action-status").textContent, /game-1 created.*[Nn]o payment was collected/);
  assert.deepEqual(commands.at(-1), { quote_id: "quote-1", confirmed: true });
  const paymentPanel = document.querySelector("#recipient-checkout-payment");
  assert.ok(paymentPanel);
  const methodButton = paymentPanel.children[2].children[0];
  checkoutPaymentFailure = true;
  await methodButton.listeners.click();
  await settle();
  assert.match(paymentPanel.children[3].textContent, /Payment instructions could not be verified\. Check Order History before retrying\./);
  assert.equal(methodButton.disabled, false, "the same payment method can be retried after a gateway error");
  await methodButton.listeners.click();
  await settle();
  await settle();
  assert.deepEqual(commands.at(-1), { order_id: "game-1", rail: "kpay" });
  assert.match(paymentPanel.children[2].children[0].textContent, /Transfer the exact amount/);
  const paymentQr = paymentPanel.children[2].children.find((child) => child.tagName === "img");
  assert.ok(paymentQr, "an approved receiver QR is shown with the payment instructions");
  assert.equal(paymentQr.src, "/api/v1/orders/game-1/payment-qr");
  assert.match(paymentQr.alt, /KPay.*QR/i);
  assert.match(paymentPanel.children[3].textContent, /No transfer has been verified/);
  assert.equal(elements.get("#detail-buy").disabled, true);

  walletAvailable = 6000;
  vm.runInContext("checkoutPaymentAttemptRail = null", context);
  const walletMethods = new ElementStub();
  const walletStatus = new ElementStub();
  context.walletTestMethods = walletMethods;
  context.walletTestStatus = walletStatus;
  await vm.runInContext("addWalletPaymentOption({orderId: 'wallet-ui-order', amountMinor: 5000, currency: 'MMK'}, walletTestMethods, walletTestStatus)", context);
  assert.ok(walletMethods.children[0], walletStatus.textContent);
  assert.match(walletMethods.children[0].textContent, /Use wallet · 5,000 MMK/);
  walletAvailable = 4000;
  const shortWalletMethods = new ElementStub();
  const shortWalletStatus = new ElementStub();
  context.shortWalletMethods = shortWalletMethods;
  context.shortWalletStatus = shortWalletStatus;
  await vm.runInContext("addWalletPaymentOption({orderId: 'short-wallet-order', amountMinor: 5000, currency: 'MMK'}, shortWalletMethods, shortWalletStatus)", context);
  assert.equal(shortWalletMethods.children.length, 0);
  assert.match(shortWalletStatus.textContent, /short by 1,000 MMK.*verified funds for the full order total/);
  await walletMethods.children[0].listeners.click();
  assert.deepEqual(commands.at(-1), { confirmed: true });
  assert.match(walletStatus.textContent, /Wallet payment reserved/);

  vm.runInContext('renderReferenceSnapshot({observed_on:"2026-11-17",description:"Package terms",regions:[{id:"with-notes",name:"Notes",description:"Regional details",offers:[]},{id:"without-notes",name:"No notes",description:"",offers:[]}]})', context);
  assert.equal(elements.get(".package-notes").hidden, false, "package notes appear when details exist");
  elements.get("#detail-region-list").children[1].listeners.click();
  assert.equal(elements.get(".package-notes").hidden, true, "empty notes hide when the selected region has no details");
  vm.runInContext('activeLiveOffers=[{offer_id:"single",region:{type:"global",label:"Global"}}];playerAccountCheck=null;renderLiveRegions()', context);
  assert.equal(elements.get("#detail-region-label").hidden, true, "a single available region needs no separate choice heading");
  assert.equal(elements.get("#detail-region-list").children[0].textContent, "Region · Global");
  assert.equal(elements.get("#detail-region-list").children[0].tagName, "span", "a one-option region is context, not a dead selector");

  const openProductDetails = card.children.find((child) => child.className === "product-card__details");
  const commandCount = commands.length;
  const productRequestCount = calls.filter((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/catalog/products/mobile-legends").length;
  productStatusUnavailable = true;
  await openProductDetails.listeners.click();
  await settle();
  assert.equal(elements.get("#detail-retry").hidden, false);
  assert.match(elements.get("#detail-live-status").textContent, /Try again before choosing a package/);
  assert.equal(elements.get("#product-dialog").dataset.offerState, "unavailable");
  assert.equal(elements.get("#detail-buy").disabled, true);
  assert.equal(commands.length, commandCount, "a failed product refresh does not create an order or payment");
  productStatusUnavailable = false;
  await elements.get("#detail-retry").listeners.click();
  await settle();
  assert.equal(elements.get("#detail-retry").hidden, true);
  assert.equal(calls.filter((url) => new URL(url, "http://aurix.test").pathname === "/api/v1/catalog/products/mobile-legends").length, productRequestCount + 2);
});

test("screenshot-derived reference surfaces remain wired to the preview shell", async () => {
  const [markup, source, styles, theme, serviceWorker, refinements, premium] = await Promise.all([
    readFile(new URL("./web/game-store/index.html", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/app.js", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/styles.css", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/theme.css", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/service-worker.js", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/storefront-refinements.css", import.meta.url), "utf8"),
    readFile(new URL("./web/game-store/premium-theme.css", import.meta.url), "utf8"),
  ]);
  assert.match(source, /renderHeroGuide/);
  assert.match(source, /renderRegionChecker/);
  assert.match(source, /Zone names are unavailable\. Enter the numeric Zone ID from your game profile\./);
  assert.doesNotMatch(source, /No saved zone list is available here/);
  assert.match(source, /account\.setAttribute\("aria-label", "Account ID"\)/);
  assert.match(source, /\/api\/v1\/games\/mlbb\/account-check/);
  assert.match(source, /searchForm\.dataset\.mobileOpen = String\(isOpen\)/);
  assert.match(source, /auxiliaryDialog\.dataset\.surface = item\.surface/);
  assert.match(source, /globalThis\.AurixCustomer\.openProfile/);
  assert.match(source, /renderMyanmarGameGuide/);
  assert.match(source, /function guideSvgIcon\(/);
  assert.match(source, /createElementNS\("http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(source, /PUBG Myanmar Guide/);
  assert.match(source, /wallet-confirmation-preview/);
  assert.match(source, /walletAccountLabel\.textContent = selectedWalletMethod/);
  assert.match(source, /if \(!walletConfirmationPreview\.hidden\)/);
  assert.match(markup, /id="wallet-confirmation-preview"/);
  assert.match(markup, /class="wallet-dialog__header"/);
  assert.match(markup, /id="wallet-transfer-amount"/);
  assert.match(markup, /Top-up is unavailable/);
  assert.match(markup, /No customer payment account or receipt review is connected/);
  assert.match(styles, /\.guide-tool-grid/);
  assert.match(styles, /\.region-checker/);
  assert.match(styles, /#search-form\[data-mobile-open="true"\] \{ display: flex; \}/);
  assert.match(styles, /#catalog-status\[data-state="ready"\] \{ position: absolute; width: 1px; height: 1px; overflow: hidden/);
  assert.match(styles, /\.home-wallet-card__note \{ position: absolute; width: 1px; height: 1px/);
  assert.match(markup, /id="search-toggle"/);
  assert.match(markup, /id="catalog-title">Choose your game<\/h2>/);
  assert.match(styles, /\.region-checker-crest/);
  assert.match(styles, /\.auxiliary-dialog__header \{ position: sticky/);
  assert.match(styles, /data-surface="region_checker"\] #auxiliary-copy/);
  assert.match(styles, /\.profile-reference/);
  assert.match(styles, /\.myanmar-guide-section/);
  assert.match(markup, /id="bottom-wallet" class="bottom-navigation__item"/);
  assert.match(styles, /\.bottom-navigation__spacer/);
  assert.match(styles, /grid-template-columns: repeat\(2, minmax\(0, 1fr\)\) 72px repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(styles, /scroll-snap-type: x mandatory/);
  assert.match(styles, /\.detail-offers \{ grid-template-columns: 1fr;/);
  assert.match(styles, /\.detail-art/);
  assert.match(markup, /id="detail-art"/);
  assert.match(markup, /id="detail-view-tabs" class="detail-view-tabs" role="tablist"/);
  assert.match(markup, /id="detail-tab-packages"[^>]*role="tab"/);
  assert.match(markup, /id="detail-tab-stock"[^>]*>Stock<\/button>/);
  assert.match(markup, /id="detail-tab-info"[^>]*>Info<\/button>/);
  assert.match(markup, /id="detail-panel-stock"[^>]*role="tabpanel"[^>]*hidden/);
  assert.match(markup, /id="detail-favorite"[^>]*aria-pressed="false"/);
  assert.match(source, /function renderLiveStock\(\)/);
  assert.match(source, /does not report unit quantities/);
  assert.match(premium, /\.detail-view-tab\[aria-selected="true"\]/);
  assert.match(premium, /\.detail-view-panel\[hidden\] \{ display: none !important; \}/);
  assert.match(premium, /@media \(max-width: 760px\)[\s\S]*\.detail-view-tabs \{ gap:/);
  assert.match(markup, /class="detail-order-promise">Sign in only to order\.<\/p>/);
  assert.match(theme, /#detail-action-status \{ min-height: 0; margin: 0; padding: 0;/);
  assert.match(theme, /#detail-action-status:not\(:empty\)[\s\S]*animation: detail-status-enter/);
  assert.match(theme, /prefers-reduced-motion: reduce[\s\S]*#detail-action-status:not\(:empty\) \{ animation: none;/);
  assert.ok(markup.indexOf('class="store-intro"') < markup.indexOf('class="catalog"'));
  assert.ok(markup.indexOf('class="catalog"') < markup.indexOf('id="home-overview"'));
  assert.doesNotMatch(markup, /home-focus-strip|home-buying-flow/);
  assert.doesNotMatch(theme, /main\[data-store-view="home"\] \.catalog\[data-catalog-mode="reference"\] \{ display: none; \}/);
  assert.match(source, /product\.cached_listing[\s\S]{0,100}"Live status unverified"/);
  assert.match(source, /product\.cached_listing \? "Check availability"/);
  assert.match(source, /No opted-in entries in the last 30 days\./);
  assert.match(source, /No buyers have opted in yet\./);
  assert.match(source, /Delivered top-ups · rolling 30 days\. Names appear only by choice; no prize is active\./);
  assert.match(theme, /@media \(max-width: 900px\), \(max-aspect-ratio: 4\/5\)[\s\S]*dialog\.auxiliary-dialog\[data-surface="leaderboard"\]\[open\] \{[\s\S]*inset: auto 0 0;[\s\S]*width: 100vw;[\s\S]*height: fit-content !important;[\s\S]*max-height: min\(78dvh,720px\);[\s\S]*border-radius: 18px 18px 0 0;[\s\S]*\}[\s\S]*\.auxiliary-dialog\[data-surface="leaderboard"\] \.auxiliary-dialog__body \{[\s\S]*flex: 0 1 auto;[\s\S]*overflow-y: auto;/);
  assert.match(source, /dialog\.dataset\.offerState = "unavailable";/);
  assert.match(source, /Current offers are unavailable\. No order or payment has started\./);
  assert.match(theme, /#product-dialog\[data-offer-state="unavailable"\][\s\S]*\.detail-actions[\s\S]*display: none/);
  assert.match(premium, /#product-dialog\[data-offer-state="unavailable"\] \{ height: max-content; grid-template-rows: 72px max-content;/);
  assert.match(premium, /#product-dialog\[data-offer-state="unavailable"\] \.product-detail__body \{ max-height: none; \}/);
  assert.match(theme, /#catalog-status\[data-state="cached"\] \{ color:/);
  assert.match(markup, /class="catalog-pulse"/);
  assert.match(markup, /rel="manifest"/);
  assert.match(markup, /premium-theme\.css\?v=20261118-detail-views-1/);
  assert.match(markup, /id="offline-banner"/);
  assert.match(source, /serviceWorker\.register/);
  assert.match(source, /liveProductPriceSummary/);
  assert.match(styles, /\.product-card__price-row/);
  assert.match(styles, /\.product-card \{[\s\S]*grid-template-columns: 54px minmax\(0, 1fr\)[\s\S]*min-height: 88px/);
  assert.match(styles, /\.product-card__details \{[\s\S]*inset: 0[\s\S]*font-size: 0/);
  assert.match(source, /details\.setAttribute\("aria-label", `\$\{action\} for \$\{product\.name\}`\)/);
  assert.match(markup, /styles\.css\?v=20261117-compact-detail-1/);
  assert.match(markup, /id="auxiliary-header-actions"/);
  assert.match(markup, /storefront-refinements\.css\?v=20261115-preview-card-layout-1/);
  assert.match(markup, /theme\.css\?v=20261117-leaderboard-sheet-7/);
  assert.match(markup, /app\.js\?v=20261118-detail-view-1/);
  assert.match(markup, /class="dialog-state" hidden/);
  assert.match(markup, /auth\.js\?v=20261117-theme-1/);
  assert.ok(markup.indexOf("styles.css?v=20261117-compact-detail-1") < markup.indexOf("storefront-refinements.css?v=20261115-preview-card-layout-1"));
  assert.match(serviceWorker, /aurix-game-store-shell-v181/);
  assert.match(serviceWorker, /styles\.css\?v=20261117-compact-detail-1/);
  assert.match(serviceWorker, /storefront-refinements\.css\?v=20261115-preview-card-layout-1/);
  assert.match(serviceWorker, /theme\.css\?v=20261117-leaderboard-sheet-7/);
  assert.match(serviceWorker, /premium-theme\.css\?v=20261118-detail-views-1/);
  assert.match(theme, /data-surface="leaderboard"\] \.auxiliary-empty \{[\s\S]*grid-template-columns: 40px minmax\(0, 1fr\);[\s\S]*text-align: left/);
  assert.match(theme, /\.auxiliary-dialog \.store-leaderboard-join:active \{[\s\S]*transform: translateY\(1px\) scale\(\.99\)/);
  assert.match(serviceWorker, /app\.js\?v=20261118-detail-view-1/);
  assert.match(serviceWorker, /auth\.js\?v=20261117-theme-1/);
  assert.match(source, /"mobile-legends-region-checker", "leaderboard"/);
  assert.doesNotMatch(markup, /checkout-dialog|Simulate Paid Order/);
  assert.doesNotMatch(source, /Synthetic leaderboard data|Synthetic code generated|Gift code creation is simulated/);
  assert.match(serviceWorker, /url\.pathname\.startsWith\("\/api\/v1\/auth\/"\)[\s\S]*fetch\(request, \{ cache: "no-store" \}\)/);
  assert.match(source, /headerActions: \[\.\.\.auxiliaryHeaderActions\.children\]/);
  assert.match(source, /locale: auxiliaryDialog\.dataset\.guideLocale/);
  assert.match(refinements, /data-guide-page="tool:item-list"\] \.auxiliary-dialog__header-spacer/);
  assert.match(refinements, /data-guide-page="tool:emblems-talents"\] \.auxiliary-dialog__header-spacer/);
  assert.match(refinements, /grid-template-columns: 38px minmax\(0, 1fr\) auto/);
  assert.match(refinements, /grid-column: 3;[\s\S]*grid-row: 1;/);
  assert.match(refinements, /grid-template-columns: repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(refinements, /grid-template-columns: 50px minmax\(0, 1fr\)/);
  assert.match(refinements, /font-family: "Noto Sans Myanmar", "Myanmar Sangam MN"/);
  assert.match(styles, /brightness\(0\) saturate\(100%\) invert\(73%\)/);
  assert.match(refinements, /\.guide-item-recipe,\s*\.auxiliary-dialog \.guide-item-best-for \{\s*width: 100%;/);
  assert.match(refinements, /\.auxiliary-dialog \.guide-item-price \{[\s\S]*grid-column: 3;[\s\S]*justify-self: end;/);
  assert.match(refinements, /prefers-reduced-motion: reduce/);
  assert.match(styles, /\.guide-item-orb\[data-asset\] > svg, \.guide-emblem-orb\[data-asset\] > svg \{ display: none; \}/);
  assert.match(styles, /\.guide-item-recipe, \.guide-item-best-for \{ display: block;/);
  assert.match(refinements, /\.guide-rankings-freshness/);
  assert.match(refinements, /data-rankings-freshness="stale"/);
  for (const asset of ["coins", "cut-diamond", "present", "spell-book", "boots", "jungle", "footsteps"]) {
    assert.match(serviceWorker, new RegExp(`/game-store/assets/game-icons/${asset}\\.svg`));
  }
  assert.match(serviceWorker, /catalog-glyphs\.manifest\.json/);
  assert.match(markup, /Artwork &amp; trademark credits/);
  assert.match(markup, /Kenney Vleugels/);
  assert.match(markup, /CC0/);
  assert.match(markup, /Catalog media does not imply publisher endorsement or grant rights beyond the supplier's terms/);
  assert.match(markup, /CC BY 3\.0/);
  assert.match(source, /function guideHeroRole\(/);
  assert.match(source, /assets\/game-icons\/\$\{art\.icon\}\.svg/);
  assert.doesNotMatch(source, /function guideAvatarHue\(/);
  assert.match(styles, /\.guide-hero-card__stats/);
  const iconAttribution = await readFile(new URL("./web/game-store/assets/game-icons/ATTRIBUTION.txt", import.meta.url), "utf8");
  assert.match(iconAttribution, /Author: Lorc/);
  assert.match(iconAttribution, /Author: Delapouite/);
  assert.match(iconAttribution, /Author: Skoll/);
  assert.match(iconAttribution, /creativecommons\.org\/licenses\/by\/3\.0/);
  assert.match(iconAttribution, /catalog-glyphs\.manifest\.json/);
  assert.match(source, /function detailOfferIconKind\(/);
  assert.match(source, /function openGuideRoute\(/);
  assert.match(source, /function createGuideLanguageControl\(/);
  assert.match(styles, /\.detail-offer__icon\[data-icon-kind="diamond"\]/);
  assert.match(source, /\/game-store\/assets\/aurix\/mlbb-moonlight\.jpg/);
  const artworkContext = { window: {} };
  vm.runInNewContext(await readFile(new URL('./web/game-store/game-art-manifest.js', import.meta.url), 'utf8'), artworkContext);
  const pubgArt = artworkContext.window.AURIX_GAME_ARTWORK['pubg-mobile'];
  assert.ok(pubgArt?.src.startsWith('/game-store/assets/fazercards/'));
  assert.ok((await readFile(new URL(`./web${pubgArt.src}`, import.meta.url))).length > 0);
  assert.doesNotMatch(source, /upload\.wikimedia\.org|commons\.wikimedia\.org/);
  assert.match(source, /image\.loading = "lazy"/);
  assert.match(styles, /data-art-state="loaded"/);
  assert.match(markup, /id="detail-art-image"[^>]*loading="eager"/);
  assert.doesNotMatch(markup, /detail-art__(?:figure|weapon)/);
  assert.doesNotMatch(styles, /detail-art__(?:figure|weapon)/);
  assert.doesNotMatch(styles, /detail-art__image[^}]*filter:\s*invert/);
  assert.doesNotMatch(styles, /game-art--pubg[^}]*filter:\s*invert/);
  assert.match(styles, /\.detail-art\[data-slug="pubg-mobile"\]\s*\{[^}]*linear-gradient\(116deg, #694226, #342d3d 59%, #171b2a\)/);
  assert.match(styles, /footer \{ align-items: flex-start; flex-direction: column; \}[\s\S]*\.artwork-credits \{ width: 100%; min-width: 0; flex: 0 1 auto; \}/);
  assert.match(styles, /Local Kenney CC0 icons/);
  assert.match(styles, /\.reference-home > \* \{ min-width: 0; \}/);
  assert.match(markup, /class="product-detail__header"/);
  assert.match(styles, /\.product-detail[\s\S]*height: 100dvh/);
  assert.match(styles, /\.wallet-dialog \{ position: fixed; inset: 0; width: 100vw; height: 100dvh/);
  assert.match(styles, /\.wallet-steps li \+ li::before/);
  assert.match(source, /offer\.pinned === true/);
  assert.match(styles, /\.offline-banner/);
  assert.match(styles, /footer \{ padding-bottom: calc\(var\(--mobile-nav-height\) \+ env\(safe-area-inset-bottom\) \+ 65px\); \}/);
  assert.match(styles, /\.navigation-dialog \{[\s\S]*inset: 0 auto 0 0/);
  assert.match(styles, /height: 100dvh/);
  assert.match(styles, /\.auxiliary-dialog \{[\s\S]*width: 100%/);
  assert.match(styles, /\.auxiliary-dialog \.dialog-close::before/);
  assert.match(source, /card\.addEventListener\("click"/);
  assert.match(styles, /@keyframes card-enter/);
  assert.match(styles, /prefers-reduced-motion/);
  assert.match(styles, /#search::-webkit-search-cancel-button/);
  assert.match(styles, /#catalog-status\[data-state="empty"\]/);
  assert.match(styles, /\.product-card \{\s*min-height: 72px;\s*grid-template-columns: 48px minmax\(0, 1fr\);/);
  assert.doesNotMatch(markup, /home-discovery-links|home-mlbb-guide|home-region-checker/);
  assert.doesNotMatch(markup, /detail-rating|detail-review/);
  assert.doesNotMatch(source, /renderReferenceReviews|detail-review/);
  assert.match(styles, /\.detail-offer \{\s*min-height: 62px;/);
  assert.match(styles, /\.detail-offer__icon \{ width: 40px; height: 40px; \}/);
});
