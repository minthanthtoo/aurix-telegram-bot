const authCopy = document.querySelector("#auth-copy");
const telegramLogin = document.querySelector("#telegram-login");
const authUser = document.querySelector("#auth-user");
const logoutButton = document.querySelector("#logout");
const reauthButton = document.querySelector("#reauth");
const accessPanel = document.querySelector(".access-panel");
const mode = document.querySelector("#mode");
const direction = document.querySelector("#direction");
const directionControl = document.querySelector("#direction-control");
const model = document.querySelector("#model");
const modeDescription = document.querySelector("#mode-description");
const starterPrompts = document.querySelector("#starter-prompts");
const translatorWorkspace = document.querySelector("#translator-workspace");
const translationSource = document.querySelector("#translation-source");
const translationResult = document.querySelector("#translation-result");
const translationReviewState = document.querySelector("#translation-review-state");
const copyTranslationButton = document.querySelector("#copy-translation");
const useTranslationButton = document.querySelector("#use-translation");
const messageLabel = document.querySelector("#message-label");
const messageInput = document.querySelector("#message");
const form = document.querySelector("#chat-form");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");
const sendButton = document.querySelector("#send");
const cancelAttemptButton = document.querySelector("#cancel-attempt");
const newChatButton = document.querySelector("#new-chat");
const telegramWebApp = window.Telegram && window.Telegram.WebApp;
const shared = window.AuriXShared;

let authenticatedUser = null;
let activeMode = mode.value;
let activeModelId = model.value;
let lastSubmittedMode = activeMode;
let lastSubmittedModelId = activeModelId;
let pendingModeChange = null;
let pendingModelChange = null;
let conversation = [];
let activeRequests = 0;
let durableConversationId = null;
let durableLoadPromise = null;
let activeEventStream = null;
let activeAttempt = null;
let authCheckInFlight = null;
let lastAuthCheckAt = 0;
const modeConversations = { english: [], translate: [], lisu_assistant: [] };
const modelLabels = {};
const modeLabels = {
  english: "English assistant",
  translate: "English ↔ Lisu translator",
  lisu_assistant: "Lisu assistant (experimental)",
};
const modeDescriptions = {
  english: "Warm, clear answers in English.",
  translate: "Translate the submitted source and preserve its meaning.",
  lisu_assistant: "Conversational Lisu-script replies; experimental.",
};
const starterSets = {
  english: [
    ["How are you today?", "How are you today?"],
    ["Explain something simply", "Explain this simply."],
    ["Help me plan", "Help me plan my day."],
  ],
  translate: [
    ["Translate a greeting", "How are you today?"],
    ["Translate a request", "Please help me carry this bag."],
    ["Translate a question", "Where are the keys?"],
  ],
  lisu_assistant: [
    ["Start a conversation", "ꓮ ꓓꓳ ꓡꓯꓽ"],
    ["Ask how it is", "ꓠꓴ ꓮ ꓫꓵꓽ ꓬꓰ ꓠꓲꓹ ꓫꓵ ꓡ?"],
    ["Ask for help", "ꓟꓬꓱꓽ ꓐꓯ ꓖꓶ꓿"],
  ],
};
const MAX_CONTEXT_BYTES = 48 * 1024;
const MAX_HISTORY_MESSAGES = 24;

function utf8Bytes(value) {
  return new TextEncoder().encode(value).length;
}

function historyChunks(history) {
  const chunks = [];
  let index = history.length - 1;
  while (index >= 0) {
    if (index > 0 && history[index - 1].role === "user" && history[index].role === "assistant") {
      chunks.push(history.slice(index - 1, index + 1));
      index -= 2;
    } else {
      chunks.push([history[index]]);
      index -= 1;
    }
  }
  return chunks;
}

function selectContextHistory(history, message) {
  const source = Array.isArray(history) ? history.slice(-MAX_HISTORY_MESSAGES) : [];
  let selected = [];
  let dropped = Math.max(0, (Array.isArray(history) ? history.length : 0) - source.length);
  for (const chunk of historyChunks(source)) {
    const candidate = [...chunk, ...selected];
    if (utf8Bytes(JSON.stringify({ message: message, history: candidate })) <= MAX_CONTEXT_BYTES) {
      selected = candidate;
    } else {
      dropped += chunk.length;
    }
  }
  return { history: selected, dropped: dropped };
}

function messageNode(role, text) {
  const node = document.createElement("div");
  node.className = "message " + role;
  node.textContent = text;
  return node;
}

async function copyText(value) {
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch (_error) {
    const helper = document.createElement("textarea");
    helper.value = value;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (_fallbackError) { copied = false; }
    helper.remove();
    return copied;
  }
}

function updateTranslationSource(value = "") {
  translationSource.value = value || "";
}

function updateTranslationResult(value = "", state = "Waiting for translation", { force = false } = {}) {
  if (!force && translationResult.dataset.userEdited === "true" && value !== "Translating…") {
    translationReviewState.textContent = "Edited result — review before using";
    return;
  }
  translationResult.value = value;
  translationResult.dataset.userEdited = "false";
  translationReviewState.textContent = state;
  const ready = Boolean(value && value !== "Translating…");
  copyTranslationButton.disabled = !ready;
  useTranslationButton.disabled = !ready;
}

function responseNode(role, text, onRetry = null) {
  const wrapper = document.createElement("div");
  wrapper.className = "response-content";
  wrapper.appendChild(messageNode(role, text));
  if (role === "assistant") {
    const actions = document.createElement("div");
    actions.className = "response-actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "button-quiet response-copy";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      const copied = await copyText(text);
      copy.textContent = copied ? "Copied" : "Copy failed";
      window.setTimeout(() => { copy.textContent = "Copy"; }, 1600);
    });
    actions.appendChild(copy);
    wrapper.appendChild(actions);
  } else if (role === "error" && onRetry) {
    const actions = document.createElement("div");
    actions.className = "response-actions";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "button-quiet response-copy";
    retry.textContent = "Retry submitted turn";
    retry.addEventListener("click", onRetry);
    actions.appendChild(retry);
    wrapper.appendChild(actions);
  }
  return wrapper;
}

function nearBottom() {
  return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 96;
}

function scrollIfFollowing() {
  if (nearBottom()) messages.scrollTop = messages.scrollHeight;
}

function modeEventNode(toMode) {
  const node = document.createElement("div");
  node.className = "mode-event mode-selection";
  node.textContent = modeLabels[toMode];
  node.setAttribute("aria-label", "Mode selected: " + modeLabels[toMode]);
  return node;
}

function modelEventNode(fromModel, toModel) {
  const node = document.createElement("div");
  node.className = "mode-event model-selection";
  node.textContent = "Model changed: " + (modelLabels[fromModel] || fromModel) + " → " + (modelLabels[toModel] || toModel);
  return node;
}

function directionEventNode(value) {
  const node = document.createElement("div");
  node.className = "mode-event direction-selection";
  node.textContent = value === "lisu_to_en" ? "Lisu → English" : "English → Lisu";
  return node;
}

function createTurn({ text, modeValue, modeChange, modelChange, directionValue, workingConversation, userEntry }) {
  const root = document.createElement("div");
  root.className = "conversation-turn";
  if (modeChange) root.appendChild(modeEventNode(modeChange.to));
  if (modeValue === "translate") root.appendChild(directionEventNode(directionValue));
  if (modelChange) root.appendChild(modelEventNode(modelChange.from, modelChange.to));
  root.appendChild(messageNode("user", text));
  const response = document.createElement("div");
  response.className = "turn-response";
  const pending = document.createElement("div");
  pending.className = "turn-pending";
  pending.textContent = "Thinking…";
  response.appendChild(pending);
  root.appendChild(response);
  messages.appendChild(root);
  scrollIfFollowing();
  return {
    root: root,
    response: response,
    modeValue: modeValue,
    workingConversation: workingConversation,
    userEntry: userEntry,
    attemptId: null,
    assistantEntry: null,
  };
}

function setTurnResponse(turn, role, text, onRetry = null) {
  const shouldFollow = nearBottom();
  turn.response.replaceChildren(responseNode(role, text, onRetry));
  if (turn.modeValue === "translate") {
    updateTranslationResult(
      role === "assistant" ? text : "",
      role === "assistant" ? "Review ready — edit the result before using it" : "Translation failed",
    );
  }
  if (shouldFollow) messages.scrollTop = messages.scrollHeight;
}

function addOrderedAssistantEntry(turn, text) {
  const userIndex = turn.workingConversation.indexOf(turn.userEntry);
  if (userIndex < 0) return;
  const assistantEntry = { role: "assistant", content: text };
  turn.workingConversation.splice(userIndex + 1, 0, assistantEntry);
  turn.assistantEntry = assistantEntry;
}

function removeTurnUserEntry(turn) {
  const userIndex = turn.workingConversation.indexOf(turn.userEntry);
  if (userIndex >= 0) turn.workingConversation.splice(userIndex, 1);
}

function setStatus(text, busy = false) {
  status.textContent = text;
  sendButton.disabled = busy || activeRequests > 0 || !authenticatedUser;
}

function draftKey() {
  return authenticatedUser
    ? "aurix-draft:" + (authenticatedUser.telegram_id || authenticatedUser.id) + ":" + activeMode
    : "";
}

function restoreDraft() {
  const key = draftKey();
  if (!key) return;
  try {
    const value = localStorage.getItem(key);
    if (value && !messageInput.value) messageInput.value = value;
  } catch (_error) { /* private mode may block storage */ }
}

function saveDraft() {
  const key = draftKey();
  if (!key) return;
  try {
    if (messageInput.value) localStorage.setItem(key, messageInput.value);
    else localStorage.removeItem(key);
  } catch (_error) { /* private mode may block storage */ }
}

function setAuthenticated(user) {
  const wasAuthenticated = Boolean(authenticatedUser);
  authenticatedUser = user || null;
  const authenticated = Boolean(authenticatedUser);
  document.body.classList.toggle("is-authenticated", authenticated);
  messageInput.disabled = !authenticated;
  mode.disabled = !authenticated;
  direction.disabled = !authenticated;
  model.disabled = !authenticated;
  if (authenticated) {
    const handle = user.username ? "@" + user.username : user.first_name || "Telegram user";
    authUser.textContent = "Signed in as " + handle;
    authUser.hidden = false;
    logoutButton.hidden = false;
    reauthButton.hidden = true;
    authCopy.textContent = "Your Telegram identity is verified for this browser session.";
    accessPanel.classList.add("access-compact");
    telegramLogin.replaceChildren();
    restoreDraft();
    setStatus("Ready");
    if (!wasAuthenticated || !durableConversationId) {
      ensureDurableConversation().catch((error) => {
        setStatus(error instanceof Error ? error.message : "Conversation history is unavailable");
      });
    }
  } else {
    closeActiveEventStream();
    durableConversationId = null;
    authUser.hidden = true;
    logoutButton.hidden = true;
    reauthButton.hidden = false;
    accessPanel.classList.remove("access-compact");
    setStatus("Sign in with Telegram first");
  }
}

async function renderSignIn(message = "Sign in with Telegram to use the private AuriX AI service.") {
  setAuthenticated(null);
  authCopy.textContent = message;
  try {
    const config = await shared.getAuthConfig();
    if (config.telegram_login_enabled && config.bot_username) {
      reauthButton.hidden = true;
      shared.renderTelegramWidget(telegramLogin, config.bot_username, "onAuriXTelegramAuth", finishTelegramLogin);
    } else {
      authCopy.textContent = "Telegram sign-in is not configured yet. Open this app from Telegram after setup.";
      reauthButton.hidden = true;
    }
  } catch (error) {
    authCopy.textContent = error instanceof Error ? error.message : "Telegram sign-in is unavailable.";
  }
}

function showAuthRequired(message = "Your Telegram session expired. Sign in again.") {
  renderSignIn(message);
}

async function postJSON(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "Telegram authentication failed");
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function requestJSON(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || "The AuriX AI request failed");
    error.status = response.status;
    throw error;
  }
  return payload;
}

function conversationStorageKey() {
  const userId = authenticatedUser && (authenticatedUser.telegram_id || authenticatedUser.id);
  return userId ? "aurix-conversation:" + userId + ":" + activeMode : "";
}

function rememberConversation(id) {
  durableConversationId = id || null;
  const key = conversationStorageKey();
  if (!key || !id) return;
  try { localStorage.setItem(key, id); } catch (_error) { /* private mode may block storage */ }
}

function closeActiveEventStream() {
  if (activeEventStream) activeEventStream.close();
  activeEventStream = null;
  activeAttempt = null;
  cancelAttemptButton.hidden = true;
}

function attemptErrorText(attempt) {
  if (attempt.status === "cancelled") return "Generation cancelled. The submitted source is preserved.";
  if (attempt.status === "interrupted") return "Generation was interrupted after a service restart.";
  return "The AI request failed. The submitted source is preserved for retry.";
}

function watchAttempt(turn, attemptId) {
  closeActiveEventStream();
  return new Promise((resolve) => {
    const url = "/api/conversations/" + encodeURIComponent(durableConversationId)
      + "/attempts/" + encodeURIComponent(attemptId) + "/events";
    const stream = new EventSource(url, { withCredentials: true });
    activeEventStream = stream;
    activeAttempt = { id: attemptId, turn: turn };
    cancelAttemptButton.hidden = false;
    cancelAttemptButton.disabled = false;
    let settled = false;

    const finish = (attempt) => {
      if (settled) return;
      settled = true;
      stream.close();
      if (activeEventStream === stream) activeEventStream = null;
      if (activeAttempt && activeAttempt.id === attemptId) activeAttempt = null;
      cancelAttemptButton.hidden = true;
      if (attempt.status === "completed") {
        const responseText = attempt.output_text || "The AI returned no text.";
        setTurnResponse(turn, "assistant", responseText);
        addOrderedAssistantEntry(turn, responseText);
        modeConversations[turn.modeValue] = turn.workingConversation;
        setStatus("Completed");
      } else {
        setTurnResponse(turn, "error", attemptErrorText(attempt), () => retryTurn(turn));
        setStatus(attempt.status === "cancelled" ? "Cancelled" : "Request failed");
      }
      resolve(attempt);
    };

    stream.addEventListener("snapshot", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.text) setTurnResponse(turn, "assistant", payload.text);
      } catch (_error) { /* ignore malformed reconnect data */ }
    });
    stream.addEventListener("terminal", (event) => {
      try { finish(JSON.parse(event.data)); } catch (_error) { /* wait for a reconnect */ }
    });
    stream.addEventListener("timeout", () => {
      stream.close();
      setStatus("Stream timed out; reconnect to resume");
      resolve(null);
    });
    stream.onerror = () => {
      if (!settled) setStatus("Connection interrupted; reconnecting…", true);
    };
  });
}

function renderStoredConversation(detail) {
  conversation = [];
  messages.replaceChildren();
  const turns = Array.isArray(detail && detail.turns) ? detail.turns : [];
  turns.forEach((storedTurn) => {
    const userEntry = { role: "user", content: storedTurn.submitted_source };
    conversation.push(userEntry);
    const attempts = Array.isArray(storedTurn.attempts) ? storedTurn.attempts : [];
    const latest = attempts[attempts.length - 1];
    const turn = createTurn({
      text: storedTurn.submitted_source,
      modeValue: storedTurn.mode || activeMode,
      directionValue: storedTurn.direction || direction.value,
      workingConversation: conversation,
      userEntry: userEntry,
    });
    turn.attemptId = latest && latest.id;
    if (turn.modeValue === "translate") updateTranslationSource(storedTurn.submitted_source);
    if (!latest || latest.status === "running") {
      if (latest) {
        activeRequests += 1;
        setStatus("Resuming generation…", true);
        watchAttempt(turn, latest.id).finally(() => {
          activeRequests = Math.max(0, activeRequests - 1);
          if (!activeRequests && authenticatedUser) setStatus("Ready");
        });
      }
      return;
    }
    if (latest.status === "completed") {
      setTurnResponse(turn, "assistant", latest.output_text || "The AI returned no text.");
      addOrderedAssistantEntry(turn, latest.output_text || "The AI returned no text.");
    } else {
      setTurnResponse(turn, "error", attemptErrorText(latest), () => retryTurn(turn));
    }
  });
  if (!turns.length) resetVisibleConversation();
  scrollIfFollowing();
}

async function ensureDurableConversation({ force = false } = {}) {
  if (!authenticatedUser) return null;
  if (durableLoadPromise && !force) return durableLoadPromise;
  durableLoadPromise = (async () => {
    const listing = await requestJSON("/api/conversations");
    const available = Array.isArray(listing.conversations) ? listing.conversations : [];
    let selected = null;
    const key = conversationStorageKey();
    let remembered = null;
    try { remembered = key ? localStorage.getItem(key) : null; } catch (_error) { /* private mode */ }
    if (remembered) selected = available.find((item) => item.id === remembered && item.mode === activeMode);
    if (!selected) selected = available.find((item) => item.mode === activeMode);
    if (!selected) {
      selected = await requestJSON("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: activeMode, direction: activeMode === "translate" ? direction.value : null }),
      });
    }
    rememberConversation(selected.id);
    const detail = await requestJSON("/api/conversations/" + encodeURIComponent(selected.id));
    renderStoredConversation(detail);
    return selected.id;
  })();
  try {
    return await durableLoadPromise;
  } catch (error) {
    if (error && error.status === 401) showAuthRequired();
    throw error;
  } finally {
    durableLoadPromise = null;
  }
}

async function finishTelegramLogin(user) {
  try {
    const payload = await postJSON("/api/auth/telegram", user);
    setAuthenticated(payload.user);
    shared.emitSessionChange("login");
  } catch (error) {
    renderSignIn(error instanceof Error ? error.message : "Telegram sign-in failed");
  }
}

async function loadAuth({ force = false } = {}) {
  const now = Date.now();
  if (!force && now - lastAuthCheckAt < 15000) return;
  if (authCheckInFlight) return authCheckInFlight;
  lastAuthCheckAt = now;
  authCheckInFlight = (async () => {
    try {
      const session = await shared.getSession();
      if (session.user) {
        setAuthenticated(session.user);
        return;
      }
      if (telegramWebApp && telegramWebApp.initData) {
        telegramWebApp.ready();
        telegramWebApp.expand();
        const payload = await postJSON("/api/auth/miniapp", { init_data: telegramWebApp.initData });
        setAuthenticated(payload.user);
        shared.emitSessionChange("login");
        return;
      }
      await renderSignIn();
    } catch (error) {
      if (error && error.status === 401) await renderSignIn("Your Telegram session expired. Sign in again.");
      else await renderSignIn(error instanceof Error ? error.message : "Telegram sign-in is unavailable.");
    } finally {
      authCheckInFlight = null;
    }
  })();
  return authCheckInFlight;
}

function updateModeUI() {
  const isTranslator = mode.value === "translate";
  directionControl.hidden = !isTranslator;
  translatorWorkspace.hidden = !isTranslator;
  messageLabel.textContent = isTranslator ? "Source" : "Message";
  messageInput.placeholder = isTranslator ? "Enter source text to translate…" : "Write a message…";
  modeDescription.textContent = modeDescriptions[mode.value] || "";
  starterPrompts.replaceChildren();
  (starterSets[mode.value] || []).forEach(([label, prompt]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "starter-button";
    button.textContent = label;
    button.dataset.prompt = prompt;
    starterPrompts.appendChild(button);
  });
}

function resetVisibleConversation() {
  messages.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "message assistant";
  empty.textContent = mode.value === "translate"
    ? "Choose a direction, enter source text, and translate."
    : "Choose a suggestion or write a message to begin.";
  messages.appendChild(empty);
}

mode.addEventListener("change", () => {
  const nextMode = mode.value;
  if (nextMode === activeMode) return;
  modeConversations[activeMode] = conversation;
  pendingModeChange = {
    from: activeMode,
    to: nextMode,
    handoff: nextMode === "translate" ? [] : conversation.slice(-4),
  };
  activeMode = nextMode;
  conversation = modeConversations[nextMode] || [];
  updateModeUI();
  updateTranslationResult("");
  restoreDraft();
  setStatus("Mode ready: " + modeLabels[nextMode]);
  if (authenticatedUser) {
    ensureDurableConversation({ force: true }).catch((error) => {
      setStatus(error instanceof Error ? error.message : "Conversation history is unavailable");
    });
  }
});

model.addEventListener("change", () => {
  const nextModelId = model.value;
  if (nextModelId === activeModelId) return;
  pendingModelChange = { from: activeModelId, to: nextModelId };
  activeModelId = nextModelId;
  setStatus("Model ready: " + (modelLabels[nextModelId] || nextModelId));
});

direction.addEventListener("change", () => {
  setStatus(direction.value === "lisu_to_en" ? "Direction ready: Lisu → English" : "Direction ready: English → Lisu");
});

translationResult.addEventListener("input", () => {
  translationResult.dataset.userEdited = "true";
  translationReviewState.textContent = "Edited result — review before using";
});

copyTranslationButton.addEventListener("click", async () => {
  const copied = await copyText(translationResult.value);
  setStatus(copied ? "Translation copied" : "Copy failed; select the result manually");
});

useTranslationButton.addEventListener("click", () => {
  if (!translationResult.value || translationResult.value === "Translating…") return;
  messageInput.value = translationResult.value;
  saveDraft();
  messageInput.focus();
  setStatus("Translation placed in the source composer");
});

starterPrompts.addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt]");
  if (!button || !authenticatedUser) return;
  messageInput.value = button.dataset.prompt || "";
  saveDraft();
  messageInput.focus();
});

messageInput.addEventListener("input", saveDraft);

newChatButton.addEventListener("click", async () => {
  if (!authenticatedUser) return;
  closeActiveEventStream();
  modeConversations[activeMode] = [];
  conversation = [];
  pendingModeChange = null;
  pendingModelChange = null;
  lastSubmittedMode = activeMode;
  lastSubmittedModelId = activeModelId;
  updateTranslationSource("");
  updateTranslationResult("", "Waiting for translation", { force: true });
  resetVisibleConversation();
  setStatus("Creating conversation…", true);
  try {
    const created = await requestJSON("/api/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: activeMode,
        direction: activeMode === "translate" ? direction.value : null,
        title: "New conversation",
      }),
    });
    rememberConversation(created.id);
    setStatus("New conversation ready");
    messageInput.focus();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Could not create conversation");
  }
});

async function loadModels() {
  try {
    const response = await fetch("/api/models", { credentials: "same-origin", cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !Array.isArray(payload.models)) throw new Error("Model list unavailable");
    model.innerHTML = "";
    let defaultModelId = "";
    payload.models.forEach((item) => {
      modelLabels[item.id] = item.label;
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      option.title = item.description || "";
      model.appendChild(option);
      if (item.default) defaultModelId = item.id;
    });
    if (defaultModelId) model.value = defaultModelId;
    activeModelId = model.value;
    lastSubmittedModelId = activeModelId;
  } catch (_error) {
    setStatus("Model list unavailable; using the configured model");
  }
}

async function retryTurn(turn) {
  if (!turn.attemptId || !durableConversationId || activeRequests > 0) return;
  activeRequests += 1;
  setTurnResponse(turn, "assistant", "Retrying submitted source…");
  setStatus("Retrying…", true);
  try {
    const payload = await requestJSON(
      "/api/conversations/" + encodeURIComponent(durableConversationId)
        + "/attempts/" + encodeURIComponent(turn.attemptId) + "/retry",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    );
    turn.attemptId = payload.attempt.id;
    await watchAttempt(turn, turn.attemptId);
  } catch (error) {
    setTurnResponse(turn, "error", error instanceof Error ? error.message : "Retry failed", () => retryTurn(turn));
    setStatus("Retry failed");
  } finally {
    activeRequests = Math.max(0, activeRequests - 1);
    if (!activeRequests && authenticatedUser) sendButton.disabled = false;
  }
}

cancelAttemptButton.addEventListener("click", async () => {
  if (!activeAttempt || !durableConversationId) return;
  cancelAttemptButton.disabled = true;
  try {
    await requestJSON(
      "/api/conversations/" + encodeURIComponent(durableConversationId)
        + "/attempts/" + encodeURIComponent(activeAttempt.id) + "/cancel",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    );
    setStatus("Cancelling…", true);
  } catch (error) {
    cancelAttemptButton.disabled = false;
    setStatus(error instanceof Error ? error.message : "Cancellation failed");
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  if (!authenticatedUser) {
    await loadAuth({ force: true });
    setStatus("Sign in with Telegram first");
    return;
  }
  if (activeRequests > 0) return;

  const requestMode = mode.value;
  const requestModel = model.value;
  const directionValue = direction.value;
  activeRequests += 1;
  setStatus("Preparing secure conversation…", true);
  let turn = null;

  try {
    await ensureDurableConversation();
    const modeChange = activeMode === lastSubmittedMode ? null : pendingModeChange;
    const modelChange = activeModelId === lastSubmittedModelId ? null : pendingModelChange;
    pendingModeChange = null;
    pendingModelChange = null;
    const workingConversation = conversation;
    const userEntry = { role: "user", content: text };
    workingConversation.push(userEntry);
    turn = createTurn({
      text: text,
      modeValue: requestMode,
      modeChange: modeChange,
      modelChange: modelChange,
      directionValue: directionValue,
      workingConversation: workingConversation,
      userEntry: userEntry,
    });
    messageInput.value = "";
    saveDraft();
    if (requestMode === "translate") {
      updateTranslationSource(text);
      updateTranslationResult("Translating…", "Generating translation…", { force: true });
    }
    setStatus("Thinking…", true);
    const payload = await requestJSON(
      "/api/conversations/" + encodeURIComponent(durableConversationId) + "/turns",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: requestMode,
          model_id: requestModel,
          message: text,
          direction: requestMode === "translate" ? directionValue : undefined,
          client_submission_id: crypto.randomUUID(),
        }),
      },
    );
    turn.attemptId = payload.attempt.id;
    lastSubmittedMode = requestMode;
    lastSubmittedModelId = requestModel;
    modeConversations[requestMode] = workingConversation;
    if (payload.attempt.status === "running") {
      await watchAttempt(turn, turn.attemptId);
    } else if (payload.attempt.status === "completed") {
      const responseText = payload.attempt.output_text || "The AI returned no text.";
      setTurnResponse(turn, "assistant", responseText);
      addOrderedAssistantEntry(turn, responseText);
      setStatus("Completed");
    } else {
      setTurnResponse(turn, "error", attemptErrorText(payload.attempt), () => retryTurn(turn));
      setStatus("Request failed");
    }
  } catch (error) {
    if (error && error.status === 401) showAuthRequired();
    if (turn) {
      setTurnResponse(
        turn,
        "error",
        error instanceof Error ? error.message : "The AI request could not be submitted",
      );
      removeTurnUserEntry(turn);
    }
    setStatus(error instanceof Error ? error.message : "The AI request failed");
    messageInput.value = text;
    saveDraft();
  } finally {
    activeRequests = Math.max(0, activeRequests - 1);
    if (activeRequests === 0 && authenticatedUser) sendButton.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  try { await shared.logout(); } catch (_error) { /* clear local view even if network is unavailable */ }
  setAuthenticated(null);
  await renderSignIn("Signed out. Sign in with Telegram to continue.");
});

reauthButton.addEventListener("click", () => renderSignIn());
window.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadAuth({ force: true });
});
window.addEventListener("focus", () => loadAuth({ force: true }));
shared.subscribeSession(() => loadAuth({ force: true }));

updateModeUI();
setAuthenticated(null);
loadModels();
loadAuth({ force: true });
