(function attachAuriXShared() {
  const channel = typeof BroadcastChannel === "function"
    ? new BroadcastChannel("aurix-ai-session")
    : null;

  function emitSessionChange(type) {
    const event = { type, at: Date.now() };
    try { localStorage.setItem("aurix-ai-session-event", JSON.stringify(event)); } catch (_error) { /* private mode */ }
    channel?.postMessage(event);
  }

  async function getAuthConfig() {
    const response = await fetch("/api/auth/config", { credentials: "same-origin", cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Authentication configuration unavailable");
    return payload;
  }

  async function getSession() {
    const response = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.error || "Session check failed");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function renderTelegramWidget(container, botUsername, callbackName, callback) {
    container.replaceChildren();
    if (!botUsername) return;
    window[callbackName] = callback;
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.dataset.telegramLogin = botUsername;
    script.dataset.size = "large";
    script.dataset.radius = "8";
    script.dataset.onauth = `${callbackName}(user)`;
    script.dataset.requestAccess = "write";
    container.appendChild(script);
  }

  async function logout() {
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) throw new Error("Sign out failed");
    emitSessionChange("logout");
  }

  function subscribeSession(callback) {
    const onMessage = (event) => callback(event.data || { type: "session" });
    const onStorage = (event) => {
      if (event.key !== "aurix-ai-session-event" || !event.newValue) return;
      try { callback(JSON.parse(event.newValue)); } catch (_error) { callback({ type: "session" }); }
    };
    channel?.addEventListener("message", onMessage);
    window.addEventListener("storage", onStorage);
    return () => {
      channel?.removeEventListener("message", onMessage);
      window.removeEventListener("storage", onStorage);
    };
  }

  window.AuriXShared = {
    getAuthConfig,
    getSession,
    logout,
    renderTelegramWidget,
    subscribeSession,
    emitSessionChange,
  };
})();
