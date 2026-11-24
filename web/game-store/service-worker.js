"use strict";

const VERSION = "aurix-game-store-shell-v184";
const STATIC_ASSETS = [
  "/game-store/assets/aurix/nav-grid.svg",
  "/game-store/assets/aurix/nav-bookmark.svg",
  "/game-store/assets/aurix/nav-tag.svg",
  "/game-store/assets/aurix/nav-desktop.svg",
  "/game-store/assets/aurix/nav-phone.svg",

  "/game-store/assets/aurix/mlbb-moonlight.jpg",
  "/game-store/assets/aurix/promo-gift-controller.webp",
  "/game-store/game-art-manifest.js?v=20261114-fazercards-art-2",
  "/game-store/assets/aurix/nav-help.svg",
  "/game-store/assets/aurix/nav-event.svg",
  "/game-store/interactions.js?v=20261113-pager-1",
  "/game-store/assets/aurix/nav-game.svg",
  "/game-store/assets/aurix/nav-history.svg",
  "/game-store/assets/aurix/nav-wallet.svg",
  "/game-store/assets/aurix/nav-profile.svg",
  "/game-store/assets/aurix/nav-gift.svg",
  "/game-store/assets/aurix/nav-region.svg",
  "/game-store/assets/providers/google-g.png",
  "/game-store/assets/providers/telegram-logo.png",

  "/game-store/",
  "/game-store/index.html",
  "/game-store/privacy.html",
  "/game-store/terms.html",
  "/game-store/styles.css?v=20261117-compact-detail-1",
  "/game-store/storefront-refinements.css?v=20261115-preview-card-layout-1",
  "/game-store/app.js?v=20261125-supplier-status-clarity-1",
  "/game-store/auth.js?v=20261117-theme-1",
  "/game-store/manifest.webmanifest",
  "/game-store/icon.svg",
  "/game-store/assets/aurix/arena.svg",
  "/game-store/assets/aurix/arcade.svg",
  "/game-store/assets/aurix/item-diamond.svg",
  "/game-store/assets/aurix/item-coins.svg",
  "/game-store/assets/aurix/item-gift.svg",
  "/game-store/assets/aurix/item-spell.svg",
  "/game-store/assets/aurix/battleground.svg",
  "/game-store/theme.css?v=20261117-leaderboard-sheet-7",
  "/game-store/premium-theme.css?v=20261118-detail-views-1",
  "/game-store/assets/kenney/barsVertical.png",
  "/game-store/assets/kenney/gamepad.png",
  "/game-store/assets/kenney/joystick.png",
  "/game-store/assets/kenney/leaderboardsComplex.png",
  "/game-store/assets/kenney/multiplayer.png",
  "/game-store/assets/kenney/target.png",
  "/game-store/assets/kenney/trophy.png",
  "/game-store/assets/game-icons/assassin-pocket.svg",
  "/game-store/assets/game-icons/bow-arrow.svg",
  "/game-store/assets/game-icons/coins.svg",
  "/game-store/assets/game-icons/cut-diamond.svg",
  "/game-store/assets/game-icons/boots.svg",
  "/game-store/assets/game-icons/crossed-swords.svg",
  "/game-store/assets/game-icons/footsteps.svg",
  "/game-store/assets/game-icons/healing.svg",
  "/game-store/assets/game-icons/jungle.svg",
  "/game-store/assets/game-icons/present.svg",
  "/game-store/assets/game-icons/spell-book.svg",
  "/game-store/assets/game-icons/shield-reflect.svg",
  "/game-store/assets/game-icons/wizard-staff.svg",
  "/game-store/assets/game-icons/ATTRIBUTION.txt",
  "/game-store/assets/game-icons/catalog-glyphs.manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/v1/admin/")
      || url.pathname.startsWith("/api/v1/store-staff/") || url.pathname.startsWith("/game-store/staff/")
      || url.pathname.startsWith("/game-store/admin/")) return;

  if (url.pathname.startsWith("/api/")) {
    if (
      url.pathname.startsWith("/api/v1/auth/")
      || url.pathname === "/api/v1/orders"
      || url.pathname.startsWith("/api/v1/orders/")
      || url.pathname === "/api/v1/wallet"
      || url.pathname === "/api/v1/top-up-history"
      || url.pathname === "/api/v1/store/notices"
      || url.pathname === "/api/v1/store/leaderboard"
      || url.pathname === "/api/v1/leaderboard/profile"
      || url.pathname === "/api/v1/live-catalog"
      || url.pathname === "/api/v1/catalog"
      || url.pathname.startsWith("/api/v1/catalog/")
      || url.pathname === "/api/v1/checkout/config"
      || url.pathname === "/api/v1/games/mlbb/zones"
    ) {
      event.respondWith(fetch(request, { cache: "no-store" }));
      return;
    }
    // Cache informational APIs; catalog reads above must honor current stock and controls.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(VERSION).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request)),
    );
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && (request.mode === "navigate" || url.pathname.startsWith("/game-store/assets/fazercards/topups/") || STATIC_ASSETS.some((asset) => asset.split("?")[0] === url.pathname))) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || caches.match("/game-store/"))),
  );
});
