/** Browser regressions against a loopback preview. Auth/payment data below is synthetic. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.AURIX_PLAYWRIGHT_MODULE || 'playwright');
const origin = new URL(process.env.AURIX_GAME_UI_URL || 'http://127.0.0.1:64100');
assert(['127.0.0.1', 'localhost'].includes(origin.hostname), 'UI checks require an isolated loopback preview');
const output = path.resolve(process.env.AURIX_GAME_UI_OUTPUT || '.local/ui-review-20261110');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const results = [];
async function geometry(page, surface) {
  const sizes = await page.evaluate(() => ({ viewport: innerWidth, page: document.documentElement.scrollWidth }));
  assert(sizes.page <= sizes.viewport, `${surface} overflows: ${JSON.stringify(sizes)}`);
  results.push({ surface, ...sizes });
}
async function screenshot(page, options) {
  await page.evaluate(() => Promise.all(
    [...document.querySelectorAll('dialog[open]')].flatMap((el) => el.getAnimations())
      .map((animation) => animation.finished.catch(() => {}))
  ));
  await page.screenshot(options);
}
async function open(page) {
  await page.goto(new URL('/game-store/', origin).href);
  await page.locator('.product-card').first().waitFor();
  await page.waitForFunction(() => !document.querySelector('#account-status').textContent.includes('Checking'));
}
try {
  for (const [width, height] of [[320, 568], [390, 844], [430, 932], [768, 1024], [1440, 1000]]) {
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: 'no-preference', serviceWorkers: 'block' });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await open(page);
    await geometry(page, `home-${width}`);
    const firstGame = await page.locator('.product-card').first().boundingBox();
    if (width <= 430) assert(firstGame.y < height - 76, 'A game entry must be visible above mobile navigation');
    const card = page.locator('.product-card').first();
    await card.hover();
    await page.waitForTimeout(200);
    const hoveredGame = await card.boundingBox();
    assert(Math.abs(firstGame.y - hoveredGame.y) < 0.1, 'Hover must not move game cards');
    results.push({ surface: `discovery-${width}`, firstGameTop: firstGame.y, normalMotionStable: true });
    await page.locator('#saved-games').click();
    await page.locator('#catalog-status').filter({ hasText: 'No saved games' }).waitFor();
    assert.equal(await page.locator('.product-card').count(), 0);
    await screenshot(page, { path: path.join(output, `empty-${width}.png`) });
    await page.locator('#catalog-reset').click();
    await page.locator('.product-card').first().waitFor();
    await screenshot(page, { path: path.join(output, `home-${width}.png`) });
    assert.equal(await page.locator('#wallet-balance').textContent(), '—');
    assert(await page.locator('#wallet-balance-toggle').isDisabled());
    await page.locator('#account-open').click();
    await page.locator('#account-status').filter({ hasText: 'not configured' }).waitFor();
    await geometry(page, `account-${width}`);
    await screenshot(page, { path: path.join(output, `account-${width}.png`) });
    await page.locator('#account-close').click();
    await page.getByRole('button', { name: 'View packages for Mobile Legends', exact: true }).click();
    await page.locator('#detail-offers button').first().waitFor();
    await page.waitForFunction(() => {
      const image = document.querySelector('#detail-art-image');
      return image.complete && image.naturalWidth > 0;
    });
    assert(await page.locator('#detail-reference-note').isVisible());
    const hierarchy = await page.evaluate(() => {
      const body = document.querySelector('.product-detail__body');
      const children = [...body.children];
      const identity = children.indexOf(document.querySelector('.detail-product-heading'));
      const region = children.indexOf(document.querySelector('#detail-tabs'));
      const note = children.indexOf(document.querySelector('#detail-reference-note'));
      const offers = children.indexOf(document.querySelector('#detail-reference'));
      return identity < region && region < note && note < offers;
    });
    assert(hierarchy, 'Game identity must precede region, reference evidence and package decisions in reading order');
    const choice = page.locator('#detail-offers button').first();
    await choice.focus();
    const beforeChoice = await choice.boundingBox();
    const originalChoice = await choice.elementHandle();
    await page.keyboard.press('Enter');
    assert(await originalChoice.evaluate((el) => el.isConnected && el === document.activeElement), 'Selection must retain the original focused button');
    assert.equal(await choice.getAttribute('aria-pressed'), 'true');
    const afterChoice = await choice.boundingBox();
    assert(Math.abs(beforeChoice.width - afterChoice.width) < 0.1 && Math.abs(beforeChoice.height - afterChoice.height) < 0.1, 'Selection must preserve tile dimensions');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.keyboard.press('Enter');
    assert(await originalChoice.evaluate((el) => el === document.activeElement), 'Reduced motion must preserve selection focus');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    assert(await page.locator('#detail-buy').isDisabled());
    assert.notEqual(await page.locator('#detail-selection').textContent(), 'Select a package');
    assert(await page.locator('#detail-selection').isVisible(), 'Selected package summary must remain visible');
    await geometry(page, `detail-${width}`);
    await screenshot(page, { path: path.join(output, `detail-${width}.png`) });
    await page.locator('.product-detail__body').evaluate((element) => { element.scrollTop = element.scrollHeight; });
    const rail = await page.locator('.detail-actions').boundingBox();
    assert(rail.y + rail.height <= height + 1, 'Purchase rail must stay in the viewport');
    await page.locator('#detail-clear').click();
    assert.equal(await page.locator('#detail-selection').textContent(), 'Select a package');
    await page.locator('#dialog-close').click();
    await page.getByRole('button', { name: 'View packages for Mobile Legends', exact: true }).click();
    await page.locator('#detail-offers button').first().waitFor();
    await page.waitForFunction(() => document.querySelector('.product-detail__body').scrollTop === 0);
    await page.locator('#dialog-close').click();
    const pin = page.getByRole('button', { name: 'Pin Mobile Legends', exact: true });
    await pin.click();
    await page.reload();
    await page.getByRole('button', { name: 'Unpin Mobile Legends', exact: true }).waitFor();
    await page.locator('#saved-games').click();
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 1);
    assert((await page.locator('.product-card h3').textContent()).includes('Mobile Legends'));
    await page.locator('#saved-games').click();
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 65);
    await page.locator('#home-wallet-topup').click();
    await page.locator('#wallet-custom-amount').fill('2,00');
    assert.equal(await page.locator('#wallet-custom-amount').getAttribute('aria-invalid'), 'true');
    assert(await page.locator('#wallet-next').isDisabled());
    await page.locator('#wallet-amounts button[data-value="10,000 Ks"]').click();
    await page.locator('#wallet-methods button[data-value="Wave Money"]').click();
    await page.locator('#wallet-next').click();
    assert(await page.locator('#wallet-pick-image').isDisabled());
    assert(await page.locator('#wallet-next').isDisabled());
    await geometry(page, `wallet-${width}`);
    await screenshot(page, { path: path.join(output, `wallet-${width}.png`) });
    await page.locator('#wallet-close').click();
    assert.equal(await page.locator('#wallet-custom-amount').inputValue(), '10,000');
    await page.locator('#wallet-close').click();
    if (width <= 760) await page.locator('#search-toggle').click();
    await page.locator('#search').fill('PUBG');
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 1);
    await geometry(page, `search-${width}`);
    await page.locator('#search-clear').click();
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 65);
    await page.locator('#search').fill('no-such-game-fixture');
    await page.locator('#catalog-reset').waitFor({ state: 'visible' });
    assert.equal(await page.locator('.product-card').count(), 0);
    await page.locator('#catalog-reset').click();
    await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 65);
    if (width === 390) {
      let failCatalog = true;
      const catalogRoute = /\/api\/v1\/catalog\/products\?/;
      await page.route(catalogRoute, (route) => {
        if (failCatalog && new URL(route.request().url()).searchParams.get('q') === 'ui-error-fixture') {
          return route.fulfill({ status: 503, json: { error: 'Synthetic catalog outage' } });
        }
        return route.continue();
      });
      await page.locator('#search').fill('ui-error-fixture');
      await page.locator('#catalog-retry').waitFor({ state: 'visible' });
      assert.equal(await page.locator('.product-card').count(), 0);
      assert.equal(await page.locator('#product-grid').getAttribute('aria-busy'), 'false');
      failCatalog = false;
      await page.locator('#catalog-retry').click();
      await page.locator('#catalog-reset').waitFor({ state: 'visible' });
      await page.locator('#catalog-reset').click();
      await page.waitForFunction(() => document.querySelectorAll('.product-card').length === 65);
      await page.unroute(catalogRoute);
    }
    await page.locator('#home-events-news').click();
    await page.locator('.guide-wiki-row').first().waitFor();
    await page.locator('.guide-wiki-row').filter({ hasText: 'Item List' }).click();
    await page.locator('.guide-item-card').first().waitFor();
    await geometry(page, `guide-${width}`);
    assert.equal(await page.locator('.auxiliary-dialog__body').evaluate((element) => element.scrollTop), 0);
    await page.locator('.auxiliary-dialog__body').evaluate((element) => { element.scrollTop = element.scrollHeight; });
    const guideHeader = await page.locator('.auxiliary-dialog__header').boundingBox();
    assert(guideHeader.y >= 0 && guideHeader.y + guideHeader.height <= height, 'Guide back/header must stay reachable after scrolling');
    await page.locator('.auxiliary-dialog__body').evaluate((element) => { element.scrollTop = 0; });
    await screenshot(page, { path: path.join(output, `guide-${width}.png`) });
    await page.locator('#auxiliary-close').click();
    await page.locator('#auxiliary-close').click();
    await page.locator('#home-support-chat').click();
    assert.equal(await page.locator('.support-chat-box input').count(), 0);
    assert((await page.locator('#auxiliary-status').textContent()).includes('unavailable'));
    await page.locator('#auxiliary-close').click();
    assert.deepEqual(errors, []);
    await context.close();
  }

  // UI fixture only: exercise signed-in controls without live OAuth or money.
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block', reducedMotion: 'reduce' });
  const page = await context.newPage();
  let signedIn = true;
  let delayOrders = false;
  let failOrders = false;
  let releaseOrders;
  const calls = [];
  const orders = [{ order_id: 'fixture-order', product_slug: 'mobile-legends', state: 'delivered', amount_minor: 12345, currency: 'USD' }];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname;
    calls.push(name);
    const json = (value, status = 200) => route.fulfill({ status, json: value });
    if (name === '/api/v1/auth/config') return json({ providers: { telegram: { enabled: false }, google: { enabled: false } } });
    if (name === '/api/v1/auth/session') return json({ authenticated: signedIn, user: signedIn ? { customer_id: 'fixture-customer', providers: ['telegram', 'google'] } : null });
    if (name === '/api/v1/auth/logout') { signedIn = false; return json({ authenticated: false }); }
    if (name === '/api/v1/auth/telegram') { signedIn = true; return json({ authenticated: true }); }
    if (name === '/api/v1/wallet') return json({ wallet: { available_minor: 12345, reserved_minor: 500, currency: 'USD', status: 'active' } });
    if (name === '/api/v1/orders') {
      if (failOrders) return json({ error: 'Synthetic history outage' }, 503);
      if (delayOrders) await new Promise((resolve) => { releaseOrders = resolve; });
      return json({ orders });
    }
    if (name === '/api/v1/top-up-history') {
      const offset = Number(url.searchParams.get('offset') || 0);
      return json({ entries: Array.from({ length: offset ? 1 : 50 }, (_, index) => ({ entry_id: offset + index, amount_minor: 250, currency: 'USD', occurred_at: '2026-11-10T00:00:00+00:00' })), limit: 50, offset });
    }
    return route.continue();
  });
  await open(page);
  await page.waitForFunction(() => document.querySelector('#wallet-balance-toggle').disabled === false);
  await page.locator('#wallet-balance-toggle').click();
  assert.equal(await page.locator('#wallet-balance').textContent(), '123.45 USD');
  assert((await page.locator('#wallet-account-note').textContent()).includes('5.00 USD held'));
  await page.locator('#home-order-history').click();
  await page.locator('.customer-history-row').filter({ hasText: 'fixture-order' }).waitFor();
  assert((await page.locator('.customer-history-row').textContent()).includes('123.45 USD'));
  await page.getByRole('button', { name: 'Wallet top-ups', exact: true }).click();
  await page.locator('.history-more').waitFor();
  assert.equal(await page.locator('.customer-history-row').count(), 50);
  assert((await page.locator('.customer-history-row').first().textContent()).includes('2.50 USD'));
  await page.locator('.history-more').click();
  await page.waitForFunction(() => document.querySelectorAll('.customer-history-row').length === 51);
  await geometry(page, 'signed-in-history');
  await page.locator('.auxiliary-dialog__body').evaluate((element) => { element.scrollTop = 0; });
  await screenshot(page, { path: path.join(output, 'history-390.png') });
  await page.locator('#auxiliary-close').click();
  await page.locator('#nav-open').click();
  await page.locator('.navigation-item').filter({ hasText: /^Order History$/ }).click();
  await page.locator('.customer-history-row').filter({ hasText: 'fixture-order' }).waitFor();
  assert.equal(await page.locator('#auxiliary-title').textContent(), 'Your activity');
  await page.locator('#auxiliary-close').click();
  await page.locator('#bottom-profile').click();
  await page.locator('#account-logout').click();
  await page.waitForFunction(() => document.querySelector('#wallet-balance').textContent === '—');
  await page.locator('#account-close').click();
  // A blocked history intent resumes after successful sign-in.
  await page.locator('#home-order-history').click();
  await page.locator('#account-dialog').waitFor({ state: 'visible' });
  await page.evaluate(() => window.onTelegramAuth({}));
  await page.locator('.customer-history-row').filter({ hasText: 'fixture-order' }).waitFor();
  await page.locator('#auxiliary-close').click();
  // A failed read offers recovery instead of inventing an empty history.
  failOrders = true;
  await page.locator('#home-order-history').click();
  await page.locator('.history-retry').waitFor();
  assert.equal(await page.locator('.customer-history-row').count(), 0);
  failOrders = false;
  await page.locator('.history-retry').click();
  await page.locator('.customer-history-row').filter({ hasText: 'fixture-order' }).waitFor();
  await page.locator('#auxiliary-close').click();
  // Closing a loading view and signing out must discard late private responses.
  delayOrders = true;
  await page.locator('#home-order-history').click();
  await page.locator('#auxiliary-status').filter({ hasText: 'Loading' }).waitFor();
  await page.keyboard.press('Escape');
  await page.locator('#bottom-profile').click();
  await page.locator('#account-logout').click();
  await page.waitForFunction(() => document.querySelector('#wallet-balance').textContent === '—');
  assert(releaseOrders);
  const lateResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/orders');
  releaseOrders();
  await (await lateResponse).finished();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.locator('.customer-history-row').count(), 0);
  assert.equal(await page.locator('#wallet-balance').textContent(), '—');
  assert(calls.includes('/api/v1/wallet'));
  results.push({ surface: 'synthetic-account-workflows', passed: true, cases: ['wallet units', 'reserved funds', 'history tabs', 'pagination', 'menu routing', 'profile/logout', 'resume after login', 'failed history retry', 'discard late private response'] });
  await context.close();
  await writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
} finally { await browser.close(); }
