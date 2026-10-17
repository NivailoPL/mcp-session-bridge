/* ------------------------------------------------------------------
   MCP Session Bridge — Topics lab

   One manual step at a time: keep a versioned topic map, classify chapters
   with Jev, read the results and the journal. Nothing here runs on a timer
   except polling a run the owner started.
------------------------------------------------------------------- */
(() => {
  "use strict";

  const STEPS = [
    { id: "map", num: "1", name: "Map" },
    { id: "classify", num: "2", name: "Classify" },
    { id: "results", num: "3", name: "Results" },
    { sep: true },
    { id: "journal", num: "J", name: "Journal" },
    { id: "settings", num: "S", name: "Settings" },
  ];
  const STEP_KEY = "mcp-topics:step";
  const POLL_MS = 1500;

  const state = {
    csrf: "",
    step: readStep(),
    overview: null,
    assignments: null,
    journal: [],
    draft: null,
    editing: null,
    scope: { mode: "unclassified" },
    estimate: null,
    estimateError: "",
    filters: { topic: "", minConf: 0, query: "", sensitive: true },
    tree: { open: new Set(), closed: new Set() },
    pollTimer: 0,
    busy: false,
  };

  const dom = {
    steps: document.getElementById("steps"),
    view: document.getElementById("view"),
    sideNote: document.getElementById("sideNote"),
    toast: document.getElementById("toast"),
  };

  // ---------- helpers ----------

  function readStep() {
    try {
      const saved = localStorage.getItem(STEP_KEY);
      return STEPS.some((step) => step.id === saved) ? saved : "map";
    } catch (_) {
      return "map";
    }
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }

  function when(seconds) {
    if (!seconds) return "-";
    return new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function pct(value) {
    return value == null ? "-" : `${Math.round(value * 100)}%`;
  }

  function usd(value, digits = 4) {
    return `$${Number(value || 0).toFixed(digits)}`;
  }

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["content-type"] = "application/json";
    if (options.method && options.method !== "GET") headers["x-csrf-token"] = state.csrf;
    const response = await fetch(path, { ...options, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    if (response.status === 401) {
      window.location.assign(`/admin/login?next=${encodeURIComponent("/admin/topics")}`);
      throw new Error("Signed out.");
    }
    const body = await response.json().catch(() => ({ ok: false, error: "Invalid server response." }));
    if (!response.ok || body.ok === false) {
      const error = new Error(body.error || `Request failed (${response.status}).`);
      error.code = body.code;
      throw error;
    }
    return body;
  }

  let toastTimer = 0;
  function toast(message, isError = false) {
    dom.toast.textContent = message;
    dom.toast.classList.toggle("is-error", isError);
    dom.toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { dom.toast.hidden = true; }, isError ? 6000 : 3200);
  }

  async function act(fn) {
    if (state.busy) return;
    state.busy = true;
    try {
      await fn();
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      render();
    }
  }

  function topics() {
    return state.overview?.active_version?.topics || [];
  }

  function topicByKey(key) {
    return topics().find((topic) => topic.key === key) || { key, name: key || "-", color: "#4b5563" };
  }

  function topicCounts() {
    const counts = {};
    for (const row of state.assignments?.chapters || []) {
      if (row.topic_key) counts[row.topic_key] = (counts[row.topic_key] || 0) + 1;
    }
    return counts;
  }

  function activeRun() {
    return state.overview?.active_run || null;
  }

  // ---------- loading ----------

  async function loadOverview() {
    const overview = await request("/admin/api/topics");
    state.overview = overview;
    const run = overview.active_run;
    window.clearTimeout(state.pollTimer);
    if (run && (run.status === "queued" || run.status === "running")) {
      state.pollTimer = window.setTimeout(() => refresh().catch(() => {}), POLL_MS);
    }
  }

  async function loadAssignments() {
    state.assignments = await request("/admin/api/topics/assignments");
  }

  async function loadJournal() {
    state.journal = (await request("/admin/api/topics/journal")).journal;
  }

  async function refresh() {
    const wasRunning = Boolean(activeRun() && ["queued", "running"].includes(activeRun().status));
    await loadOverview();
    const nowRunning = Boolean(activeRun() && ["queued", "running"].includes(activeRun().status));
    if (!nowRunning || !wasRunning) await Promise.all([loadAssignments(), loadJournal()]);
    if (wasRunning && !nowRunning) state.estimate = null;
    render();
  }

  // ---------- render ----------

  function render() {
    renderSteps();
    const views = { map: renderMap, classify: renderClassify, results: renderResults, journal: renderJournal, settings: renderSettings };
    if (!state.overview) {
      dom.view.innerHTML = '<p class="tp-empty">Loading…</p>';
      return;
    }
    const scroll = dom.view.scrollTop;
    dom.view.innerHTML = banner() + views[state.step]();
    dom.view.scrollTop = scroll;
    bind[state.step]?.();
  }

  function renderSteps() {
    const o = state.overview;
    const counts = o?.counts || {};
    const run = activeRun();
    const hints = {
      map: o?.active_version ? `v${o.active_version.version_id}` : "none",
      classify: run && ["queued", "running"].includes(run.status) ? `${run.done_count}/${run.total_count}` : (counts.unclassified ? `${counts.unclassified} new` : ""),
      results: counts.classified ? String(counts.classified) : "",
      journal: "",
      settings: o && !o.key_set ? "key!" : "",
    };
    dom.steps.innerHTML = STEPS.map((step) => step.sep ? '<div class="tp-step-sep"></div>' : `
      <button class="tp-step" type="button" data-step="${step.id}" aria-current="${state.step === step.id}">
        <span class="tp-step-num">${step.num}</span>
        <span class="tp-step-name">${step.name}</span>
        <small>${esc(hints[step.id])}</small>
      </button>`).join("");
    for (const button of dom.steps.querySelectorAll("[data-step]")) {
      button.addEventListener("click", () => {
        state.step = button.dataset.step;
        try { localStorage.setItem(STEP_KEY, state.step); } catch (_) { /* private mode */ }
        dom.view.scrollTop = 0;
        render();
      });
    }
    dom.sideNote.innerHTML = o
      ? `Chapters: <b>${counts.chapters ?? 0}</b><br>Classified: <b>${counts.classified ?? 0}</b> · Unsorted: <b>${counts.pool ?? 0}</b><br>Sensitive skipped: <b>${counts.sensitive_skipped ?? 0}</b>`
      : "";
  }

  function banner() {
    const o = state.overview;
    if (!o.enabled) return '<div class="tp-banner">Token-spending features are switched off in Sessions › Settings. Turn them on to run Jev.</div>';
    if (!o.key_set && state.step === "classify") return '<div class="tp-banner">Add the OpenRouter API key in Settings before the first run.</div>';
    return "";
  }

  // ---------- 1. map ----------

  function renderMap() {
    const version = state.overview.active_version;
    if (!version) {
      return `
        <h1>Map</h1>
        <p class="tp-sub">The map is the list of topics Jev chooses from. Start by importing one: paste JSON, either a list of topics
        (<span class="mono">key, name, description, family, color</span>) or the experiment shape <span class="mono">{"key": "description"}</span>.
        The pool for chapters that fit nowhere (Unsorted, "Do ułożenia") is added automatically.</p>
        <div class="tp-card">${importForm("Import as v1")}</div>`;
    }
    if (!state.draft || state.draft.versionId !== version.version_id) resetDraft();
    const counts = topicCounts();
    const families = [];
    for (const topic of state.draft.topics) {
      let family = families.find((item) => item.name === topic.family);
      if (!family) families.push(family = { name: topic.family, topics: [] });
      family.topics.push(topic);
    }
    const dirty = draftChanged();
    return `
      <h1>Map <span class="tp-tag is-active">v${version.version_id} active</span></h1>
      <p class="tp-sub">Edit names and descriptions here. Jev reads only the descriptions, so write them as concrete, recognisable lists.
      Saving creates a new version; old versions stay readable and can be made active again. Classify again after a change.</p>
      <div class="tp-grid">
        <div>
          ${families.map((family) => `
            <div class="tp-family">
              <div class="tp-family-name">${esc(family.name)}</div>
              ${family.topics.map((topic) => topicRow(topic, counts[topic.key] || 0)).join("")}
            </div>`).join("")}
          <div class="tp-family">
            <div class="tp-family-name">Built in</div>
            ${topicRow({ ...version.topics[version.topics.length - 1], builtIn: true }, counts[state.assignments?.pool_key] || 0)}
          </div>
          <button class="tp-btn" type="button" data-add-topic>+ Add topic</button>
          ${dirty ? `
            <div class="tp-savebar">
              <b>Unsaved map changes</b>
              <input class="tp-input" data-note placeholder="Note for the journal (optional)" style="flex:1;min-width:220px">
              <button class="tp-btn" type="button" data-discard>Discard</button>
              <button class="tp-btn primary" type="button" data-save-map>Save as v${maxVersion() + 1}</button>
            </div>` : ""}
        </div>
        <div>
          <div class="tp-card">
            <h2>Versions</h2>
            ${state.overview.versions.map((item) => `
              <div class="tp-version">
                <div><b>v${item.version_id}</b> <span class="tp-muted">${esc(item.origin)} · ${item.topic_count} topics · ${when(item.created_at)}</span>
                  ${item.note ? `<div class="tp-muted tp-small">${esc(item.note)}</div>` : ""}</div>
                ${item.is_active ? '<span class="tp-tag is-active">active</span>' : `<button class="tp-btn small" type="button" data-activate="${item.version_id}">Make active</button>`}
              </div>`).join("")}
          </div>
          <div class="tp-card">
            <h2>Replace by import</h2>
            <p class="tp-muted tp-small">Imports a whole new map as the next version.</p>
            ${importForm("Import")}
          </div>
        </div>
      </div>`;
  }

  function importForm(label) {
    return `
      <div class="tp-form">
        <textarea class="tp-textarea code" data-import placeholder='[{"key": "radio_job", "name": "Radio", "description": "Praca w Radiu Kraków: dyżury, grafik…", "family": "Praca", "color": "#f97316"}]'></textarea>
        <div class="tp-row">
          <input class="tp-input" data-import-file type="file" accept=".json,application/json">
          <span class="tp-spacer"></span>
          <input class="tp-input" data-import-note placeholder="Note (optional)">
          <button class="tp-btn primary" type="button" data-import-go>${label}</button>
        </div>
      </div>`;
  }

  function topicRow(topic, count) {
    const editing = state.editing === topic.key && !topic.builtIn;
    const original = state.overview.active_version.topics.find((item) => item.key === topic.key);
    const changed = original && !topic.builtIn && ["name", "description", "family", "color"].some((field) => original[field] !== topic[field]);
    return `
      <div class="tp-topic${changed ? " is-dirty" : ""}${!original ? " is-new" : ""}">
        <span class="tp-dot" style="background:${esc(topic.color)}"></span>
        <div>
          <span class="tp-topic-name">${esc(topic.name)}</span><span class="tp-topic-key">${esc(topic.key)}</span>
          <div class="tp-topic-desc">${esc(topic.description)}</div>
        </div>
        <div class="tp-count">${count} ch.<br>${topic.builtIn ? "" : `<button class="tp-link" type="button" data-edit="${esc(topic.key)}">${editing ? "close" : "edit"}</button>`}</div>
        ${editing ? `
          <div class="tp-topic-edit">
            <div class="tp-form-2">
              <label class="tp-field">Name<input class="tp-input" data-field="name" value="${esc(topic.name)}" maxlength="60"></label>
              <label class="tp-field">Family<input class="tp-input" data-field="family" value="${esc(topic.family)}" maxlength="40"></label>
            </div>
            ${original ? "" : `<label class="tp-field">Key (snake_case, cannot change later)<input class="tp-input mono" data-field="key" value="${esc(topic.key)}" maxlength="48"></label>`}
            <label class="tp-field">Description (what Jev reads)<textarea class="tp-textarea" data-field="description" maxlength="600">${esc(topic.description)}</textarea></label>
            <div class="tp-row">
              <label class="tp-field">Color<input class="tp-input" data-field="color" type="color" value="${esc(topic.color)}" style="width:70px;padding:2px"></label>
              <span class="tp-spacer"></span>
              ${count === 0 ? `<button class="tp-btn danger small" type="button" data-remove="${esc(topic.key)}">Remove topic</button>` : '<span class="tp-muted tp-small">A topic with chapters cannot be removed.</span>'}
            </div>
          </div>` : ""}
      </div>`;
  }

  function resetDraft() {
    const version = state.overview.active_version;
    state.draft = { versionId: version.version_id, topics: version.topics.filter((t) => t.key !== state.assignments?.pool_key && t.key !== "unsorted").map((t) => ({ ...t })) };
    state.editing = null;
  }

  function draftChanged() {
    const original = state.overview.active_version.topics.filter((t) => t.key !== "unsorted");
    return JSON.stringify(original) !== JSON.stringify(state.draft.topics);
  }

  function maxVersion() {
    return Math.max(0, ...state.overview.versions.map((item) => item.version_id));
  }

  function bindMap() {
    for (const button of dom.view.querySelectorAll("[data-import-go]")) {
      button.addEventListener("click", () => {
        const form = button.closest(".tp-form");
        const text = form.querySelector("[data-import]").value.trim();
        const note = form.querySelector("[data-import-note]").value.trim();
        act(async () => {
          let topicsPayload;
          try { topicsPayload = JSON.parse(text); } catch (_) { throw new Error("That is not valid JSON."); }
          const version = state.overview.active_version;
          await request("/admin/api/topics/versions", { method: "POST", body: { topics: topicsPayload, note, origin: "import", parent_id: version ? version.version_id : null } });
          state.draft = null;
          await refresh();
          toast("Map imported and active.");
        });
      });
    }
    for (const input of dom.view.querySelectorAll("[data-import-file]")) {
      input.addEventListener("change", async () => {
        const file = input.files?.[0];
        if (file) input.closest(".tp-form").querySelector("[data-import]").value = await file.text();
      });
    }
    for (const button of dom.view.querySelectorAll("[data-edit]")) {
      button.addEventListener("click", () => {
        state.editing = state.editing === button.dataset.edit ? null : button.dataset.edit;
        render();
      });
    }
    for (const input of dom.view.querySelectorAll(".tp-topic-edit [data-field]")) {
      input.addEventListener("change", () => {
        const topic = state.draft.topics.find((item) => item.key === state.editing);
        if (!topic) return;
        topic[input.dataset.field] = input.value.trim();
        if (input.dataset.field === "key") state.editing = topic.key;
        render();
      });
    }
    dom.view.querySelector("[data-add-topic]")?.addEventListener("click", () => {
      let index = 1;
      while (state.draft.topics.some((t) => t.key === `new_topic_${index}`)) index += 1;
      const key = `new_topic_${index}`;
      state.draft.topics.push({ key, name: "New topic", description: "Describe what belongs here.", family: "Other", color: "#7a7df0" });
      state.editing = key;
      render();
    });
    for (const button of dom.view.querySelectorAll("[data-remove]")) {
      button.addEventListener("click", () => {
        state.draft.topics = state.draft.topics.filter((t) => t.key !== button.dataset.remove);
        state.editing = null;
        render();
      });
    }
    dom.view.querySelector("[data-discard]")?.addEventListener("click", () => { resetDraft(); render(); });
    dom.view.querySelector("[data-save-map]")?.addEventListener("click", () => {
      const note = dom.view.querySelector("[data-note]").value.trim();
      act(async () => {
        await request("/admin/api/topics/versions", { method: "POST", body: { topics: state.draft.topics, note, parent_id: state.overview.active_version.version_id } });
        state.draft = null;
        await refresh();
        toast("Saved as a new active version. Classify again to use it.");
      });
    });
    for (const button of dom.view.querySelectorAll("[data-activate]")) {
      button.addEventListener("click", () => act(async () => {
        const ok = await window.adminConfirmation.confirm({
          title: `Make v${button.dataset.activate} active?`,
          message: "Results shown everywhere switch to that version's classification.",
          confirmLabel: "Make active",
        });
        if (!ok) return;
        await request(`/admin/api/topics/versions/${button.dataset.activate}/activate`, { method: "POST", body: {} });
        state.draft = null;
        await refresh();
      }));
    }
  }

  // ---------- 2. classify ----------

  function renderClassify() {
    const o = state.overview;
    if (!o.active_version) return '<h1>Classify</h1><p class="tp-sub">Import a map first.</p>';
    const counts = o.counts;
    const run = activeRun();
    const scope = state.scope;
    const options = topics().map((t) => `<option value="${esc(t.key)}"${scope.topic_key === t.key ? " selected" : ""}>${esc(t.name)}</option>`).join("");
    const est = state.estimate;
    const blockedByRun = run && ["queued", "running", "paused"].includes(run.status);
    return `
      <h1>Classify</h1>
      <p class="tp-sub">Jev reads three things per chapter: its title, the session title and the group name. Never the transcript.
      ${o.settings.include_sensitive ? "Sensitive groups are included." : "Chapters in sensitive groups are skipped (change it in Settings)."}</p>
      ${run ? runCard(run) : ""}
      <div class="tp-card">
        <h2>What to send</h2>
        <div class="tp-scope">
          <label><input type="radio" name="scope" value="unclassified"${scope.mode === "unclassified" ? " checked" : ""}>
            <span><b>New and renamed chapters</b><span>${counts.unclassified} without a topic on v${o.active_version.version_id}, ${counts.changed} renamed since their run.</span></span></label>
          <label><input type="radio" name="scope" value="all"${scope.mode === "all" ? " checked" : ""}>
            <span><b>All chapters</b><span>Classify everything again on this map version.</span></span></label>
          <label><input type="radio" name="scope" value="topic"${scope.mode === "topic" ? " checked" : ""}>
            <span><b>One topic again</b><span><select class="tp-select" data-scope-topic>${options}</select></span></span></label>
        </div>
        <div class="tp-estimate">
          ${est ? `
            <div class="tp-stat"><b>${est.send_count}</b><span>chapters to send</span></div>
            <div class="tp-stat"><b>${est.skipped_sensitive}</b><span>sensitive skipped</span></div>
            <div class="tp-stat"><b>≈ ${usd(est.estimated_cost_usd)}</b><span>${est.measured ? "from your last runs" : "default estimate"} · ${usd(est.cost_per_chapter_usd, 5)} per chapter</span></div>`
            : `<span class="tp-muted">${esc(state.estimateError || "Estimating…")}</span>`}
        </div>
        <div class="tp-row">
          <button class="tp-btn primary" type="button" data-start ${!o.key_set || !o.enabled || blockedByRun || !est || !est.send_count ? "disabled" : ""}>Start classification</button>
          ${blockedByRun ? '<span class="tp-muted tp-small">Finish, resume or close the current run first.</span>' : ""}
        </div>
      </div>
      <div class="tp-card">
        <h2>Recent runs</h2>
        ${o.runs.length ? `<table class="tp-runs"><thead><tr><th>Run</th><th>Map</th><th>Status</th><th>Done</th><th>Cost</th><th>Started</th><th></th></tr></thead><tbody>
          ${o.runs.map((r) => `<tr><td class="mono">#${r.run_id}</td><td>v${r.version_id}</td><td><span class="tp-status ${r.status}">${r.status}</span></td>
            <td>${r.done_count}/${r.total_count}${r.failed_count ? ` <span style="color:var(--danger)">· ${r.failed_count} failed</span>` : ""}</td>
            <td class="mono">${usd(r.cost_usd)}</td><td>${when(r.created_at)}</td>
            <td><a href="/admin/api/topics/runs/${r.run_id}/export">JSON</a></td></tr>`).join("")}
        </tbody></table>` : '<p class="tp-empty">No runs yet.</p>'}
      </div>`;
  }

  function runCard(run) {
    const live = ["queued", "running"].includes(run.status);
    const done = run.total_count ? (run.done_count / run.total_count) * 100 : 0;
    const failed = run.total_count ? (run.failed_count / run.total_count) * 100 : 0;
    return `
      <div class="tp-card">
        <div class="tp-run-head">
          ${live ? '<span class="tp-spinner"></span>' : ""}
          <h2 style="margin:0">Run #${run.run_id}</h2>
          <span class="tp-status ${run.status}">${run.status}</span>
          <span class="tp-spacer"></span>
          ${live ? '<button class="tp-btn" type="button" data-pause>Pause</button>' : ""}
          ${run.status === "paused" ? '<button class="tp-btn" type="button" data-close-run>Close as is</button><button class="tp-btn primary" type="button" data-resume>Resume</button>' : ""}
        </div>
        <div class="tp-progress"><i style="width:${done}%"></i><i class="failed" style="width:${failed}%"></i></div>
        <div class="tp-row tp-small tp-muted" style="margin-top:8px">
          <span>${run.done_count} of ${run.total_count} done</span>
          ${run.failed_count ? `<span style="color:var(--danger)">${run.failed_count} failed</span>` : ""}
          ${run.skipped_count ? `<span>${run.skipped_count} sensitive skipped</span>` : ""}
          <span>cost ${usd(run.cost_usd)}</span>
          <span>map v${run.version_id}</span>
        </div>
        ${run.error_message ? `<p class="tp-small" style="color:var(--warn);margin:10px 0 0">${esc(run.error_message)}</p>` : ""}
      </div>`;
  }

  async function estimate() {
    state.estimate = null;
    state.estimateError = "";
    try {
      state.estimate = (await request("/admin/api/topics/estimate", { method: "POST", body: { scope: state.scope } })).estimate;
    } catch (error) {
      state.estimateError = error.message;
    }
    if (state.step === "classify") render();
  }

  function bindClassify() {
    for (const radio of dom.view.querySelectorAll('input[name="scope"]')) {
      radio.addEventListener("change", () => {
        const select = dom.view.querySelector("[data-scope-topic]");
        state.scope = radio.value === "topic" ? { mode: "topic", topic_key: select.value } : { mode: radio.value };
        estimate();
      });
    }
    dom.view.querySelector("[data-scope-topic]")?.addEventListener("change", (event) => {
      state.scope = { mode: "topic", topic_key: event.target.value };
      estimate();
    });
    if (!state.estimate && !state.estimateError && state.overview.active_version) estimate();
    dom.view.querySelector("[data-start]")?.addEventListener("click", () => act(async () => {
      const est = state.estimate;
      const ok = await window.adminConfirmation.confirm({
        title: `Send ${est.send_count} chapters to Jev?`,
        message: `Estimated cost ${usd(est.estimated_cost_usd)} on OpenRouter. You can pause at any time.`,
        confirmLabel: "Start",
      });
      if (!ok) return;
      await request("/admin/api/topics/runs", { method: "POST", body: { scope: state.scope } });
      await refresh();
    }));
    dom.view.querySelector("[data-pause]")?.addEventListener("click", () => act(async () => {
      await request(`/admin/api/topics/runs/${activeRun().run_id}/pause`, { method: "POST", body: {} });
      toast("Pausing after the current batch.");
      await refresh();
    }));
    dom.view.querySelector("[data-resume]")?.addEventListener("click", () => act(async () => {
      await request(`/admin/api/topics/runs/${activeRun().run_id}/resume`, { method: "POST", body: {} });
      await refresh();
    }));
    dom.view.querySelector("[data-close-run]")?.addEventListener("click", () => act(async () => {
      const run = activeRun();
      const ok = await window.adminConfirmation.confirm({
        title: `Close run #${run.run_id}?`,
        message: `${run.done_count} chapters keep their topics; the other ${run.total_count - run.done_count} stay unclassified.`,
        confirmLabel: "Close run",
      });
      if (!ok) return;
      await request(`/admin/api/topics/runs/${run.run_id}/close`, { method: "POST", body: {} });
      state.estimate = null;
      await refresh();
    }));
  }

  // ---------- 3. results ----------

  // Sessions lead: chapter titles are synthetic and only make sense under the session they belong to.
  function filteredChapters() {
    const f = state.filters;
    const query = f.query.trim().toLowerCase();
    return (state.assignments?.chapters || []).filter((row) => {
      if (f.topic === "__none") { if (row.topic_key) return false; }
      else if (f.topic && row.topic_key !== f.topic) return false;
      if (!f.sensitive && row.sensitive) return false;
      if (f.minConf && (row.confidence == null || row.confidence < f.minConf)) return false;
      if (query && !`${row.title} ${row.session_title}`.toLowerCase().includes(query)) return false;
      return true;
    });
  }

  function sessionTree(rows) {
    const sessions = new Map();
    for (const row of rows) {
      let session = sessions.get(row.session_id);
      if (!session) {
        session = { id: row.session_id, title: row.session_title, group: row.group_name, color: row.group_color,
          icon: row.group_icon, sensitive: row.sensitive, updated: row.session_updated_at || 0, chapters: [] };
        sessions.set(row.session_id, session);
      }
      session.chapters.push(row);
    }
    for (const session of sessions.values()) {
      session.chapters.sort((a, b) => (a.first_exchange_id ?? 0) - (b.first_exchange_id ?? 0));
    }
    return [...sessions.values()].sort((a, b) => b.updated - a.updated);
  }

  function filtersActive() {
    return Boolean(state.filters.topic || state.filters.query.trim() || state.filters.minConf);
  }

  function sessionOpen(id) {
    if (state.tree.open.has(id)) return true;
    if (state.tree.closed.has(id)) return false;
    return filtersActive();
  }

  function resetTree() {
    state.tree = { open: new Set(), closed: new Set() };
  }

  function renderResults() {
    const data = state.assignments;
    if (!data?.version) return '<h1>Results</h1><p class="tp-sub">Import a map and classify first.</p>';
    const rows = data.chapters;
    const counts = topicCounts();
    const f = state.filters;
    const shown = filteredChapters();
    const tree = sessionTree(shown);
    const unclassified = rows.filter((row) => !row.topic_key).length;
    const chips = topics().filter((t) => counts[t.key]).map((t) => `
      <button class="tp-chip" type="button" data-chip="${esc(t.key)}" aria-pressed="${f.topic === t.key}"><i style="background:${esc(t.color)}"></i>${esc(t.name)} <small>${counts[t.key]}</small></button>`).join("");
    return `
      <h1>Results <span class="tp-tag">map v${data.version.version_id}</span></h1>
      <p class="tp-sub">Sessions, newest first, with the chapters Jev placed. Pick a topic to see which sessions it is made of.</p>
      <div class="tp-chips">
        <button class="tp-chip" type="button" data-chip="" aria-pressed="${!f.topic}">All <small>${rows.length}</small></button>
        ${chips}
        ${unclassified ? `<button class="tp-chip" type="button" data-chip="__none" aria-pressed="${f.topic === "__none"}">Not classified <small>${unclassified}</small></button>` : ""}
      </div>
      <div class="tp-filters">
        <input class="tp-input" data-query type="search" placeholder="Search session or chapter titles" value="${esc(f.query)}" style="min-width:260px">
        <label class="tp-check">Min. confidence
          <select class="tp-select" data-minconf>${[0, 0.4, 0.6, 0.8].map((v) => `<option value="${v}"${f.minConf === v ? " selected" : ""}>${v ? pct(v) : "any"}</option>`).join("")}</select></label>
        <label class="tp-check"><input type="checkbox" data-sensitive${f.sensitive ? " checked" : ""}>Show sensitive groups</label>
        <span class="tp-spacer"></span>
        <span class="tp-muted tp-small">${tree.length} sessions · ${shown.length} chapters</span>
        <button class="tp-link" type="button" data-tree="open">Expand all</button>
        <button class="tp-link" type="button" data-tree="close">Collapse all</button>
      </div>
      <div class="tp-tree" role="tree">
        ${tree.map(sessionNode).join("") || '<p class="tp-empty">Nothing matches.</p>'}
      </div>`;
  }

  function sessionNode(session) {
    const open = sessionOpen(session.id);
    const topicKeys = [...new Set(session.chapters.map((c) => c.topic_key).filter(Boolean))];
    const dots = topicKeys.map((key) => {
      const topic = topicByKey(key);
      return `<i title="${esc(topic.name)}" style="background:${esc(topic.color)}"></i>`;
    }).join("");
    const icon = window.BridgeIdentity ? window.BridgeIdentity.groupIconSvg(session.icon) : "";
    const eye = session.sensitive && window.BridgeIdentity ? `<span class="tp-eye">${window.BridgeIdentity.sensitiveIconSvg()}</span>` : "";
    return `
      <div class="tp-node${open ? " is-open" : ""}" role="treeitem" aria-expanded="${open}">
        <button class="tp-session" type="button" data-session="${esc(session.id)}">
          <span class="tp-chev" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"></path></svg></span>
          <span class="tp-gicon" style="--g:${esc(session.color)}" title="${esc(session.group)}">${icon}${eye}</span>
          <span class="tp-session-title">${esc(session.title)}</span>
          <span class="tp-dots">${dots}</span>
          <span class="tp-session-count">${session.chapters.length}</span>
        </button>
        ${open ? `<div class="tp-children" role="group">${session.chapters.map(chapterLeaf).join("")}</div>` : ""}
      </div>`;
  }

  function chapterLeaf(row) {
    const topic = row.topic_key ? topicByKey(row.topic_key) : null;
    const second = row.second ? topicByKey(row.second.key) : null;
    const link = `/admin/sessions?session=${encodeURIComponent(row.session_id)}&chapter=${row.strand_id}`;
    return `
      <div class="tp-leaf" role="treeitem">
        <a class="tp-leaf-title" href="${link}" title="Open in the session">${esc(row.title)}${row.changed ? ' <span class="tp-renamed">renamed</span>' : ""}</a>
        <span>${topic ? `<span class="tp-topic-pill"><i style="background:${esc(topic.color)}"></i>${esc(topic.name)}</span>` : '<span class="tp-muted">not classified</span>'}</span>
        <span>${row.confidence != null ? `<span class="tp-conf"><span><i style="width:${pct(row.confidence)}"></i></span>${pct(row.confidence)}</span>` : ""}</span>
        <span>${second ? `<span class="tp-topic-pill tp-second"><i style="background:${esc(second.color)}"></i>${esc(second.name)} ${pct(row.second.p)}</span>` : ""}</span>
      </div>`;
  }

  function bindResults() {
    for (const chip of dom.view.querySelectorAll("[data-chip]")) {
      chip.addEventListener("click", () => { state.filters.topic = chip.dataset.chip; resetTree(); render(); });
    }
    const query = dom.view.querySelector("[data-query]");
    query?.addEventListener("input", () => {
      state.filters.query = query.value;
      resetTree();
      const caret = query.selectionStart;
      render();
      const again = dom.view.querySelector("[data-query]");
      again.focus();
      again.setSelectionRange(caret, caret);
    });
    dom.view.querySelector("[data-minconf]")?.addEventListener("change", (event) => { state.filters.minConf = Number(event.target.value); resetTree(); render(); });
    dom.view.querySelector("[data-sensitive]")?.addEventListener("change", (event) => { state.filters.sensitive = event.target.checked; render(); });
    for (const button of dom.view.querySelectorAll("[data-session]")) {
      button.addEventListener("click", () => {
        const id = button.dataset.session;
        const open = sessionOpen(id);
        state.tree.open.delete(id);
        state.tree.closed.delete(id);
        (open ? state.tree.closed : state.tree.open).add(id);
        render();
      });
    }
    for (const button of dom.view.querySelectorAll("[data-tree]")) {
      button.addEventListener("click", () => {
        const ids = sessionTree(filteredChapters()).map((session) => session.id);
        resetTree();
        for (const id of ids) (button.dataset.tree === "open" ? state.tree.open : state.tree.closed).add(id);
        render();
      });
    }
  }

  // ---------- journal ----------

  function renderJournal() {
    return `
      <h1>Journal</h1>
      <p class="tp-sub">Every step and decision, newest first. The same log is readable on the server with <span class="mono">mcp-bridge topics journal</span>.</p>
      <div class="tp-journal">
        ${state.journal.map((entry) => `
          <div class="tp-entry">
            <time>${when(entry.created_at)}</time>
            <span class="tp-actor ${esc(entry.actor)}">${esc(entry.actor)}</span>
            <div><code>${esc(entry.action)}</code>${esc(entry.message)}${entry.run_id ? ` <a href="/admin/api/topics/runs/${entry.run_id}/export">run #${entry.run_id} JSON</a>` : ""}</div>
          </div>`).join("") || '<p class="tp-empty">Nothing yet.</p>'}
      </div>`;
  }

  // ---------- settings ----------

  function renderSettings() {
    const o = state.overview;
    const s = o.settings;
    return `
      <h1>Settings</h1>
      <p class="tp-sub">Jev runs on OpenRouter with your own key. The key is stored encrypted and never shown again; runs, exports and the journal never contain it.</p>
      <div class="tp-card">
        <h2>OpenRouter API key</h2>
        <p class="tp-muted tp-small">Status: ${o.key_set ? '<span style="color:var(--ok)">saved</span>' : "not set"}</p>
        <div class="tp-row">
          <input class="tp-input" data-key type="password" autocomplete="off" placeholder="sk-or-…" style="flex:1;min-width:260px">
          <button class="tp-btn primary" type="button" data-save-key>Save key</button>
          ${o.key_set ? '<button class="tp-btn danger" type="button" data-remove-key>Remove</button>' : ""}
        </div>
      </div>
      <div class="tp-card">
        <h2>Classification</h2>
        <div class="tp-form">
          <label class="tp-field">Jev model<input class="tp-input mono" data-setting="jev_model" value="${esc(s.jev_model)}"></label>
          <label class="tp-check"><input type="checkbox" data-setting="include_sensitive"${s.include_sensitive ? " checked" : ""}>Include chapters from sensitive groups (their titles and group name go to OpenRouter)</label>
          <div><button class="tp-btn" type="button" data-save-settings>Save settings</button></div>
        </div>
      </div>`;
  }

  function bindSettings() {
    dom.view.querySelector("[data-save-key]")?.addEventListener("click", () => act(async () => {
      const value = dom.view.querySelector("[data-key]").value.trim();
      if (!value) throw new Error("Paste the key first.");
      await request("/admin/api/topics/key", { method: "PUT", body: { api_key: value } });
      await refresh();
      toast("Key saved.");
    }));
    dom.view.querySelector("[data-remove-key]")?.addEventListener("click", () => act(async () => {
      const ok = await window.adminConfirmation.confirm({ title: "Remove the OpenRouter key?", message: "Runs stop working until you add a key again.", confirmLabel: "Remove", tone: "danger" });
      if (!ok) return;
      await request("/admin/api/topics/key", { method: "PUT", body: { api_key: "" } });
      await refresh();
    }));
    dom.view.querySelector("[data-save-settings]")?.addEventListener("click", () => act(async () => {
      const body = {
        jev_model: dom.view.querySelector('[data-setting="jev_model"]').value.trim(),
        include_sensitive: dom.view.querySelector('[data-setting="include_sensitive"]').checked,
      };
      await request("/admin/api/topics/settings", { method: "PUT", body });
      state.estimate = null;
      await refresh();
      toast("Settings saved.");
    }));
  }

  const bind = { map: bindMap, classify: bindClassify, results: bindResults, settings: bindSettings };

  // ---------- start ----------

  async function load() {
    try {
      const me = await request("/admin/api/me");
      state.csrf = me.csrf_token;
      await refresh();
    } catch (error) {
      dom.view.innerHTML = `<p class="tp-empty">${esc(error.message)}</p>`;
    }
  }

  load();
})();
