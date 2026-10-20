/* ------------------------------------------------------------------
   MCP Session Bridge — shared workspace header actions

   Every workspace bar carries Search (⌘K), Settings and the owner name.
   Sessions owns the Search and Settings dialogs and wires its own buttons.
   Other workspaces open Settings as the same Sessions overlay in a frame,
   and Search on Sessions (the search redesign comes later).
------------------------------------------------------------------- */
(() => {
  "use strict";

  const body = document.body;
  const workspace = body.dataset.workspace || "";
  const ownsDialogs = workspace === "sessions";
  const searchButton = document.querySelector('[data-header-action="search"]');
  const settingsButton = document.querySelector('[data-header-action="settings"]');
  const userNode = document.querySelector("[data-header-user]");
  const dot = document.querySelector("[data-header-dot]");

  async function getJson(path) {
    const response = await fetch(path, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  function openSettingsFrame() {
    if (document.querySelector(".wa-settings-frame")) return;
    const frame = document.createElement("iframe");
    frame.className = "wa-settings-frame";
    frame.title = "Settings";
    const tab = body.dataset.settingsTab || "general";
    frame.src = `/admin/sessions?embed=settings&tab=${encodeURIComponent(tab)}`;
    body.append(frame);
    frame.addEventListener("load", () => frame.focus());
  }

  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    if (event.data && event.data.type === "bridge-settings-closed") {
      document.querySelector(".wa-settings-frame")?.remove();
      settingsButton?.focus();
      if (event.data.changed && typeof window.onBridgeSettingsChanged === "function") window.onBridgeSettingsChanged();
    }
  });

  if (!ownsDialogs) {
    searchButton?.addEventListener("click", () => window.location.assign("/admin/sessions?search=1"));
    settingsButton?.addEventListener("click", openSettingsFrame);
    getJson("/admin/api/status")
      .then((payload) => { if (dot) dot.hidden = payload?.status?.update?.state !== "available"; })
      .catch(() => {});
  }

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
      if (document.querySelector("dialog[open]") || document.querySelector(".wa-settings-frame")) return;
      event.preventDefault();
      searchButton?.click();
    }
  });

  if (userNode && !userNode.textContent.trim()) {
    getJson("/admin/api/me")
      .then((me) => { userNode.textContent = me.username || ""; })
      .catch(() => {});
  }

  window.BridgeHeader = Object.freeze({ openSettings: ownsDialogs ? null : openSettingsFrame });

  // Settings framed into another workspace shows only its dialog: no Codex meter there.
  if (new URLSearchParams(window.location.search).get("embed") !== "settings") startCodexActivity(getJson);
})();

/* ------------------------------------------------------------------
   Codex activity: a meter in the header and a panel it toggles.

   The meter shows today's estimated Codex tokens and what runs now.
   One click opens every area of the day side by side (Titles, Chapters,
   Topics); the 30-day strip below picks an earlier day. A second click,
   Esc or a click outside closes it.
------------------------------------------------------------------- */
function startCodexActivity(getJson) {
  "use strict";

  const actions = document.querySelector(".workspace-actions");
  if (!actions) return;
  const SEEN_KEY = "bridge.codexActivity.seen";
  const ROWS = 7;
  const AREAS = [
    { id: "titles", name: "Titles", color: "var(--ca-titles)" },
    { id: "chapters", name: "Chapters", color: "var(--ca-chapters)" },
    { id: "topics", name: "Topics", color: "var(--ca-topics)" },
  ];
  const state = { data: null, open: false, day: 0, expanded: {}, timer: 0, lastBatch: null, csrf: "" };

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const svgIcon = (path) => {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const shape = document.createElementNS(ns, "path");
    shape.setAttribute("d", path);
    svg.append(shape);
    return svg;
  };
  const PULSE = "M3 12h4l3-7 4 14 3-7h4";
  const tokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n || 0));
  const dollars = (n) => (n >= 0.01 ? `$${n.toFixed(2)}` : n > 0 ? "<$0.01" : "");
  const clock = (at) => new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dayName = (iso, index) => {
    if (index === 0) return "Today";
    if (index === 1) return "Yesterday";
    return new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  };

  // ----- meter -----
  const meter = el("button", "wa-btn ca-meter");
  meter.type = "button";
  meter.setAttribute("aria-haspopup", "dialog");
  meter.setAttribute("aria-expanded", "false");
  const meterIcon = svgIcon(PULSE);
  const meterDot = el("span", "ca-live-dot");
  const meterLabel = el("span", "ca-meter-label", "…");
  const meterBar = el("span", "ca-meter-bar");
  const meterNew = el("span", "wa-dot ca-new-dot");
  meterNew.hidden = true;
  meter.append(meterIcon, meterDot, meterLabel, meterBar, meterNew);
  actions.prepend(meter);

  // ----- panel -----
  const backdrop = el("div", "ca-backdrop");
  backdrop.hidden = true;
  const panel = el("section", "ca-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Codex activity");
  panel.hidden = true;
  document.body.append(backdrop, panel);

  const toasts = el("div", "ca-toasts");
  toasts.setAttribute("role", "status");
  document.body.append(toasts);

  function today() { return state.data?.days?.[0]; }

  function liveText(live) {
    if (!live) return null;
    if (["running", "pausing"].includes(live.chapters.status)) {
      return { text: "Chapters", count: live.chapters.total ? `${live.chapters.processed}/${live.chapters.total}` : "", pause: true };
    }
    if (live.topics.status === "running") {
      const kind = live.topics.kind === "classify" ? "Sorting" : "Topics";
      const p = live.topics.progress;
      return { text: kind, count: p ? `${p.done}/${p.total}` : "", pause: false };
    }
    return null;
  }

  function signature(day) {
    return day ? `${day.date}:${day.titles.count}:${day.chapters.sessions}:${day.topics.chapters}:${day.topics.new_topics}` : "";
  }
  function readSeen() { try { return localStorage.getItem(SEEN_KEY) || ""; } catch { return ""; } }
  function writeSeen(value) { try { localStorage.setItem(SEEN_KEY, value); } catch { /* private mode */ } }

  function renderMeter() {
    const data = state.data;
    const day = today();
    const live = liveText(data?.live);
    meter.className = "wa-btn ca-meter";
    meterDot.hidden = !live;
    meterIcon.style.display = live ? "none" : "";
    meterBar.hidden = false;
    meterBar.replaceChildren();
    if (!data) return;
    const paused = data.live.chapters.status === "paused" && data.live.chapters.last_error;
    if (!data.enabled) {
      meter.classList.add("is-off");
      meterLabel.textContent = "Off";
      meterBar.hidden = true;
    } else if (paused) {
      meter.classList.add("is-paused");
      meterLabel.textContent = "Paused";
      meterBar.hidden = true;
    } else if (live) {
      meter.classList.add("is-running");
      meterLabel.textContent = live.count ? `${live.text} ${live.count}` : live.text;
      meterBar.hidden = true;
    } else {
      meterLabel.textContent = tokens(day.estimated_tokens);
      const busiest = Math.max(1, ...data.days.map((d) => d.estimated_tokens));
      for (const area of ["titles", "chapters"]) {
        const part = el("span");
        part.style.width = `${(day[area].estimated_tokens / busiest) * 100}%`;
        part.style.background = `var(--ca-${area})`;
        meterBar.append(part);
      }
    }
    if (state.open) meter.classList.add("is-open");
    const fresh = signature(day);
    meterNew.hidden = state.open || !fresh || readSeen() === fresh || !(day.titles.count + day.chapters.sessions + day.topics.chapters);
    meter.setAttribute(
      "aria-label",
      paused ? "Codex activity: paused after errors" : live ? `Codex activity: ${meterLabel.textContent}` :
        `Codex activity: about ${meterLabel.textContent} tokens today`,
    );
  }

  // The arrow reads "in → out"; the caption says it in words.
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  function caption(id, data) {
    if (id === "titles") return `${plural(data.count, "session", "sessions")} renamed`;
    if (id === "chapters") {
      return `${plural(data.sessions, "session", "sessions")} split into ${plural(data.chapters, "chapter", "chapters")}`;
    }
    const sorted = `${plural(data.chapters, "chapter", "chapters")} sorted into ${plural(data.topics, "topic", "topics")}`;
    return data.new_topics ? `${sorted} · ${data.new_topics} new` : sorted;
  }

  function column(area, day) {
    const box = el("div", "ca-col");
    box.style.setProperty("--ca-color", area.color);
    const head = el("div", "ca-col-head");
    const name = el("span", "ca-col-name");
    name.append(el("span", "ca-swatch"), document.createTextNode(area.name));
    const data = day[area.id];
    let number;
    let cost;
    let items;
    if (area.id === "titles") {
      number = String(data.count);
      cost = data.count ? `~${tokens(data.estimated_tokens)}` : "";
      items = data.items;
    } else if (area.id === "chapters") {
      number = data.sessions ? `${data.sessions} → ${data.chapters}` : "0";
      cost = data.sessions ? `~${tokens(data.estimated_tokens)}` : "";
      items = data.items;
    } else {
      number = data.chapters ? `${data.chapters} → ${data.topics}` : "0";
      cost = dollars(data.cost_usd);
      items = data.items;
    }
    const big = el("span", "ca-col-num", number);
    head.append(name, big, el("span", "ca-col-cost", cost));
    head.append(el("span", "ca-col-caption", caption(area.id, data)));
    box.append(head);

    const list = el("div", "ca-rows");
    const expanded = Boolean(state.expanded[area.id]);
    const shown = expanded ? items : items.slice(0, ROWS);
    if (!items.length) list.append(el("div", "ca-empty", area.id === "topics" ? "No chapters sorted." : "Nothing done."));
    for (const item of shown) {
      if (area.id === "titles") {
        const row = el("a", "ca-row");
        row.href = `/admin/sessions?session=${encodeURIComponent(item.session_id)}`;
        row.title = `Was: ${item.old_title}`;
        row.append(el("span", "ca-ell", item.new_title), el("span", "ca-meta", clock(item.at)));
        list.append(row);
      } else if (area.id === "chapters") {
        const row = el("a", "ca-row");
        row.href = `/admin/sessions?session=${encodeURIComponent(item.session_id)}`;
        row.title = item.chapters.join(" · ");
        row.append(el("span", "ca-ell", item.title), el("span", "ca-badge", item.count));
        list.append(row);
      } else {
        const row = el("a", "ca-row");
        row.href = "/admin/topics";
        row.title = item.chapters.join(" · ");
        row.append(el("span", "ca-ell", item.name));
        if (item.is_new) row.append(el("span", "ca-badge is-new", "new"));
        if (item.added) row.append(el("span", "ca-meta ca-added", `+${item.added}`));
        list.append(row);
      }
    }
    box.append(list);
    const total = area.id === "titles" ? data.count : area.id === "chapters" ? data.sessions : items.length;
    if (total > ROWS) {
      const more = el("button", "ca-more", expanded ? "Show less" : `+${total - ROWS} more`);
      more.type = "button";
      more.addEventListener("click", () => { state.expanded[area.id] = !expanded; renderPanel(); });
      box.append(more);
    } else if (area.id === "topics" && data.new_topics) {
      const more = el("a", "ca-more", "Open Topics");
      more.href = "/admin/topics";
      box.append(more);
    }
    return box;
  }

  function renderPanel() {
    const data = state.data;
    if (!data || !state.open) return;
    const day = data.days[state.day];
    panel.replaceChildren();

    const head = el("div", "ca-head");
    const prev = el("button", "ca-step", "‹");
    prev.type = "button";
    prev.setAttribute("aria-label", "Earlier day");
    prev.disabled = state.day >= data.days.length - 1;
    prev.addEventListener("click", () => pickDay(state.day + 1));
    const next = el("button", "ca-step", "›");
    next.type = "button";
    next.setAttribute("aria-label", "Later day");
    next.disabled = state.day === 0;
    next.addEventListener("click", () => pickDay(state.day - 1));
    head.append(prev, el("h2", "ca-day", dayName(day.date, state.day)), next);
    head.append(el("span", "ca-total", `≈ ${tokens(day.estimated_tokens)} tok`));
    if (day.topics.cost_usd) head.append(el("span", "ca-sub", `+ ${dollars(day.topics.cost_usd)} sorting`));
    head.append(el("span", "ca-spacer"));
    const live = liveText(data.live);
    if (live) {
      const status = el("span", "ca-live");
      status.append(el("span", "ca-live-dot"), document.createTextNode(`${live.text} `), el("span", "ca-meta", live.count));
      head.append(status);
      if (live.pause && data.live.chapters.status === "running") {
        const pause = el("button", "ca-step ca-text-btn", "Pause");
        pause.type = "button";
        pause.addEventListener("click", pauseChapters);
        head.append(pause);
      }
    } else if (data.live.chapters.status === "paused" && data.live.chapters.last_error) {
      const status = el("span", "ca-live is-error", data.live.chapters.last_error);
      status.title = data.live.chapters.last_error;
      head.append(status);
    }
    const help = el("button", "ca-step ca-help", "?");
    help.type = "button";
    help.title = data.estimate_note;
    help.setAttribute("aria-label", data.estimate_note);
    head.append(help);
    panel.append(head);

    const cols = el("div", "ca-cols");
    for (const area of AREAS) cols.append(column(area, day));
    panel.append(cols);

    const strip = el("div", "ca-strip");
    strip.append(el("span", "ca-meta ca-strip-label", `${data.days.length} days`));
    const bars = el("div", "ca-bars");
    const busiest = Math.max(1, ...data.days.map((d) => d.estimated_tokens));
    [...data.days].reverse().forEach((d, reversed) => {
      const index = data.days.length - 1 - reversed;
      const bar = el("button", `ca-bar${index === state.day ? " is-on" : ""}`);
      bar.type = "button";
      bar.setAttribute("aria-label", `${dayName(d.date, index)}: about ${tokens(d.estimated_tokens)} tokens`);
      bar.title = `${dayName(d.date, index)} · ~${tokens(d.estimated_tokens)} tok`;
      const fill = el("span");
      const active = d.estimated_tokens || d.topics.chapters;
      fill.style.height = active ? `${Math.max(10, (d.estimated_tokens / busiest) * 100)}%` : "3px";
      bar.append(fill);
      bar.addEventListener("click", () => pickDay(index));
      bars.append(bar);
    });
    const sum = data.days.reduce((n, d) => n + d.estimated_tokens, 0);
    strip.append(bars, el("span", "ca-meta", `≈ ${tokens(sum)}`));
    panel.append(strip);
  }

  function pickDay(index) {
    state.day = Math.max(0, Math.min(index, state.data.days.length - 1));
    state.expanded = {};
    renderPanel();
  }

  function setOpen(open) {
    state.open = open;
    panel.hidden = backdrop.hidden = !open;
    meter.setAttribute("aria-expanded", String(open));
    if (open) {
      state.day = 0;
      state.expanded = {};
      writeSeen(signature(today()));
      renderPanel();
      load();
    } else {
      meter.focus();
    }
    renderMeter();
    schedule();
  }

  meter.addEventListener("click", () => setOpen(!state.open));
  backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.open) {
      event.stopPropagation();
      setOpen(false);
    }
  });

  // ----- actions and notifications -----
  async function pauseChapters() {
    try {
      if (!state.csrf) state.csrf = (await getJson("/admin/api/me")).csrf_token || "";
      await fetch("/admin/api/chapters/batch/pause", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json", "x-csrf-token": state.csrf },
        body: "{}",
      });
    } catch { /* the next refresh shows the real state */ }
    load();
  }

  function toast(kind, title, detail) {
    const box = el("div", `ca-toast is-${kind}`);
    const text = el("div", "ca-toast-text");
    text.append(el("strong", "", title), el("span", "", detail));
    const view = el("button", "ca-step ca-text-btn", "View");
    view.type = "button";
    view.addEventListener("click", () => { box.remove(); setOpen(true); });
    const close = el("button", "ca-step", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", () => box.remove());
    box.append(el("span", "ca-toast-dot"), text, view, close);
    toasts.append(box);
    if (kind !== "error") setTimeout(() => box.remove(), 12000);
  }

  // Only the end of a batch makes a toast; single titles go quietly into the meter.
  function noticeBatch(live) {
    const now = live.chapters.status;
    const before = state.lastBatch;
    state.lastBatch = now;
    if (!["running", "pausing"].includes(before)) return;
    const day = today();
    if (now === "done") {
      toast("ok", `Chapters done · ${day.chapters.sessions} sessions → ${day.chapters.chapters} chapters`,
        `~${tokens(day.chapters.estimated_tokens)} tokens today`);
    } else if (now === "paused" && live.chapters.last_error) {
      toast("error", "Chapters paused", live.chapters.last_error);
    }
  }

  // ----- loading -----
  async function load() {
    try {
      const payload = await getJson("/admin/api/automation/activity");
      state.data = payload.activity;
      noticeBatch(state.data.live);
    } catch {
      return;
    } finally {
      schedule();
    }
    renderMeter();
    renderPanel();
  }

  function schedule() {
    clearTimeout(state.timer);
    const busy = state.data && liveText(state.data.live);
    state.timer = setTimeout(load, busy || state.open ? 10000 : 60000);
  }

  load();
}
