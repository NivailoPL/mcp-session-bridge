const state = { csrf: null, config: null, codexReady: false };

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
  refreshAnalysis: document.querySelector("#refreshAnalysis"),
  labForm: document.querySelector("#labForm"), labSession: document.querySelector("#labSession"),
  labModel: document.querySelector("#labModel"), labEffort: document.querySelector("#labEffort"),
  labPrompt: document.querySelector("#labPrompt"), labMaxConcepts: document.querySelector("#labMaxConcepts"),
  labRuns: document.querySelector("#labRuns"), refreshLab: document.querySelector("#refreshLab"),
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
function formatTime(value) { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value * 1000)) : "—"; }

async function loadProcessing() {
  try {
    const { jobs } = await request("/admin/api/graph/jobs");
    dom.jobsBody.replaceChildren();
    if (!jobs.length) {
      const row = document.createElement("tr"); const empty = cell("No Graph jobs yet. Eligible sessions will appear after Graph is enabled."); empty.colSpan = 5; row.append(empty); dom.jobsBody.append(row); return;
    }
    for (const job of jobs) {
      const row = document.createElement("tr"); row.append(cell(job.session_id)); const status = document.createElement("td"); status.append(pill(job.status)); row.append(status, cell(`#${job.source_exchange_id}`), cell(`${job.attempts}/${job.max_attempts}`), cell(formatTime(job.updated_at))); dom.jobsBody.append(row);
    }
  } catch (error) { setStatus(error.message, true); }
}

async function loadAnalysisDetail(sessionId) {
  try {
    const { analysis } = await request(`/admin/api/graph/analysis/${encodeURIComponent(sessionId)}`);
    dom.analysisDetail.replaceChildren();
    const heading = document.createElement("h3"); heading.textContent = sessionId;
    const meta = document.createElement("p"); meta.textContent = `Profile v${analysis.profile_version} · ${analysis.model} · source #${analysis.source_exchange_id}`;
    dom.analysisDetail.append(heading, meta);
    for (const concept of analysis.concepts) {
      const card = document.createElement("section"); card.className = "concept-card";
      const head = document.createElement("div"); head.className = "concept-head";
      const name = document.createElement("strong"); name.textContent = concept.canonical_name;
      const type = document.createElement("span"); type.textContent = concept.type; head.append(name, type);
      const summary = document.createElement("p"); summary.textContent = concept.summary; card.append(head, summary);
      for (const item of concept.evidence) { const quote = document.createElement("blockquote"); quote.className = "evidence"; quote.textContent = item.quote; const source = document.createElement("small"); source.textContent = `Exchange #${item.exchange_id}`; quote.append(source); card.append(quote); }
      dom.analysisDetail.append(card);
    }
  } catch (error) { dom.analysisDetail.textContent = error.message; }
}

async function loadAnalysis() {
  try {
    const { sessions } = await request("/admin/api/graph/analysis"); dom.analysisSessions.replaceChildren(); dom.labSession.replaceChildren();
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Select a session"; dom.labSession.append(placeholder);
    if (!sessions.length) { const empty = document.createElement("p"); empty.textContent = "No sessions are available yet."; dom.analysisSessions.append(empty); return; }
    for (const session of sessions) {
      if (session.latest_exchange_id && session.freshness !== "excluded") { const option = document.createElement("option"); option.value = session.session_id; option.textContent = session.title; dom.labSession.append(option); }
      const button = document.createElement("button"); button.type = "button"; button.className = "analysis-session";
      const title = document.createElement("strong"); title.textContent = session.title; const status = pill(session.freshness);
      const meta = document.createElement("small"); meta.textContent = `${session.group_name} · ${session.concept_count} concepts${session.latest_job_status ? ` · job ${session.latest_job_status}` : ""}`;
      button.append(title, status, meta); if (session.extraction_id) button.addEventListener("click", () => loadAnalysisDetail(session.session_id)); else button.disabled = true; dom.analysisSessions.append(button);
    }
  } catch (error) { setStatus(error.message, true); }
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

dom.refreshProcessing.addEventListener("click", loadProcessing);
dom.refreshAnalysis.addEventListener("click", loadAnalysis);
dom.refreshLab.addEventListener("click", loadLab);
dom.labForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await request("/admin/api/graph/lab", { method: "POST", body: JSON.stringify({ session_id: dom.labSession.value, settings: { model: dom.labModel.value.trim(), effort: dom.labEffort.value, prompt: dom.labPrompt.value.trim(), max_concepts: Number(dom.labMaxConcepts.value) } }) });
    setStatus("Lab run queued. Production analysis will not be changed."); await loadLab();
  } catch (error) { setStatus(error.message, true); }
});
document.querySelectorAll(".graph-nav a").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".graph-nav a").forEach((item) => item.removeAttribute("aria-current")); link.setAttribute("aria-current", "page"); }));

async function init() {
  try {
    const [me, graph, codex] = await Promise.all([
      request("/admin/api/me"), request("/admin/api/graph/config"),
      request("/admin/api/codex/status").catch(() => ({ codex: { authenticated: false } })),
    ]);
    state.csrf = me.csrf_token; state.config = graph.config; state.codexReady = codex.codex?.authenticated === true;
    dom.providerNotice.textContent = state.codexReady ? "Codex is configured and ready. Graph can use your authenticated ChatGPT plan." : "Graph needs an authenticated Codex connection. Open Sessions → Codex to configure or sign in.";
    dom.providerNotice.classList.toggle("is-ready", state.codexReady);
    dom.unlock.disabled = false; render();
    dom.labModel.value = state.config.active_profile.model; dom.labEffort.value = state.config.active_profile.effort;
    dom.labPrompt.value = state.config.active_profile.prompt; dom.labMaxConcepts.value = state.config.active_profile.max_concepts;
    await Promise.all([loadProcessing(), loadAnalysis(), loadLab()]);
  } catch (error) { setStatus(error.message, true); }
}

init();
