// Pipeline config editor: live validation, problem-to-line mapping, save/create, history.
(() => {
  "use strict";
  const root = document.getElementById("config-editor");
  if (!root) return;

  const mode = root.dataset.mode;
  let version = root.dataset.version;
  const textarea = root.querySelector("[data-yaml]");
  const gutter = root.querySelector("[data-gutter]");
  const result = root.querySelector("[data-result]");
  const stateLabel = root.querySelector("[data-state]");
  const nameInput = root.querySelector("[name='report-name']");
  const templateSelect = root.querySelector("[name='template']");
  let saved = textarea.value;
  let errorLines = new Set();
  let timer = null;
  let requestId = 0;

  const reportName = () => (mode === "new" ? nameInput.value.trim() : root.dataset.name);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // ---- gutter ------------------------------------------------------------------
  const renderGutter = () => {
    const count = textarea.value.split("\n").length;
    const lines = [];
    for (let i = 1; i <= count; i += 1) lines.push(errorLines.has(i) ? `● ${i}` : String(i));
    gutter.textContent = lines.join("\n");
    gutter.scrollTop = textarea.scrollTop;
  };
  textarea.addEventListener("scroll", () => { gutter.scrollTop = textarea.scrollTop; });

  // Map a problem location such as "destination.columns.2.type" to a line number
  // by walking the keys through the YAML text in order (list indexes are skipped).
  const lineFor = (location) => {
    const m = /^line (\d+)$/.exec(location);
    if (m) return Number(m[1]);
    const keys = location.split(".").filter((k) => k && !/^\d+$/.test(k) && !k.startsWith("("));
    const lines = textarea.value.split("\n");
    let from = 0;
    let found = null;
    for (const key of keys) {
      const pattern = new RegExp(`^\\s*(- )?${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:`);
      const index = lines.findIndex((line, i) => i >= from && pattern.test(line));
      if (index === -1) break;
      found = index + 1;
      from = index + 1;
    }
    return found;
  };

  const jumpTo = (line) => {
    const lines = textarea.value.split("\n");
    const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
    textarea.focus();
    textarea.setSelectionRange(start, start + lines[line - 1].length);
    const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 20;
    textarea.scrollTop = Math.max(0, (line - 4) * lineHeight);
  };

  // ---- dirty state -------------------------------------------------------------
  const dirty = () => textarea.value !== saved;
  const updateState = (text) => {
    stateLabel.textContent = text || (dirty() ? "Unsaved changes" : "Unchanged");
  };
  window.addEventListener("beforeunload", (event) => {
    if (dirty()) event.preventDefault();
  });

  // ---- validation --------------------------------------------------------------
  const showProblems = (message, problems) => {
    result.replaceChildren();
    const head = el("div", "validation-head bad");
    head.append(el("strong", "", "Invalid"), el("span", "muted", ` · ${problems.length} problem${problems.length === 1 ? "" : "s"}`));
    result.append(head);
    const list = el("ul", "problem-list");
    errorLines = new Set();
    for (const problem of problems) {
      const line = lineFor(problem.location);
      if (line) errorLines.add(line);
      const item = el("li");
      const button = el("button", "problem", "");
      button.type = "button";
      button.append(el("code", "", problem.location), el("span", "", problem.message));
      if (line) {
        button.append(el("span", "muted", `line ${line}`));
        button.addEventListener("click", () => jumpTo(line));
      } else {
        button.disabled = true;
      }
      item.append(button);
      list.append(item);
    }
    result.append(list);
    renderGutter();
  };

  const showSummary = (summary) => {
    errorLines = new Set();
    renderGutter();
    result.replaceChildren();
    const head = el("div", "validation-head ok");
    head.append(el("strong", "", "Valid"), el("span", "muted", " · ready to save"));
    result.append(head);
    const dl = el("dl", "kv");
    const rows = [
      ["Source", `${summary.location} (${summary.format.toUpperCase()})`],
      ["Destination", `${summary.destination}${summary.table_exists ? "" : " (created on first run)"}`],
      ["Columns", String(summary.columns)],
      ["Transforms", summary.transforms.join(" → ") || "none"],
      ["Quality rules", String(summary.rules)],
      ["Schedule", summary.schedule + (summary.enabled ? "" : " · disabled")],
    ];
    for (const [k, v] of rows) dl.append(el("dt", "", k), el("dd", "", v));
    result.append(dl);
    for (const warning of summary.warnings) result.append(el("p", "warning-text", `⚠ ${warning}`));
    const details = el("details", "ddl");
    details.append(el("summary", "", "Table DDL"), el("pre", "code", summary.ddl));
    result.append(details);
  };

  const validate = async () => {
    const name = reportName();
    if (!name) {
      showProblems("", [{ location: "name", message: "enter a name for the new pipeline" }]);
      return false;
    }
    const id = (requestId += 1);
    const response = await fetch(`/api/reports/${encodeURIComponent(name)}/config/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: textarea.value }),
    });
    const body = await response.json().catch(() => ({}));
    if (id !== requestId) return null; // a newer validation is in flight
    if (!response.ok) {
      showProblems("", [{ location: "(request)", message: body.detail || `HTTP ${response.status}` }]);
      return false;
    }
    if (body.valid) showSummary(body.summary);
    else showProblems(body.message, body.problems);
    return body.valid;
  };

  const scheduleValidation = () => {
    clearTimeout(timer);
    timer = setTimeout(validate, 600);
  };

  textarea.addEventListener("input", () => {
    renderGutter();
    updateState();
    scheduleValidation();
  });

  // Tab inserts two spaces instead of leaving the field.
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && !event.shiftKey) {
      event.preventDefault();
      textarea.setRangeText("  ", textarea.selectionStart, textarea.selectionEnd, "end");
      textarea.dispatchEvent(new Event("input"));
    }
  });

  // ---- new pipeline: name + template -----------------------------------------
  const syncNameLine = () => {
    const name = reportName() || "new_report";
    const untouched = !dirty(); // renaming alone is not an edit worth confirming
    textarea.value = textarea.value.replace(/^name:.*$/m, `name: ${name}`).replace(
      /^(\s*table:\s*)new_report\s*$/m, `$1${name}`,
    );
    if (untouched) saved = textarea.value;
    renderGutter();
    scheduleValidation();
  };
  nameInput?.addEventListener("input", syncNameLine);
  templateSelect?.addEventListener("change", async () => {
    if (dirty() && !window.confirm("Replace the editor contents with this template?")) return;
    const params = new URLSearchParams({ name: reportName() || "new_report" });
    if (templateSelect.value) params.set("source", templateSelect.value);
    const response = await fetch(`/api/config-templates?${params}`);
    const body = await response.json();
    textarea.value = body.yaml;
    saved = body.yaml;
    syncNameLine();
    updateState();
  });

  // ---- AI draft from a sample file (new pipelines, when the assistant is on) ----
  const draftCard = root.querySelector("[data-draft]");
  const base64Of = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("could not read the file"));
    reader.readAsDataURL(file);
  });
  draftCard?.querySelector("[data-draft-run]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const file = draftCard.querySelector("[data-draft-file]").files[0];
    const notes = draftCard.querySelector("[data-draft-notes]");
    const name = reportName();
    if (!name || !nameInput.checkValidity()) { toast("Enter a valid pipeline name first."); nameInput.focus(); return; }
    if (!file) { toast("Choose a sample export file first."); return; }
    if (dirty() && !window.confirm("Replace the editor contents with the AI draft?")) return;
    const label = button.innerHTML;
    button.disabled = true;
    button.textContent = "Drafting… (can take a minute or two)";
    notes.replaceChildren();
    try {
      const response = await fetch(`/api/reports/${encodeURIComponent(name)}/config/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          filename: file.name,
          content_base64: await base64Of(file),
          from_report: templateSelect?.value || null,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `Draft failed (${response.status})`);
      textarea.value = body.yaml;
      textarea.dispatchEvent(new Event("input"));
      const items = [...body.notes, ...body.problems.map((p) => `Still invalid: ${p.location}: ${p.message}`)];
      if (items.length) {
        const list = el("ul", "draft-notes");
        items.forEach((text) => list.append(el("li", "", text)));
        notes.append(el("p", "muted", `Drafted by ${body.model}. Check before creating:`), list);
      }
    } catch (error) {
      toast(error.message);
    } finally {
      button.innerHTML = label;
      button.disabled = false;
    }
  });

  // ---- save / create / archive / history ---------------------------------------
  const toast = (message) => {
    const node = el("div", "toast", message);
    node.setAttribute("role", "status");
    document.body.append(node);
    setTimeout(() => node.remove(), 5000);
  };

  const save = async () => {
    const name = reportName();
    updateState("Saving…");
    const request = mode === "new"
      ? fetch("/api/reports", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, yaml: textarea.value }),
        })
      : fetch(`/api/reports/${encodeURIComponent(name)}/config`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ yaml: textarea.value, base_version: version }),
        });
    const response = await request;
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      saved = textarea.value;
      version = body.version;
      updateState("Saved");
      window.location.href = `/reports/${encodeURIComponent(name)}`;
      return;
    }
    updateState();
    if (response.status === 422 && body.detail?.problems) showProblems(body.detail.message, body.detail.problems);
    else toast(typeof body.detail === "string" ? body.detail : `Save failed (HTTP ${response.status})`);
  };

  root.ownerDocument.querySelector("[data-save]").addEventListener("click", save);
  root.ownerDocument.querySelector("[data-validate]").addEventListener("click", validate);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault();
      save();
    }
  });

  document.querySelector("[data-archive]")?.addEventListener("click", async () => {
    const name = reportName();
    if (!window.confirm(`Archive "${name}"? It stops running and disappears from the UI; the file is kept in reports/.archive/.`)) return;
    const response = await fetch(`/api/reports/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (response.ok) {
      saved = textarea.value;
      window.location.href = "/";
    } else {
      toast(`Archive failed (HTTP ${response.status})`);
    }
  });

  document.querySelectorAll("[data-load-version]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (dirty() && !window.confirm("Replace your unsaved changes with this version?")) return;
      const name = reportName();
      const response = await fetch(`/api/reports/${encodeURIComponent(name)}/config/history/${button.dataset.loadVersion}`);
      const body = await response.json();
      textarea.value = body.yaml;
      renderGutter();
      updateState("Loaded an earlier version - save to restore it");
      validate();
    });
  });

  renderGutter();
  if (mode === "new") syncNameLine();
  else validate();
})();
