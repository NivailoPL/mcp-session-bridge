/* ------------------------------------------------------------------
   MCP Session Bridge — Contexts workspace

   The library (left) lists every session and file as a card; the manager
   (right) shows each context as a column of blocks. Dragging a card into a
   column adds a snapshot of it; dragging a block moves it, within its column
   or to another one (Alt copies). The server owns every rule: positions,
   locks, duplicates and the snapshots themselves.
------------------------------------------------------------------- */
(() => {
  "use strict";

  const { modelIdentity, voiceprintSvg, groupIconSvg, sensitiveIconSvg, fileKindSvg } = window.BridgeIdentity;

  const PALETTE = ["#7a7df0", "#c084fc", "#2dd4bf", "#38bdf8", "#f472b6", "#fbbf24", "#a3e635", "#fb923c"];
  const ALL_GROUPS_COLOR = "#e3c35a";
  const FILE_KIND_COLORS = {
    text: { ft: "#b4b7fa", bg: "rgba(99, 102, 241, .16)", bd: "rgba(122, 125, 240, .42)" },
    data: { ft: "#5eead4", bg: "rgba(45, 212, 191, .12)", bd: "rgba(45, 212, 191, .38)" },
    pdf: { ft: "#fda4af", bg: "rgba(251, 113, 133, .13)", bd: "rgba(251, 113, 133, .4)" },
    image: { ft: "#7dd3fc", bg: "rgba(56, 189, 248, .12)", bd: "rgba(56, 189, 248, .38)" },
    web: { ft: "#fdba74", bg: "rgba(251, 146, 60, .12)", bd: "rgba(251, 146, 60, .38)" },
  };
  const DATA_EXTENSIONS = new Set(["json", "yaml", "yml", "csv", "tsv"]);

  const ICON = {
    plus: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>',
    minus: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"></path></svg>',
    eye: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle></svg>',
    lock: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>',
    unlock: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 7.6-1.8"></path></svg>',
    copy: '<svg class="i sm" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M5 15V5a2 2 0 0 1 2-2h8"></path></svg>',
    close: '<svg class="i sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>',
    drop: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v12M7 11l5 5 5-5M5 20h14"></path></svg>',
    session: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" aria-hidden="true"><path d="M4 6h11"></path><path d="M9 12h11"></path><path d="M4 18h11"></path></svg>',
  };

  const state = {
    csrf: "",
    groups: new Map(),
    groupOrder: [],
    sessions: [],
    files: [],
    contexts: [],
    tab: "sessions",
    query: "",
    group: null,
    drag: null,
    dropIndex: null,
    flash: null,
    previewContextId: null,
    previewMarkdown: "",
    undo: null,
  };

  const dom = {
    tabSessions: document.getElementById("tabSessions"),
    tabFiles: document.getElementById("tabFiles"),
    countSessions: document.getElementById("countSessions"),
    countFiles: document.getElementById("countFiles"),
    search: document.getElementById("librarySearch"),
    groupTiles: document.getElementById("groupTiles"),
    filterLabel: document.getElementById("filterLabel"),
    grid: document.getElementById("libraryGrid"),
    empty: document.getElementById("libraryEmpty"),
    board: document.getElementById("board"),
    contextCount: document.getElementById("contextCount"),
    newContext: document.getElementById("newContext"),
    toast: document.getElementById("toast"),
    preview: document.getElementById("preview"),
    previewDot: document.getElementById("previewDot"),
    previewEyebrow: document.getElementById("previewEyebrow"),
    previewTitle: document.getElementById("previewTitle"),
    previewStats: document.getElementById("previewStats"),
    previewBody: document.getElementById("previewBody"),
    previewHighlight: document.getElementById("previewHighlight"),
  };

  // ---------- helpers ----------

  function el(tag, className, html) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  function text(tag, className, value) {
    const node = el(tag, className);
    node.textContent = value;
    return node;
  }

  function button(className, label, html, onClick) {
    const node = el("button", className, html);
    node.type = "button";
    if (label) {
      node.setAttribute("aria-label", label);
      node.title = label;
    }
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["content-type"] = "application/json";
    if (options.method && options.method !== "GET") headers["x-csrf-token"] = state.csrf;
    const response = await fetch(path, {
      ...options,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const body = await response.json().catch(() => ({ ok: false, error: "Invalid server response." }));
    if (!response.ok || body.ok === false) {
      const error = new Error(body.error || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function formatTokens(value) {
    const n = Number(value || 0);
    if (n < 1000) return String(n);
    return `${Math.round(n / 100) / 10}k`;
  }

  function formatDay(seconds) {
    if (!seconds) return "";
    const date = new Date(seconds * 1000);
    const today = new Date();
    const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((startOf(today) - startOf(date)) / 86400000);
    if (days === 0) return "today";
    if (days === 1) return "yesterday";
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  function shade(hex, factor) {
    const value = parseInt(String(hex || "#8b93a7").replace("#", ""), 16);
    const scale = (channel) => Math.round(channel * factor);
    return `rgb(${scale((value >> 16) & 255)}, ${scale((value >> 8) & 255)}, ${scale(value & 255)})`;
  }

  function groupOf(groupId) {
    return state.groups.get(groupId) || { group_id: groupId, name: groupId || "Uncategorized", color: "#8b93a7", icon_key: "folder", is_sensitive: false };
  }

  function paintGroup(node, group) {
    node.style.setProperty("--g", group.color);
    node.style.setProperty("--edge", shade(group.color, 0.42));
  }

  function groupIcon(group, small) {
    const node = el("span", small ? "cx-gicon sm" : "cx-gicon", groupIconSvg(group.icon_key || "folder"));
    paintGroup(node, group);
    if (group.is_sensitive) node.append(el("span", "cx-eye", sensitiveIconSvg()));
    return node;
  }

  function avatar(name, small) {
    const identity = modelIdentity(name);
    const node = el("span", small ? "cx-av xs" : "cx-av", voiceprintSvg(identity.label));
    node.style.setProperty("--m", identity.color);
    node.title = identity.label;
    return node;
  }

  function fileExtension(name) {
    const dot = String(name || "").lastIndexOf(".");
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  }

  function fileKind(file) {
    if (file.content_kind === "image") return "image";
    if (file.content_kind === "pdf") return "pdf";
    const extension = fileExtension(file.filename);
    if (DATA_EXTENSIONS.has(extension)) return "data";
    if (!extension || extension === "md" || extension === "markdown" || extension === "txt") return "text";
    return "web";
  }

  function paintKind(node, kind) {
    const colors = FILE_KIND_COLORS[kind] || FILE_KIND_COLORS.text;
    node.style.setProperty("--ft", colors.ft);
    node.style.setProperty("--ft-bg", colors.bg);
    node.style.setProperty("--ft-bd", colors.bd);
  }

  function kindMark(kind) {
    const node = el("span", "cx-kmark", fileKindSvg(kind));
    paintKind(node, kind);
    node.title = window.BridgeIdentity.FILE_KINDS[kind]?.name || kind;
    return node;
  }

  function usageColors(key) {
    return state.contexts
      .filter((context) => context.blocks.some((block) => block.source_key === key))
      .map((context) => context.color);
  }

  // ---------- library ----------

  function renderTabs() {
    const sessions = state.tab === "sessions";
    dom.tabSessions.setAttribute("aria-selected", String(sessions));
    dom.tabFiles.setAttribute("aria-selected", String(!sessions));
    dom.countSessions.textContent = state.sessions.length;
    dom.countFiles.textContent = state.files.length;
    dom.search.placeholder = sessions ? "Search sessions by title…" : "Search files by name…";
  }

  function renderGroupTiles() {
    const tiles = [{ group_id: null, name: "All groups", color: ALL_GROUPS_COLOR, icon_key: "all_sessions" }, ...state.groupOrder.map(groupOf)];
    dom.groupTiles.replaceChildren(...tiles.map((group) => {
      const tile = button("cx-gtile", group.name, groupIconSvg(group.icon_key || "folder"), () => {
        state.group = group.group_id;
        renderLibrary();
      });
      tile.style.setProperty("--g", group.color);
      tile.setAttribute("aria-pressed", String(state.group === group.group_id));
      if (group.is_sensitive) tile.append(el("span", "cx-eye", sensitiveIconSvg()));
      return tile;
    }));
  }

  function libraryItems() {
    const query = state.query.trim().toLowerCase();
    const items = state.tab === "sessions" ? state.sessions : state.files;
    return items.filter((item) => {
      const group = state.tab === "sessions" ? item.group_id : item.effective_group_id;
      const name = (state.tab === "sessions" ? item.title : item.filename) || "";
      return (!state.group || group === state.group) && (!query || name.toLowerCase().includes(query));
    });
  }

  function renderLibrary() {
    renderTabs();
    renderGroupTiles();
    const items = libraryItems();
    const groupName = state.group ? groupOf(state.group).name : "All groups";
    dom.filterLabel.textContent = `${groupName.toUpperCase()} · ${items.length} ${state.tab === "sessions" ? "SESSIONS" : "FILES"}`;
    dom.grid.replaceChildren(...items.map((item) => (state.tab === "sessions" ? sessionCard(item) : fileCard(item))));
    dom.empty.hidden = items.length > 0;
  }

  function cardFoot(dateSeconds, key, tokens) {
    const foot = el("div", "cx-foot");
    foot.append(text("span", "", formatDay(dateSeconds)));
    const used = el("span", "cx-used");
    for (const color of usageColors(key)) {
      const dot = el("span");
      dot.style.background = color;
      used.append(dot);
    }
    foot.append(used);
    const stat = el("span", "cx-stat");
    stat.append(document.createTextNode(tokens === null ? "—" : formatTokens(tokens)), text("small", "", "TOK"));
    foot.append(stat);
    return foot;
  }

  function sessionCard(session) {
    const group = groupOf(session.group_id);
    const card = el("div", "cx-card is-session");
    paintGroup(card, group);
    if (group.is_sensitive) card.classList.add("is-sensitive");
    card.draggable = true;
    card.title = `${group.name} · drag into a context`;
    const inner = el("div", "cx-card-in");
    const top = el("div", "cx-card-top");
    top.append(groupIcon(group), el("span", "cx-kind", `${ICON.session}SESSION`));
    const art = el("div", "cx-art");
    const avatars = el("div", "cx-avatars");
    for (const model of session.models || []) avatars.append(avatar(model));
    art.append(avatars);
    const exchanges = session.exchange_count === 1 ? "1 EXCHANGE" : `${session.exchange_count} EXCHANGES`;
    inner.append(
      top,
      art,
      text("div", "cx-title", session.title),
      text("div", "cx-type-line", `${exchanges} · ${group.name.toUpperCase()}`),
      cardFoot(session.last_turn_at, `session:${session.session_id}`, session.token_estimate),
    );
    card.append(inner);
    card.addEventListener("dragstart", (event) => startDrag(event, card, { kind: "library", sourceType: "session", id: session.session_id, key: `session:${session.session_id}` }));
    card.addEventListener("dragend", endDrag);
    return card;
  }

  function fileCard(file) {
    const group = groupOf(file.effective_group_id);
    const kind = fileKind(file);
    const isImage = kind === "image";
    const card = el("div", "cx-card is-file");
    paintGroup(card, group);
    if (group.is_sensitive) card.classList.add("is-sensitive");
    if (isImage) card.classList.add("is-disabled");
    card.draggable = !isImage;
    card.title = isImage ? "Images have no text, so they cannot go into context.md" : `${group.name} · drag into a context`;
    const inner = el("div", "cx-card-in");
    const top = el("div", "cx-card-top");
    top.append(groupIcon(group));
    const name = el("div", "cx-name");
    name.append(text("div", "cx-title", file.filename), kindMark(kind));
    inner.append(el("span", "cx-fold"), top, fileArt(file, kind), name, cardFoot(file.created_at, `file:${file.file_id}`, isImage ? null : file.token_estimate));
    card.append(inner);
    if (!isImage) {
      card.addEventListener("dragstart", (event) => startDrag(event, card, { kind: "library", sourceType: "file", id: file.file_id, key: `file:${file.file_id}` }));
      card.addEventListener("dragend", endDrag);
    }
    return card;
  }

  function fileArt(file, kind) {
    const art = el("div", "cx-art");
    const extension = fileExtension(file.filename);
    const preview = file.preview || "";
    if (kind === "image") {
      const image = el("img", "cx-thumb-img");
      image.alt = "";
      image.loading = "lazy";
      image.src = `/admin/api/files/${file.file_id}/thumbnail`;
      art.append(image);
    } else if (kind === "pdf") {
      const page = el("div", "cx-pdf");
      for (const width of [70, 92, 100, 84, 100, 70, 0, 96, 88, 100, 60]) {
        const line = el("i");
        if (width) line.style.width = `${width}%`;
        else line.style.background = "transparent";
        page.append(line);
      }
      art.append(page);
    } else if ((extension === "csv" || extension === "tsv") && preview) {
      art.append(csvThumb(preview, extension === "tsv" ? "\t" : ","));
    } else if (preview && (extension === "json" || extension === "html" || extension === "htm" || extension === "yaml" || extension === "yml")) {
      art.append(codeThumb(preview, extension));
    } else if (preview && kind === "text") {
      const thumb = el("div", "cx-thumb-text");
      for (const line of preview.split("\n").slice(0, 14)) {
        thumb.append(text("div", line.startsWith("#") ? "h" : "", line || " "));
      }
      art.append(thumb);
    } else {
      const tile = text("div", "cx-ext", (extension || kind).toUpperCase().slice(0, 4));
      paintKind(tile, kind);
      art.append(tile);
    }
    return art;
  }

  function csvThumb(preview, delimiter) {
    const rows = preview.split("\n").filter(Boolean).slice(0, 8).map((row) => row.split(delimiter).slice(0, 4));
    const columns = Math.max(1, ...rows.map((row) => row.length));
    const grid = el("div", "cx-thumb-csv");
    grid.style.gridTemplateColumns = `repeat(${columns}, auto)`;
    rows.forEach((row, index) => {
      for (let column = 0; column < columns; column += 1) grid.append(text("span", index === 0 ? "h" : "", row[column] || ""));
    });
    return grid;
  }

  const JSON_TOKENS = /("(?:[^"\\]|\\.)*")(\s*:)?|(\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?)/g;
  const HTML_TOKENS = /(<\/?[a-zA-Z0-9!-]+)|(\/?>)|([a-zA-Z-]+=)("[^"]*")/g;

  function codeThumb(preview, extension) {
    const thumb = el("div", "cx-thumb-code");
    const html = extension === "html" || extension === "htm";
    const pattern = extension === "json" ? JSON_TOKENS : html ? HTML_TOKENS : null;
    for (const line of preview.split("\n").slice(0, 14)) {
      const row = el("div");
      if (!pattern) {
        row.textContent = line || " ";
      } else {
        let last = 0;
        pattern.lastIndex = 0;
        let match;
        while ((match = pattern.exec(line))) {
          if (match.index > last) row.append(document.createTextNode(line.slice(last, match.index)));
          if (html) {
            if (match[1] || match[2]) row.append(text("span", "t", match[1] || match[2]));
            else row.append(text("span", "a", match[3]), text("span", "s", match[4]));
          } else if (match[1]) {
            row.append(text("span", match[2] ? "k" : "s", match[1]));
            if (match[2]) row.append(document.createTextNode(match[2]));
          } else {
            row.append(text("span", "n", match[3]));
          }
          last = pattern.lastIndex;
        }
        row.append(document.createTextNode(line.slice(last) || (last ? "" : " ")));
      }
      thumb.append(row);
    }
    return thumb;
  }

  // ---------- board ----------

  function sourceItem(block) {
    if (block.source_type === "session") return state.sessions.find((s) => s.session_id === block.source_session_id) || null;
    return state.files.find((f) => f.file_id === block.source_file_id) || null;
  }

  function renderBoard() {
    dom.contextCount.textContent = `CONTEXTS · ${state.contexts.length}`;
    const columns = state.contexts.map(column);
    const add = button("cx-add-col", "Add a context", `<span>${ICON.plus}</span>New context`, createContext);
    dom.board.replaceChildren(...columns, add);
  }

  function column(context) {
    const col = el("div", "cx-col");
    col.dataset.contextId = context.context_id;
    col.style.setProperty("--c", context.color);
    if (context.is_locked) col.classList.add("is-locked");
    col.append(el("div", "cx-col-band"), columnHead(context), columnBody(context));
    col.addEventListener("dragover", (event) => dragOver(event, col, context));
    col.addEventListener("dragleave", (event) => {
      if (!col.contains(event.relatedTarget)) clearDropMarks(col);
    });
    col.addEventListener("drop", (event) => drop(event, context));
    return col;
  }

  function columnHead(context) {
    const head = el("div", "cx-col-head");
    const row = el("div", "cx-row");
    const swatch = button("cx-swatch", "Change context color", "", () => recolor(context));
    swatch.disabled = context.is_locked;
    const name = el("input", "cx-name-input");
    name.value = context.name;
    name.disabled = context.is_locked;
    name.setAttribute("aria-label", "Context name");
    name.addEventListener("keydown", (event) => {
      if (event.key === "Enter") name.blur();
      if (event.key === "Escape") {
        name.value = context.name;
        name.blur();
      }
    });
    name.addEventListener("change", () => rename(context, name));
    const preview = button("cx-icon-btn", "Preview context.md", ICON.eye, () => openPreview(context));
    const lock = button(`cx-icon-btn${context.is_locked ? " is-on" : ""}`, context.is_locked ? "Unlock context" : "Lock context", context.is_locked ? ICON.lock : ICON.unlock, () => toggleLock(context));
    const remove = button("cx-icon-btn", "Delete context", ICON.minus, () => deleteContext(context));
    remove.disabled = context.is_locked;
    row.append(swatch, name, preview, lock, remove);

    const meta = el("div", "cx-row");
    const id = button("cx-id", "Copy context ID", `${ICON.copy}<span></span>`, () => copyId(context.context_id, id));
    id.querySelector("span").textContent = context.context_id;
    meta.append(id);
    if (context.is_locked) meta.append(text("span", "cx-locked", "LOCKED"));
    meta.append(text("span", "cx-count", context.blocks.length === 1 ? "1 block" : `${context.blocks.length} blocks`));

    const tokbox = el("div", "cx-tokbox");
    const tokRow = el("div", "cx-tokbox-row");
    tokRow.append(text("span", "cx-tok-big", Number(context.token_count || 0).toLocaleString("en-US")), text("span", "cx-tok-label", "TOKENS"));
    const segbar = el("div", "cx-segbar");
    if (!context.blocks.length) {
      const empty = el("span");
      empty.style.flex = "1 1 0";
      empty.style.background = "rgba(255, 255, 255, .08)";
      segbar.append(empty);
    }
    for (const block of context.blocks) {
      const segment = el("span");
      const item = sourceItem(block);
      const group = groupOf(item ? (item.group_id || item.effective_group_id) : null);
      segment.style.flex = `${Math.max(block.token_count, 1)} 1 0`;
      segment.style.background = group.color;
      segment.title = `${block.title} · ${Number(block.token_count).toLocaleString("en-US")} tokens`;
      segbar.append(segment);
    }
    tokbox.append(tokRow, segbar);
    head.append(row, meta, tokbox);
    return head;
  }

  function columnBody(context) {
    const body = el("div", "cx-col-body");
    context.blocks.forEach((block, index) => {
      if (index > 0) body.append(plus());
      body.append(blockNode(context, block));
    });
    if (context.blocks.length) body.append(plus());
    body.append(slot(context));
    return body;
  }

  function plus() {
    return el("div", "cx-plus", ICON.plus);
  }

  function slot(context, mode) {
    const node = el("div", "cx-slot");
    node.dataset.mode = mode || "idle";
    let label = context.blocks.length ? "Drop the next block here" : "Drop sessions or files here";
    let hint = "Cards are added in order, top to bottom";
    if (mode === "accept") {
      node.classList.add("is-accept");
      label = state.drag?.kind === "block" ? "Release to move here" : "Release to add";
      hint = state.drag?.kind === "block" ? "Hold Alt to copy instead of move" : "Added as a snapshot of its current state";
    } else if (mode === "reject") {
      node.classList.add("is-reject");
      label = context.is_locked ? "This context is locked" : "Already in this context";
      hint = context.is_locked ? "Unlock it to add blocks" : "Each source can appear once";
    } else if (context.is_locked) {
      node.classList.add("is-off");
      label = "Locked";
      hint = "Unlock to add or change blocks";
    }
    node.innerHTML = ICON.drop;
    node.append(text("span", "", label), text("small", "", hint));
    return node;
  }

  function blockNode(context, block) {
    const item = sourceItem(block);
    const session = block.source_type === "session";
    const group = groupOf(item ? (session ? item.group_id : item.effective_group_id) : null);
    const node = el("div", `cx-blk ${session ? "is-session" : "is-file"}`);
    node.dataset.blockId = block.block_id;
    paintGroup(node, group);
    if (group.is_sensitive) node.classList.add("is-sensitive");
    if (state.flash === block.block_id) node.classList.add("is-new");
    node.draggable = !context.is_locked;
    const inner = el("div", "cx-blk-in");
    if (!session) inner.append(el("span", "cx-fold"));
    inner.append(groupIcon(group, true));

    const main = el("div", "cx-blk-main");
    const name = el("div", "cx-name");
    name.append(text("div", "cx-blk-title", block.title));
    if (!session) name.append(kindMark(item ? fileKind(item) : "text"));
    main.append(name, text("div", "cx-blk-meta", `${session ? "SESSION" : "FILE"} · ${formatTokens(block.token_count)} TOK`));
    if (session) {
      const row = el("div", "cx-blk-row");
      const avatars = el("span", "cx-avatars");
      for (const model of item?.models || []) avatars.append(avatar(model, true));
      row.append(avatars, text("span", "", `snapshot · ${formatDay(block.snapshot_at)}`));
      main.append(row);
    }
    if (block.status === "outdated" || block.status === "missing") {
      const flag = el("div", "cx-flag");
      const missing = block.status === "missing";
      flag.append(text("span", `cx-pill${missing ? " is-missing" : ""}`, missing ? "SOURCE DELETED" : "OUT OF DATE"));
      if (!missing && !context.is_locked) flag.append(button("cx-link", "", "Update", () => refreshBlock(block)));
      main.append(flag);
    }
    inner.append(main);
    if (!context.is_locked) inner.append(button("cx-icon-btn xs", "Remove from context", ICON.close, () => removeBlock(context, block)));
    node.append(inner);
    node.addEventListener("dragstart", (event) => startDrag(event, node, { kind: "block", blockId: block.block_id, fromContext: context.context_id, key: block.source_key }));
    node.addEventListener("dragend", endDrag);
    return node;
  }

  // ---------- drag and drop ----------

  function startDrag(event, node, drag) {
    state.drag = drag;
    event.dataTransfer.effectAllowed = "copyMove";
    event.dataTransfer.setData("text/plain", drag.key);
    document.body.classList.add("is-dragging");
    // After the browser has taken its drag image, so the image is not faded.
    requestAnimationFrame(() => node.classList.add("is-dragging"));
  }

  function endDrag() {
    state.drag = null;
    state.dropIndex = null;
    document.body.classList.remove("is-dragging");
    document.querySelectorAll(".is-dragging").forEach((node) => node.classList.remove("is-dragging"));
    document.querySelectorAll(".cx-col").forEach(clearDropMarks);
  }

  function canDrop(context, drag = state.drag) {
    if (!drag || context.is_locked) return false;
    if (drag.kind === "block" && drag.fromContext === context.context_id) return true;
    return !context.blocks.some((block) => block.source_key === drag.key);
  }

  function dropIndexFor(col, clientY) {
    const blocks = [...col.querySelectorAll(".cx-blk")].filter((node) => Number(node.dataset.blockId) !== state.drag?.blockId);
    const index = blocks.findIndex((node) => {
      const box = node.getBoundingClientRect();
      return clientY < box.top + box.height / 2;
    });
    return { blocks, index: index === -1 ? blocks.length : index };
  }

  function dragOver(event, col, context) {
    if (!state.drag) return;
    const accept = canDrop(context);
    const reorder = state.drag.kind === "block" && state.drag.fromContext === context.context_id;
    col.classList.toggle("is-accept", accept && !reorder);
    col.classList.toggle("is-reject", !accept);
    col.querySelectorAll(".cx-insert").forEach((node) => node.remove());
    let mode = "reject";
    if (accept) {
      event.preventDefault();
      event.dataTransfer.dropEffect = state.drag.kind === "block" && !event.altKey ? "move" : "copy";
      const { blocks, index } = dropIndexFor(col, event.clientY);
      state.dropIndex = index;
      // Between blocks a line marks the spot; past the last block the slot lights up.
      if (index < blocks.length) {
        blocks[index].before(el("div", "cx-insert"));
        mode = "idle";
      } else {
        mode = "accept";
      }
    }
    const currentSlot = col.querySelector(".cx-slot");
    if (currentSlot && currentSlot.dataset.mode !== mode) currentSlot.replaceWith(slot(context, mode === "idle" ? undefined : mode));
  }

  function clearDropMarks(col) {
    col.classList.remove("is-accept", "is-reject");
    col.querySelectorAll(".cx-insert").forEach((node) => node.remove());
    const context = state.contexts.find((c) => c.context_id === col.dataset.contextId);
    const currentSlot = col.querySelector(".cx-slot");
    if (context && currentSlot) currentSlot.replaceWith(slot(context));
  }

  async function drop(event, context) {
    event.preventDefault();
    const drag = state.drag;
    const position = state.dropIndex;
    endDrag();
    if (!drag || !canDrop(context, drag)) return;
    try {
      if (drag.kind === "library") {
        const body = drag.sourceType === "session"
          ? { source_type: "session", session_id: drag.id, position }
          : { source_type: "file", file_id: drag.id, position };
        const result = await request(`/admin/api/contexts/${encodeURIComponent(context.context_id)}/blocks`, { method: "POST", body });
        state.flash = result.block.block_id;
        replaceContexts([result.context]);
      } else {
        const copy = event.altKey && drag.fromContext !== context.context_id;
        const result = await request(`/admin/api/context-blocks/${drag.blockId}`, {
          method: "PATCH",
          body: { context_id: context.context_id, position, copy },
        });
        state.flash = result.block.block_id;
        replaceContexts(result.contexts);
      }
      render();
    } catch (error) {
      showError(error);
    }
  }

  // ---------- actions ----------

  function replaceContexts(updated) {
    for (const context of updated) {
      if (!context) continue;
      const index = state.contexts.findIndex((c) => c.context_id === context.context_id);
      if (index === -1) state.contexts.push(context);
      else state.contexts[index] = context;
    }
  }

  async function createContext() {
    const used = new Set(state.contexts.map((c) => c.color));
    const color = PALETTE.find((value) => !used.has(value)) || PALETTE[state.contexts.length % PALETTE.length];
    try {
      const result = await request("/admin/api/contexts", { method: "POST", body: { color } });
      replaceContexts([result.context]);
      render();
      requestAnimationFrame(() => {
        dom.board.scrollTo({ left: dom.board.scrollWidth, behavior: "smooth" });
        dom.board.querySelector(`[data-context-id="${result.context.context_id}"] .cx-name-input`)?.select();
      });
    } catch (error) {
      showError(error);
    }
  }

  async function patchContext(context, body) {
    const result = await request(`/admin/api/contexts/${encodeURIComponent(context.context_id)}`, { method: "PATCH", body });
    replaceContexts([result.context]);
    render();
  }

  async function rename(context, input) {
    const name = input.value.trim();
    if (!name || name === context.name) {
      input.value = context.name;
      return;
    }
    try {
      await patchContext(context, { name });
    } catch (error) {
      input.value = context.name;
      showError(error);
    }
  }

  function recolor(context) {
    const next = PALETTE[(PALETTE.indexOf(context.color) + 1) % PALETTE.length];
    patchContext(context, { color: next }).catch(showError);
  }

  function toggleLock(context) {
    patchContext(context, { is_locked: !context.is_locked }).catch(showError);
  }

  async function deleteContext(context) {
    const confirmed = await window.adminConfirmation.confirm({
      title: "Delete this context?",
      message: `“${context.name}” and its ${context.blocks.length === 1 ? "block" : "blocks"} will be removed. The sessions and files it was built from are not touched.`,
      detail: `A model that was given ${context.context_id} will no longer be able to read it.`,
      confirmLabel: "Delete context",
      tone: "danger",
    });
    if (!confirmed) return;
    try {
      await request(`/admin/api/contexts/${encodeURIComponent(context.context_id)}`, { method: "DELETE" });
      state.contexts = state.contexts.filter((c) => c.context_id !== context.context_id);
      render();
    } catch (error) {
      showError(error);
    }
  }

  async function refreshBlock(block) {
    try {
      const result = await request(`/admin/api/context-blocks/${block.block_id}/refresh`, { method: "POST" });
      replaceContexts([result.context]);
      render();
    } catch (error) {
      showError(error);
    }
  }

  async function removeBlock(context, block) {
    try {
      const result = await request(`/admin/api/context-blocks/${block.block_id}`, { method: "DELETE" });
      replaceContexts([result.context]);
      render();
      showUndo(context, result.block);
    } catch (error) {
      showError(error);
    }
  }

  async function copyId(contextId, node) {
    try {
      await navigator.clipboard.writeText(contextId);
      const label = node.querySelector("span");
      label.textContent = "Copied";
      setTimeout(() => { label.textContent = contextId; }, 1400);
    } catch {
      showError(new Error("The browser did not allow copying. Select the ID and copy it by hand."));
    }
  }

  // ---------- toast ----------

  let toastTimer = null;

  function showToast(nodes, isError) {
    clearTimeout(toastTimer);
    dom.toast.replaceChildren(...nodes);
    dom.toast.classList.toggle("is-error", Boolean(isError));
    dom.toast.hidden = false;
    toastTimer = setTimeout(() => { dom.toast.hidden = true; }, isError ? 6000 : 5000);
  }

  function showError(error) {
    showToast([text("span", "", error.message || String(error))], true);
  }

  function showUndo(context, removed) {
    const message = el("span");
    message.append("Removed ", text("strong", "", removed.title), ` from ${context.name}`);
    const undo = button("cx-ghost", "", "Undo", async () => {
      dom.toast.hidden = true;
      try {
        const result = await request(`/admin/api/contexts/${encodeURIComponent(context.context_id)}/blocks`, {
          method: "POST",
          body: { restore: removed, position: removed.position },
        });
        state.flash = result.block.block_id;
        replaceContexts([result.context]);
        render();
      } catch (error) {
        showError(error);
      }
    });
    showToast([message, undo]);
  }

  // ---------- preview ----------

  async function openPreview(context) {
    state.previewContextId = context.context_id;
    dom.previewDot.style.background = context.color;
    dom.previewEyebrow.textContent = `PREVIEW · CONTEXT.MD · ${context.context_id}`;
    dom.previewTitle.textContent = context.name;
    dom.previewStats.textContent = "Loading…";
    dom.previewBody.textContent = "";
    dom.preview.showModal();
    try {
      const result = await request(`/admin/api/contexts/${encodeURIComponent(context.context_id)}/preview`);
      state.previewMarkdown = result.markdown;
      const parts = [];
      let cursor = 0;
      for (const redaction of result.redactions) {
        parts.push(document.createTextNode(result.markdown.slice(cursor, redaction.start)));
        parts.push(text("mark", "", result.markdown.slice(redaction.start, redaction.end)));
        cursor = redaction.end;
      }
      parts.push(document.createTextNode(result.markdown.slice(cursor)));
      dom.previewBody.replaceChildren(...parts);
      const count = result.redactions.length;
      dom.previewStats.textContent = `${Number(result.token_count).toLocaleString("en-US")} tokens · ${count} ${count === 1 ? "ID" : "IDs"} replaced`;
    } catch (error) {
      dom.previewStats.textContent = "";
      dom.previewBody.textContent = error.message;
    }
  }

  dom.previewHighlight.addEventListener("change", () => {
    dom.preview.classList.toggle("show-redactions", dom.previewHighlight.checked);
  });
  document.getElementById("previewClose").addEventListener("click", () => dom.preview.close());
  document.getElementById("previewCopyId").addEventListener("click", (event) => copyId(state.previewContextId, event.currentTarget));
  document.getElementById("previewDownload").addEventListener("click", () => {
    const blob = new Blob([state.previewMarkdown], { type: "text/markdown" });
    const link = el("a");
    link.href = URL.createObjectURL(blob);
    link.download = "context.md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });

  // ---------- wiring ----------

  function render() {
    renderLibrary();
    renderBoard();
    state.flash = null;
  }

  dom.tabSessions.addEventListener("click", () => {
    state.tab = "sessions";
    renderLibrary();
  });
  dom.tabFiles.addEventListener("click", () => {
    state.tab = "files";
    renderLibrary();
  });
  dom.search.addEventListener("input", () => {
    state.query = dom.search.value;
    renderLibrary();
  });
  dom.newContext.addEventListener("click", createContext);

  async function load() {
    try {
      const me = await request("/admin/api/me");
      state.csrf = me.csrf_token;
      const [sessions, files, contexts] = await Promise.all([
        request("/admin/api/context-library?kind=sessions"),
        request("/admin/api/context-library?kind=files"),
        request("/admin/api/contexts"),
      ]);
      for (const group of sessions.groups) state.groups.set(group.group_id, group);
      state.groupOrder = sessions.groups.map((group) => group.group_id);
      state.sessions = sessions.items;
      state.files = files.items;
      state.contexts = contexts.contexts;
      render();
    } catch (error) {
      showError(error);
    }
  }

  load();
})();
