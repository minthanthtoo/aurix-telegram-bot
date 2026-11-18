/* Feedback follows pointer intent; the control's hit area never moves. */
(() => {
  let press = null;
  const cancelled = new WeakSet();
  function finish(state) {
    if (!press) return;
    const { button } = press;
    button.dataset.press = state;
    if (state === "cancelled") cancelled.add(button);
    setTimeout(() => {
      if (button.dataset.press === state) delete button.dataset.press;
    }, 180);
    press = null;
  }
  document.addEventListener("pointerdown", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled || event.button !== 0) return;
    finish("cancelled");
    cancelled.delete(button);
    press = { button, id: event.pointerId, x: event.clientX, y: event.clientY };
    button.dataset.press = "down";
  }, true);
  document.addEventListener("pointermove", (event) => {
    if (!press || event.pointerId !== press.id) return;
    const rect = press.button.getBoundingClientRect();
    if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10 ||
        event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom) finish("cancelled");
  }, true);
  document.addEventListener("pointerup", (event) => {
    if (press && event.pointerId === press.id) {
      const rect = press.button.getBoundingClientRect();
      finish(event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom ? "cancelled" : "released");
    }
  }, true);
  document.addEventListener("pointercancel", () => finish("cancelled"), true);
  window.addEventListener("blur", () => finish("cancelled"));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button && event.detail !== 0 && cancelled.has(button)) {
      cancelled.delete(button);
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("button:not(:disabled)") && ["Enter", " "].includes(event.key)) {
      event.target.dataset.press = "down";
    }
  });
  document.addEventListener("keyup", (event) => {
    if (event.target.matches("button") && ["Enter", " "].includes(event.key)) {
      event.target.dataset.press = "released";
      setTimeout(() => delete event.target.dataset.press, 180);
    }
  });
})();

/* Snapshot the page change so scrolling and content commit as one short slide. */
(() => {
  let transition = null;
  let animation = null;
  let revision = 0;
  globalThis.AurixPager = Object.freeze({
    move(update, direction = 1) {
      const current = ++revision;
      transition?.skipTransition();
      animation?.cancel();
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!document.startViewTransition || reduced) {
        transition = null;
        delete document.documentElement.dataset.pagerDirection;
        update();
        if (!reduced) animation = document.querySelector("main")?.animate?.([
          { opacity: .4, transform: `translateX(${direction < 0 ? -32 : 32}px)` },
          { opacity: 1, transform: "translateX(0)" },
        ], { duration: 240, easing: "cubic-bezier(.22,.7,.25,1)" });
        return;
      }
      document.documentElement.dataset.pagerDirection = direction < 0 ? "back" : "forward";
      transition = document.startViewTransition(() => {
        if (current === revision) update();
      });
      transition.finished.catch(() => {}).finally(() => {
        if (current === revision) {
          transition = null;
          delete document.documentElement.dataset.pagerDirection;
        }
      });
    },
  });
})();
