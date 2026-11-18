import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const html = await readFile(new URL("./web/game-store/index.html", import.meta.url), "utf8");
const auth = await readFile(new URL("./web/game-store/auth.js", import.meta.url), "utf8");
const worker = await readFile(new URL("./web/game-store/service-worker.js", import.meta.url), "utf8");
const api = await readFile(new URL("./aurix_games/web_api.py", import.meta.url), "utf8");

test("customer sign-in offers Telegram and Google with accessible status and logout", () => {
  assert.match(html, /id="account-dialog"/);
  assert.match(html, /id="account-status"[^>]*role="status"/);
  assert.match(html, /id="account-retry"/);
  assert.match(html, /id="telegram-login-slot"/);
  assert.match(html, /id="google-login-slot"/);
  assert.match(html, /id="account-logout"/);
  assert.match(html, /auth\.js\?v=20261117-theme-1/);
  assert.match(auth, /accountRetry\?\.addEventListener\("click", \(\) => void openAccount\(\)\)/);
  assert.match(auth, /accountRetry\.hidden = false/);
  assert.match(auth, /\/api\/v1\/auth\/google\/challenge/);
  assert.match(auth, /\/api\/v1\/auth\/link\/\$\{provider\}/);
  assert.match(auth, /https:\/\/accounts\.google\.com\/gsi\/client/);
  assert.match(auth, /https:\/\/telegram\.org\/js\/telegram-widget\.js\?22/);
  assert.doesNotMatch(auth, /localStorage|sessionStorage/);
});

test("concurrent Google renders initialize one challenge and one button", async () => {
  const mode = {};
  let challengeCalls = 0;
  let initializeCalls = 0;
  let buttonCalls = 0;
  const slot = {
    firstChild: null,
    attributes: {},
    replaceChildren(...children) { this.firstChild = children[0] || null; },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] || null; },
    removeAttribute(name) { delete this.attributes[name]; },
    querySelector() { return null; },
    getBoundingClientRect() { return { width: 320 }; },
  };
  const context = vm.createContext({
    authMode: mode,
    config: { providers: { google: { enabled: true, client_id: "client-id" } } },
    account: null,
    linked: () => false,
    googleExpiresAt: 0,
    googleSlot: slot,
    loadGoogleScript: async () => {},
    postJson: async (url) => {
      assert.equal(url, "/api/v1/auth/google/challenge");
      challengeCalls += 1;
      return { nonce: "nonce-1" };
    },
    showProviderLoading(target) {
      target.setAttribute("aria-busy", "true");
      target.replaceChildren({ textContent: "Loading Google sign-in…" });
    },
    showProviderError() {},
    finishSignIn() {},
    window: { google: { accounts: { id: {
      initialize(options) {
        assert.equal(options.nonce, "nonce-1");
        initializeCalls += 1;
      },
      renderButton(target) {
        buttonCalls += 1;
        target.replaceChildren({ className: "google-button" });
      },
    } } } },
  });
  const source = auth.slice(auth.indexOf("  async function renderGoogle()"), auth.indexOf("  async function renderProviders()"));
  vm.runInContext(source, context);

  await Promise.all([context.renderGoogle(), context.renderGoogle()]);
  await context.renderGoogle();

  assert.equal(challengeCalls, 1);
  assert.equal(initializeCalls, 1);
  assert.equal(buttonCalls, 1);
});

test("private history uses authenticated APIs and safe text nodes", () => {
  assert.match(auth, /\/api\/v1\/orders/);
  assert.match(auth, /\/api\/v1\/top-up-history/);
  assert.match(auth, /heading\.textContent =/);
  assert.match(auth, /detail\.textContent =/);
  assert.match(auth, /showOrderDetails\(entry\.order_id/);
  assert.match(auth, /checkout\/payment/);
  assert.match(auth, /order_id: orderId, rail/);
  assert.match(auth, /payment-qr/);
  assert.doesNotMatch(auth, /innerHTML/);
});

test("Telegram uses a CSP-safe SDK callback and ignores cancelled or stale sign-ins", async () => {
  assert.doesNotMatch(auth, /dataset\.onauth|new Function|eval\(/);
  const mode = {};
  let click;
  let sdkCallback;
  let scriptCount = 0;
  const signedIn = [];
  const slot = {
    firstChild: null,
    replaceChildren(...values) { this.firstChild = values[0] || null; },
    setAttribute() {},
    removeAttribute() {},
  };
  const context = vm.createContext({
    authMode: mode, signingIn: false, dialog: { open: true },
    config: { providers: { telegram: { enabled: true, bot_id: "123456" } } },
    linked: () => false, telegramSlot: slot, telegramScriptPromise: null,
    showProviderLoading(target, label) { target.replaceChildren({ textContent: `Loading ${label} sign-in…` }); },
    showProviderError(target, label) { target.replaceChildren({ textContent: `${label} sign-in is unavailable.` }); },
    status: {},
    window: { Telegram: { Login: { auth(options, callback) {
      assert.equal(options.bot_id, "123456"); sdkCallback = callback;
    } } } },
    document: {
      createElement: () => ({ addEventListener(event, callback) { if (event === "click") click = callback; } }),
      head: { append(script) { scriptCount += 1; script.onload(); } },
    },
    finishSignIn(provider, proof, callbackMode) {
      if (callbackMode === context.authMode) signedIn.push({ provider, proof });
    },
  });
  vm.runInContext(auth.slice(auth.indexOf("  async function renderTelegram()"), auth.indexOf("  async function renderGoogle()")), context);
  await context.renderTelegram();
  assert.equal(slot.firstChild.textContent, "Continue with Telegram");
  click(); sdkCallback(false);
  assert.equal(signedIn.length, 0);
  const proof = { id: 42, hash: "signed-by-telegram" };
  sdkCallback(proof);
  assert.equal(signedIn[0].proof, proof);
  context.authMode = {};
  sdkCallback(proof);
  assert.equal(signedIn.length, 1);
  slot.replaceChildren();
  await context.renderTelegram();
  assert.equal(scriptCount, 1);
  sdkCallback = null;
  context.dialog.open = false;
  click();
  assert.equal(sdkCallback, null);
});

test("service worker does not persist auth, wallet, or customer history responses", () => {
  assert.match(worker, /\/api\/v1\/auth\//);
  assert.match(worker, /\/api\/v1\/orders/);
  assert.match(worker, /\/api\/v1\/wallet/);
  assert.match(worker, /\/api\/v1\/top-up-history/);
  assert.match(worker, /fetch\(request, \{ cache: "no-store" \}\)/);
  assert.match(api, /Set-Cookie/);
  assert.match(api, /same-origin authentication request required/);
});
