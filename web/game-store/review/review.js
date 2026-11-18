"use strict";
(() => {
  const $ = (id) => document.querySelector(`#${id}`);
  const staff = $("staff-workspace");
  const root = staff ? "/api/v1/store-staff" : "/api/v1/admin";
  const reviews = `${root}/payment-review`;
  let role = null;
  let token = "", generation = 0;
  const element = (tag, text = "", cls = "") => {
    const el = document.createElement(tag); el.textContent = text; el.className = cls; return el;
  };
  function selectStaffTab(name, focus = false) {
    const tabs = [...document.querySelectorAll("#staff-dashboard [data-staff-tab]")];
    const selected = tabs.find((tab) => tab.dataset.staffTab === name && !tab.hidden);
    if (!selected) return;
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !active;
    }
    if (focus) selected.focus();
  }
  function disconnect(message = "Disconnected.", staffAccessState = "Signed out") {
    token = ""; generation++;
    $("review-cases").replaceChildren(); $("review-controls").hidden = true;
    if (staff) {
      role = null;
      $("staff-role").textContent = staffAccessState;
      $("staff-dashboard").hidden = true;
      $("staff-tab-history").hidden = true;
      $("staff-tab-controls").hidden = true;
      $("staff-tab-access").hidden = true;
      $("staff-catalog-console").hidden = true;
      $("staff-admin-list").replaceChildren();
      $("staff-history-result").replaceChildren();
      $("staff-recent-orders").replaceChildren();
      $("staff-sign-in").hidden = false;
    } else {
      $("review-token").value = "";
      $("review-login").hidden = false;
    }
    $("review-status").textContent = message;
  }
  async function request(path, payload) {
    if (!staff && !token) throw new Error("Connect a reviewer first.");
    const response = await fetch(path, {
      method: payload === undefined ? "GET" : "POST", credentials: staff ? "same-origin" : "omit", cache: "no-store",
      signal: AbortSignal.timeout(15000), headers: { ...(staff ? {} : { Authorization: `Bearer ${token}` }),
        ...(payload === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    if (response.status === 401) {
      const message = staff ? "Sign in to your Store account to review payments." : "Reviewer credential rejected.";
      disconnect(message, "Sign-in required");
      throw new Error(message);
    }
    if (staff && response.status === 403) {
      const { error } = await response.json();
      if (error === "This store account has no admin access.") disconnect(error, "Access denied");
      throw new Error(error || "This action requires admin access.");
    }
    if (!response.ok) throw new Error("Operation not confirmed. Refresh the queue before retrying; resume any saved decision.");
    return response;
  }
  function input(text, type = "text") {
    const label = element("label", text), field = element("input");
    field.type = type; field.required = true; field.autocomplete = "off"; label.append(field);
    return { label, field };
  }
  function formatMinorAmount(amount, currency) {
    if (!Number.isSafeInteger(amount) || amount < 0 || !/^[A-Z]{3}$/.test(currency || "")) return "—";
    try {
      const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
      return `${(amount / 10 ** digits).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })} ${currency}`;
    } catch { return `${amount.toLocaleString("en-US")} ${currency}`; }
  }
  function render(item, actor, current) {
    const panel = element("article", "", "admin-panel review-case"), payment = item.payment || {};
    panel.append(element("h2", `Order ${item.order_id}`), element("p", `Submission ${item.revision} · ${item.state}`));
    const details = element("dl");
    for (const [label, value] of [["Game", item.product_slug], ["Package", item.offer_id],
      ["Delivery", item.fulfillment_kind], ["Order total", formatMinorAmount(item.amount_minor, item.currency)],
      ["Rail", payment.rail], ["Receiver", payment.receiver_id], ["Receiver revision", payment.receiver_revision], ["Merchant account", payment.account], ["Account name", payment.account_name],
      ["Customer reference (unverified)", item.claim.transaction_reference], ["Receipt SHA-256", item.claim.evidence_sha256],
      ["Submitted", item.submitted_at]]) details.append(element("dt", label), element("dd", value || "Unavailable"));
    panel.append(details);
    const feedback = element("p", "", "admin-status"); feedback.setAttribute("role", "status");
    const base = `${reviews}/orders/${encodeURIComponent(item.order_id)}/revisions/${item.revision}`;
    const download = element("button", "Download untrusted receipt attachment", "admin-button"); download.type = "button";
    download.addEventListener("click", async () => {
      download.disabled = true;
      try {
        const response = await request(`${base}/receipt`), blob = await response.blob();
        if (!current()) return;
        const url = URL.createObjectURL(blob), anchor = element("a");
        anchor.href = url; anchor.download = `receipt-r${item.revision}.bin`; document.body.append(anchor);
        anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        feedback.textContent = "Attachment downloaded. Verify the actual transfer independently in merchant records.";
      } catch (error) { if (current()) feedback.textContent = error.message; }
      finally { download.disabled = false; }
    });
    panel.append(download, feedback);
    if (item.decision) {
      const label = item.decision.resolution === "external_refund_authorized" ? "external refund authorization"
        : item.decision.approve ? "approval" : "rejection";
      panel.append(element("p", `Saved ${label} by ${item.decision.actor}; completion pending.`));
      const resume = element("button", "Resume saved decision", "admin-button"); resume.type = "button";
      resume.disabled = item.decision.actor !== actor;
      resume.addEventListener("click", async () => {
        resume.disabled = true; feedback.textContent = "Resuming saved decision…";
        try { await request(`${base}/resume`, {}); if (current()) await refresh(); }
        catch (error) { if (current()) feedback.textContent = error.message; }
      });
      panel.append(resume); return panel;
    }
    const form = element("form", "", "admin-form"), fields = element("fieldset");
    fields.append(element("legend", "Merchant verification"));
    const transaction = input("Verified merchant transaction ID"), amount = input(
      item.currency === "MMK" ? "Actual amount received (MMK)" : `Actual amount received (${item.currency} minor units)`, "number");
    transaction.field.maxLength = 128; amount.field.min = "1"; amount.field.step = "1";
    amount.field.placeholder = String(item.amount_minor);
    const time = input("Actual transfer time (ISO timestamp with timezone)"); time.field.placeholder = "2026-11-13T11:30:00+06:30";
    const reason = element("label", "Private review reason"), note = element("textarea");
    note.required = true; note.maxLength = 500; reason.append(note);
    const attest = element("label", "", "review-attest"), check = element("input"); check.type = "checkbox";
    attest.append(check, element("span", "I independently checked the exact receiving account, transfer amount, time and transaction ID in merchant records."));
    const refundAttest = element("label", "", "review-attest"), refundCheck = element("input"); refundCheck.type = "checkbox";
    refundAttest.append(refundCheck, element("span", "I verified this incoming transfer in merchant records and will return its full amount only to the sender account on that transaction."));
    const refundNote = element("p", "A refund authorization cancels this order. This app will not send money; after the manual repayment, a reviewer must record its verified transaction ID.", "admin-status");
    fields.append(transaction.label, amount.label, time.label, reason, attest, refundNote, refundAttest);
    const actions = element("div", "", "review-actions"), approve = element("button", "Approve exact payment", "admin-button admin-button--primary"),
      authorizeRefund = element("button", "Authorize full sender refund", "admin-button"), reject = element("button", "Reject this submission", "admin-button");
    approve.type = "submit"; authorizeRefund.type = "button"; reject.type = "button";
    actions.append(approve, authorizeRefund, reject); fields.append(actions); form.append(fields);
    async function commit(approval, refunding = false) {
      if (fields.disabled || !current()) return;
      if (!note.value.trim()) { note.reportValidity(); return; }
      const payload = { approve: approval, reason: note.value.trim() };
      if (approval || refunding) {
        if (!form.reportValidity()) return;
        if (refunding && !refundCheck.checked) { feedback.textContent = "Verify the sender account and tick the refund attestation first."; return; }
        if (approval && !check.checked) { feedback.textContent = "Verify merchant records and tick the payment attestation first."; return; }
        const received = Number(amount.field.value);
        if (!Number.isSafeInteger(received) || received <= 0 || (approval && received !== item.amount_minor)) {
          feedback.textContent = approval ? "Amount differs from the order. Use the refund path for a verified partial, excess or late transfer."
            : "Enter the actual positive amount received; the full amount must be returned.";
          return;
        }
        const transfer = { rail: payment.rail, receiver_id: payment.receiver_id,
          transaction_id: transaction.field.value.trim(), amount_minor: received, currency: item.currency,
          occurred_at: time.field.value.trim() };
        if (refunding) payload.external_refund = { ...transfer, refund_destination_confirmed: true };
        else payload.verified_transfer = transfer;
      }
      fields.disabled = true;
      feedback.textContent = refunding ? "Saving refund authorization and cancelling the order…"
        : approval ? "Saving verification and reserving funds…" : "Saving rejection…";
      try { await request(`${base}/decision`, payload); if (current()) await refresh(); }
      catch (error) { if (current()) feedback.textContent = error.message; }
      // Refresh before retrying: a lost response may follow a committed decision.
    }
    form.addEventListener("submit", (event) => { event.preventDefault(); void commit(true); });
    authorizeRefund.addEventListener("click", () => void commit(false, true));
    reject.addEventListener("click", () => void commit(false)); panel.append(form); return panel;
  }
  function renderRefund(item, current) {
    const panel = element("article", "", "admin-panel review-case review-refund-case");
    panel.append(element("h2", `Full refund · Order ${item.order_id}`),
      element("p", `Submission ${item.revision} · authorized by ${item.authorized_by} · ${item.authorized_at}`));
    const details = element("dl");
    for (const [label, value] of [["Incoming amount to return", formatMinorAmount(item.incoming_amount_minor, item.currency)],
      ["Original payment rail", item.incoming_rail], ["Verified incoming transaction", item.incoming_transaction_id],
      ["Received at", item.incoming_occurred_at], ["Refund rail", item.refund_rail], ["Authorization reason", item.reason]]) {
      details.append(element("dt", label), element("dd", value || "Unavailable"));
    }
    panel.append(details);
    const feedback = element("p", "", "admin-status"); feedback.setAttribute("role", "status"); panel.append(feedback);
    const form = element("form", "", "admin-form"), fields = element("fieldset");
    fields.append(element("legend", "Record completed external refund"));
    const transaction = input("Verified outbound refund transaction ID"); transaction.field.maxLength = 128;
    const time = input("Actual refund time (ISO timestamp with timezone)"); time.field.placeholder = "2026-11-13T11:30:00+06:30";
    const reason = element("label", "Private completion note"), note = element("textarea");
    note.required = true; note.maxLength = 500; reason.append(note);
    const attest = element("label", "", "review-attest"), check = element("input"); check.type = "checkbox";
    attest.append(check, element("span", "I verified the outbound merchant record, full authorized amount and original sender destination. No transfer was initiated by this app."));
    const save = element("button", "Record verified refund", "admin-button admin-button--primary"); save.type = "submit";
    fields.append(transaction.label, time.label, reason, attest, save); form.append(fields); panel.append(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (fields.disabled || !current() || !form.reportValidity()) return;
      if (!check.checked) { feedback.textContent = "Verify the actual outbound transaction and original sender destination first."; return; }
      fields.disabled = true; feedback.textContent = "Recording verified full refund…";
      try {
        const base = `${reviews}/orders/${encodeURIComponent(item.order_id)}/revisions/${item.revision}`;
        await request(`${base}/refund-complete`, { refund_transaction_id: transaction.field.value.trim(),
          refunded_at: time.field.value.trim(), reason: note.value.trim() });
        if (current()) await refresh();
      } catch (error) { if (current()) feedback.textContent = error.message; }
    });
    return panel;
  }
  async function refresh() {
    const version = ++generation, current = () => generation === version && !!(staff ? role : token);
    $("review-cases").replaceChildren(); $("review-status").textContent = "Loading review queue…";
    try {
      const response = await request(`${reviews}/queue`), data = await response.json();
      if (!current()) return;
      if (!staff) $("review-login").hidden = true;
      $("review-controls").hidden = false;
      const refunds = Array.isArray(data.refunds) ? data.refunds : [];
      $("review-status").textContent = `${data.cases.length} receipts to review · ${refunds.length} refunds awaiting completion (up to ${data.limit} each).`;
      for (const item of data.cases) $("review-cases").append(staff ? queueItem(item, render(item, data.actor, current), "Receipt") : render(item, data.actor, current));
      for (const item of refunds) $("review-cases").append(staff ? queueItem(item, renderRefund(item, current), "Refund") : renderRefund(item, current));
      if (!data.cases.length && !refunds.length) $("review-cases").append(element("p", "No pending payment reviews or refunds."));
      if (staff) await loadRecentOrders(current);
    } catch (error) { if (current()) $("review-status").textContent = error.message; }
  }
  function queueItem(item, panel, label) {
    const card = element("details", "", "admin-panel staff-case");
    const amount = item.amount_minor ?? item.incoming_amount_minor;
    const state = item.decision ? "Saved decision" : (item.payment?.rail || item.refund_rail || item.state);
    card.append(element("summary", `${label} · ${item.order_id} · ${formatMinorAmount(amount, item.currency)} · ${state}`), panel);
    return card;
  }

  async function loadRecentOrders(current) {
    const response = await request(`${reviews}/recent`);
    const { orders } = await response.json();
    if (!current()) return;
    const list = $("staff-recent-orders");
    list.replaceChildren();
    for (const order of orders) {
      const row = element("li");
      const button = element("button", `Order ${order.order_id}`, "admin-button");
      button.type = "button";
      button.addEventListener("click", () => {
        $("staff-order-id").value = order.order_id;
        selectStaffTab("history");
        void showHistory(order.order_id);
      });
      row.append(button, element("span", ` ${order.rail} · ${order.state} · ${order.opened_at}`));
      list.append(row);
    }
    if (!orders.length) list.append(element("li", "No payment intents yet."));
  }

  async function loadAdmins() {
    const response = await request(`${root}/admins`);
    const { admins } = await response.json();
    if (role !== "super_admin") return;
    $("staff-admin-list").replaceChildren();
    for (const admin of admins) {
      const row = element("li");
      const button = element("button", "Revoke access", "admin-button");
      button.type = "button";
      button.setAttribute("aria-label", `Revoke admin access for ${admin.customer_id}`);
      button.addEventListener("click", () => void changeAdmin(admin.customer_id, false, button));
      row.append(element("span", admin.customer_id), button);
      $("staff-admin-list").append(row);
    }
    if (!admins.length) $("staff-admin-list").append(element("li", "No delegated admins."));
  }

  async function changeAdmin(customerId, grant, button) {
    button.disabled = true;
    const status = $("staff-access-status");
    try {
      const response = await request(`${root}/admins`, { customer_id: customerId, grant });
      const { changed } = await response.json();
      status.textContent = changed ? (grant ? "Admin access granted." : "Admin access revoked.") : "Access was already set.";
      if (grant) $("staff-account-id").value = "";
      await loadAdmins();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function bankRefundForm(order, output) {
    if ((!order.bank_refund && !order.can_request_bank_refund) || !order.rail) return;
    if (order.bank_refund?.completed_at && order.state === "refunded") return;
    const completing = !!order.bank_refund;
    const form = element("form", "", "admin-form"), fields = element("fieldset");
    fields.append(element("legend", completing ? "Record verified bank refund" : "Authorize bank refund"));
    const sender = input("Verified sender fingerprint (SHA-256)");
    sender.field.pattern = "[a-f0-9]{64}";
    if (completing) sender.field.value = order.bank_refund.sender_fingerprint;
    const merchant = input("Merchant evidence reference");
    merchant.field.maxLength = 240;
    const transaction = input("Verified outbound transaction ID");
    transaction.field.maxLength = 240;
    const time = input("Actual transfer time (ISO timestamp with timezone)");
    const reason = input("Private authorization reason");
    reason.field.maxLength = 240;
    const attest = input(completing
      ? "I verified the outbound transfer, full amount and original sender in merchant records."
      : "I verified the original sender and inbound settlement in merchant records.", "checkbox");
    attest.label.className = "review-attest";
    const button = element("button", completing ? "Record verified refund" : "Authorize refund", "admin-button");
    button.type = "submit";
    const feedback = element("p", "", "admin-status"); feedback.setAttribute("role", "status");
    fields.append(element("p", `${formatMinorAmount(order.amount_minor, order.currency)} · ${order.rail} · Manual transfer.`),
      sender.label, merchant.label, ...(completing ? [transaction.label, time.label] : [reason.label]), attest.label, button, feedback);
    form.append(fields);
    output.append(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (fields.disabled || !form.reportValidity() || !attest.field.checked) return;
      fields.disabled = true;
      feedback.textContent = "Saving refund record…";
      const evidence = { sender_fingerprint: sender.field.value.trim(), merchant_reference: merchant.field.value.trim() };
      const payload = completing
        ? { ...evidence, transaction_id: transaction.field.value.trim(), paid_at: time.field.value.trim(),
            amount_minor: order.amount_minor, currency: order.currency, verified_paid: true }
        : { ...evidence, rail: order.rail, reason: reason.field.value.trim(), original_sender_confirmed: true };
      try {
        await request(`${reviews}/orders/${encodeURIComponent(order.order_id)}/bank-refund/${completing ? "complete" : "authorize"}`, payload);
        if (output.contains(form)) await showHistory(order.order_id);
      } catch (error) {
        feedback.textContent = error.message;
      }
    });
  }

  let historyVersion = 0;
  async function showHistory(orderId) {
    const version = ++historyVersion;
    const output = $("staff-history-result");
    const status = $("staff-history-status");
    output.replaceChildren();
    status.textContent = "Loading order…";
    try {
      const response = await request(`${reviews}/orders/${encodeURIComponent(orderId)}/history`);
      const order = await response.json();
      if (version !== historyVersion || !role) return;
      status.textContent = "";
      output.append(element("h3", `Order ${order.order_id}`),
        element("p", `${order.state} · ${formatMinorAmount(order.amount_minor, order.currency)} · ${order.rail || "No payment rail"}`));
      const records = element("ul", "", "staff-history-list");
      for (const review of order.reviews) {
        const decision = review.decision;
        const result = decision ? (decision.resolution || (decision.approve ? "Approved" : "Rejected")) : "Awaiting review";
        records.append(element("li", `Receipt ${review.revision} · ${review.submitted_at} · ${review.claim.transaction_reference} · ${result}${decision ? ` by ${decision.actor} on ${review.decided_at}. ${decision.reason}` : ""}${decision && !review.applied_at ? " · Completion pending" : ""}`));
      }
      for (const refund of order.refunds) {
        records.append(element("li", `Original-sender refund · ${formatMinorAmount(refund.incoming_amount_minor, refund.currency)} · ${refund.refund_transaction_id ? `Recorded ${refund.refunded_at} · ${refund.refund_transaction_id}` : "Awaiting manual repayment"}`));
      }
      if (order.bank_refund) records.append(element("li", `Bank refund · Authorized ${order.bank_refund.authorized_at}${order.bank_refund.completed_at ? ` · Recorded ${order.bank_refund.completed_at}` : " · Awaiting repayment"}`));
      for (const event of order.events) records.append(element("li", `${event.occurred_at} · ${event.from_state} → ${event.to_state} · ${event.reason}`));
      output.append(records);
      bankRefundForm(order, output);
    } catch (error) {
      if (version === historyVersion && role) status.textContent = error.message;
    }
  }

  async function startStaff() {
    try {
      const response = await request(`${root}/session`), session = await response.json();
      role = session.role;
      $("staff-role").textContent = role === "super_admin" ? "Super admin" : "Admin";
      $("staff-sign-in").hidden = true;
      $("staff-dashboard").hidden = false;
      $("staff-catalog-console").hidden = role !== "super_admin";
      $("staff-panel-controls").hidden = role !== "super_admin";
      $("staff-panel-access").hidden = role !== "super_admin";
      $("staff-tab-controls").hidden = role !== "super_admin";
      $("staff-tab-access").hidden = role !== "super_admin";
      $("staff-panel-history").hidden = !session.review_available;
      $("staff-tab-history").hidden = !session.review_available;
      selectStaffTab("payments");
      $("staff-intake").textContent = session.accepting_payments
        ? "Payment rails configured · catalog and checkout gates still apply"
        : "New payments paused";
      if (session.review_available) await refresh();
      else $("review-status").textContent = "Payment review is unavailable.";
      if (role === "super_admin") await loadAdmins();
    } catch (error) {
      if (!role && $("staff-role").textContent === "Checking access") {
        $("staff-role").textContent = "Access check failed";
        $("review-status").textContent = "Could not verify access. Refresh this page to retry.";
        return;
      }
      $("review-status").textContent = error.message;
    }
  }
  if (staff) {
    const tabs = [...document.querySelectorAll("#staff-dashboard [data-staff-tab]")];
    for (const tab of tabs) {
      tab.addEventListener("click", () => selectStaffTab(tab.dataset.staffTab));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
        const available = tabs.filter((item) => !item.hidden);
        const index = available.indexOf(tab);
        const next = event.key === "Home" ? 0 : event.key === "End" ? available.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : available.length - 1)) % available.length;
        event.preventDefault();
        selectStaffTab(available[next]?.dataset.staffTab, true);
      });
    }
    $("staff-grant").addEventListener("submit", (event) => {
      event.preventDefault();
      void changeAdmin($("staff-account-id").value.trim(), true, $("staff-grant-button"));
    });
    $("staff-order-search").addEventListener("submit", (event) => {
      event.preventDefault();
      void showHistory($("staff-order-id").value.trim());
    });
    void startStaff();
  } else {
    $("review-login").addEventListener("submit", (event) => { event.preventDefault(); token = $("review-token").value; $("review-token").value = ""; void refresh(); });
    $("review-disconnect").addEventListener("click", () => disconnect());
  }
  $("review-refresh").addEventListener("click", () => void refresh());
  window.addEventListener("pagehide", () => disconnect());
  if (staff) window.addEventListener("pageshow", (event) => { if (event.persisted) void startStaff(); });
})();
