const MAX_CODEX_TRANSCRIPT_MESSAGES = 100;
const state = {
  csrf: null, config: null, codexReady: false, dataGeneration: 0,
  analysisDetailGeneration: 0, rescanBusy: false,
  codex: { status: null, login: null, threadId: null, messages: [], busy: false, pollTimer: 0 },
};

const dom = {
  enabled: document.querySelector("#graphEnabled"), stateWord: document.querySelector("#graphStateWord"),
  masterHint: document.querySelector("#masterHint"), providerNotice: document.querySelector("#providerNotice"),
  status: document.querySelector("#status"), activePolicy: document.querySelector("#activePolicy strong"),
  lockState: document.querySelector("#lockState"), unlock: document.querySelector("#unlockButton"),
  form: document.querySelector("#configForm"), fields: document.querySelector("#configFields"),
  actions: document.querySelector("#draftActions"), discard: document.querySelector("#discardButton"),
  activate: document.querySelector("#activateButton"), model: document.querySelector("#model"),
  effort: document.querySelector("#effort"), inactivity: document.querySelector("#inactivityHours"),
  maxConcepts: document.querySelector("#maxConcepts"), prompt: document.querySelector("#prompt"),
  includeSensitive: document.querySelector("#includeSensitive"),
  jobsBody: document.querySelector("#jobsBody"), analysisSessions: document.querySelector("#analysisSessions"),
  analysisDetail: document.querySelector("#analysisDetail"), refreshProcessing: document.querySelector("#refreshProcessing"),
  refreshAnalysis: document.querySelector("#refreshAnalysis"), rescanAll: document.querySelector("#rescanAll"),
  labForm: document.querySelector("#labForm"), labSession: document.querySelector("#labSession"),
  labModel: document.querySelector("#labModel"), labEffort: document.querySelector("#labEffort"),
  labPrompt: document.querySelector("#labPrompt"), labMaxConcepts: document.querySelector("#labMaxConcepts"),
  labRuns: document.querySelector("#labRuns"), refreshLab: document.querySelector("#refreshLab"),
  codexOpenButton: document.querySelector("#codexOpenButton"), codexDialog: document.querySelector("#codexDialog"),
  codexClose: document.querySelector("#codexClose"), codexStatus: document.querySelector("#codexStatus"),
  codexLoginStart: document.querySelector("#codexLoginStart"), codexLoginCancel: document.querySelector("#codexLoginCancel"),
  codexLogout: document.querySelector("#codexLogout"), codexLoginPanel: document.querySelector("#codexLoginPanel"),
  codexUserCode: document.querySelector("#codexUserCode"), codexVerificationLink: document.querySelector("#codexVerificationLink"),
  codexTranscript: document.querySelector("#codexTranscript"), codexChatForm: document.querySelector("#codexChatForm"),
  codexChatInput: document.querySelector("#codexChatInput"), codexChatSubmit: document.querySelector("#codexChatSubmit"),
};

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["content-type"] = "application/json";
  if (options.method && options.method !== "GET") headers["x-csrf-token"] = state.csrf;
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({ ok: false, error: "Invalid server response." }));
  if (!response.ok) { const error = new Error(body.error || `Request failed (${response.status}).`); error.code = body.code; throw error; }
  return body;
}

function setStatus(message, error = false) { dom.status.textContent = message; dom.status.classList.toggle("is-error", error); }

function editableProfile() { return state.config.draft || state.config.active_profile; }

function render() {
  const config = state.config;
  dom.rescanAll.disabled = !state.config || !config.enabled || !state.codexReady || state.rescanBusy;
  dom.refreshProcessing.disabled = state.rescanBusy;
  dom.refreshAnalysis.disabled = state.rescanBusy;
  if (!config) return;
  const profile = editableProfile();
  const editing = !config.locked;
  dom.enabled.checked = config.enabled;
  dom.enabled.disabled = !state.codexReady && !config.enabled;
  dom.stateWord.textContent = config.enabled ? "ON" : "OFF";
  dom.masterHint.textContent = config.enabled ? "Automatic processing is enabled for eligible sessions." : "No automatic work runs while Graph is off. Existing data and policy stay intact.";
  dom.lockState.dataset.locked = String(config.locked);
  dom.lockState.querySelector("strong").textContent = editing ? "Draft unlocked" : `Locked · v${config.active_profile.version}`;
  dom.unlock.disabled = editing;
  dom.unlock.textContent = editing ? "Configuration unlocked" : "Unlock configuration";
  dom.fields.disabled = !editing;
  dom.actions.hidden = !editing;
  dom.model.value = profile.model;
  dom.effort.value = profile.effort;
  dom.inactivity.value = String(profile.inactivity_hours);
  dom.maxConcepts.value = profile.max_concepts;
  dom.prompt.value = profile.prompt;
  dom.includeSensitive.checked = profile.include_sensitive;
  dom.activePolicy.textContent = `v${config.active_profile.version} · ${config.active_profile.model} · ${config.active_profile.inactivity_hours}h idle`;
}

function draftPayload() {
  return { provider: "codex", model: dom.model.value.trim(), effort: dom.effort.value, prompt: dom.prompt.value.trim(), max_concepts: Number(dom.maxConcepts.value), inactivity_hours: Number(dom.inactivity.value), include_sensitive: dom.includeSensitive.checked };
}

async function mutate(path, method, payload) {
  const body = await request(path, { method, body: payload === undefined ? undefined : JSON.stringify(payload) });
  state.config = body.config; render(); return body;
}

dom.enabled.addEventListener("change", async () => { const wanted = dom.enabled.checked; dom.enabled.disabled = true; try { await mutate("/admin/api/graph/state", "PUT", { enabled: wanted }); setStatus(wanted ? "Graph enabled." : "Graph paused. Stored data and policy were preserved."); } catch (error) { dom.enabled.checked = !wanted; setStatus(error.message, true); } finally { render(); } });
dom.unlock.addEventListener("click", async () => { try { await mutate("/admin/api/graph/config/unlock", "POST"); setStatus("Draft unlocked. Production still uses the active version until activation."); } catch (error) { setStatus(error.message, true); } });
dom.form.addEventListener("submit", async (event) => { event.preventDefault(); try { await mutate("/admin/api/graph/config/draft", "PUT", draftPayload()); setStatus("Draft saved. Active production policy is unchanged."); } catch (error) { setStatus(error.message, true); } });
dom.activate.addEventListener("click", async () => { try { await mutate("/admin/api/graph/config/draft", "PUT", draftPayload()); await mutate("/admin/api/graph/config/activate", "POST"); setStatus(`Profile v${state.config.active_profile.version} activated and locked.`); } catch (error) { setStatus(error.message, true); } });
dom.discard.addEventListener("click", async () => { try { await mutate("/admin/api/graph/config/draft", "DELETE"); setStatus("Draft discarded. Active profile was not changed."); } catch (error) { setStatus(error.message, true); } });

function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function pill(value) { const span = document.createElement("span"); span.className = `status-pill ${value}`; span.textContent = value.replaceAll("_", " "); return span; }
const dateTimeFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
function formatTime(value) { return value ? dateTimeFormatter.format(new Date(value * 1000)) : "—"; }

function diagnosticLine(label, value) {
  const item = document.createElement("div");
  const term = document.createElement("dt"); term.textContent = label;
  const detail = document.createElement("dd"); detail.textContent = value ?? "—";
  item.append(term, detail); return item;
}

function renderJobDiagnostic(job) {
  const wrapper = document.createElement("dl"); wrapper.className = "job-detail-grid";
  wrapper.append(
    diagnosticLine("Job", `#${job.job_id}`),
    diagnosticLine("Status", job.status.replaceAll("_", " ")),
    diagnosticLine("Source", `Exchange #${job.source_exchange_id}`),
    diagnosticLine("Attempts", `${job.attempts}/${job.max_attempts}`),
    diagnosticLine("Updated", formatTime(job.updated_at)),
    diagnosticLine("Started", formatTime(job.started_at)),
    diagnosticLine("Completed", formatTime(job.completed_at)),
  );
  if (job.error_code || job.error_message) {
    const error = document.createElement("div"); error.className = "job-detail-error";
    const heading = document.createElement("strong"); heading.textContent = job.error_code || "Processing error";
    const message = document.createElement("p"); message.textContent = job.error_message || "No additional error details were recorded.";
    error.append(heading, message); wrapper.append(error);
  }
  return wrapper;
}

function renderJobDetail(job) {
  const detailRow = document.createElement("tr"); detailRow.className = "job-detail-row"; detailRow.hidden = true;
  const detailCell = document.createElement("td"); detailCell.colSpan = 5; detailRow.append(detailCell); return { detailRow, detailCell };
}

async function loadProcessing(generation = state.dataGeneration) {
  try {
    const { jobs } = await request("/admin/api/graph/jobs");
    if (generation !== state.dataGeneration) return;
    dom.jobsBody.replaceChildren();
    if (!jobs.length) {
      const row = document.createElement("tr"); const empty = cell("No Graph jobs yet. Eligible sessions will appear after Graph is enabled."); empty.colSpan = 5; row.append(empty); dom.jobsBody.append(row); return;
    }
    for (const job of jobs) {
      const row = document.createElement("tr");
      const sessionCell = document.createElement("td");
      const trigger = document.createElement("button"); trigger.type = "button"; trigger.className = "job-detail-trigger";
      trigger.textContent = job.session_id; trigger.setAttribute("aria-expanded", "false");
      sessionCell.append(trigger); row.append(sessionCell);
      const status = document.createElement("td"); status.append(pill(job.status));
      row.append(status, cell(`#${job.source_exchange_id}`), cell(`${job.attempts}/${job.max_attempts}`), cell(formatTime(job.updated_at)));
      const { detailRow, detailCell } = renderJobDetail(job);
      trigger.addEventListener("click", () => {
        if (!detailCell.hasChildNodes()) detailCell.append(renderJobDiagnostic(job));
        detailRow.hidden = !detailRow.hidden;
        trigger.setAttribute("aria-expanded", String(!detailRow.hidden));
      });
      dom.jobsBody.append(row, detailRow);
    }
  } catch (error) {
    if (generation !== state.dataGeneration) return;
    setStatus(error.message, true);
  }
}

function appendAnalysisDiagnostic(session) {
  if (!session.latest_job_error_code && !session.latest_job_error_message) return;
  const diagnostic = document.createElement("section"); diagnostic.className = "analysis-diagnostic";
  const heading = document.createElement("strong"); heading.textContent = session.latest_job_error_code || "Latest processing error";
  const message = document.createElement("p"); message.textContent = session.latest_job_error_message || "No additional error details were recorded.";
  diagnostic.append(heading, message); dom.analysisDetail.append(diagnostic);
}

function renderUnavailableAnalysis(session) {
  dom.analysisDetail.replaceChildren();
  const heading = document.createElement("h3"); heading.textContent = session.title;
  const intro = document.createElement("p");
  intro.textContent = session.latest_job_status ? "This session does not have a validated production extraction yet." : "This session has not entered Graph processing yet.";
  const details = document.createElement("dl"); details.className = "job-detail-grid";
  details.append(
    diagnosticLine("Job", session.latest_job_id == null ? "—" : `#${session.latest_job_id}`),
    diagnosticLine("Session", session.session_id),
    diagnosticLine("Job status", session.latest_job_status?.replaceAll("_", " ") || "not queued"),
    diagnosticLine("Attempts", session.latest_job_attempts == null ? "—" : `${session.latest_job_attempts}/${session.latest_job_max_attempts}`),
    diagnosticLine("Source", session.latest_job_source_exchange_id == null ? "—" : `Exchange #${session.latest_job_source_exchange_id}`),
    diagnosticLine("Updated", formatTime(session.latest_job_updated_at)),
  );
  dom.analysisDetail.append(heading, intro, details); appendAnalysisDiagnostic(session);
}

async function loadAnalysisDetail(session, generation) {
  try {
    const { analysis } = await request(`/admin/api/graph/analysis/${encodeURIComponent(session.session_id)}`);
    if (generation !== state.analysisDetailGeneration) return;
    dom.analysisDetail.replaceChildren();
    const heading = document.createElement("h3"); heading.textContent = session.title;
    const meta = document.createElement("p"); meta.textContent = `Profile v${analysis.profile_version} · ${analysis.model} · source #${analysis.source_exchange_id}`;
    dom.analysisDetail.append(heading, meta);
    appendAnalysisDiagnostic(session);
    for (const concept of analysis.concepts) {
      const card = document.createElement("section"); card.className = "concept-card";
      const head = document.createElement("div"); head.className = "concept-head";
      const name = document.createElement("strong"); name.textContent = concept.canonical_name;
      const type = document.createElement("span"); type.textContent = concept.type; head.append(name, type);
      const summary = document.createElement("p"); summary.textContent = concept.summary; card.append(head, summary);
      for (const item of concept.evidence) { const quote = document.createElement("blockquote"); quote.className = "evidence"; quote.textContent = item.quote; const source = document.createElement("small"); source.textContent = `Exchange #${item.exchange_id}`; quote.append(source); card.append(quote); }
      dom.analysisDetail.append(card);
    }
  } catch (error) {
    if (generation !== state.analysisDetailGeneration) return;
    dom.analysisDetail.textContent = error.message;
  }
}

async function loadAnalysis(generation = state.dataGeneration) {
  try {
    const { sessions } = await request("/admin/api/graph/analysis");
    if (generation !== state.dataGeneration) return;
    dom.analysisSessions.replaceChildren(); dom.labSession.replaceChildren();
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Select a session"; dom.labSession.append(placeholder);
    if (!sessions.length) { const empty = document.createElement("p"); empty.textContent = "No sessions are available yet."; dom.analysisSessions.append(empty); return; }
    for (const session of sessions) {
      if (session.latest_exchange_id && session.freshness !== "excluded") { const option = document.createElement("option"); option.value = session.session_id; option.textContent = session.title; dom.labSession.append(option); }
      const button = document.createElement("button"); button.type = "button"; button.className = "analysis-session";
      const title = document.createElement("strong"); title.textContent = session.title; const status = pill(session.freshness);
      const meta = document.createElement("small"); meta.textContent = `${session.group_name} · ${session.concept_count} concepts${session.latest_job_status ? ` · job ${session.latest_job_status}` : ""}`;
      button.append(title, status, meta);
      button.addEventListener("click", () => {
        const generation = ++state.analysisDetailGeneration;
        if (session.extraction_id) loadAnalysisDetail(session, generation);
        else renderUnavailableAnalysis(session);
      }); dom.analysisSessions.append(button);
    }
  } catch (error) {
    if (generation !== state.dataGeneration) return;
    setStatus(error.message, true);
  }
}

async function loadLab() {
  try {
    const { runs } = await request("/admin/api/graph/lab"); dom.labRuns.replaceChildren();
    if (!runs.length) { const empty = document.createElement("p"); empty.textContent = "No Lab runs yet."; dom.labRuns.append(empty); return; }
    let hasActive = false;
    for (const run of runs) {
      hasActive ||= run.status === "queued" || run.status === "running";
      const card = document.createElement("article"); card.className = "data-card lab-run";
      const header = document.createElement("header"); const title = document.createElement("strong"); title.textContent = run.session_id; header.append(title, pill(run.status));
      const meta = document.createElement("p"); meta.textContent = `#${run.lab_run_id} · ${run.settings.model} · ${run.settings.effort} · source #${run.source_exchange_id}`;
      card.append(header, meta);
      if (run.result) { const result = document.createElement("p"); result.textContent = run.result.concepts.map((item) => item.canonical_name).join(" · "); card.append(result); }
      if (run.error_message) { const error = document.createElement("p"); error.textContent = run.error_message; card.append(error); }
      dom.labRuns.append(card);
    }
    if (hasActive) window.setTimeout(loadLab, 3000);
  } catch (error) { setStatus(error.message, true); }
}

dom.refreshProcessing.addEventListener("click", () => loadProcessing());
dom.refreshAnalysis.addEventListener("click", () => loadAnalysis());
dom.refreshLab.addEventListener("click", loadLab);
dom.rescanAll.addEventListener("click", async () => {
  const confirmed = window.confirm("Delete every production Graph scan and job, then queue a fresh scan of all allowed sessions? Lab runs will be preserved.");
  if (!confirmed) return;
  const generation = ++state.dataGeneration;
  state.analysisDetailGeneration += 1;
  state.rescanBusy = true; render(); setStatus("Deleting old scans and creating a fresh queue…");
  try {
    const { reset } = await request("/admin/api/graph/rescan", { method: "POST" });
    const emptyDetail = document.createElement("p"); emptyDetail.textContent = "Select a session to inspect its new scan when processing completes.";
    dom.analysisDetail.replaceChildren(emptyDetail);
    setStatus(`Fresh scan queued for ${reset.queued_jobs} sessions. Deleted ${reset.deleted_extractions} extractions and ${reset.deleted_jobs} old jobs.`);
    await Promise.all([loadProcessing(generation), loadAnalysis(generation)]);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.rescanBusy = false; render();
  }
});
dom.labForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await request("/admin/api/graph/lab", { method: "POST", body: JSON.stringify({ session_id: dom.labSession.value, settings: { model: dom.labModel.value.trim(), effort: dom.labEffort.value, prompt: dom.labPrompt.value.trim(), max_concepts: Number(dom.labMaxConcepts.value) } }) });
    setStatus("Lab run queued. Production analysis will not be changed."); await loadLab();
  } catch (error) { setStatus(error.message, true); }
});
document.querySelectorAll(".graph-nav a").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".graph-nav a").forEach((item) => item.removeAttribute("aria-current")); link.setAttribute("aria-current", "page"); }));

function updateProviderNotice() {
  dom.providerNotice.textContent = state.codexReady
    ? "Codex is configured and ready. Graph can use your authenticated ChatGPT plan."
    : "Graph needs an authenticated Codex connection. Use Codex in the top bar to configure or sign in.";
  dom.providerNotice.classList.toggle("is-ready", state.codexReady);
}

function setCodexBusy(busy) {
  state.codex.busy = busy;
  dom.codexLoginStart.disabled = busy;
  dom.codexLoginCancel.disabled = busy;
  dom.codexLogout.disabled = busy;
  dom.codexChatInput.disabled = busy;
  dom.codexChatSubmit.disabled = busy;
}

function pushCodexMessage(message) {
  state.codex.messages.push(message);
  if (state.codex.messages.length > MAX_CODEX_TRANSCRIPT_MESSAGES) {
    state.codex.messages.splice(0, state.codex.messages.length - MAX_CODEX_TRANSCRIPT_MESSAGES);
  }
}

function renderCodexMarkdown(markdown) {
  const lines = String(markdown).replace(/\r\n?/g, "\n").split("\n");
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    if (/^```/.test(line)) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      html.push(`<pre><code>${escapeCodexHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${renderCodexInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ""));
      html.push(`<blockquote>${renderCodexMarkdown(quote.join("\n"))}</blockquote>`);
      continue;
    }

    if (/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const tag = ordered ? "ol" : "ul";
      const items = [];
      const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      while (index < lines.length && pattern.test(lines[index])) {
        items.push(lines[index++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ""));
      }
      html.push(`<${tag}>${items.map((item) => `<li>${renderCodexInline(item)}</li>`).join("")}</${tag}>`);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^```|^(#{1,4})\s+|^>\s?|^\s*(?:[-*+]|\d+[.)])\s+/.test(lines[index])) {
      paragraph.push(lines[index++]);
    }
    html.push(`<p>${paragraph.map(renderCodexInline).join("<br>")}</p>`);
  }
  return html.join("");
}

function renderCodexInline(text) {
  let value = escapeCodexHtml(text);
  value = value.replace(/`([^`]+)`/g, "<code>$1</code>");
  value = value.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, href) => (
    /^(https?:|mailto:|#|\/(?!\/))/i.test(href)
      ? `<a href="${escapeCodexHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : label
  ));
  value = value.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  value = value.replace(/(^|[^\w])\*([^*\n]+?)\*(?=[^\w]|$)/g, "$1<em>$2</em>");
  return value;
}

function escapeCodexHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function renderCodex(statusMessage = "", statusKind = "") {
  const status = state.codex.status;
  const authenticated = Boolean(status?.authenticated);
  const available = status?.available !== false;
  const account = status?.account;
  const accountLabel = account?.email || (account?.type === "api_key" ? "API key account" : "OpenAI account");
  dom.codexStatus.textContent = statusMessage || (authenticated
    ? `Connected as ${accountLabel}${account?.plan_type ? ` (${account.plan_type})` : ""}.`
    : available ? "Sign in with OpenAI to start a private test conversation." : "Codex App Server is unavailable.");
  dom.codexStatus.className = `codex-status${statusKind ? ` ${statusKind}` : ""}`;
  dom.codexLoginStart.hidden = authenticated || Boolean(state.codex.login) || !available;
  dom.codexLogout.hidden = !authenticated;
  dom.codexLoginPanel.hidden = !state.codex.login || authenticated;
  if (state.codex.login && !authenticated) {
    dom.codexUserCode.textContent = state.codex.login.user_code || "";
    dom.codexVerificationLink.href = state.codex.login.verification_url || "#";
  }
  dom.codexChatForm.hidden = !authenticated;
  dom.codexTranscript.replaceChildren();
  if (!state.codex.messages.length) {
    const empty = document.createElement("p"); empty.className = "codex-empty";
    empty.textContent = authenticated
      ? "Start a test conversation. It exists only in this browser tab and is not written to the Bridge database."
      : "No conversation is stored while Codex is signed out.";
    dom.codexTranscript.append(empty);
  } else {
    for (const message of state.codex.messages) {
      const item = document.createElement("article"); item.className = `codex-message ${message.role === "user" ? "is-user" : "is-model"}`;
      const label = document.createElement("small"); label.textContent = message.role === "user" ? "You" : message.role === "error" ? "Error" : "Codex";
      const body = document.createElement("div");
      if (message.role === "model") body.innerHTML = renderCodexMarkdown(message.content); else body.textContent = message.content;
      item.append(label, body); dom.codexTranscript.append(item);
    }
    dom.codexTranscript.scrollTop = dom.codexTranscript.scrollHeight;
  }
  setCodexBusy(state.codex.busy);
}

async function refreshCodexStatus(loginPoll = false) {
  window.clearTimeout(state.codex.pollTimer); state.codex.pollTimer = 0;
  try {
    const path = loginPoll ? "/admin/api/codex/auth/device/status" : "/admin/api/codex/status";
    const payload = await request(path); state.codex.status = payload.codex;
    state.codexReady = payload.codex.authenticated === true; updateProviderNotice(); render();
    if (payload.codex.authenticated || (payload.codex.login_status && payload.codex.login_status !== "pending")) state.codex.login = null;
    renderCodex();
    if (loginPoll && payload.codex.login_status === "pending" && dom.codexDialog.open) state.codex.pollTimer = window.setTimeout(() => refreshCodexStatus(true), 2000);
    if (payload.codex.authenticated) dom.codexChatInput.focus();
  } catch (error) {
    state.codex.status = { available: false, authenticated: false }; state.codex.login = null; state.codexReady = false;
    updateProviderNotice(); render(); renderCodex(error.message || "Codex App Server is unavailable.", "error");
  }
}

function openCodexDialog() {
  if (!dom.codexDialog.open) dom.codexDialog.showModal();
  renderCodex(); refreshCodexStatus();
}

async function startCodexLogin() {
  if (state.codex.busy) return; setCodexBusy(true);
  try {
    const payload = await request("/admin/api/codex/auth/device/start", { method: "POST" });
    state.codex.login = payload.login; renderCodex("Complete sign-in in the OpenAI window.");
    state.codex.pollTimer = window.setTimeout(() => refreshCodexStatus(true), 1500);
  } catch (error) { renderCodex(error.message, "error"); } finally { setCodexBusy(false); }
}

async function cancelCodexLogin() {
  window.clearTimeout(state.codex.pollTimer); state.codex.pollTimer = 0;
  if (state.codex.busy) return; setCodexBusy(true);
  try { await request("/admin/api/codex/auth/device/cancel", { method: "POST" }); state.codex.login = null; await refreshCodexStatus(); }
  catch (error) { renderCodex(error.message, "error"); } finally { setCodexBusy(false); }
}

async function logoutCodex() {
  if (state.codex.busy) return; setCodexBusy(true);
  try {
    await request("/admin/api/codex/logout", { method: "POST" });
    state.codex.status = { available: true, authenticated: false }; state.codex.login = null; state.codex.threadId = null; state.codex.messages = []; state.codexReady = false;
    updateProviderNotice(); render(); renderCodex("Signed out. The test conversation was cleared.", "ok");
  } catch (error) { renderCodex(error.message, "error"); } finally { setCodexBusy(false); }
}

async function sendCodexMessage() {
  const message = dom.codexChatInput.value.trim();
  if (!message || state.codex.busy || !state.codex.status?.authenticated) return;
  pushCodexMessage({ role: "user", content: message }); dom.codexChatInput.value = ""; setCodexBusy(true); renderCodex();
  try {
    const payload = await request("/admin/api/codex/chat", { method: "POST", body: JSON.stringify({ message, thread_id: state.codex.threadId }) });
    state.codex.threadId = payload.chat.thread_id; pushCodexMessage({ role: "model", content: payload.chat.message }); renderCodex();
  } catch (error) {
    if (error.code === "codex_conversation_expired") state.codex.threadId = null;
    pushCodexMessage({ role: "error", content: error.message }); renderCodex(error.message, "error");
  } finally { setCodexBusy(false); dom.codexChatInput.focus(); }
}

dom.codexOpenButton.addEventListener("click", openCodexDialog);
dom.codexClose.addEventListener("click", () => dom.codexDialog.close());
dom.codexLoginStart.addEventListener("click", startCodexLogin);
dom.codexLoginCancel.addEventListener("click", cancelCodexLogin);
dom.codexLogout.addEventListener("click", logoutCodex);
dom.codexChatForm.addEventListener("submit", (event) => { event.preventDefault(); sendCodexMessage(); });
dom.codexDialog.addEventListener("close", () => { window.clearTimeout(state.codex.pollTimer); state.codex.pollTimer = 0; dom.codexOpenButton.focus(); });
dom.codexDialog.addEventListener("click", (event) => { if (event.target === dom.codexDialog) dom.codexDialog.close(); });

async function init() {
  try {
    const [me, graph, codex] = await Promise.all([
      request("/admin/api/me"), request("/admin/api/graph/config"),
      request("/admin/api/codex/status").catch(() => ({ codex: { authenticated: false } })),
    ]);
    state.csrf = me.csrf_token; state.config = graph.config; state.codexReady = codex.codex?.authenticated === true;
    state.codex.status = codex.codex; updateProviderNotice();
    dom.unlock.disabled = false; render();
    dom.labModel.value = state.config.active_profile.model; dom.labEffort.value = state.config.active_profile.effort;
    dom.labPrompt.value = state.config.active_profile.prompt; dom.labMaxConcepts.value = state.config.active_profile.max_concepts;
    await Promise.all([loadProcessing(), loadAnalysis(), loadLab()]);
  } catch (error) { setStatus(error.message, true); }
}

init();
