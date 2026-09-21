// DashDashGo UI behaviour: run/retry actions, live refresh, tooltips, log filter, theme.
(() => {
  "use strict";

  const toast = (message) => {
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.textContent = message;
    document.body.append(el);
    setTimeout(() => el.remove(), 5000);
  };

  const post = async (url) => {
    const response = await fetch(url, { method: "POST", headers: { Accept: "application/json" } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    return body;
  };

  // ---- actions: "Run now" and "Retry" --------------------------------------
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-run-report], [data-retry-run]");
    if (!button) return;
    event.preventDefault();
    button.disabled = true;
    try {
      const run = button.dataset.runReport
        ? await post(`/api/reports/${encodeURIComponent(button.dataset.runReport)}/runs${button.dataset.force ? "?force=true" : ""}`)
        : await post(`/api/runs/${encodeURIComponent(button.dataset.retryRun)}/retry`);
      window.location.href = `/runs/${encodeURIComponent(run.run_id)}`;
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  });

  // ---- live refresh while something is queued/running ------------------------
  const root = () => document.getElementById("live-root");
  let timer = null;
  const refresh = async () => {
    try {
      const response = await fetch(window.location.href, { headers: { Accept: "text/html" } });
      if (!response.ok) return;
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = doc.getElementById("live-root");
      if (next && root()) {
        const level = document.querySelector("[data-log-filter][aria-pressed='true']")?.dataset.logFilter;
        root().replaceWith(next);
        if (level) applyLogFilter(level);
        if (next.dataset.live !== "true") clearInterval(timer);
      }
    } catch {
      /* transient network error: try again on the next tick */
    }
  };
  if (root()?.dataset.live === "true") timer = setInterval(refresh, 2500);

  // ---- log level filter -------------------------------------------------------
  const RANK = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
  const applyLogFilter = (level) => {
    document.querySelectorAll("[data-log-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.logFilter === level)));
    document.querySelectorAll(".log-line").forEach((line) => {
      line.hidden = (RANK[line.dataset.level] ?? 1) < (RANK[level] ?? 0);
    });
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-log-filter]");
    if (button) applyLogFilter(button.dataset.logFilter);
  });

  // ---- tooltips for chart marks & status strips -------------------------------
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("role", "tooltip");
  document.body.append(tip);
  const show = (target, x, y) => {
    tip.textContent = target.dataset.tip;
    tip.classList.add("show");
    const { width, height } = tip.getBoundingClientRect();
    const left = Math.min(Math.max(8, x - width / 2), window.innerWidth - width - 8);
    const top = y - height - 12 < 8 ? y + 16 : y - height - 12;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  document.addEventListener("pointermove", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (target) show(target, event.clientX, event.clientY);
    else tip.classList.remove("show");
  });
  document.addEventListener("focusin", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (!target) return;
    const r = target.getBoundingClientRect();
    show(target, r.left + r.width / 2, r.top);
  });
  document.addEventListener("focusout", () => tip.classList.remove("show"));

  // ---- theme toggle (per-viewer preference) -----------------------------------
  const THEME_KEY = "ddg-theme";
  const setTheme = (theme) => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  };
  try { setTheme(localStorage.getItem(THEME_KEY)); } catch { /* storage unavailable */ }
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    setTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* storage unavailable */ }
  });
})();
