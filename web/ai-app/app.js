const authCopy = document.querySelector("#auth-copy");
const telegramLogin = document.querySelector("#telegram-login");
const authUser = document.querySelector("#auth-user");
const logoutButton = document.querySelector("#logout");
const mode = document.querySelector("#mode");
const model = document.querySelector("#model");
const messageInput = document.querySelector("#message");
const form = document.querySelector("#chat-form");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");
const sendButton = form.querySelector("button");
const telegramWebApp = window.Telegram && window.Telegram.WebApp;

let authenticated = false;
let activeMode = mode.value;
let pendingModeChange = null;
let activeModelId = model.value;
let pendingModelChange = null;
let conversation = [];
let activeRequests = 0;
const modelLabels = {};
const modeLabels = {
  english: "English assistant",
  translate: "English ↔ Lisu translator",
  lisu_assistant: "Lisu assistant (experimental)",
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
    const bytes = utf8Bytes(JSON.stringify({ message, history: candidate }));
    if (bytes <= MAX_CONTEXT_BYTES) {
      selected = candidate;
    } else {
      dropped += chunk.length;
    }
  }
  return { history: selected, dropped };
}

function messageNode(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  return node;
}

function addMessage(role, text) {
  messages.appendChild(messageNode(role, text));
  messages.scrollTop = messages.scrollHeight;
}

function modeEventNode(toMode) {
  const node = document.createElement("div");
  node.className = "mode-event mode-selection";
  node.textContent = modeLabels[toMode];
  node.setAttribute("aria-label", `Mode selected: ${modeLabels[toMode]}`);
  return node;
}

function modelEventNode(fromModel, toModel) {
  const node = document.createElement("div");
  node.className = "mode-event model-selection";
  node.textContent = `Model changed: ${modelLabels[fromModel] || fromModel} → ${modelLabels[toModel] || toModel}`;
  return node;
}

function createTurn({ text, modeChange, modelChange, workingConversation, userEntry }) {
  const root = document.createElement("div");
  root.className = "conversation-turn";
  // Events are part of the same turn so a late response cannot cross a
  // later user message. The model event is deliberately the last event,
  // immediately above the user message it governs.
  if (modeChange) root.appendChild(modeEventNode(modeChange.to));
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
  messages.scrollTop = messages.scrollHeight;
  return { root, response, workingConversation, userEntry };
}

function setTurnResponse(turn, role, text) {
  turn.response.replaceChildren(messageNode(role, text));
  messages.scrollTop = messages.scrollHeight;
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
  sendButton.disabled = busy || activeRequests > 0 || !authenticated;
}

function setAuthenticated(user) {
  authenticated = Boolean(user);
  messageInput.disabled = !authenticated;
  mode.disabled = !authenticated;
  model.disabled = !authenticated;
  if (user) {
    const handle = user.username ? `@${user.username}` : user.first_name || "Telegram user";
    authUser.textContent = `Signed in as ${handle}`;
    authUser.hidden = false;
    logoutButton.hidden = false;
    authCopy.textContent = "Your Telegram identity is verified for this browser session.";
    telegramLogin.replaceChildren();
    setStatus("Ready");
  } else {
    authUser.hidden = true;
    logoutButton.hidden = true;
    setStatus("Sign in with Telegram first");
  }
}

function showAuthRequired(message = "Sign in with Telegram first") {
  setAuthenticated(null);
  authCopy.textContent = message;
}

async function postJSON(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Telegram authentication failed");
  return payload;
}

async function finishTelegramLogin(user) {
  try {
    const payload = await postJSON("/api/auth/telegram", user);
    setAuthenticated(payload.user);
  } catch (error) {
    showAuthRequired(error instanceof Error ? error.message : "Telegram sign-in failed");
  }
}

function renderLoginWidget(botUsername) {
  telegramLogin.replaceChildren();
  window.onTelegramAuth = finishTelegramLogin;
  const script = document.createElement("script");
  script.src = "https://telegram.org/js/telegram-widget.js?22";
  script.async = true;
  script.dataset.telegramLogin = botUsername;
  script.dataset.size = "large";
  script.dataset.radius = "8";
  script.dataset.onauth = "onTelegramAuth(user)";
  script.dataset.requestAccess = "write";
  telegramLogin.appendChild(script);
}

async function loadAuth() {
  try {
    const sessionResponse = await fetch("/api/session", { credentials: "same-origin" });
    if (sessionResponse.ok) {
      const session = await sessionResponse.json();
      if (session.user) {
        setAuthenticated(session.user);
        return;
      }
    }

    if (telegramWebApp && telegramWebApp.initData) {
      telegramWebApp.ready();
      telegramWebApp.expand();
      const payload = await postJSON("/api/auth/miniapp", { init_data: telegramWebApp.initData });
      setAuthenticated(payload.user);
      return;
    }

    const configResponse = await fetch("/api/auth/config", { credentials: "same-origin" });
    const config = await configResponse.json();
    if (config.telegram_login_enabled && config.bot_username) {
      authCopy.textContent = "Sign in with Telegram to use the private AuriX AI service.";
      renderLoginWidget(config.bot_username);
    } else {
      showAuthRequired("Telegram sign-in is not configured yet. Open this app from Telegram after setup.");
    }
  } catch (error) {
    showAuthRequired(error instanceof Error ? error.message : "Telegram sign-in is unavailable");
  }
}

mode.addEventListener("change", () => {
  const nextMode = mode.value;
  if (nextMode === activeMode) return;
  pendingModeChange = { from: activeMode, to: nextMode, handoff: conversation.slice(-4) };
  activeMode = nextMode;
  conversation = [];
  setStatus(`Mode selected: ${modeLabels[nextMode]}`);
});

model.addEventListener("change", () => {
  const nextModelId = model.value;
  if (nextModelId === activeModelId) return;
  pendingModelChange = { from: activeModelId, to: nextModelId };
  activeModelId = nextModelId;
  setStatus(`Model selected: ${modelLabels[nextModelId] || nextModelId}`);
});

async function loadModels() {
  try {
    const response = await fetch("/api/models", { credentials: "same-origin" });
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
  } catch (_error) {
    setStatus("Model list unavailable; using the configured model");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  if (!authenticated) {
    setStatus("Sign in with Telegram first");
    return;
  }

  const modeChange = pendingModeChange;
  const modelChange = pendingModelChange;
  pendingModeChange = null;
  pendingModelChange = null;
  const workingConversation = conversation;
  const sourceHistory = modeChange ? modeChange.handoff : workingConversation;
  const contextSelection = selectContextHistory(sourceHistory, text);
  const history = contextSelection.history;
  const userEntry = { role: "user", content: text };
  workingConversation.push(userEntry);
  const turn = createTurn({ text, modeChange, modelChange, workingConversation, userEntry });
  messageInput.value = "";
  activeRequests += 1;
  setStatus("Thinking…", true);
  const requestMode = mode.value;
  const requestModel = model.value;

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: requestMode, model_id: requestModel, message: text, history }),
    });
    const payload = await response.json();
    if (response.status === 401) {
      showAuthRequired("Your Telegram session expired. Sign in again.");
      throw new Error("Telegram session expired");
    }
    if (!response.ok) throw new Error(payload.error || "The AI request failed");
    const responseText = payload.text || "The AI returned no text.";
    setTurnResponse(turn, "assistant", responseText);
    addOrderedAssistantEntry(turn, responseText);
    const selectedLabel = payload.model_label || payload.model_id || "configured model";
    const providerLabel = payload.returned_model ? ` → ${payload.returned_model}` : "";
    const contextLabel = payload.context && payload.context.context_truncated
      ? ` · ${payload.context.history_dropped} older context message(s) trimmed`
      : "";
    setStatus(`Model: ${selectedLabel}${providerLabel}${contextLabel}`);
  } catch (error) {
    setTurnResponse(turn, "error", error instanceof Error ? error.message : "The AI request failed");
    removeTurnUserEntry(turn);
    setStatus("Request failed");
  } finally {
    activeRequests = Math.max(0, activeRequests - 1);
    if (activeRequests === 0) setStatus(status.textContent);
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await postJSON("/api/auth/logout", {});
  } finally {
    setAuthenticated(null);
    const configResponse = await fetch("/api/auth/config", { credentials: "same-origin" });
    const config = await configResponse.json();
    if (config.bot_username) renderLoginWidget(config.bot_username);
  }
});

setAuthenticated(null);
loadModels();
loadAuth();
