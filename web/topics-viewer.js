/* ------------------------------------------------------------------
   MCP Session Bridge — Topics

   One main view: the strip (Manage topics → Sort new, List | Graph), topics
   on the left, the selected topic as a session tree on the right. Taxonomy
   work happens in the Manage topics overlay. Nothing runs on a timer except
   polling a step the owner started.
------------------------------------------------------------------- */
(() => {
  "use strict";

  const POOL = "unsorted";
  const POLL_MS = 1500;
  const WEEKS = 12;
  const WEEK_SECONDS = 7 * 24 * 3600;
  const SECOND_MIN = 0.2;
  const PALETTE = ["#7a7df0", "#a5a8f6", "#5457e5", "#22d3ee", "#67e8f9", "#2dd4bf", "#38bdf8", "#0ea5e9", "#fbbf24", "#fcd34d", "#f59e0b", "#fb923c", "#f97316", "#fdba74", "#34d399", "#6ee7b7", "#10b981", "#a7f3d0", "#059669", "#bef264", "#f472b6", "#fb7185", "#c084fc", "#e879f9", "#94a3b8", "#fca5a5"];
  const MARK = '<svg class="tp-mark" viewBox="0 0 24 24" role="img" aria-label="Codex"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"></path></svg>';
  const CHEV = '<span class="tp-chev" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"></path></svg></span>';
  const ARROW = '<span class="arrow" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"></path></svg></span>';
  const SOURCE = { find: "from Find topics", refine: "from Refine topics", owner: "your edit" };
  const KIND = { first_map: "First map", new_topic: "New topic", split: "Split", redescribe: "Description", edit: "Your edit" };

  const state = {
    csrf: "",
    overview: null,
    chapters: [],
    proposals: [],
    journal: [],
    view: readPref("view", "list"),
    selected: null,
    openSessions: {},
    manage: { section: "proposals", detail: "balanced", note: "", focus: {}, drafts: {}, editKey: null, editDraft: null, editProposal: null, importOpen: false },
    graph: { second: true, sensitive: true, query: "", focus: null },
    pollTimer: 0,
    busy: false,
    lastJobKey: "",
  };

  const dom = {
    strip: document.getElementById("strip"),
    listView: document.getElementById("listView"),
    topicList: document.getElementById("topicList"),
    detail: document.getElementById("topicDetail"),
    graphView: document.getElementById("graphView"),
    graphSide: document.getElementById("graphSide"),
    graphInfo: document.getElementById("graphInfo"),
    graph: document.getElementById("graph"),
    graphLegend: document.getElementById("graphLegend"),
    manage: document.getElementById("manage"),
    manageMeta: document.getElementById("manageMeta"),
    manageNav: document.getElementById("manageNav"),
    manageMain: document.getElementById("manageMain"),
    manageClose: document.getElementById("manageClose"),
    tip: document.getElementById("tip"),
    toast: document.getElementById("toast"),
  };

  // ---------- helpers ----------

  function readPref(key, fallback) {
    try { return localStorage.getItem(`mcp-topics:${key}`) || fallback; } catch (_) { return fallback; }
  }
  function writePref(key, value) {
    try { localStorage.setItem(`mcp-topics:${key}`, value); } catch (_) { /* private mode */ }
  }
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }
  const pct = (value) => (value == null ? "–" : `${Math.round(value * 100)}%`);
  const usd = (value, digits = 3) => `$${Number(value || 0).toFixed(digits)}`;
  function ago(seconds) {
    if (!seconds) return "";
    const diff = Math.max(0, Date.now() / 1000 - seconds);
    if (diff < 3600) return "just now";
    if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
    const days = Math.round(diff / 86400);
    if (days < 14) return `${days} day${days === 1 ? "" : "s"} ago`;
    const weeks = Math.round(days / 7);
    if (weeks < 9) return `${weeks} weeks ago`;
    return `${Math.round(days / 30)} months ago`;
  }
  function when(seconds) {
    return seconds ? new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  }

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["content-type"] = "application/json";
    if (options.method && options.method !== "GET") headers["x-csrf-token"] = state.csrf;
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", body: options.body === undefined ? undefined : JSON.stringify(options.body) });
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
    toastTimer = window.setTimeout(() => { dom.toast.hidden = true; }, isError ? 6500 : 3500);
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

  function confirm(options) {
    return window.adminConfirmation ? window.adminConfirmation.confirm(options) : Promise.resolve(window.confirm(options.message));
  }

  // ---------- derived data ----------

  function version() { return state.overview?.active_version || null; }
  function topics() { return (version()?.topics || []).filter((t) => t.key !== POOL); }
  function topicInfo(key) {
    if (!key || key === POOL) return { key: POOL, name: "Unsorted", color: "#4b5563", family: "", description: "Chapters that fit no topic yet." };
    return version()?.topics.find((t) => t.key === key) || { key, name: key, color: "#94a3b8", family: "", description: "" };
  }
  function jevModel() { return state.overview?.settings?.jev_model?.split("/").pop() || "Jev"; }

  function byTopic() {
    const map = new Map();
    for (const row of state.chapters) {
      if (version() && !row.topic_key) continue;
      const key = version() ? row.topic_key : POOL;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(row);
    }
    return map;
  }

  function sessionTopics() {
    const map = new Map();
    for (const row of state.chapters) {
      if (!row.topic_key) continue;
      if (!map.has(row.session_id)) map.set(row.session_id, new Set());
      map.get(row.session_id).add(row.topic_key);
    }
    return map;
  }

  function weeksFor(rows, now) {
    const buckets = Array.from({ length: WEEKS }, () => new Set());
    for (const row of rows) {
      if (!row.first_at) continue;
      const age = Math.floor((now - row.first_at) / WEEK_SECONDS);
      if (age >= 0 && age < WEEKS) buckets[WEEKS - 1 - age].add(row.session_id);
    }
    return buckets.map((set) => set.size);
  }

  function sessionsOf(rows) {
    const sessions = new Map();
    for (const row of rows) {
      let s = sessions.get(row.session_id);
      if (!s) {
        s = { id: row.session_id, title: row.session_title, group: row.group_name, color: row.group_color, icon: row.group_icon, sensitive: row.sensitive, updated: row.session_updated_at || 0, rows: [] };
        sessions.set(row.session_id, s);
      }
      s.rows.push(row);
    }
    for (const s of sessions.values()) s.rows.sort((a, b) => (a.first_exchange_id ?? 0) - (b.first_exchange_id ?? 0));
    return [...sessions.values()].sort((a, b) => b.updated - a.updated);
  }

  function groupIcon(session) {
    const svg = window.BridgeIdentity ? window.BridgeIdentity.groupIconSvg(session.icon || "folder") : "";
    const eye = session.sensitive && window.BridgeIdentity ? `<span class="tp-eye">${window.BridgeIdentity.sensitiveIconSvg()}</span>` : "";
    return `<span class="tp-gicon" style="--g:${esc(session.color || "#8e93a8")}" title="${esc(session.group || "")}">${svg}${eye}</span>`;
  }

  function sortCount() {
    const counts = state.overview?.counts || {};
    return (counts.unclassified || 0) + (counts.changed || 0);
  }

  function jobRunning() { return Boolean(state.overview?.running); }

  // ---------- loading ----------

  async function loadOverview() { state.overview = await request("/admin/api/topics"); }
  async function loadChapters() { state.chapters = (await request("/admin/api/topics/assignments")).chapters; }
  async function loadProposals() { state.proposals = (await request("/admin/api/topics/proposals")).proposals; }
  async function loadJournal() { state.journal = (await request("/admin/api/topics/journal")).journal; }

  async function refresh(full = true) {
    const before = state.lastJobKey;
    await loadOverview();
    const job = state.overview.job;
    const run = state.overview.active_run;
    const jobKey = job ? `${job.kind}:${job.step}:${job.proposal_id || ""}` : run ? `${run.run_id}:${run.status}` : "";
    const changed = jobKey !== before;
    state.lastJobKey = jobKey;
    if (full || changed || !jobRunning()) {
      await Promise.all([loadChapters(), loadProposals(), dom.manage.open ? loadJournal() : Promise.resolve()]);
    }
    schedulePoll();
    render();
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    const run = state.overview?.active_run;
    if (jobRunning() || (run && ["queued", "running"].includes(run.status))) {
      state.pollTimer = window.setTimeout(() => refresh(false).catch(() => {}), POLL_MS);
    }
  }

  // ---------- render ----------

  function render() {
    if (!state.overview) return;
    renderStrip();
    const graph = state.view === "graph" && version();
    dom.listView.hidden = Boolean(graph);
    dom.graphView.hidden = !graph;
    if (graph) renderGraph(); else { renderTopicList(); renderDetail(); }
    if (dom.manage.open) renderManage();
  }

  function renderStrip() {
    const o = state.overview;
    const v = version();
    const n = sortCount();
    const run = o.active_run;
    const job = o.job;
    const pending = o.pending_proposals || 0;
    const status = v ? `map v${v.version_id} · ${v.topic_count} topics · ${o.counts.chapters} chapters` : `no topics yet · ${o.counts.chapters} chapters`;
    let live = "";
    if (job && job.kind !== "classify") {
      const label = job.step === "testing" ? "Testing a proposal" : "Proposing topics";
      const progress = job.progress ? ` ${job.progress.done} / ${job.progress.total}` : "";
      live = `<span class="tp-progress-pill"><span class="tp-spinner"></span>${label}${progress}</span>`;
    } else if (run && ["queued", "running"].includes(run.status)) {
      const done = run.total_count ? Math.round((run.done_count / run.total_count) * 100) : 0;
      live = `<span class="tp-progress-pill"><span class="tp-spinner"></span>Sorting ${run.done_count} / ${run.total_count}<span class="bar"><i style="width:${done}%"></i></span><span class="cost">${usd(run.cost_usd, 4)}</span><button type="button" class="tp-btn small" data-act="pause">Pause</button></span>`;
    } else if (run && run.status === "paused") {
      live = `<span class="tp-paused-pill" title="${esc(run.error_message || "")}">Sorting paused ${run.done_count} / ${run.total_count}<button type="button" class="tp-btn small" data-act="close-run">Close</button><button type="button" class="tp-btn small cyan" data-act="resume">Resume</button></span>`;
    }
    const manageClass = v ? "tp-btn" : "tp-btn primary glow";
    const sortDisabled = !v || n === 0 || jobRunning() || (run && run.status === "paused");
    dom.strip.innerHTML = `
      <span class="status">${esc(status)}</span>
      <button type="button" class="${manageClass}" data-act="manage">${v ? "" : '<span class="step">1</span>'}Manage topics${pending ? `<span class="tp-badge">${pending}</span>` : ""}</button>
      ${ARROW}
      <button type="button" class="tp-btn ${n && v ? "cyan" : ""}" data-act="sort" ${sortDisabled ? "disabled" : ""}>${v ? "" : '<span class="step">2</span>'}Sort new${n && v ? ` · ${n}` : ""} <span class="model">(${esc(jevModel())})</span></button>
      ${live}
      <span class="tp-spacer"></span>
      <span class="tp-seg" role="group" aria-label="View">
        <button type="button" data-view="list" aria-pressed="${state.view !== "graph" || !v}">List</button>
        <button type="button" data-view="graph" aria-pressed="${state.view === "graph" && Boolean(v)}" ${v ? "" : "disabled"}>Graph</button>
      </span>`;
    dom.strip.querySelector('[data-act="manage"]').addEventListener("click", () => openManage());
    dom.strip.querySelector('[data-act="sort"]').addEventListener("click", sortNew);
    dom.strip.querySelector('[data-act="pause"]')?.addEventListener("click", () => act(async () => {
      await request(`/admin/api/topics/runs/${run.run_id}/pause`, { method: "POST", body: {} });
      toast("Pausing after the current batch.");
      await refresh();
    }));
    dom.strip.querySelector('[data-act="resume"]')?.addEventListener("click", () => act(async () => {
      await request(`/admin/api/topics/runs/${run.run_id}/resume`, { method: "POST", body: {} });
      await refresh();
    }));
    dom.strip.querySelector('[data-act="close-run"]')?.addEventListener("click", () => act(async () => {
      if (!(await confirm({ title: "Close the paused run?", message: `${run.done_count} chapters keep their topics; the rest wait for the next Sort new.`, confirmLabel: "Close run" }))) return;
      await request(`/admin/api/topics/runs/${run.run_id}/close`, { method: "POST", body: {} });
      await refresh();
    }));
    for (const button of dom.strip.querySelectorAll("[data-view]")) {
      button.addEventListener("click", () => { state.view = button.dataset.view; writePref("view", state.view); render(); });
    }
  }

  async function sortNew() {
    await act(async () => {
      const estimate = (await request("/admin/api/topics/estimate", { method: "POST", body: { scope: { mode: "unclassified" } } })).estimate;
      const ok = await confirm({
        title: `Sort ${estimate.send_count} new chapter${estimate.send_count === 1 ? "" : "s"}?`,
        message: `From ${estimate.session_count} session${estimate.session_count === 1 ? "" : "s"}, on map v${estimate.version_id}. About ${usd(estimate.estimated_cost_usd, 4)} with ${estimate.model}. Progress shows in the bar; you can pause at any time.`
          + (estimate.skipped_sensitive ? ` ${estimate.skipped_sensitive} chapters in sensitive groups are skipped.` : ""),
        confirmLabel: "Start",
      });
      if (!ok) return;
      await request("/admin/api/topics/runs", { method: "POST", body: { scope: { mode: "unclassified" } } });
      await refresh();
    });
  }

  // ---------- list view ----------

  function currentSelection(groups) {
    if (state.selected && (state.selected === POOL || topics().some((t) => t.key === state.selected))) return state.selected;
    if (!version()) return POOL;
    let best = null;
    for (const t of topics()) {
      const n = new Set((groups.get(t.key) || []).map((r) => r.session_id)).size;
      if (!best || n > best.n) best = { key: t.key, n };
    }
    return best ? best.key : POOL;
  }

  function renderTopicList() {
    const groups = byTopic();
    state.selected = currentSelection(groups);
    const now = Date.now() / 1000;
    const v = version();
    const weekly = new Map(topics().map((t) => [t.key, weeksFor(groups.get(t.key) || [], now)]));
    const peak = Math.max(1, ...[...weekly.values()].flat());
    const families = [];
    for (const t of topics()) {
      let family = families.find((f) => f.name === t.family);
      if (!family) families.push(family = { name: t.family, items: [] });
      family.items.push(t);
    }
    const poolRows = groups.get(POOL) || [];
    const poolSessions = new Set(poolRows.map((r) => r.session_id)).size;
    dom.topicList.innerHTML = `
      ${v ? '<div class="tp-topics-head"><span>TOPIC</span><span>12 WEEKS</span><span style="text-align:right">SES.</span></div>' : '<div class="tp-family-name">TOPICS</div>'}
      ${families.map((family) => `
        <div class="tp-family">
          <div class="tp-family-name">${esc(family.name)}</div>
          ${family.items.map((t) => {
            const rows = groups.get(t.key) || [];
            const sessions = new Set(rows.map((r) => r.session_id)).size;
            const bars = weekly.get(t.key).map((value) => `<i style="height:${Math.max(2, (value / peak) * 18)}px;background:${esc(t.color)};opacity:${value ? 0.9 : 0.22}"></i>`).join("");
            return `<button type="button" class="tp-topic" data-topic="${esc(t.key)}" aria-current="${state.selected === t.key}">
              <span class="tp-topic-name"><i class="tp-dot" style="background:${esc(t.color)}"></i><span>${esc(t.name)}</span></span>
              <span class="tp-weeks" aria-label="Sessions per week, last 12 weeks">${bars}</span>
              <span class="tp-count">${sessions}</span></button>`;
          }).join("")}
        </div>`).join("")}
      <button type="button" class="tp-pool" data-topic="${POOL}" aria-current="${state.selected === POOL}">
        <span style="display:flex;align-items:center;gap:9px"><i class="tp-dot" style="background:#4b5563"></i><b>Unsorted</b><span class="tp-muted">${poolRows.length} ch.</span></span>
        <span class="tp-count">${poolSessions}</span>
      </button>
      ${v ? "" : '<p class="tp-empty-note">No topics yet. <b>Manage topics</b> › Find topics proposes them from subjects you came back to in two or more sessions. Then Sort new places every new chapter.</p>'}`;
    for (const button of dom.topicList.querySelectorAll("[data-topic]")) {
      button.addEventListener("click", () => { state.selected = button.dataset.topic; renderTopicList(); renderDetail(); dom.detail.scrollTop = 0; });
    }
  }

  function renderDetail() {
    const groups = byTopic();
    const key = state.selected || POOL;
    const t = topicInfo(key);
    const rows = groups.get(key) || [];
    const sessions = sessionsOf(rows);
    const sTopics = sessionTopics();
    const last = Math.max(0, ...rows.map((r) => r.session_updated_at || 0));
    const open = state.openSessions[key] || (state.openSessions[key] = new Set(sessions.slice(0, sessions.length <= 6 ? sessions.length : 3).map((s) => s.id)));
    const isPool = key === POOL;
    const v = version();
    dom.detail.innerHTML = `
      ${!state.overview.key_set ? '<div class="tp-banner">Add the OpenRouter key in Settings › Topics before sorting or testing.</div>' : ""}
      <div class="tp-detail-head">
        <div class="tp-detail-title">
          <h1><i class="tp-dot" style="background:${esc(t.color)}"></i>${esc(t.name)}</h1>
          <span class="tp-muted">${sessions.length} sessions · ${rows.length} chapters${last ? ` · last talked about ${esc(ago(last))}` : ""}</span>
          ${t.description ? `<p>${esc(t.description)}</p>` : ""}
        </div>
        ${isPool ? `<button type="button" class="tp-btn ${v ? "" : "primary"}" data-act="find">Find topics ${MARK}</button>` : '<button type="button" class="tp-btn" data-act="edit">Edit</button>'}
      </div>
      <div class="tp-tree">
        ${sessions.map((s) => sessionNode(s, open.has(s.id), sTopics.get(s.id), isPool && !v)).join("") || '<p class="tp-muted">Nothing here yet.</p>'}
      </div>`;
    dom.detail.querySelector('[data-act="edit"]')?.addEventListener("click", () => openManage("topics", key));
    dom.detail.querySelector('[data-act="find"]')?.addEventListener("click", () => openManage("proposals"));
    for (const button of dom.detail.querySelectorAll("[data-session]")) {
      button.addEventListener("click", () => {
        const id = button.dataset.session;
        if (open.has(id)) open.delete(id); else open.add(id);
        renderDetail();
      });
    }
  }

  function sessionNode(session, isOpen, topicKeys, plain) {
    const dots = [...(topicKeys || [])].map((key) => { const t = topicInfo(key); return `<i style="background:${esc(t.color)}" title="${esc(t.name)}"></i>`; }).join("");
    return `<div class="tp-node${isOpen ? " is-open" : ""}">
      <button type="button" class="tp-session" data-session="${esc(session.id)}" aria-expanded="${isOpen}">
        ${CHEV}${groupIcon(session)}
        <span class="tp-session-title">${esc(session.title)}</span>
        <span class="tp-dots">${dots}</span>
        <span class="tp-when">${esc(ago(session.updated))}</span>
        <span class="tp-count">${session.rows.length}</span>
      </button>
      ${isOpen ? `<div class="tp-children">${session.rows.map((row) => leaf(row, plain)).join("")}</div>` : ""}
    </div>`;
  }

  function leaf(row, plain) {
    const link = `/admin/sessions?session=${encodeURIComponent(row.session_id)}&chapter=${row.strand_id}`;
    const second = row.second && row.second.p >= SECOND_MIN ? topicInfo(row.second.key) : null;
    return `<div class="tp-leaf">
      <a href="${link}" title="Open in its session">${esc(row.title)}</a>
      ${plain || row.confidence == null ? "<span></span>" : `<span class="tp-conf"><span><i style="width:${pct(row.confidence)}"></i></span>${pct(row.confidence)}</span>`}
      ${second ? `<span class="tp-also"><i style="background:${esc(second.color)}"></i>${esc(second.name)} ${pct(row.second.p)}</span>` : "<span></span>"}
    </div>`;
  }

  // ---------- graph view ----------

  let graphSim = null;
  let graphParts = null;

  function renderGraph() {
    const g = state.graph;
    const rows = state.chapters.filter((r) => r.topic_key && (g.sensitive || !r.sensitive));
    const counts = new Map();
    for (const r of rows) counts.set(r.topic_key, (counts.get(r.topic_key) || 0) + 1);
    const list = [...topics(), topicInfo(POOL)].filter((t) => counts.get(t.key));
    dom.graphSide.innerHTML = `
      <label class="tp-check">Second topics (≥ 20%)<input type="checkbox" data-g="second" ${g.second ? "checked" : ""}></label>
      <label class="tp-check">Sensitive groups<input type="checkbox" data-g="sensitive" ${g.sensitive ? "checked" : ""}></label>
      <input class="tp-input" data-g="query" type="search" placeholder="Search chapters" value="${esc(g.query)}">
      <div class="tp-family-name" style="margin-top:6px">TOPICS</div>
      ${list.map((t) => `<button type="button" class="tp-gtopic" data-gtopic="${esc(t.key)}" aria-current="${g.focus === t.key}"><i class="tp-dot" style="background:${esc(t.color)}"></i><span>${esc(t.name)}</span><span class="tp-count">${counts.get(t.key)}</span></button>`).join("")}`;
    dom.graphSide.querySelector('[data-g="second"]').addEventListener("change", (e) => { g.second = e.target.checked; renderGraph(); });
    dom.graphSide.querySelector('[data-g="sensitive"]').addEventListener("change", (e) => { g.sensitive = e.target.checked; renderGraph(); });
    dom.graphSide.querySelector('[data-g="query"]').addEventListener("input", (e) => { g.query = e.target.value; highlightGraph(); });
    for (const b of dom.graphSide.querySelectorAll("[data-gtopic]")) b.addEventListener("click", () => { g.focus = g.focus === b.dataset.gtopic ? null : b.dataset.gtopic; renderGraph(); });
    dom.graphLegend.innerHTML = '<span>● topic</span><span>· chapter (closer = more confident)</span><span>- - second topic</span><span style="color:#d9b96a">○ sensitive</span>';
    renderGraphInfo(rows);
    drawGraph(rows, list, counts);
  }

  function renderGraphInfo(rows) {
    const g = state.graph;
    if (g.focus) {
      const t = topicInfo(g.focus);
      const sessions = sessionsOf(rows.filter((r) => r.topic_key === g.focus));
      dom.graphInfo.innerHTML = `<h3 style="display:flex;gap:8px;align-items:center"><i class="tp-dot" style="background:${esc(t.color)}"></i>${esc(t.name)}</h3><p class="tp-muted" style="margin:0;line-height:1.5">${esc(t.description)}</p>
        <div class="tp-family-name">SESSIONS (${sessions.length})</div>
        ${sessions.map((s) => `<div class="tp-row-session">${groupIcon(s)}<b>${esc(s.title)}</b><span class="tp-count">${s.rows.length}</span></div>`).join("")}`;
      return;
    }
    const pairs = new Map();
    for (const r of rows) {
      if (!r.second || r.second.p < SECOND_MIN || r.second.key === POOL || r.topic_key === POOL) continue;
      const id = [r.topic_key, r.second.key].sort().join("|");
      pairs.set(id, (pairs.get(id) || 0) + 1);
    }
    const top = [...pairs].sort((a, b) => b[1] - a[1]).slice(0, 12);
    dom.graphInfo.innerHTML = `<h3>How to read it</h3><p class="tp-muted" style="margin:0;line-height:1.6">Each dot is a chapter pulled toward its topic; weak ones drift outward. Dashed lines show where a chapter also leans. Click a topic to keep only its chapters; click a chapter to open it in its session.</p>
      <div class="tp-family-name" style="margin-top:8px">STRONGEST BRIDGES</div>
      ${top.map(([id, n]) => { const [a, b] = id.split("|"); return `<button type="button" class="tp-pair" data-gtopic="${esc(a)}"><span>${esc(topicInfo(a).name)} + ${esc(topicInfo(b).name)}</span><b>${n}</b></button>`; }).join("") || '<p class="tp-muted">No chapter leans toward a second topic yet.</p>'}`;
    for (const b of dom.graphInfo.querySelectorAll("[data-gtopic]")) b.addEventListener("click", () => { state.graph.focus = b.dataset.gtopic; renderGraph(); });
  }

  function drawGraph(rows, list, counts) {
    const d3 = window.d3;
    if (!d3) { dom.graphInfo.insertAdjacentHTML("afterbegin", '<p class="tp-banner">The graph library did not load.</p>'); return; }
    if (graphSim) graphSim.stop();
    const svg = dom.graph;
    const sel = d3.select(svg);
    sel.selectAll("*").remove();
    const { width, height } = svg.getBoundingClientRect();
    const root = sel.append("g");
    const zoom = d3.zoom().scaleExtent([0.2, 4]).on("zoom", (e) => root.attr("transform", e.transform));
    sel.call(zoom);
    const topicNodes = list.map((t) => ({ id: `t:${t.key}`, kind: "topic", t, r: 10 + Math.sqrt(counts.get(t.key)) * 3.4 }));
    const known = new Set(list.map((t) => t.key));
    const chapterNodes = rows.filter((r) => known.has(r.topic_key)).map((r) => ({ id: `c:${r.strand_id}`, kind: "chapter", row: r }));
    const links = chapterNodes.map((n) => ({ source: n.id, target: `t:${n.row.topic_key}`, kind: "main", conf: n.row.confidence ?? 0.5, key: n.row.topic_key }));
    if (state.graph.second) {
      for (const n of chapterNodes) {
        const s = n.row.second;
        if (s && s.p >= SECOND_MIN && known.has(s.key)) links.push({ source: n.id, target: `t:${s.key}`, kind: "second", conf: s.p, key: s.key });
      }
    }
    const nodes = [...topicNodes, ...chapterNodes];
    const linkSel = root.append("g").selectAll("line").data(links).join("line")
      .attr("stroke", (l) => topicInfo(l.key).color)
      .attr("stroke-opacity", (l) => (l.kind === "second" ? 0.35 : 0.12))
      .attr("stroke-dasharray", (l) => (l.kind === "second" ? "3 4" : null));
    const nodeSel = root.append("g").selectAll("g").data(nodes).join("g").style("cursor", "pointer");
    nodeSel.filter((d) => d.kind === "topic").append("circle").attr("r", (d) => d.r).attr("fill", (d) => d.t.color).attr("stroke", "#0b0d12").attr("stroke-width", 2);
    nodeSel.filter((d) => d.kind === "chapter").append("circle").attr("r", 3.4)
      .attr("fill", (d) => topicInfo(d.row.topic_key).color).attr("fill-opacity", (d) => 0.45 + (d.row.confidence ?? 0.5) * 0.55)
      .attr("stroke", (d) => (d.row.sensitive ? "#d9b96a" : "none")).attr("stroke-width", 1.2);
    const labels = root.append("g").selectAll("text").data(topicNodes).join("text").attr("class", "tp-glabel")
      .attr("text-anchor", "middle").attr("fill", (d) => d.t.color).text((d) => d.t.name);
    const moveTip = (e) => { dom.tip.style.left = `${e.clientX + 14}px`; dom.tip.style.top = `${e.clientY + 14}px`; };
    nodeSel.on("mouseenter", (e, d) => {
      dom.tip.innerHTML = d.kind === "topic"
        ? `<b style="color:${esc(d.t.color)}">${esc(d.t.name)}</b><span class="tp-muted">${counts.get(d.t.key)} chapters · click to focus</span>`
        : `<b>${esc(d.row.title)}</b><span class="tp-muted">${esc(d.row.session_title)} · ${esc(topicInfo(d.row.topic_key).name)} ${pct(d.row.confidence)}</span>`;
      dom.tip.hidden = false;
      moveTip(e);
    }).on("mousemove", moveTip)
      .on("mouseleave", () => { dom.tip.hidden = true; })
      .on("click", (e, d) => {
        dom.tip.hidden = true;
        if (d.kind === "topic") { state.graph.focus = state.graph.focus === d.t.key ? null : d.t.key; renderGraph(); }
        else window.location.assign(`/admin/sessions?session=${encodeURIComponent(d.row.session_id)}&chapter=${d.row.strand_id}`);
      });
    graphSim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id)
        .distance((l) => (l.kind === "second" ? 160 : 24 + (1 - l.conf) * 80))
        .strength((l) => (l.kind === "second" ? 0.004 : 0.7)))
      .force("charge", d3.forceManyBody().strength((d) => (d.kind === "topic" ? -900 : -6)))
      .force("collide", d3.forceCollide().radius((d) => (d.kind === "topic" ? d.r + 8 : 4.5)))
      .force("x", d3.forceX(width / 2).strength(0.04)).force("y", d3.forceY(height / 2).strength(0.06))
      .stop();
    for (let i = 0; i < 280; i++) graphSim.tick();
    linkSel.attr("x1", (l) => l.source.x).attr("y1", (l) => l.source.y).attr("x2", (l) => l.target.x).attr("y2", (l) => l.target.y);
    nodeSel.attr("transform", (d) => `translate(${d.x},${d.y})`);
    labels.attr("x", (d) => d.x).attr("y", (d) => d.y - d.r - 7);
    if (nodes.length) {
      const xs = nodes.map((d) => d.x), ys = nodes.map((d) => d.y);
      const x0 = Math.min(...xs) - 40, x1 = Math.max(...xs) + 40, y0 = Math.min(...ys) - 40, y1 = Math.max(...ys) + 40;
      const k = Math.min(width / (x1 - x0 || 1), height / (y1 - y0 || 1), 1.5);
      sel.call(zoom.transform, d3.zoomIdentity.translate(width / 2 - k * (x0 + x1) / 2, height / 2 - k * (y0 + y1) / 2).scale(k));
    }
    graphParts = { nodeSel, linkSel, labels };
    highlightGraph();
  }

  function highlightGraph() {
    if (!graphParts) return;
    const focus = state.graph.focus;
    const query = state.graph.query.trim().toLowerCase();
    const match = (d) => {
      if (d.kind === "topic") return !focus || d.t.key === focus;
      if (focus && d.row.topic_key !== focus && d.row.second?.key !== focus) return false;
      return !query || `${d.row.title} ${d.row.session_title}`.toLowerCase().includes(query);
    };
    graphParts.nodeSel.attr("opacity", (d) => (match(d) ? 1 : 0.08));
    graphParts.labels.attr("opacity", (d) => (!focus || d.t.key === focus ? 1 : 0.15));
    graphParts.linkSel.attr("display", (l) => (!focus || l.key === focus || l.source.row?.topic_key === focus ? null : "none"));
  }

  // ---------- manage overlay ----------

  function openManage(section = null, editKey = null) {
    if (section) state.manage.section = section;
    if (editKey) { state.manage.section = "topics"; state.manage.editKey = editKey; state.manage.editDraft = null; state.manage.editProposal = null; }
    if (!dom.manage.open) dom.manage.showModal();
    loadJournal().then(() => { if (dom.manage.open) renderManage(); }).catch(() => {});
    renderManage();
  }

  dom.manageClose.addEventListener("click", () => dom.manage.close());
  dom.manage.addEventListener("click", (event) => {
    if (event.target !== dom.manage) return;
    const box = dom.manage.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dom.manage.close();
  });

  function renderManage() {
    const o = state.overview;
    const v = version();
    const m = state.manage;
    const poolN = o.pool_count || 0;
    const hint = o.settings.pool_hint || 15;
    const running = jobRunning();
    const lastRefine = o.last_refine;
    const sinceRefine = lastRefine ? state.chapters.filter((r) => (r.first_at || 0) > (lastRefine.finished_at || 0)).length : 0;
    dom.manageMeta.textContent = v ? `map v${v.version_id} · ${v.topic_count} topics · ${o.counts.chapters} chapters` : `no map yet · ${poolN} chapters in Unsorted`;
    const findPrimary = !v || poolN >= hint;
    dom.manageNav.innerHTML = `
      <div class="tp-action">
        <button type="button" class="tp-btn ${findPrimary ? "primary" : ""}" data-act="find" ${running || !poolN ? "disabled" : ""}>Find topics ${MARK}</button>
        <span class="tp-action-state">${v ? `Unsorted ${poolN} / ${hint}<span class="bar"><i style="width:${Math.min(100, (poolN / hint) * 100)}%"></i></span>` : `${poolN} chapters: builds the first map`}</span>
        <button type="button" class="tp-btn" data-act="refine" ${running || !v ? "disabled" : ""}>Refine topics ${MARK}</button>
        <span class="tp-action-state">${!v ? "Needs topics first" : lastRefine ? `Last run ${esc(ago(lastRefine.finished_at))} · ${sinceRefine} new chapters` : "Not run yet"}</span>
      </div>
      <div class="tp-options">
        <label>Detail
          <select class="tp-select" data-opt="detail">${["broad", "balanced", "fine"].map((d) => `<option value="${d}" ${m.detail === d ? "selected" : ""}>${d[0].toUpperCase() + d.slice(1)}</option>`).join("")}</select></label>
        <label>Note for the proposal
          <textarea class="tp-textarea" data-opt="note" maxlength="1000" placeholder="e.g. keep health topics together">${esc(m.note)}</textarea></label>
      </div>
      <div class="tp-sep"></div>
      <div class="tp-sections">
        ${[["proposals", "Proposals", state.proposals.length], ["topics", "Topics", topics().length], ["versions", "Versions", o.versions.length], ["journal", "Journal", ""]]
          .map(([id, label, n]) => `<button type="button" class="tp-section" data-section="${id}" aria-current="${m.section === id}">${label}<small>${n}</small></button>`).join("")}
      </div>
      <span class="tp-spacer"></span>
      <span style="display:flex;gap:14px;padding:0 4px"><button type="button" class="tp-link" data-act="import">Import</button>${v ? '<button type="button" class="tp-link" data-act="export">Export</button>' : ""}</span>`;
    dom.manageNav.querySelector('[data-opt="detail"]').addEventListener("change", (e) => { m.detail = e.target.value; });
    dom.manageNav.querySelector('[data-opt="note"]').addEventListener("input", (e) => { m.note = e.target.value; });
    dom.manageNav.querySelector('[data-act="find"]').addEventListener("click", () => startProposing("find"));
    dom.manageNav.querySelector('[data-act="refine"]').addEventListener("click", () => startProposing("refine"));
    dom.manageNav.querySelector('[data-act="import"]').addEventListener("click", () => { m.section = "versions"; m.importOpen = true; renderManage(); });
    dom.manageNav.querySelector('[data-act="export"]')?.addEventListener("click", exportMap);
    for (const b of dom.manageNav.querySelectorAll("[data-section]")) b.addEventListener("click", () => { m.section = b.dataset.section; renderManage(); });
    // Re-rendering replaces the scrolling lists; keep where the owner was (the detail pane starts at the top).
    const kept = m.section === m.lastSection
      ? ["tp-review-list", "tp-edit-list", "tp-pane"].map((cls) => [cls, dom.manageMain.querySelector(`.${cls}`)?.scrollTop || 0])
      : [];
    m.lastSection = m.section;
    const renderers = { proposals: renderProposals, topics: renderTopicsEditor, versions: renderVersions, journal: renderJournal };
    renderers[m.section]();
    for (const [cls, top] of kept) {
      const node = dom.manageMain.querySelector(`.${cls}`);
      if (node) node.scrollTop = top;
    }
  }

  function startProposing(kind) {
    act(async () => {
      const m = state.manage;
      const perTest = state.overview.cost_per_chapter_usd * state.overview.counts.chapters;
      const ok = await confirm({
        title: kind === "find" ? (version() ? "Find topics in Unsorted?" : "Build the first map?") : "Refine the topics?",
        message: `${kind === "find" ? "The proposing model reads the titles in Unsorted" : "The proposing model reviews every sorted chapter"} once, on your Codex plan. Each proposal is then tested by sorting all chapters (about ${usd(perTest, 3)} per proposal on OpenRouter) before you decide.`,
        confirmLabel: kind === "find" ? "Find topics" : "Refine",
      });
      if (!ok) return;
      await request(`/admin/api/topics/${kind}`, { method: "POST", body: { detail: m.detail, note: m.note } });
      m.section = "proposals";
      await refresh();
    });
  }

  function exportMap() {
    const v = version();
    const data = JSON.stringify(v.topics.filter((t) => t.key !== POOL), null, 1);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([data], { type: "application/json" }));
    link.download = `topics-map-v${v.version_id}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  function jobBanner() {
    const job = state.overview.job;
    if (!job || job.kind === "classify") return "";
    const text = job.step === "testing"
      ? `Testing a proposal: sorting every chapter on the candidate map${job.progress ? ` (${job.progress.done} / ${job.progress.total})` : ""}…`
      : "The proposing model is reading your chapters. This takes a minute or two…";
    return `<div class="tp-banner info" style="display:flex;gap:10px;align-items:center"><span class="tp-spinner"></span>${esc(text)}</div>`;
  }

  // ----- proposals -----

  function renderProposals() {
    const proposals = state.proposals;
    const first = proposals.find((p) => p.op === "first_map" && p.status !== "testing") || null;
    if (first && !jobRunning()) { renderFirstMap(first); return; }
    dom.manageMain.innerHTML = `<div class="tp-pane">
      ${jobBanner()}
      <div class="tp-pane-head"><b>${proposals.length ? `${proposals.length} open proposal${proposals.length === 1 ? "" : "s"}` : "Nothing to decide"}</b>
        <span class="tp-muted">${proposals.length ? "Each was tested on all chapters before you see it. Nothing changes until you accept." : version() ? "Find topics looks in Unsorted; Refine topics reviews the whole map." : "Press Find topics to build the first map from Unsorted."}</span></div>
      ${proposals.map(proposalCard).join("")}
      ${waitingNote()}
    </div>`;
    bindProposalButtons(dom.manageMain);
  }

  function waitingNote() {
    const waiting = state.overview.last_find?.metrics?.waiting;
    if (!waiting || !waiting.length) return "";
    return `<div class="tp-card" style="border-style:dashed"><b style="font-size:13.5px;color:var(--text-2)">Waiting in Unsorted, nothing to decide</b>
      ${waiting.slice(0, 8).map((w) => `<span class="tp-muted">${esc(w.label)} (${w.chapter_ids.length} chapters): ${esc(w.reason)}</span>`).join("")}</div>`;
  }

  function verdictChip(p) {
    if (p.status === "testing") return '<span class="tp-verdict off"><span class="tp-spinner"></span>Testing…</span>';
    if (p.status === "outdated") return '<span class="tp-verdict off">Map changed: test again</span>';
    if (p.status === "failed") return `<span class="tp-verdict warn">Test failed${p.test?.error ? `: ${esc(p.test.error)}` : ""}</span>`;
    const t = p.test || {};
    if (p.op === "edit") return `<span class="tp-verdict ${t.moves_count > 0 ? "ok" : "off"}">Test: ${t.moves_count} chapter${t.moves_count === 1 ? "" : "s"} move · ${pct(t.confidence_before)} → ${pct(t.confidence_after)}</span>`;
    if (t.verdict === "spreads") return `<span class="tp-verdict warn">Test: spreads, pulls ${t.pulled_count} chapters from elsewhere</span>`;
    if (t.verdict === "weak") return `<span class="tp-verdict warn">Test: only ${t.landed} of ${t.intended} land as proposed</span>`;
    return `<span class="tp-verdict ok">Test: ${t.landed} of ${t.intended} land as proposed · ${pct(t.confidence_before)} → ${pct(t.confidence_after)}</span>`;
  }

  function moveLines(t, focusKey = null) {
    const names = t.names || {};
    return (t.moves || []).map((mv) => {
      const into = focusKey ? mv.to === focusKey : mv.to !== "unsorted";
      return `<span class="${into ? "in" : "out"}">${into ? "+ in" : "− out"}</span><span>${esc(mv.title)} <span class="sess">· ${esc(mv.session_title)}</span></span><span class="where">${esc(names[mv.from] || "new")} → ${esc(names[mv.to] || mv.to)}</span>`;
    }).join("");
  }

  function proposalCard(p) {
    const t = p.test || {};
    const warn = t.verdict === "spreads" || t.verdict === "weak";
    const parts = (p.payload.parts || []).map((part) => `<span class="tp-part"><b>${esc(part.name)}</b><span class="tp-muted">${part.chapter_ids.length} chapters</span></span>`).join("");
    const moves = moveLines(t);
    const actions = p.status === "pending"
      ? (warn
        ? `<button type="button" class="tp-btn" data-proposal="${p.proposal_id}" data-do="accept">Accept anyway</button><button type="button" class="tp-btn light" data-proposal="${p.proposal_id}" data-do="reject">Reject, don’t propose again</button>`
        : `<button type="button" class="tp-btn" data-proposal="${p.proposal_id}" data-do="reject">Reject</button><button type="button" class="tp-btn primary" data-proposal="${p.proposal_id}" data-do="accept">Accept</button>`)
      : p.status === "testing" ? "" : `<button type="button" class="tp-btn" data-proposal="${p.proposal_id}" data-do="reject">Reject</button><button type="button" class="tp-btn cyan" data-proposal="${p.proposal_id}" data-do="retest" ${jobRunning() ? "disabled" : ""}>Test again <span class="model">(${esc(jevModel())})</span></button>`;
    return `<article class="tp-card ${warn ? "warn" : ""}">
      <div class="tp-card-head"><span class="tp-kind">${esc(KIND[p.op] || p.op)}</span><b>${esc(p.summary)}</b><span class="tp-faint tp-small">${esc(SOURCE[p.source] || p.source)}</span><span class="tp-spacer"></span>${verdictChip(p)}</div>
      ${p.payload.reason ? `<p>${esc(p.payload.reason)}</p>` : ""}
      ${p.op === "redescribe" ? `<p><span class="tp-muted">New description:</span> ${esc(p.payload.new_description)}</p>` : ""}
      ${parts ? `<div class="tp-parts">${parts}</div>` : ""}
      ${p.status === "pending" ? `<div class="tp-stats"><span>Unsorted <b>${t.pool_before}</b> → <b>${t.pool_after}</b></span><span>Moving <b>${t.moves_count}</b></span>${t.pulled_count ? `<span>Pulled from elsewhere <b>${t.pulled_count}</b></span>` : ""}${t.cost_usd != null ? `<span>Test cost <b>${usd(t.cost_usd, 4)}</b></span>` : ""}</div>` : ""}
      ${moves ? `<details class="tp-more"><summary>Chapters that move (${t.moves_count})</summary><div class="tp-moves">${moves}</div></details>` : ""}
      <div style="display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap">${actions}</div>
    </article>`;
  }

  function bindProposalButtons(root) {
    for (const b of root.querySelectorAll("[data-proposal]")) b.addEventListener("click", () => decide(Number(b.dataset.proposal), b.dataset.do));
  }

  function decide(id, action) {
    act(async () => {
      let note = "";
      if (action === "reject") {
        const value = window.adminConfirmation?.prompt
          ? await window.adminConfirmation.prompt({ title: "Reject this proposal?", message: "It will not be proposed again. A short note helps the next proposal.", inputLabel: "Note (optional)", confirmLabel: "Reject" })
          : (window.confirm("Reject this proposal?") ? "" : null);
        if (value === null) return;
        note = value || "";
      }
      const body = await request(`/admin/api/topics/proposals/${id}/${action}`, { method: "POST", body: { note } });
      if (action === "accept") {
        toast(`Map v${body.version.version_id} is active.`);
        state.openSessions = {};
        state.selected = null;
      }
      await refresh();
    });
  }

  // ----- first map review -----

  function draftFor(p) {
    const drafts = state.manage.drafts;
    const stamp = `${p.status}:${p.summary}:${p.test?.sorted || 0}`;
    if (!drafts[p.proposal_id] || drafts[p.proposal_id].stamp !== stamp) {
      drafts[p.proposal_id] = { stamp, topics: p.payload.topics.map((t, i) => ({ ...t, keep: t.keep !== false, color: t.color || PALETTE[(i * 5) % PALETTE.length] })) };
    }
    return drafts[p.proposal_id];
  }

  function draftChanged(p, draft) {
    return draft.topics.some((t, i) => {
      const o = p.payload.topics[i];
      return t.keep !== (o.keep !== false) || t.name !== o.name || t.family !== o.family || t.description !== o.description;
    });
  }

  function renderFirstMap(p) {
    const draft = draftFor(p);
    const t = p.test || {};
    const per = t.per_topic || {};
    const focusKey = state.manage.focus[p.proposal_id] || draft.topics[0].key;
    const focus = draft.topics.find((x) => x.key === focusKey) || draft.topics[0];
    const chaptersById = new Map(state.chapters.map((r) => [r.strand_id, r]));
    const families = [];
    for (const x of draft.topics) {
      let f = families.find((y) => y.name === x.family);
      if (!f) families.push(f = { name: x.family, items: [] });
      f.items.push(x);
    }
    const changed = draftChanged(p, draft);
    const kept = draft.topics.filter((x) => x.keep).length;
    const focusRows = focus.chapter_ids.map((id) => chaptersById.get(id)).filter(Boolean);
    const focusSessions = sessionsOf(focusRows);
    const fp = per[focus.key] || {};
    dom.manageMain.innerHTML = `
      <div style="padding:14px 20px;border-bottom:1px solid var(--stroke);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="tp-kind">First map</span><b style="font-size:15px">${kept} topics proposed</b>
        <span class="tp-muted">${p.status === "pending" ? `Tested on ${t.sorted} chapters: ${t.sorted - t.pool_after} land in a topic, ${t.pool_after} stay in Unsorted` : esc(p.status === "outdated" ? "The map changed since this test." : p.test?.error || "")}</span>
        ${p.payload.reason ? `<span class="tp-faint tp-small" style="flex-basis:100%">${esc(p.payload.reason)}</span>` : ""}
      </div>
      <div class="tp-review">
        <div class="tp-review-list">
          ${families.map((f) => `<div class="tp-family"><div class="tp-family-name">${esc(f.name)}</div>${f.items.map((x) => {
            const sessions = new Set(x.chapter_ids.map((id) => chaptersById.get(id)?.session_id).filter(Boolean)).size;
            const pt = per[x.key];
            const cls = pt ? (pt.verdict === "ok" ? "ok" : "warn") : "";
            return `<div class="tp-review-row" data-focus="${esc(x.key)}" aria-current="${x.key === focus.key}">
              <input type="checkbox" data-keep="${esc(x.key)}" ${x.keep ? "checked" : ""} aria-label="Keep ${esc(x.name)}">
              <span class="name"><i class="tp-dot" style="background:${esc(x.color)}"></i><span>${esc(x.name)}</span></span>
              <span class="num">${sessions}</span>
              <span class="num ${cls}">${pt ? (pt.verdict === "ok" ? pct(pt.landed / Math.max(1, pt.intended)) : "!") : "–"}</span></div>`;
          }).join("")}</div>`).join("")}
          ${(p.payload.dropped || []).length ? `<p class="tp-muted" style="margin:6px 8px 0;line-height:1.5">Left out (fewer than 3 chapters or 2 sessions): ${p.payload.dropped.map((d) => esc(d.name)).join(", ")}</p>` : ""}
        </div>
        <div class="tp-review-detail">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <i class="tp-dot" style="background:${esc(focus.color)};width:12px;height:12px"></i>
            <input class="tp-input" data-field="name" value="${esc(focus.name)}" maxlength="60" aria-label="Topic name" style="font-weight:800;width:220px">
            <input class="tp-input" data-field="family" value="${esc(focus.family)}" maxlength="40" aria-label="Family" style="width:180px">
            <span class="tp-muted">${focusSessions.length} sessions · ${focusRows.length} chapters</span>
            <span class="tp-spacer"></span>
            ${fp.intended ? `<span class="tp-verdict ${fp.verdict === "ok" ? "ok" : "warn"}">Test: ${fp.landed} of ${fp.intended} land here${fp.pulled ? ` · +${fp.pulled} from elsewhere` : ""} · avg ${pct(fp.confidence)}</span>` : ""}
          </div>
          <label class="tp-field">Description (what sorting reads)<textarea class="tp-textarea" data-field="description" maxlength="600">${esc(focus.description)}</textarea></label>
          ${focusSessions.map((s) => `<div class="tp-row-session">${groupIcon(s)}<b>${esc(s.title)}</b><span class="tp-count">${s.rows.length}</span></div>`).join("")}
          ${fp.verdict && fp.verdict !== "ok" ? `<div class="tp-banner">${fp.verdict === "spreads" ? `In the test this topic also pulled ${fp.pulled} chapters from other topics. Consider unchecking it so they stay in Unsorted.` : "In the test fewer than three quarters of its chapters landed here. A clearer description may help."}</div>` : ""}
        </div>
      </div>
      <div class="tp-footer">
        <span class="tp-muted">${changed ? "You changed the proposal. Test it again before accepting." : "Uncheck a topic to keep its chapters in Unsorted; edit names and descriptions here."}</span>
        <span class="tp-spacer"></span>
        <button type="button" class="tp-btn" data-proposal="${p.proposal_id}" data-do="reject">Reject</button>
        ${changed || p.status !== "pending"
          ? `<button type="button" class="tp-btn cyan" data-act="revise" ${jobRunning() ? "disabled" : ""}>Test again <span class="model">(${esc(jevModel())})</span></button>`
          : `<button type="button" class="tp-btn primary" data-proposal="${p.proposal_id}" data-do="accept">Accept ${kept} topics</button>`}
      </div>`;
    for (const row of dom.manageMain.querySelectorAll("[data-focus]")) {
      row.addEventListener("click", (e) => { if (e.target.matches("input")) return; state.manage.focus[p.proposal_id] = row.dataset.focus; renderManage(); });
    }
    for (const box of dom.manageMain.querySelectorAll("[data-keep]")) {
      box.addEventListener("change", () => { draft.topics.find((x) => x.key === box.dataset.keep).keep = box.checked; renderManage(); });
    }
    for (const field of dom.manageMain.querySelectorAll("[data-field]")) {
      field.addEventListener("change", () => { focus[field.dataset.field] = field.value.trim(); renderManage(); });
    }
    dom.manageMain.querySelector('[data-act="revise"]')?.addEventListener("click", () => act(async () => {
      await request(`/admin/api/topics/proposals/${p.proposal_id}/revise`, { method: "POST", body: { topics: draft.topics } });
      await refresh();
    }));
    bindProposalButtons(dom.manageMain);
  }

  // ----- topics editor -----

  function renderTopicsEditor() {
    const m = state.manage;
    const v = version();
    if (!v) {
      dom.manageMain.innerHTML = '<div class="tp-pane"><p class="tp-muted">No topics yet. Find topics proposes the first map, or Import one.</p></div>';
      return;
    }
    const groups = byTopic();
    const weak = (key) => (groups.get(key) || []).filter((r) => r.confidence != null && r.confidence < 0.4).length;
    if (!m.editDraft || m.editDraft.versionId !== v.version_id) m.editDraft = { versionId: v.version_id, topics: topics().map((t) => ({ ...t })) };
    const draft = m.editDraft.topics;
    if (!m.editKey || !draft.some((t) => t.key === m.editKey)) m.editKey = draft[0]?.key || null;
    const t = draft.find((x) => x.key === m.editKey);
    const original = topics().find((x) => x.key === m.editKey);
    const rows = groups.get(m.editKey) || [];
    const sessions = new Set(rows.map((r) => r.session_id)).size;
    const changed = JSON.stringify(draft) !== JSON.stringify(topics());
    const editProposal = state.proposals.find((p) => p.op === "edit" && p.proposal_id === m.editProposal) || null;
    const weakest = rows.filter((r) => r.confidence != null).sort((a, b) => a.confidence - b.confidence).slice(0, 4);
    const test = editProposal?.test;
    const perTest = state.overview.cost_per_chapter_usd * state.overview.counts.chapters;
    dom.manageMain.innerHTML = `
      <div class="tp-review">
        <div class="tp-edit-list">
          <button type="button" class="tp-btn small" data-act="add" style="margin:0 4px 8px">+ Add topic</button>
          ${draft.map((x) => {
            const n = new Set((groups.get(x.key) || []).map((r) => r.session_id)).size;
            const w = weak(x.key);
            return `<button type="button" class="tp-edit-row" data-edit="${esc(x.key)}" aria-current="${x.key === m.editKey}"><span class="name"><i class="tp-dot" style="background:${esc(x.color)}"></i><span>${esc(x.name)}</span></span><span class="num">${n}</span><span class="num ${w >= 3 ? "warn" : ""}" title="Chapters sorted under 40%">${w}</span></button>`;
          }).join("")}
        </div>
        <div class="tp-review-detail">
          ${jobBanner()}
          ${t ? `
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><i class="tp-dot" style="background:${esc(t.color)};width:13px;height:13px"></i><b style="font-size:17px">${esc(t.name)}</b><span class="tp-muted">${sessions} sessions · ${rows.length} chapters · ${weak(t.key)} weak (under 40%)${original ? "" : " · new"}</span></div>
          <div class="tp-form-3">
            <label class="tp-field">Name<input class="tp-input" data-ef="name" value="${esc(t.name)}" maxlength="60"></label>
            <label class="tp-field">Family<input class="tp-input" data-ef="family" value="${esc(t.family)}" maxlength="40"></label>
            <div class="tp-field">Color<div class="tp-swatches">${PALETTE.slice(0, 12).concat(PALETTE.slice(0, 12).includes(t.color) ? [] : [t.color]).map((c) => `<button type="button" class="tp-swatch" data-color="${c}" style="background:${c}" aria-pressed="${c === t.color}" aria-label="${c}"></button>`).join("")}</div></div>
          </div>
          ${original ? "" : `<label class="tp-field">Key (snake_case, cannot change later)<input class="tp-input mono" data-ef="key" value="${esc(t.key)}" maxlength="48"></label>`}
          <label class="tp-field">Description (what sorting reads)<textarea class="tp-textarea" data-ef="description" maxlength="600">${esc(t.description)}</textarea></label>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <button type="button" class="tp-btn cyan" data-act="test" ${changed && !jobRunning() ? "" : "disabled"}>Test taxonomy <span class="model">(${esc(jevModel())}) · ${usd(perTest, 3)}</span></button>
            <span class="tp-muted">${changed ? "Sorts every chapter again with your changes. Nothing is saved yet." : "Change a name, description, color or add a topic to test it."}</span>
          </div>
          ${test && editProposal.status === "pending" ? `
          <div class="tp-card" style="border-color:rgba(34,211,238,.3)">
            <div class="tp-card-head"><b>Tested</b><span class="tp-muted">confidence ${pct(test.confidence_before)} → ${pct(test.confidence_after)} · Unsorted ${test.pool_before} → ${test.pool_after} · ${test.moves_count} chapters move</span></div>
            ${test.moves.length ? `<div class="tp-moves">${moveLines(test, m.editKey)}</div>` : '<span class="tp-muted">No chapter changes topic.</span>'}
          </div>` : editProposal && editProposal.status === "testing" ? '<div class="tp-banner info" style="display:flex;gap:10px;align-items:center"><span class="tp-spinner"></span>Testing your changes…</div>' : ""}
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button type="button" class="tp-btn small" data-act="split" ${jobRunning() || !original ? "disabled" : ""}>Propose a split ${MARK}</button>
            <button type="button" class="tp-btn small danger" data-act="remove" ${rows.length ? "disabled" : ""} title="${rows.length ? "Only a topic without chapters can be removed" : ""}">Remove topic</button>
          </div>
          ${weakest.length ? `<div class="tp-family-name" style="margin-top:6px">WEAKEST HERE</div>${weakest.map((r) => `<div class="tp-leaf"><a href="/admin/sessions?session=${encodeURIComponent(r.session_id)}&chapter=${r.strand_id}">${esc(r.title)}</a><span class="tp-conf"><span><i style="width:${pct(r.confidence)}"></i></span>${pct(r.confidence)}</span><span class="tp-muted">${esc(r.session_title)}</span></div>`).join("")}` : ""}
          ` : ""}
        </div>
      </div>
      <div class="tp-footer">
        <span class="tp-muted">${editProposal?.status === "pending" ? "Saving makes this map active and keeps the tested sorting." : changed ? "Test your changes before saving." : "Every saved change becomes a new map version."}</span>
        <span class="tp-spacer"></span>
        <button type="button" class="tp-btn" data-act="discard" ${changed ? "" : "disabled"}>Discard</button>
        <button type="button" class="tp-btn primary" data-act="save" ${editProposal?.status === "pending" && !jobRunning() ? "" : "disabled"}>Save</button>
      </div>`;
    for (const b of dom.manageMain.querySelectorAll("[data-edit]")) b.addEventListener("click", () => { m.editKey = b.dataset.edit; renderManage(); });
    for (const f of dom.manageMain.querySelectorAll("[data-ef]")) {
      f.addEventListener("change", () => {
        const value = f.value.trim();
        if (f.dataset.ef === "key") { t.key = value; m.editKey = value; } else t[f.dataset.ef] = value;
        m.editProposal = null;
        renderManage();
      });
    }
    for (const s of dom.manageMain.querySelectorAll("[data-color]")) s.addEventListener("click", () => { t.color = s.dataset.color; m.editProposal = null; renderManage(); });
    dom.manageMain.querySelector('[data-act="add"]').addEventListener("click", () => {
      let i = 1;
      while (draft.some((x) => x.key === `new_topic_${i}`)) i += 1;
      draft.push({ key: `new_topic_${i}`, name: "New topic", family: t?.family || "Other", description: "Describe what belongs here as a concrete list.", color: PALETTE[(draft.length * 5) % PALETTE.length] });
      m.editKey = `new_topic_${i}`;
      m.editProposal = null;
      renderManage();
    });
    dom.manageMain.querySelector('[data-act="test"]')?.addEventListener("click", () => act(async () => {
      const body = await request("/admin/api/topics/test", { method: "POST", body: { topics: draft, note: `Edited ${t.name}` } });
      m.editProposal = body.proposal.proposal_id;
      await refresh();
    }));
    dom.manageMain.querySelector('[data-act="remove"]')?.addEventListener("click", () => {
      m.editDraft.topics = draft.filter((x) => x.key !== m.editKey);
      m.editKey = null;
      m.editProposal = null;
      renderManage();
    });
    dom.manageMain.querySelector('[data-act="split"]')?.addEventListener("click", () => {
      m.note = `Look only at the topic "${t.name}" (${t.key}) and split it if the material supports it.`;
      startProposing("refine");
    });
    dom.manageMain.querySelector('[data-act="discard"]').addEventListener("click", () => act(async () => {
      if (m.editProposal) {
        await request(`/admin/api/topics/proposals/${m.editProposal}/reject`, { method: "POST", body: { note: "discarded edit" } }).catch(() => {});
      }
      m.editDraft = null;
      m.editProposal = null;
      await refresh();
    }));
    dom.manageMain.querySelector('[data-act="save"]').addEventListener("click", () => act(async () => {
      const body = await request(`/admin/api/topics/proposals/${m.editProposal}/accept`, { method: "POST", body: {} });
      toast(`Saved as map v${body.version.version_id}.`);
      m.editDraft = null;
      m.editProposal = null;
      await refresh();
    }));
  }

  // ----- versions and journal -----

  function renderVersions() {
    const m = state.manage;
    const versions = state.overview.versions;
    dom.manageMain.innerHTML = `<div class="tp-pane">
      <div class="tp-pane-head"><b>Versions</b><span class="tp-muted">Restoring brings back that map and the sorting made on it.</span></div>
      <div>${versions.map((v) => `<div class="tp-version"><b>v${v.version_id}</b><div><div style="font-size:13px">${esc(v.note || v.origin)}</div><div class="tp-faint tp-small">${esc(when(v.created_at))} · ${v.topic_count} topics · ${esc(v.origin)}</div></div>${v.is_active ? '<span class="tp-verdict ok">active</span>' : `<button type="button" class="tp-btn small" data-restore="${v.version_id}">Restore</button>`}</div>`).join("") || '<p class="tp-muted">No versions yet.</p>'}</div>
      <details class="tp-more" ${m.importOpen ? "open" : ""}><summary>Import a map (JSON)</summary>
        <p class="tp-muted">A list of topics (key, name, description, family, color). It becomes the active map as is, without a test; sort the chapters afterwards.</p>
        <textarea class="tp-textarea code" data-import placeholder='[{"key": "radio_job", "name": "Radio", "description": "…", "family": "Praca", "color": "#f97316"}]'></textarea>
        <div style="display:flex;gap:10px;margin-top:8px"><input class="tp-input" type="file" accept=".json,application/json" data-import-file><span class="tp-spacer"></span><button type="button" class="tp-btn primary" data-act="import-go">Import</button></div>
      </details>
    </div>`;
    for (const b of dom.manageMain.querySelectorAll("[data-restore]")) {
      b.addEventListener("click", () => act(async () => {
        if (!(await confirm({ title: `Restore map v${b.dataset.restore}?`, message: "Open proposals will need a new test.", confirmLabel: "Restore" }))) return;
        await request(`/admin/api/topics/versions/${b.dataset.restore}/activate`, { method: "POST", body: {} });
        state.openSessions = {};
        state.selected = null;
        await refresh();
      }));
    }
    dom.manageMain.querySelector("[data-import-file]").addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      if (file) dom.manageMain.querySelector("[data-import]").value = await file.text();
    });
    dom.manageMain.querySelector('[data-act="import-go"]').addEventListener("click", () => act(async () => {
      let topicsPayload;
      try { topicsPayload = JSON.parse(dom.manageMain.querySelector("[data-import]").value); } catch (_) { throw new Error("That is not valid JSON."); }
      const v = version();
      await request("/admin/api/topics/versions", { method: "POST", body: { topics: topicsPayload, origin: "import", note: "imported", parent_id: v ? v.version_id : null } });
      m.importOpen = false;
      toast("Map imported. Sort the chapters to place them.");
      await refresh();
    }));
  }

  function renderJournal() {
    dom.manageMain.innerHTML = `<div class="tp-pane"><div class="tp-pane-head"><b>Journal</b><span class="tp-muted">Every step and decision, newest first. Also on the server: <span class="mono">mcp-bridge topics journal</span>.</span></div>
      <div>${state.journal.map((e) => `<div class="tp-journal-entry"><time>${esc(when(e.created_at))}</time><span class="tp-actor ${esc(e.actor)}">${esc(e.actor)}</span><div>${esc(e.message)}${e.run_id ? ` <a href="/admin/api/topics/runs/${e.run_id}/export">run ${e.run_id}</a>` : ""}</div></div>`).join("") || '<p class="tp-muted">Nothing yet.</p>'}</div></div>`;
  }

  // ---------- start ----------

  window.onBridgeSettingsChanged = () => refresh().catch(() => {});
  window.addEventListener("resize", () => { if (state.view === "graph" && version()) renderGraph(); });

  async function load() {
    try {
      const me = await request("/admin/api/me");
      state.csrf = me.csrf_token;
      await refresh();
    } catch (error) {
      dom.detail.innerHTML = `<p class="tp-muted">${esc(error.message)}</p>`;
    }
  }

  load();
})();
