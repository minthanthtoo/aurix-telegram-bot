import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.AURIX_PLAYWRIGHT_MODULE || 'playwright');
const html = (await readFile('web/game-store/index.html', 'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
const auth = await readFile('web/game-store/auth.js', 'utf8');
const app = await readFile('web/game-store/app.js', 'utf8');

test('customer order refresh preserves evidence, follows delivery and distinguishes refunds', async (t) => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  t.after(() => browser.close());

  async function fixture(styled = false) {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(5_000);
    t.after(() => page.close());
    let order = {
      order_id: 'order-one', product_slug: 'mobile-legends', state: 'awaiting_payment',
      amount_minor: 5000, currency: 'MMK',
      payment: { rail: 'kpay', account_name: 'Fixture Merchant', account: 'fixture-account', qr_asset: 'fixture.png', expired: false, expires_at: new Date(Date.now() + 600_000).toISOString() },
      payment_review: { order_id: 'order-one', receipt_max_bytes: 5 * 1024 * 1024, review_status: 'in_review', revision: 1, can_correct: true, receipt_upload_enabled: true },
    };
    let errorStatus = 0;
    let reads = 0;
    let authenticated = true;
    let pendingRead = null;
    let upload;
    let uploadMode = '';
    let uploadCount = 0;
    let reviewUnavailable = false;
    let historyReads = 0;
    await page.route('**/*', async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = {};
      let status = 200;
      if (path === '/game-store/') return route.fulfill({ contentType: 'text/html', body: html });
      if (styled && path.startsWith('/game-store/') && path.endsWith('.css')) {
        return route.fulfill({ contentType: 'text/css', body: await readFile(`web${path}`) });
      }
      if (path === '/api/v1/auth/config') body = { providers: {} };
      else if (path === '/api/v1/auth/session') body = { authenticated, user: authenticated ? { customer_id: 'one' } : null };
      else if (path === '/api/v1/orders') { historyReads++; body = { orders: [order] }; }
      else if (path === '/api/v1/checkout/payment') body = { payment: {
        ...order.payment, order_id: order.order_id, amount_minor: order.amount_minor,
        currency: order.currency, order_state: order.state,
      } };
      else if (path === '/api/v1/orders/order-one') {
        reads++;
        body = structuredClone(order);
        if (pendingRead) { const wait = pendingRead; pendingRead = null; await wait; }
        status = errorStatus || 200;
      } else if (path === '/api/v1/orders/order-one/payment-evidence') {
        if (route.request().method() === 'POST') {
          uploadCount++;
          upload = route.request().postDataJSON();
          if (uploadMode === 'unauthorized') { authenticated = false; status = 401; }
          else if (uploadMode === 'unavailable') status = 503;
          else {
            order.payment_review = { ...order.payment_review, revision: 2, can_correct: false };
            if (uploadMode === 'lost') status = 503;
          }
        } else if (reviewUnavailable) status = 503;
        body = order.payment_review;
      } else if (path === '/api/v1/wallet') body = { wallet: { available_minor: 0, reserved_minor: 0, currency: 'MMK' } };
      else return route.fulfill({ status: 404, body: '' });
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto('http://127.0.0.1:64101/game-store/');
    await page.clock.install();
    await page.addScriptTag({ content: auth });
    await page.waitForFunction(() => document.querySelector('#account-status').textContent === 'Your private account is ready.');
    await page.evaluate(() => window.AurixCustomer.openOrder('order-one'));
    return { page, get order() { return order; }, set order(value) { order = value; },
      get reads() { return reads; }, get upload() { return upload; },
      get uploadCount() { return uploadCount; }, get historyReads() { return historyReads; },
      setUploadMode(mode, unavailable = false) { uploadMode = mode; reviewUnavailable = unavailable; },
      setError(status) { errorStatus = status; if (status === 401) authenticated = false; },
      delayRead(promise) { pendingRead = promise; } };
  }

  await t.test('expiry refresh keeps reference, selected file and focus, then submits the expected revision', async () => {
    const f = await fixture();
    await f.page.locator('.payment-evidence-form input[type=text]').fill('my-transfer');
    await f.page.locator('.payment-evidence-form input[type=file]').setInputFiles({ name: 'receipt.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-receipt') });
    await f.page.locator('.payment-evidence-form input[type=text]').focus();
    f.order.payment.expired = true;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('These saved instructions have expired. Do not transfer using them.', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.payment-evidence-form input[type=text]').inputValue(), 'my-transfer');
    assert.equal(await f.page.locator('.payment-evidence-form input[type=file]').evaluate((el) => el.files[0].name), 'receipt.pdf');
    assert.equal(await f.page.locator('.payment-evidence-form input[type=text]').evaluate((el) => document.activeElement === el), true);
    await f.page.getByRole('button', { name: 'Send corrected receipt' }).click();
    await f.page.waitForFunction(() => !document.querySelector('.payment-evidence-form'));
    assert.equal(f.upload.expected_revision, 1);
    assert.equal(f.upload.transaction_reference, 'my-transfer');
  });

  await t.test('processing refresh continues until delivery and stops afterwards', async () => {
    const f = await fixture();
    f.order.state = 'processing';
    f.order.payment_review = { ...f.order.payment_review, review_status: 'verified', can_correct: false };
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('Status: Delivering', { exact: true }).waitFor();
    f.order.state = 'delivered';
    await f.page.clock.runFor(15_010);
    await f.page.getByText('Status: Delivered', { exact: true }).waitFor();
    const reads = f.reads;
    await f.page.clock.runFor(60_000);
    assert.equal(f.reads, reads);
  });

  await t.test('bank payout status overrides a delayed order handoff and wallet wording', async () => {
    const f = await fixture();
    f.order.state = 'review_refund';
    f.order.payment_review.review_status = 'verified';
    f.order.refund = { route: 'bank', status: 'pending', amount_minor: 5000, currency: 'MMK' };
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('Status: Bank refund pending', { exact: true }).waitFor();
    f.order.refund.status = 'completed';
    await f.page.clock.runFor(15_010);
    await f.page.getByText('Status: Refunded to original sender', { exact: true }).waitFor();
    assert.equal(await f.page.getByText('Returned to wallet', { exact: true }).count(), 0);
    const reads = f.reads;
    await f.page.clock.runFor(60_000);
    assert.equal(f.reads, reads);
  });

  await t.test('session expiry clears private details and stops polling', async () => {
    const f = await fixture();
    f.setError(401);
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.locator('#account-dialog[open]').waitFor();
    assert.equal(await f.page.locator('.customer-order-detail').count(), 0);
    const reads = f.reads;
    await f.page.clock.runFor(60_000);
    assert.equal(f.reads, reads);
  });

  await t.test('missing order stops refreshing and removes its private panel', async () => {
    const f = await fixture();
    f.setError(404);
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('This order is unavailable in your account. Refresh History or contact support.', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.customer-order-detail').count(), 0);
    const reads = f.reads;
    await f.page.clock.runFor(60_000);
    assert.equal(f.reads, reads);
  });

  await t.test('wallet reimbursement has its own final wording', async () => {
    const f = await fixture();
    f.order.refund = { route: 'wallet', status: 'completed', amount_minor: 5000, currency: 'MMK' };
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('Status: Returned to wallet', { exact: true }).waitFor();
    assert.equal(await f.page.getByText('Status: Refunded to original sender', { exact: true }).count(), 0);
  });

  await t.test('checkout payment action opens the same saved order receipt view', { timeout: 10_000 }, async () => {
    const f = await fixture();
    await f.page.evaluate(() => document.querySelector('#auxiliary-dialog').close());
    await f.page.addScriptTag({ content: app });
    await f.page.evaluate(() => {
      checkoutOrder = { orderId: 'order-one', amountMinor: 5000, currency: 'MMK' };
      checkoutPaymentsEnabled = true;
      checkoutPaymentRails = ['kpay'];
      referenceDetail.hidden = false;
      recipientSection.hidden = false;
      renderCheckoutPaymentOptions();
      dialog.showModal();
    });
    await f.page.getByRole('button', { name: 'KPay', exact: true }).click();
    await f.page.locator('#auxiliary-dialog[open] .payment-evidence-form').waitFor();
    assert.equal(await f.page.locator('.customer-order-detail').getAttribute('data-order-id'), 'order-one');
    assert.equal(await f.page.locator('#product-dialog').evaluate((el) => el.open), false);
    assert.equal(f.historyReads, 0);
    await f.page.getByRole('button', { name: 'All orders', exact: true }).click();
    await f.page.locator('.customer-history-row').waitFor();
    assert.equal(f.historyReads, 1);
  });

  await t.test('local expiry removes transfer controls during failed status reads without losing the draft', async () => {
    const f = await fixture();
    f.order.payment_review = { ...f.order.payment_review, revision: null, review_status: 'not_submitted' };
    f.order.payment.expires_at = new Date(Date.now() + 3000).toISOString();
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByRole('button', { name: 'Copy account', exact: true }).waitFor();
    await f.page.locator('.payment-evidence-form input[type=text]').fill('keep-reference');
    f.setError(503);
    await f.page.clock.runFor(4000);
    await f.page.getByText('These saved instructions have expired. Do not transfer using them.', { exact: true }).waitFor();
    assert.equal(await f.page.locator('.customer-payment-instructions').count(), 0);
    assert.equal(await f.page.locator('.payment-evidence-form input[type=text]').inputValue(), 'keep-reference');
  });

  async function chooseReceipt(f, size = 32) {
    await f.page.locator('.payment-evidence-form input[type=text]').fill('phone-transfer');
    const buffer = Buffer.alloc(size, 120);
    buffer.write('%PDF-');
    await f.page.locator('.payment-evidence-form input[type=file]').setInputFiles({ name: 'receipt.pdf', mimeType: 'application/pdf', buffer });
    return buffer;
  }

  await t.test('phone receipt bytes survive chunked base64 encoding and lost response recovery', async () => {
    const f = await fixture();
    const original = await chooseReceipt(f, 1024 * 1024 + 7);
    f.setUploadMode('lost');
    await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).click();
    await f.page.waitForFunction(() => !document.querySelector('.payment-evidence-form'));
    assert.deepEqual(Buffer.from(f.upload.receipt_base64, 'base64'), original);
    assert.equal(f.uploadCount, 1);
  });

  await t.test('unknown upload plus failed recovery read fences another submission until reloaded', async () => {
    const f = await fixture();
    await chooseReceipt(f);
    f.setUploadMode('unavailable', true);
    await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).click();
    await f.page.getByRole('button', { name: 'Reload saved order', exact: true }).waitFor();
    assert.equal(await f.page.getByRole('button', { name: 'Checking saved submission…', exact: true }).isEnabled(), false);
    assert.equal(f.uploadCount, 1);
    f.setUploadMode('');
    f.setError(503);
    await f.page.getByRole('button', { name: 'Reload saved order', exact: true }).click();
    await f.page.locator('.customer-order-detail').getByText('Order details could not be loaded. Please try again.', { exact: true }).waitFor();
    f.setError(0);
    await f.page.getByRole('button', { name: 'Reload saved order', exact: true }).click();
    await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).waitFor();
    assert.equal(await f.page.locator('.payment-evidence-form input[type=text]').inputValue(), 'phone-transfer');
    assert.equal(await f.page.locator('.payment-evidence-form input[type=file]').evaluate((el) => el.files[0].name), 'receipt.pdf');
    assert.equal(await f.page.locator('.payment-evidence-form input[type=file]').isEnabled(), true);
    assert.equal(f.uploadCount, 1);
  });

  await t.test('upload session expiry clears the receipt and private order immediately', async () => {
    const f = await fixture();
    await chooseReceipt(f);
    f.setUploadMode('unauthorized');
    await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).click();
    await f.page.locator('#account-dialog[open]').waitFor();
    assert.equal(await f.page.locator('.customer-order-detail').count(), 0);
  });

  await t.test('styled order view fits phone and desktop and keeps receipt entry reachable', async () => {
    const f = await fixture(true);
    await f.page.clock.resume();
    f.order.payment.expired = true;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.getByText('These saved instructions have expired. Do not transfer using them.', { exact: true }).waitFor();
    for (const width of [390, 1440]) {
      await f.page.setViewportSize({ width, height: 844 });
      await f.page.locator('#auxiliary-dialog').evaluate(async (el) => {
        await Promise.all(el.getAnimations().map((animation) => animation.finished.catch(() => {})));
      });
      const geometry = await f.page.locator('#auxiliary-dialog').evaluate((el) => ({
        left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right,
        scroll: el.scrollWidth, client: el.clientWidth,
      }));
      await f.page.screenshot({ path: `/tmp/aurix-order-${width}.png` });
      assert.ok(geometry.left >= 0 && geometry.right <= width + 1, JSON.stringify({ width, ...geometry }));
      assert.ok(geometry.scroll <= geometry.client + 1);
      await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).scrollIntoViewIfNeeded();
      assert.equal(await f.page.getByRole('button', { name: 'Send corrected receipt', exact: true }).isVisible(), true);
      await f.page.screenshot({ path: `/tmp/aurix-order-${width}.png` });
    }
  });

  await t.test('close while a status read is in flight discards the response', async () => {
    const f = await fixture();
    let resolve;
    f.delayRead(new Promise((done) => { resolve = done; }));
    const before = f.reads;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.page.waitForTimeout(30);
    assert.equal(f.reads, before + 1);
    await f.page.evaluate(() => {
      window.dispatchEvent(new Event('focus'));
      window.dispatchEvent(new Event('online'));
    });
    assert.equal(f.reads, before + 1);
    await f.page.evaluate(() => document.querySelector('#auxiliary-dialog').close());
    resolve();
    await f.page.clock.runFor(60_000);
    assert.equal(await f.page.locator('#auxiliary-dialog').evaluate((el) => el.open), false);
    assert.equal(f.reads, before + 1);
  });
});
