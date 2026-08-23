import asyncio
import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.search import SearchConfig
from app.storage import SESSION_GROUP_ICON_KEYS
from app.time_format import DISPLAY_TIMEZONE_SETTING_KEY
from tests.pdf_samples import make_pdf
from tests.viewer_harness import requires_node, run_js, slice_source, viewer_source


def test_admin_login_uses_dark_branding_and_inline_lockup(load_main) -> None:
    main = load_main(graph_experimental=True)
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    response = client.get("/admin/login?next=/admin/sessions")

    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "color-scheme: dark" in response.text
    assert 'class="login-shell"' in response.text
    assert 'class="login-logo"' in response.text
    assert '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 404 96"' in response.text
    assert "/admin/assets/brand/svg/lockup-horizontal-dark.svg" not in response.text
    autofill_style = response.text[response.text.index("input:-webkit-autofill") : response.text.index(".login-submit")]
    for selector in (
        "input:-webkit-autofill",
        "input:-webkit-autofill:hover",
        "input:-webkit-autofill:focus",
        "input:-webkit-autofill:active",
        "input:-moz-autofill",
        "input:autofill",
    ):
        assert selector in autofill_style
    for declaration in (
        "background-color: #1d1e27 !important;",
        "-webkit-box-shadow: 0 0 0 1000px #1d1e27 inset;",
        "box-shadow: 0 0 0 1000px #1d1e27 inset;",
        "-webkit-text-fill-color: var(--text);",
        "outline: 2px solid var(--accent);",
    ):
        assert declaration in autofill_style
    assert 'action="/admin/login"' in response.text

    invalid = client.post(
        "/admin/login",
        data={"username": "owner", "password": "wrong", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert invalid.status_code == 401
    assert 'class="login-alert" role="alert"' in invalid.text
    assert "Invalid username or password." in invalid.text


def test_admin_viewer_group_ui_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")

    icon_match = re.search(r"const GROUP_ICONS = (\[[\s\S]*?\]);", viewer)
    assert icon_match is not None
    icon_keys = set(json.loads(icon_match.group(1)))
    assert len(icon_keys) >= 40
    assert icon_keys <= SESSION_GROUP_ICON_KEYS

    assert 'id="groupDeleteButton"' in viewer
    assert 'icon_key: "all_sessions"' in viewer
    assert 'node.dataset.count = String(conversationCount);' in viewer
    assert 'spanCls("group-file-identity")' in viewer
    assert 'setStatus(`Selected ${sessionId}.`, "ok");' not in viewer
    assert 'spanCls("file-meta", "No files")' not in viewer


def test_admin_brand_assets_require_login_and_serve_png(load_main) -> None:
    main = load_main(graph_experimental=True)
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")

    login_required = anonymous.get(
        "/admin/assets/brand/png/icon-indigo-16.png",
        follow_redirects=False,
    )
    assert login_required.status_code == 303
    assert login_required.headers["location"].startswith("/admin/login")

    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    login = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    asset = client.get("/admin/assets/brand/png/icon-indigo-16.png")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/png")
    assert len(asset.content) > 0
    assert client.get("/admin/assets/brand/png/missing.png").status_code == 404


@requires_node
def test_admin_viewer_covered_row_keeps_the_row_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")

    # a covered row is redacted in place: same grid, same height, nothing laid over it
    assert "sensitive-compact-content" not in viewer
    assert "session-card-sensitive-overlay" not in viewer
    assert "sensitive-blurred" not in viewer

    redacted = viewer[
        viewer.index(".session-title-redacted {"):
        viewer.index(".session-stamp-group")
    ]
    assert "width: var(--redacted-width, 62%);" in redacted
    assert "height: 9px;" in redacted

    assert "function redactedSessionTitle(session)" in viewer
    assert 'node.style.setProperty("--redacted-width"' in viewer
    assert ".session-guard-glyph svg { width: 14px; height: 14px; }" in viewer


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_session_date_groups_use_display_timezone() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    date_helpers = viewer[
        viewer.index("const SESSION_DATE_GROUPS"):
        viewer.index("function sessionCompactTitle")
    ]
    harness = r"""
class Element {
  constructor(tag) { this.tag = tag; this.className = ""; this.textContent = ""; }
}
global.document = { createElement: (tag) => new Element(tag) };
const state = { displayTimezone: "Europe/Warsaw" };
const now = new Date("2026-08-08T00:30:00Z");
const samples = [
  "2026-08-07T22:30:00Z",
  "2026-08-07T20:00:00Z",
  "2026-08-06T20:00:00Z",
  "2026-08-01T20:00:00Z",
  "2026-07-31T20:00:00Z"
];
const heading = sessionDateHeading("Today");
process.stdout.write(JSON.stringify({
  labels: SESSION_DATE_GROUPS.map((group) => group.label),
  buckets: samples.map((value) => sessionDateBucket({ last_turn_at_iso: value }, now)),
  heading: { tag: heading.tag, className: heading.className, text: heading.textContent }
}));
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", date_helpers + harness],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)

    assert rendered["labels"] == [
        "Today",
        "Yesterday",
        "Earlier this week",
        "Older",
    ]
    assert rendered["buckets"] == [
        "today",
        "yesterday",
        "more-than-2-days",
        "more-than-2-days",
        "more-than-7-days",
    ]
    assert rendered["heading"] == {
        "tag": "h3",
        "className": "session-date-heading",
        "text": "Today",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_session_list_stamps_use_display_timezone() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    helpers = viewer[
        viewer.index("function sessionCompactTitle"):
        viewer.index("function sessionGroupChip")
    ]
    harness = r"""
const state = { displayTimezone: "Europe/Warsaw" };
process.stdout.write(JSON.stringify({
  today: sessionListStamp("2026-08-13T07:12:00Z", "today"),
  older: sessionListStamp("2026-08-11T15:08:00Z", "more-than-2-days")
}));
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", helpers + harness],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)

    assert rendered == {
        "today": "09:12",
        "older": "11.08",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_session_list_rendering() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    date_helpers = viewer[
        viewer.index("const SESSION_DATE_GROUPS"):
        viewer.index("function sessionCompactTitle")
    ]
    session_list_helpers = viewer[
        viewer.index("function sessionCompactTitle"):
        viewer.index("function sessionGroupChip")
    ]
    render_sessions = session_list_helpers + viewer[
        viewer.index("function renderSessions"):
        viewer.index("function chip(text, warn)")
    ]
    harness = r"""
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      toggle: (name, force) => {
        const next = force === undefined ? !this.classList.values.has(name) : force;
        if (next) this.classList.values.add(name);
        else this.classList.values.delete(name);
        return next;
      },
      contains: (name) => this.classList.values.has(name),
    };
    this.style = { setProperty() {} };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
global.document = { createElement: (tag) => new Element(tag) };
function textOf(node) { return (node.textContent || "") + node.children.map(textOf).join(""); }
function div(className, text) {
  const node = new Element("div");
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function spanCls(className, text) {
  const node = new Element("span");
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function emptyRow(text) { return div("empty", text); }
function chip(text) {
  return spanCls("mini-chip", text);
}
function sessionGroupChip(group, fallbackId) {
  return spanCls("mini-chip group", group?.name || fallbackId || "Uncategorized");
}
function svgNode() { return spanCls("group-icon"); }
function renderSessionActions() { return spanCls("session-actions", "actions"); }
function renderSessionRenameForm() {
  const form = new Element("form");
  form.className = "session-rename";
  form.textContent = "rename form";
  form.append(new Element("input"));
  return form;
}
function formatLastTurnDate() { return "date"; }
function setStatus() {}
function runFileContinuation(_kind, continuation) { continuation(); }
let loadSessionCalls = 0;
function loadSession() { loadSessionCalls += 1; }
function renderSessionListSensitiveGuard() {}
function filteredSessions() {
  return state.sessions.filter((session) => (
    state.activeGroupId === "all" || session.group_id === state.activeGroupId
  ));
}
function hasClass(node, className) {
  return node.classList.contains(className) || String(node.className).split(" ").includes(className) || node.children.some((child) => hasClass(child, className));
}
function sessionGroup(session) { return session.group; }
const today = new Date();
const daysAgo = (days) => new Date(Date.UTC(
  today.getUTCFullYear(),
  today.getUTCMonth(),
  today.getUTCDate() - days,
  12,
)).toISOString();
const group = { name: "Brainstorming", color: "#2563eb", icon_key: "ideas", is_sensitive: false };
const state = {
  displayTimezone: "UTC",
  sessions: [
    { session_id: "selected-id", title: "Selected session", group_id: "brainstorming", group, exchange_count: 2, last_turn_at_iso: daysAgo(0) },
    { session_id: "other-id", title: "Other session", group_id: "brainstorming", group, exchange_count: 1, last_turn_at_iso: daysAgo(1) },
    { session_id: "two-days-id", title: "Two-day session", group_id: "brainstorming", group, exchange_count: 1, last_turn_at_iso: daysAgo(2) },
    { session_id: "old-id", title: "Old session", group_id: "brainstorming", group, exchange_count: 1, last_turn_at_iso: daysAgo(8) }
  ],
  selectedSessionId: "selected-id",
  activeGroupId: "all",
  revealedSensitiveSessionLists: new Set(),
  fileOperationPending: false,
  manualRenameSessionId: ""
};
const dom = {
  sessionList: new Element("nav"),
  fileWorkspaceDialog: { open: false }
};
function cardSnapshot(node) {
  return {
    active: node.classList.contains("is-active"),
    role: node.role,
    tabIndex: node.tabIndex,
    hasKeydown: Boolean(node.listeners.keydown),
    compactIcon: hasClass(node, "session-compact-icon"),
    compactIconFirst: node.children[0]?.children[0]?.children[0]?.className === "session-compact-icon",
    compactIconHidden: node.children[0]?.children[0]?.children[0]?.attributes["aria-hidden"] === "true",
    ariaLabel: node.attributes["aria-label"] || "",
    text: textOf(node)
  };
}
renderSessions();
const listHeadings = () => dom.sessionList.children.filter((node) => node.tag === "h3").map(textOf);
const listStructure = () => dom.sessionList.children.map((node) => (
  node.tag === "h3" ? `heading:${textOf(node)}` : "card"
));
const initial = dom.sessionList.children.filter((node) => hasClass(node, "session-button")).map(cardSnapshot);
const initialHeadings = listHeadings();
const initialStructure = listStructure();
state.selectedSessionId = "other-id";
state.manualRenameSessionId = "selected-id";
renderSessions();
const switchedCards = dom.sessionList.children.filter((node) => hasClass(node, "session-button"));
const switched = switchedCards.map(cardSnapshot);
const switchedHeadings = listHeadings();
const switchedStructure = listStructure();
const renameInput = switchedCards[0].children[0].children[0].children[0];
const renameSpace = {
  key: " ",
  target: renameInput,
  prevented: false,
  preventDefault() { this.prevented = true; },
};
switchedCards[0].listeners.keydown(renameSpace);
const afterRenameInputSpace = { loadSessionCalls, prevented: renameSpace.prevented };
const rowSpace = {
  key: " ",
  target: switchedCards[1],
  prevented: false,
  preventDefault() { this.prevented = true; },
};
switchedCards[1].listeners.keydown(rowSpace);
const afterRowSpace = { loadSessionCalls, prevented: rowSpace.prevented };
process.stdout.write(JSON.stringify({
  initial,
  switched,
  initialHeadings,
  switchedHeadings,
  initialStructure,
  switchedStructure,
  afterRenameInputSpace,
  afterRowSpace,
}));
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", date_helpers + harness + render_sessions],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)

    expected_headings = ["Today", "Yesterday", "Earlier this week", "Older"]
    expected_structure = [
        "heading:Today",
        "card",
        "heading:Yesterday",
        "card",
        "heading:Earlier this week",
        "card",
        "heading:Older",
        "card",
    ]
    assert rendered["initialHeadings"] == expected_headings
    assert rendered["switchedHeadings"] == expected_headings
    assert rendered["initialStructure"] == expected_structure
    assert rendered["switchedStructure"] == expected_structure
    initial_selected, initial_other, initial_two_days, initial_old = rendered["initial"]
    assert initial_selected["active"] is True
    assert initial_selected["compactIcon"] is True
    assert initial_selected["compactIconFirst"] is True
    assert initial_selected["compactIconHidden"] is True
    assert initial_selected["ariaLabel"] == ""
    assert "selected-id" not in initial_selected["text"]
    assert "Brainstorming · 4 turns" in initial_selected["text"]
    assert "actions" in initial_selected["text"]
    assert initial_selected["role"] == "button"
    assert initial_selected["tabIndex"] == 0
    assert initial_selected["hasKeydown"] is True
    assert initial_other["active"] is False
    assert initial_other["compactIcon"] is True
    assert initial_other["compactIconFirst"] is True
    assert initial_other["compactIconHidden"] is True
    assert initial_other["ariaLabel"] == "Other session — Brainstorming"
    assert initial_other["text"].startswith("Other session")
    assert "Brainstorming · 2 turns" in initial_other["text"]
    assert "other-id" not in initial_other["text"]
    assert "actions" in initial_other["text"]
    assert initial_other["role"] == "button"
    assert initial_other["tabIndex"] == 0
    assert initial_other["hasKeydown"] is True
    assert initial_two_days["text"].startswith("Two-day session")
    assert initial_old["text"].startswith("Old session")

    switched_selected, switched_other, switched_two_days, switched_old = rendered["switched"]
    # renaming no longer requires opening the session first
    assert switched_selected["active"] is False
    assert switched_selected["ariaLabel"] == "Selected session — Brainstorming"
    assert "rename form" in switched_selected["text"]
    assert switched_selected["compactIcon"] is False
    assert switched_other["active"] is True
    assert switched_other["compactIcon"] is True
    assert switched_other["compactIconFirst"] is True
    assert switched_other["compactIconHidden"] is True
    assert switched_other["ariaLabel"] == ""
    assert "other-id" not in switched_other["text"]
    assert "Brainstorming · 2 turns" in switched_other["text"]
    assert "actions" in switched_other["text"]
    assert switched_two_days["text"].startswith("Two-day session")
    assert switched_old["text"].startswith("Old session")
    assert rendered["afterRenameInputSpace"] == {"loadSessionCalls": 0, "prevented": False}
    assert rendered["afterRowSpace"] == {"loadSessionCalls": 1, "prevented": True}


def test_admin_viewer_sensitive_group_privacy_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")

    assert 'id="groupSensitiveButton"' in viewer
    assert 'aria-pressed="false"' in viewer
    assert 'function sensitiveIconSvg()' in viewer
    assert viewer.count("sensitiveIconSvg()") >= 4
    assert 'revealedSensitiveSessionLists: new Set()' in viewer
    assert 'revealedSensitiveThreads: new Set()' in viewer
    assert 'id="threadSensitiveOverlay"' in viewer
    assert "The conversation stays covered until you reveal it." in viewer
    assert "Reveal conversation" in viewer
    assert 'id="threadSensitiveBody"' in viewer
    assert "dom.threadSensitiveBody.inert = threadIsGuarded;" in viewer
    assert "dom.threadSensitiveContent.inert" not in viewer
    assert ".sensitive-curtain:hover { background: var(--bg-base); }" in viewer
    # a covered thread keeps its header usable, but not its title
    assert 'dom.sessionTitle.classList.toggle("is-redacted", threadIsGuarded);' in viewer
    assert ".topbar-title h2.is-redacted" in viewer
    # a covered transcript is never built, so nothing survives in a screenshot or the DOM
    render_exchanges = viewer[
        viewer.index("function renderExchanges()"):
        viewer.index("function renderThreadSensitiveGuard()")
    ]
    assert "if (selectedThreadIsGuarded()) {" in render_exchanges
    assert 'dom.sessionTitle.textContent = "";' in render_exchanges
    guarded_return = render_exchanges.index("if (selectedThreadIsGuarded()) {")
    assert render_exchanges.index("const visible = state.exchanges;") > guarded_return
    assert "state.revealedSensitiveSessionLists.add(session.group_id);" in viewer
    assert "state.revealedSensitiveThreads.add(groupId);" in viewer
    assert 'input.disabled = Boolean(group.is_sensitive);' in viewer
    assert "This group stays in local BM25 search." in viewer
    assert 'payload.is_sensitive = dom.groupSensitiveButton.getAttribute("aria-pressed") === "true";' in viewer
    assert "mcp-admin:sensitive" not in viewer
    name_field = viewer.index('<label class="field">Name')
    sensitive_control = viewer.index('id="groupSensitiveControl"')
    color_field = viewer.index('<label class="field">Color')
    assert name_field < sensitive_control < color_field
    assert 'class="sensitive-help-trigger"' in viewer
    assert 'aria-describedby="groupSensitiveNote"' in viewer
    assert 'id="groupSensitiveNote"' in viewer
    assert 'role="tooltip"' in viewer
    assert ".sensitive-help:hover .sensitive-help-box" in viewer
    assert ".sensitive-help:focus-within .sensitive-help-box" in viewer
    assert ".sensitive-control { position: relative;" in viewer
    assert "top: calc(100% + .45rem); left: 0;" in viewer

    group_button = viewer[viewer.index("function groupButton"):viewer.index("function renderGroupActions")]
    assert 'node.innerHTML = iconSvg(group.icon_key || "folder");' in group_button
    assert 'const badge = spanCls("group-sensitive-badge");' in group_button
    assert "badge.innerHTML = sensitiveIconSvg();" in group_button
    assert "group.is_sensitive ? sensitiveIconSvg()" not in group_button


    # covering follows the group, under every filter the list can be in
    covered = viewer[
        viewer.index("function sessionRowIsCovered(session"):
        viewer.index("function redactedSessionTitle(session)")
    ]
    assert "Boolean(group?.is_sensitive)" in covered
    assert "!state.revealedSensitiveSessionLists.has(session.group_id)" in covered
    assert "state.activeGroupId" not in covered

    guard = viewer[
        viewer.index("const cardIsGuarded = sessionRowIsCovered(session, group);"):
        viewer.index("dom.sessionList.append(item);", viewer.index("const cardIsGuarded"))
    ]
    assert "const activate = cardIsGuarded ? revealGroup : selectSession;" in guard
    assert 'item.addEventListener("keydown"' in guard
    assert "sessionCompactTitle(session, group, dateBucket, true)" in guard
    reveal_handler = guard[guard.index("const revealGroup"):guard.index("const activate")]
    assert "state.revealedSensitiveSessionLists.add(session.group_id);" in reveal_handler
    assert "renderSessions();" in reveal_handler
    assert "loadSession" not in reveal_handler

    # every row action reveals a covered group first, so none of them can work around the cover
    actions = viewer[
        viewer.index("function renderSessionActions(session)"):
        viewer.index("function renderSessionRenameForm(session)")
    ]
    assert "const revealIfCovered = () => {" in actions
    assert "if (!sessionRowIsCovered(session)) return false;" in actions
    assert "state.revealedSensitiveSessionLists.add(session.group_id);" in actions
    assert actions.count("revealIfCovered()") == 3


def test_deployment_includes_narrow_restart_helper() -> None:
    helper = Path("deploy/mcp-session-bridge-restart.service").read_text(encoding="utf-8")

    assert "ExecStart=/bin/sleep 1" in helper
    assert "ExecStart=/bin/systemctl restart mcp-session-bridge.service" in helper
    assert "User=" not in helper


@requires_node
def test_admin_viewer_context_visibility_controls_contract() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")

    assert '"Exclude"' in viewer
    assert '"Include"' in viewer
    assert '"Mask"' in viewer
    assert '"Unmask"' in viewer
    assert "Exclude this user and model exchange from all MCP transcript chunks." in viewer
    assert "Include this user and model exchange in MCP transcript chunks again." in viewer
    assert "Mask this model response in all MCP transcript chunks." in viewer
    assert "Restore this model response in MCP transcript chunks." in viewer
    assert ".is-masked" in viewer
    assert "Show excluded exchanges" not in viewer
    assert "showDeletedToggle" not in viewer
    assert "showDeleted" not in viewer
    assert "const visible = state.exchanges;" in viewer
    assert 'function contextActionIcon(key)' in viewer
    assert 'return spanCls("pill", "In context");' in viewer
    assert 'contextActionButton("Mask", "eye-off"' in viewer
    assert 'contextActionButton("Exclude", "circle-minus"' in viewer
    assert 'button("?", null, "context-help-trigger")' in viewer
    assert '.context-help:hover .context-help-box' in viewer
    assert '.context-help:focus-within .context-help-box' in viewer
    assert "Mask hides only the model response and leaves an explicit placeholder for all models." in viewer
    assert "Exclude removes the complete user and model exchange from all MCP transcript chunks." in viewer
    assert "Both actions are reversible." in viewer
    assert 'function renderExcludedTurnBar(exchange)' in viewer
    assert '"Excluded turn"' in viewer
    assert 'spanCls("excluded-turn-note", `Note: ${exchange.deleted_reason}`)' in viewer
    assert '.ex-group.is-deleted { opacity' not in viewer


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_excluded_turn_rendering_hides_message_content() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    render_group = viewer[viewer.index("function renderExGroup"):viewer.index("function renderMsg")]
    context_controls = viewer[viewer.index("const CONTEXT_ACTION_ICONS"):viewer.index("function renderExActions")]
    render_exchange = viewer[viewer.index("function renderExchange(exchange)"):viewer.index("function renderTurn")]
    helpers = viewer[viewer.index("function button(text"):viewer.index("function label(className")]
    harness = """
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.className = ""; this.textContent = ""; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  click() { if (this.listeners.click) this.listeners.click(); }
  style = { setProperty() {} };
}
global.document = { createElement: (tag) => new Element(tag) };
function textOf(node) { return (node.textContent || "") + node.children.map(textOf).join(""); }
function findNode(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children) { const match = findNode(child, predicate); if (match) return match; }
  return null;
}
const restored = [];
function restoreExchange(id) { restored.push(id); }
"""
    exchange = """
const exchange = {
  exchange_id: 42,
  is_deleted: true,
  deleted_reason: "Fresh perspective",
  user_message: "HIDDEN USER CONTENT",
  assistant_response: "HIDDEN MODEL CONTENT"
};
const pair = renderExchange(exchange);
const conversational = renderExGroup(exchange);
const includeButton = findNode(pair, (node) => node.tag === "button" && textOf(node) === "Include");
includeButton.click();
const helpTrigger = findNode(pair, (node) => node.className === "context-help-trigger");
const tooltip = findNode(pair, (node) => node.className === "context-help-box");
const activeControls = contextVisibilityControls({ exchange_id: 7, is_masked: false });
const maskedControls = contextVisibilityControls({ exchange_id: 7, is_masked: true });
process.stdout.write(JSON.stringify({
  pair: textOf(pair),
  conversational: textOf(conversational),
  restored,
  activeControls: textOf(activeControls),
  maskedControls: textOf(maskedControls),
  tooltipLinked: helpTrigger.attributes["aria-describedby"] === tooltip.id && tooltip.attributes.role === "tooltip"
}));
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", harness + helpers + context_controls + render_group + render_exchange + exchange],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)

    for layout_text in (rendered["pair"], rendered["conversational"]):
        assert "#42Excluded turnNote: Fresh perspectiveInclude?" in layout_text
        assert "HIDDEN USER CONTENT" not in layout_text
        assert "HIDDEN MODEL CONTENT" not in layout_text
    assert rendered["restored"] == [42]
    assert rendered["activeControls"].startswith("MaskExclude?")
    assert rendered["maskedControls"].startswith("UnmaskExclude?")
    assert rendered["tooltipLinked"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_markdown_hides_excluded_message_content() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    renderer = viewer[viewer.index("function buildMarkdown()"):viewer.index("function buildSessionExportHtml")]
    state = """
const state = {
  selectedSession: { title: "Review", session_id: "review", token_count: 12 },
  exchanges: [{
    exchange_id: 42,
    is_deleted: true,
    deleted_reason: "Fresh perspective",
    user_message: "HIDDEN USER CONTENT",
    assistant_response: "HIDDEN MODEL CONTENT"
  }]
};
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", state + renderer + "\nprocess.stdout.write(buildMarkdown());"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "### Excluded turn #42" in completed.stdout
    assert "Note: Fresh perspective" in completed.stdout
    assert "HIDDEN USER CONTENT" not in completed.stdout
    assert "HIDDEN MODEL CONTENT" not in completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the browser renderer smoke test")
@requires_node
def test_admin_viewer_markdown_table_rendering() -> None:
    viewer = Path("admin-viewer.html").read_text(encoding="utf-8")
    renderer = viewer[viewer.index("function renderMarkdown"):viewer.index("function svgNode")]
    markdown = "\n".join(
        [
            "| Name | Score | Note |",
            "| :--- | ---: | :---: |",
            "| **Ada** | 42 | `a|b` |",
            "| <script>alert(1)</script> | 7 | left \\| right |",
            "| Only one |",
            "After",
        ]
    )
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", f'{renderer}\nprocess.stdout.write(renderMarkdown(process.argv[1]));', markdown],
        check=True,
        capture_output=True,
        text=True,
    )
    html = completed.stdout

    assert '<div class="markdown-table-wrap"><table>' in html
    assert '<th class="align-right">Score</th>' in html
    assert '<th class="align-center">Note</th>' in html
    assert '<td><strong>Ada</strong></td>' in html
    assert '<code>a|b</code>' in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert 'left | right' in html
    assert '<tr><td>Only one</td><td class="align-right"></td><td class="align-center"></td></tr>' in html
    assert '<p>After</p>' in html
    assert "<script>" not in html


def test_admin_api_requires_login_and_csrf_for_mutations(load_main) -> None:
    main = load_main(graph_experimental=True)
    session = main.store.create_session("s1", "Admin test", "manual-context")
    exchange = main.store.save_exchange("s1", "Claude", "Message.", "Answer to correct.")

    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    assert client.get("/admin/api/sessions").status_code == 401
    assert client.delete(f"/admin/api/exchanges/{exchange.exchange_id}").status_code == 401
    assert client.post(f"/admin/api/exchanges/{exchange.exchange_id}/mask").status_code == 401

    login = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    me = client.get("/admin/api/me")
    assert me.status_code == 200
    csrf_token = me.json()["csrf_token"]

    assert client.request(
        "DELETE",
        f"/admin/api/exchanges/{exchange.exchange_id}",
        json={"reason": "duplicate"},
    ).status_code == 403
    assert client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/mask",
    ).status_code == 403

    masked = client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/mask",
        headers={"x-csrf-token": csrf_token},
    )
    assert masked.status_code == 200
    assert masked.json()["exchange"]["is_masked"] is True
    assert masked.json()["exchange"]["is_excluded"] is False

    deleted = client.request(
        "DELETE",
        f"/admin/api/exchanges/{exchange.exchange_id}",
        json={"reason": "duplicate"},
        headers={"x-csrf-token": csrf_token},
    )
    assert deleted.status_code == 200
    assert deleted.json()["exchange"]["is_deleted"] is True
    assert main.store.list_exchanges(session.session_id) == []

    session_payload = client.get(f"/admin/api/sessions/{session.session_id}").json()
    first_exchange = session_payload["exchanges"][0]
    assert first_exchange["deleted_reason"] == "duplicate"
    assert first_exchange["user_message_token_count"] > 0
    assert first_exchange["assistant_response_token_count"] > 0
    assert first_exchange["total_token_count"] == (
        first_exchange["user_message_token_count"] + first_exchange["assistant_response_token_count"]
    )
    assert session_payload["session"]["token_count"] == first_exchange["total_token_count"]

    restored = client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/restore",
        headers={"x-csrf-token": csrf_token},
    )
    assert restored.status_code == 200
    assert restored.json()["exchange"]["is_deleted"] is False
    assert len(main.store.list_exchanges(session.session_id)) == 1
    assert restored.json()["exchange"]["is_masked"] is True

    unmasked = client.post(
        f"/admin/api/exchanges/{exchange.exchange_id}/unmask",
        headers={"x-csrf-token": csrf_token},
    )
    assert unmasked.status_code == 200
    assert unmasked.json()["exchange"]["is_masked"] is False


def test_admin_database_export_requires_csrf_and_keeps_files_on_server(
    load_main, tmp_path: Path
) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("export-me", "Export me", "manual-context")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    assert client.post("/admin/api/database/export").status_code == 401
    login = client.post(
        "/admin/login",
        data={
            "username": "owner",
            "password": "secret-admin-password",
            "next": "/admin/sessions",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert client.post("/admin/api/database/export").status_code == 403
    csrf = client.get("/admin/api/me").json()["csrf_token"]

    requested_path = tmp_path / "browser-selected-path"
    response = client.post(
        "/admin/api/database/export",
        headers={"x-csrf-token": csrf},
        json={"output": str(requested_path)},
    )

    assert response.status_code == 200
    payload = response.json()
    destination = Path(payload["export"]["artifact"]["path"])
    assert destination.parent == tmp_path / "exports"
    assert destination.is_dir()
    assert list(destination.glob("*/*.md"))
    assert "download" not in json.dumps(payload).lower()
    assert not requested_path.exists()
    assert response.headers["cache-control"] == "no-store"


@requires_node
def test_admin_database_export_button_sends_only_a_trigger_and_renders_vps_path() -> None:
    source = slice_source(
        "async function exportDatabaseMarkdown()", "// one panel, one save"
    )
    rendered = run_js(
        r"""
        const settingsDom = {
          databaseExportButton: document.createElement("button"),
          databaseExportResult: document.createElement("p"),
        };
        const calls = [];
        const statuses = [];
        const window = { confirm: () => true };
        async function api(path, options) {
          calls.push({ path, options });
          return { export: { artifact: {
            path: "/var/lib/mcp-session-bridge/exports/mcp-bridge-export-test",
            session_count: 3,
            attachment_count: 2,
          } } };
        }
        function renderSettingsStatus(message, kind) { statuses.push({ message, kind }); }
        """
        + source
        + r"""
        (async () => {
          await exportDatabaseMarkdown();
          emit({
            calls,
            result: settingsDom.databaseExportResult.textContent,
            disabled: settingsDom.databaseExportButton.disabled,
            busy: settingsDom.databaseExportButton.getAttribute("aria-busy"),
            statuses,
          });
        })();
        """,
    )

    assert rendered["calls"] == [
        {"path": "/admin/api/database/export", "options": {"method": "POST"}}
    ]
    assert "/var/lib/mcp-session-bridge/exports/" in rendered["result"]
    assert rendered["disabled"] is False
    assert rendered["busy"] == "false"
    assert rendered["statuses"][-1]["kind"] == "ok"


def test_admin_can_configure_ai_rename_and_update_session_title(load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "Chaotic long title", "manual-context")
    main.store.save_exchange("s1", "Claude", "Pierwsza wiadomość użytkownika o ewaluacji LLM.", "OK")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    settings = client.get("/admin/api/ai-settings")
    assert settings.status_code == 200
    assert settings.json()["settings"]["configured"] is False

    blocked = client.post(
        "/admin/api/sessions/s1/rename/ai",
        headers={"x-csrf-token": csrf_token},
    )
    assert blocked.status_code == 400

    saved = client.put(
        "/admin/api/ai-settings",
        json={"api_key": "sk-test-secret", "model": "gpt-5.4-nano"},
        headers={"x-csrf-token": csrf_token},
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["configured"] is True
    assert saved.json()["settings"]["api_key_preview"] == "sk-...cret"
    assert "sk-test-secret" not in saved.text
    assert main.store.get_app_setting("ai_rename.api_key") != "sk-test-secret"

    import app.admin as admin_module

    captured = {}

    def fake_suggest(api_key: str, model: str, first_user_message: str) -> str:
        captured["api_key"] = api_key
        captured["model"] = model
        captured["first_user_message"] = first_user_message
        return "Ewaluacja LLM"

    monkeypatch.setattr(admin_module, "_suggest_session_title", fake_suggest)

    renamed = client.post(
        "/admin/api/sessions/s1/rename/ai",
        headers={"x-csrf-token": csrf_token},
    )
    assert renamed.status_code == 200
    assert renamed.json()["session"]["title"] == "Ewaluacja LLM"
    assert main.store.get_session("s1").title == "Ewaluacja LLM"
    assert captured == {
        "api_key": "sk-test-secret",
        "model": "gpt-5.4-nano",
        "first_user_message": "Pierwsza wiadomość użytkownika o ewaluacji LLM.",
    }

    manual = client.patch(
        "/admin/api/sessions/s1",
        json={"title": "Manualny tytuł"},
        headers={"x-csrf-token": csrf_token},
    )
    assert manual.status_code == 200
    assert manual.json()["session"]["title"] == "Manualny tytuł"

    too_long = client.patch(
        "/admin/api/sessions/s1",
        json={"title": "x" * 73},
        headers={"x-csrf-token": csrf_token},
    )
    assert too_long.status_code == 400

    removed = client.request(
        "DELETE",
        "/admin/api/ai-settings/key",
        headers={"x-csrf-token": csrf_token},
    )
    assert removed.status_code == 200
    assert removed.json()["settings"]["configured"] is False


def test_admin_can_update_display_timezone(load_main) -> None:
    main = load_main(graph_experimental=True)
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    login = client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    invalid = client.post(
        "/admin/api/timezone",
        json={"timezone": "Not/AZone"},
        headers={"x-csrf-token": csrf_token},
    )
    assert invalid.status_code == 400

    updated = client.post(
        "/admin/api/timezone",
        json={"timezone": "Europe/Paris"},
        headers={"x-csrf-token": csrf_token},
    )

    assert updated.status_code == 200
    assert updated.json()["display_timezone"] == "Europe/Paris"
    assert main.store.get_app_setting(DISPLAY_TIMEZONE_SETTING_KEY) == "Europe/Paris"
    assert client.get("/admin/api/me").json()["display_timezone"] == "Europe/Paris"

    legacy_updated = client.put(
        "/admin/api/timezone",
        json={"timezone": "UTC"},
        headers={"x-csrf-token": csrf_token},
    )
    assert legacy_updated.status_code == 200
    assert legacy_updated.json()["display_timezone"] == "UTC"


def test_admin_can_manage_session_groups_and_move_sessions(load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "Admin group test", "manual-context")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    csrf_token = client.get("/admin/api/me").json()["csrf_token"]

    groups = client.get("/admin/api/session-groups")
    assert groups.status_code == 200
    assert {group["group_id"] for group in groups.json()["groups"]} == {"uncategorized"}

    created = client.post(
        "/admin/api/session-groups",
        json={"name": "Ideas", "color": "#22c55e", "icon_key": "car"},
        headers={"x-csrf-token": csrf_token},
    )
    assert created.status_code == 200
    assert created.json()["group"]["group_id"] == "ideas"
    assert created.json()["group"]["icon_key"] == "car"

    moved = client.patch(
        "/admin/api/sessions/s1",
        json={"group_id": "ideas"},
        headers={"x-csrf-token": csrf_token},
    )
    assert moved.status_code == 200
    assert moved.json()["session"]["group_id"] == "ideas"
    assert main.store.get_session("s1").group_id == "ideas"

    updated = client.patch(
        "/admin/api/session-groups/ideas",
        json={"name": "Idea Lab", "color": "#0ea5e9", "icon_key": "brain"},
        headers={"x-csrf-token": csrf_token},
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["name"] == "Idea Lab"

    health = client.post(
        "/admin/api/session-groups",
        json={"name": "Health", "color": "#ef4444", "icon_key": "medical_plus"},
        headers={"x-csrf-token": csrf_token},
    )
    assert health.status_code == 200
    assert health.json()["group"]["group_id"] == "health"
    assert health.json()["group"]["is_system"] is False

    system_edit = client.patch(
        "/admin/api/session-groups/uncategorized",
        json={"name": "Inbox"},
        headers={"x-csrf-token": csrf_token},
    )
    assert system_edit.status_code == 400

    deleted = client.request(
        "DELETE",
        "/admin/api/session-groups/ideas",
        json={"destination_group_id": "health"},
        headers={"x-csrf-token": csrf_token},
    )
    assert deleted.status_code == 200
    assert deleted.json()["group"]["deleted_at"] is not None
    assert main.store.get_session("s1").group_id == "health"

    bad_move = client.patch(
        "/admin/api/sessions/s1",
        json={"group_id": "ideas"},
        headers={"x-csrf-token": csrf_token},
    )
    assert bad_move.status_code == 404


def test_admin_sensitive_group_prunes_and_blocks_external_rag_scope(load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Private", "#ef4444", "lock", group_id="private")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    headers = {"x-csrf-token": client.get("/admin/api/me").json()["csrf_token"]}
    path = "/admin/api/session-groups/private"
    monkeypatch.setattr(main.admin, "_read_provider_key", lambda provider: "test-key")

    main.admin.search.set_config(SearchConfig(enabled=True, included_group_ids=("uncategorized",)))
    system_group = client.patch(
        "/admin/api/session-groups/uncategorized",
        json={"is_sensitive": True},
        headers=headers,
    )
    assert system_group.status_code == 400
    assert main.store.get_session_group("uncategorized").is_sensitive is False
    assert main.admin.search.get_config().included_group_ids == ("uncategorized",)

    main.admin.search.set_config(SearchConfig(enabled=True, included_group_ids=("private",)))
    enabled = client.patch(path, json={"is_sensitive": True}, headers=headers)
    assert enabled.status_code == 200
    assert enabled.json()["group"]["is_sensitive"] is True
    assert main.admin.search.get_config().included_group_ids == ()

    disabled = client.patch(path, json={"is_sensitive": False}, headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["group"]["is_sensitive"] is False
    assert main.admin.search.get_config().included_group_ids == ()

    assert client.patch(path, json={"is_sensitive": "true"}, headers=headers).status_code == 400
    client.patch(path, json={"is_sensitive": True}, headers=headers)
    sensitive_config = SearchConfig(enabled=True, included_group_ids=("private",)).to_dict()
    assert client.put("/admin/api/settings/search", json=sensitive_config, headers=headers).status_code == 400
    assert client.post("/admin/api/search/index/estimate", json=sensitive_config, headers=headers).status_code == 400

    client.patch(path, json={"is_sensitive": False}, headers=headers)
    main.admin.search.set_config(SearchConfig(enabled=True, included_group_ids=("private",)))
    with main.admin.search._connect() as conn:
        conn.execute("UPDATE search_index_state SET status='building' WHERE singleton=1")
    blocked = client.patch(path, json={"is_sensitive": True}, headers=headers)
    assert blocked.status_code == 409
    assert main.store.get_session_group("private").is_sensitive is False
    assert main.admin.search.get_config().included_group_ids == ("private",)


def test_admin_can_view_session_and_group_files(load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session("s1", "File admin test", "manual-context", group_id="tests")
    session_file = main.store.save_session_file("s1", "plan.md", "# Plan")
    group_file = main.store.save_group_file("tests", "shared.md", "Shared context")
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")

    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )

    session_payload = client.get("/admin/api/sessions/s1")
    assert session_payload.status_code == 200
    assert session_payload.json()["files"]["session"][0]["filename"] == "plan.md"
    assert session_payload.json()["files"]["group"][0]["filename"] == "shared.md"

    downloaded = client.get(f"/admin/api/files/{group_file.file_id}")
    assert downloaded.status_code == 200
    assert downloaded.json()["file"]["content"] == "Shared context"

    missing = client.get("/admin/api/files/999999")
    assert missing.status_code == 404

    invalid = client.get("/admin/api/files/not-a-number")
    assert invalid.status_code == 400

    assert session_file.file_id != group_file.file_id


def test_codex_admin_api_auth_csrf_and_chat_contract(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)

    class FakeCodex:
        async def status(self):
            return {
                "available": True,
                "authenticated": True,
                "version": "0.147.0",
                "account": {"type": "chatgpt", "email": "owner@example.com", "plan_type": "plus"},
            }

        async def start_device_login(self):
            return {
                "login_id": "login-1",
                "verification_url": "https://auth.openai.com/device",
                "user_code": "ABCD-EFGH",
            }

        async def device_login_status(self):
            return {**await self.status(), "login_status": "authenticated"}

        async def cancel_device_login(self):
            return None

        async def logout(self):
            return None

        async def chat(self, message, *, thread_id=None):
            assert message == "Hello"
            assert thread_id is None
            return {"thread_id": "thread-1", "message": "Hi from Codex"}

    main.admin.codex = FakeCodex()
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.get("/admin/api/codex/status").status_code == 401
    assert anonymous.post("/admin/api/codex/chat", json={"message": "Hello"}).status_code == 401

    client, csrf = admin_client(main)
    status = client.get("/admin/api/codex/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["codex"]["version"] == "0.147.0"

    assert client.post("/admin/api/codex/auth/device/start").status_code == 403
    started = client.post("/admin/api/codex/auth/device/start", headers={"x-csrf-token": csrf})
    assert started.status_code == 200
    assert started.json()["login"]["user_code"] == "ABCD-EFGH"
    assert client.get("/admin/api/codex/auth/device/status").json()["codex"]["login_status"] == "authenticated"
    assert client.post(
        "/admin/api/codex/auth/device/cancel", headers={"x-csrf-token": csrf}
    ).status_code == 200
    assert client.post(
        "/admin/api/codex/logout", headers={"x-csrf-token": csrf}
    ).status_code == 200

    chatted = client.post(
        "/admin/api/codex/chat",
        json={"message": "Hello", "thread_id": None},
        headers={"x-csrf-token": csrf},
    )
    assert chatted.status_code == 200
    assert chatted.json() == {
        "ok": True,
        "chat": {"thread_id": "thread-1", "message": "Hi from Codex"},
    }
    assert chatted.headers["cache-control"] == "no-store"

    assert client.post(
        "/admin/api/codex/chat",
        json={"message": "Hello", "unexpected": True},
        headers={"x-csrf-token": csrf},
    ).status_code == 400


def test_codex_admin_api_sanitizes_unavailable_and_protocol_errors(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    from app.codex_app_server import CodexProtocolError, CodexUnavailableError

    class UnavailableCodex:
        async def status(self):
            raise CodexUnavailableError("secret-token /var/lib/private/auth.json")

        async def chat(self, message, *, thread_id=None):
            raise CodexProtocolError("raw frame bearer-secret")

    main.admin.codex = UnavailableCodex()
    client, csrf = admin_client(main)

    status = client.get("/admin/api/codex/status")
    assert status.status_code == 503
    assert status.json() == {"ok": False, "error": "Codex App Server is unavailable."}
    assert "secret" not in status.text
    assert status.headers["cache-control"] == "no-store"

    chat = client.post(
        "/admin/api/codex/chat",
        json={"message": "Hello"},
        headers={"x-csrf-token": csrf},
    )
    assert chat.status_code == 502
    assert chat.json() == {"ok": False, "error": "Codex App Server request failed."}
    assert "bearer" not in chat.text


def test_codex_expired_conversation_has_stable_error_code(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)

    class ExpiredCodex:
        async def chat(self, message, *, thread_id=None):
            raise ValueError("Unknown or expired Codex conversation.")

    main.admin.codex = ExpiredCodex()
    client, csrf = admin_client(main)
    response = client.post(
        "/admin/api/codex/chat",
        json={"message": "Continue", "thread_id": "old-thread"},
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "codex_conversation_expired"
    assert response.headers["cache-control"] == "no-store"


def _encoded_file(content: bytes, *, filename: str = "notes.md", scope_type: str = "session") -> dict:
    return {
        "scope_type": scope_type,
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def test_admin_file_mutations_require_login_and_csrf(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "File mutations", "manual-context")
    saved = main.store.save_session_file("s1", "existing.md", "old")
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    calls = [
        ("POST", "/admin/api/sessions/s1/files", _encoded_file(b"hello")),
        ("PATCH", f"/admin/api/sessions/s1/files/{saved.file_id}", {"content": "new", "expected_sha256": saved.sha256}),
        ("DELETE", f"/admin/api/sessions/s1/files/{saved.file_id}", None),
    ]
    for method, path, payload in calls:
        assert anonymous.request(method, path, json=payload).status_code == 401

    client, csrf = admin_client(main)
    for method, path, payload in calls:
        assert client.request(method, path, json=payload).status_code == 403
    assert csrf


def test_admin_uploads_bounded_utf8_files_to_selected_session_or_group(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session("s1", "File mutations", "manual-context", group_id="tests")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"\xef\xbb\xbf# Hello", filename="README.MD"),
        headers=headers,
    )
    assert uploaded.status_code == 200
    session_file = uploaded.json()["file"]
    assert session_file["scope_type"] == "session"
    assert session_file["session_id"] == "s1"
    assert session_file["mime_type"] == "text/markdown"
    assert session_file["created_by"] == "owner"
    assert main.store.get_session_file(session_file["file_id"]).content == "# Hello"

    group_upload = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"a,b\n1,2", filename="data.csv", scope_type="group"),
        headers=headers,
    )
    assert group_upload.status_code == 200
    assert group_upload.json()["file"]["group_id"] == "tests"
    assert group_upload.json()["file"]["mime_type"] == "text/csv"

    bad_payloads = [
        {"scope_type": "session", "filename": "bad.md", "content_base64": "%%%"},
        _encoded_file(b"\xff", filename="bad.md"),
        _encoded_file(b"hello", filename="bad.exe"),
        _encoded_file(b"", filename="empty.md"),
        {"scope_type": "session", "files": [_encoded_file(b"one"), _encoded_file(b"two")]},
    ]
    for payload in bad_payloads:
        assert client.post("/admin/api/sessions/s1/files", json=payload, headers=headers).status_code == 400

    assert client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"x" * 1_000_001),
        headers=headers,
    ).status_code == 400

    oversized = b'{"padding":"' + b"x" * 26_700_000 + b'"}'
    misleading = client.build_request(
        "POST",
        "/admin/api/sessions/s1/files",
        content=oversized,
        headers={**headers, "content-type": "application/json", "content-length": "1"},
    )
    assert client.send(misleading).status_code == 413
    absent = client.build_request(
        "POST",
        "/admin/api/sessions/s1/files",
        content=oversized,
        headers={**headers, "content-type": "application/json"},
    )
    del absent.headers["content-length"]
    assert client.send(absent).status_code == 413
    assert len(main.store.list_session_files(session_id="s1")) == 1


def test_admin_uploads_previews_and_downloads_original_pdf(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "PDF admin", "manual-context")
    client, csrf = admin_client(main)
    raw = make_pdf("Admin PDF text")

    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(raw, filename="brief.PDF"),
        headers={"x-csrf-token": csrf},
    )

    assert uploaded.status_code == 200
    manifest = uploaded.json()["file"]
    assert manifest["mime_type"] == "application/pdf"
    assert manifest["content_kind"] == "pdf"
    assert manifest["page_count"] == 1
    assert manifest["text_available"] is True

    detail = client.get(f"/admin/api/files/{manifest['file_id']}")
    assert detail.status_code == 200
    assert "Admin PDF text" in detail.json()["file"]["content"]

    original = client.get(f"/admin/api/files/{manifest['file_id']}/raw")
    assert original.status_code == 200
    assert original.content == raw
    assert original.headers["content-type"] == "application/pdf"
    assert original.headers["content-disposition"].startswith("inline;")

    attachment = client.get(f"/admin/api/files/{manifest['file_id']}/raw?download=1")
    assert attachment.headers["content-disposition"].startswith("attachment;")

    group_upload = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(raw, filename="shared.pdf", scope_type="group"),
        headers={"x-csrf-token": csrf},
    )
    assert group_upload.status_code == 200
    assert group_upload.json()["file"]["scope_type"] == "group"
    assert group_upload.json()["file"]["group_id"] == "uncategorized"


def test_admin_pdf_raw_requires_login_and_pdf_cannot_be_edited(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "PDF admin", "manual-context")
    client, csrf = admin_client(main)
    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(make_pdf(), filename="brief.pdf"),
        headers={"x-csrf-token": csrf},
    ).json()["file"]

    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.get(f"/admin/api/files/{uploaded['file_id']}/raw").status_code == 401
    missing = client.get("/admin/api/files/999999/raw")
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "Unknown file_id: 999999"}
    text_file = main.store.save_session_file("s1", "notes.md", "Text only")
    text_raw = client.get(f"/admin/api/files/{text_file.file_id}/raw")
    assert text_raw.status_code == 400
    assert text_raw.json() == {
        "ok": False,
        "error": "Raw binary content is available only for PDF files.",
    }
    edited = client.patch(
        f"/admin/api/sessions/s1/files/{uploaded['file_id']}",
        json={"content": "replacement", "expected_sha256": uploaded["sha256"]},
        headers={"x-csrf-token": csrf},
    )
    assert edited.status_code == 400
    assert "cannot be edited" in edited.json()["error"]

    assert anonymous.get(
        "/admin/assets/pdfjs/pdf.min.mjs",
        follow_redirects=False,
    ).status_code == 303
    asset = client.get("/admin/assets/pdfjs/pdf.min.mjs")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert "immutable" in asset.headers["cache-control"]
    assert client.get("/admin/assets/pdfjs/not-allowed.mjs").status_code == 404


def test_admin_group_upload_uses_session_current_group_atomically(admin_client, load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("First", "#22c55e", "science")
    main.store.create_session_group("Second", "#3b82f6", "ideas")
    main.store.create_session("s1", "File mutations", "manual-context", group_id="first")
    client, csrf = admin_client(main)
    original_selected_session = main.admin._selected_session

    def select_then_move(request):
        selected, error = original_selected_session(request)
        main.store.set_session_group("s1", "second")
        return selected, error

    monkeypatch.setattr(main.admin, "_selected_session", select_then_move)

    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"Current context", filename="context.md", scope_type="group"),
        headers={"x-csrf-token": csrf},
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["file"]["group_id"] == "second"
    assert main.store.list_session_files(group_id="first") == []


def test_admin_edits_moves_and_deletes_only_visible_files(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Tests", "#22c55e", "science")
    main.store.create_session_group("Other", "#ef4444", "camera")
    main.store.create_session("s1", "File mutations", "manual-context", group_id="tests")
    main.store.create_session("s2", "Other session", "manual-context", group_id="other")
    saved = main.store.save_session_file("s1", "notes.md", "old")
    unrelated = main.store.save_session_file("s2", "private.md", "untouched")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}
    path = f"/admin/api/sessions/s1/files/{saved.file_id}"

    assert client.patch(
        path,
        json={"content": "new", "expected_sha256": saved.sha256, "scope_type": "group"},
        headers=headers,
    ).status_code == 400

    edited = client.patch(
        path,
        json={"content": "new", "expected_sha256": saved.sha256},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["file"]["file_id"] == saved.file_id
    assert edited.json()["file"]["sha256"] != saved.sha256
    assert "content" not in edited.json()["file"]

    stale = client.patch(
        path,
        json={"content": "stale write", "expected_sha256": saved.sha256},
        headers=headers,
    )
    assert stale.status_code == 409
    assert "content" not in stale.text

    moved = client.patch(path, json={"scope_type": "group"}, headers=headers)
    assert moved.status_code == 200
    assert moved.json()["file"]["group_id"] == "tests"
    assert client.patch(path, json={"scope_type": "group"}, headers=headers).status_code == 200
    moved_back = client.patch(path, json={"scope_type": "session"}, headers=headers)
    assert moved_back.status_code == 200
    assert moved_back.json()["file"]["session_id"] == "s1"

    assert client.patch(
        path,
        json={"scope_type": "group", "group_id": "other"},
        headers=headers,
    ).status_code == 400
    assert client.patch(
        f"/admin/api/sessions/s1/files/{unrelated.file_id}",
        json={"scope_type": "group"},
        headers=headers,
    ).status_code == 404

    deleted = client.delete(path, headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["file"]["file_id"] == saved.file_id
    assert "content" not in deleted.json()["file"]
    assert client.delete(path, headers=headers).status_code == 404
    assert client.get(f"/admin/api/files/{saved.file_id}").status_code == 404
    assert main.store.get_session_file(unrelated.file_id).content == "untouched"


def test_admin_file_mutations_conflict_if_file_moves_after_visibility_check(admin_client, load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("First", "#22c55e", "science")
    main.store.create_session_group("Second", "#ef4444", "camera")
    main.store.create_session("s1", "First", "manual-context", group_id="first")
    main.store.create_session("s2", "Second", "manual-context", group_id="second")
    edit_file = main.store.save_session_file("s1", "edit.md", "Original")
    move_file = main.store.save_session_file("s1", "move.md", "Original")
    delete_file = main.store.save_session_file("s1", "delete.md", "Original")
    pending_moves = {edit_file.file_id, move_file.file_id, delete_file.file_id}
    original_get = main.store.get_session_file

    def move_after_visibility_check(file_id):
        saved = original_get(file_id)
        if file_id in pending_moves:
            pending_moves.remove(file_id)
            main.store.move_session_file(file_id, scope_type="session", session_id="s2")
        return saved

    monkeypatch.setattr(main.store, "get_session_file", move_after_visibility_check)
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    assert client.patch(
        f"/admin/api/sessions/s1/files/{edit_file.file_id}",
        json={"content": "Changed", "expected_sha256": edit_file.sha256},
        headers=headers,
    ).status_code == 409
    assert client.patch(
        f"/admin/api/sessions/s1/files/{move_file.file_id}",
        json={"scope_type": "group"},
        headers=headers,
    ).status_code == 409
    assert client.delete(
        f"/admin/api/sessions/s1/files/{delete_file.file_id}",
        headers=headers,
    ).status_code == 409

    monkeypatch.setattr(main.store, "get_session_file", original_get)
    for saved in (edit_file, move_file, delete_file):
        current = main.store.get_session_file(saved.file_id)
        assert current is not None
        assert current.session_id == "s2"
        assert current.content == "Original"


def test_admin_rejects_oversized_or_malformed_patch_lengths_without_mutation(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("s1", "File mutations", "manual-context")
    saved = main.store.save_session_file("s1", "notes.md", "Original")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf, "content-type": "application/json"}
    path = f"/admin/api/sessions/s1/files/{saved.file_id}"
    oversized = (
        b'{"content":"'
        + b"x" * 6_100_000
        + f'","expected_sha256":"{saved.sha256}"}}'.encode()
    )

    misleading = client.build_request(
        "PATCH",
        path,
        content=oversized,
        headers={**headers, "content-length": "1"},
    )
    assert client.send(misleading).status_code == 413

    absent = client.build_request("PATCH", path, content=oversized, headers=headers)
    del absent.headers["content-length"]
    assert client.send(absent).status_code == 413

    malformed = client.build_request(
        "PATCH",
        path,
        content=b"{}",
        headers={**headers, "content-length": "not-a-number"},
    )
    assert client.send(malformed).status_code == 400
    assert main.store.get_session_file(saved.file_id) == saved


def test_admin_file_workspace_stays_consistent_with_mcp_reads(admin_client, load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session_group("Ideas", "#22c55e", "science")
    main.store.create_session("s1", "Owner session", "manual-context", group_id="ideas")
    main.store.create_session("s2", "Peer session", "manual-context", group_id="ideas")
    client, csrf = admin_client(main)
    headers = {"x-csrf-token": csrf}

    uploaded = client.post(
        "/admin/api/sessions/s1/files",
        json=_encoded_file(b"First draft", filename="plan.md"),
        headers=headers,
    )
    assert uploaded.status_code == 200
    original = uploaded.json()["file"]
    file_id = original["file_id"]
    manifest_keys = {
        "file_id",
        "scope_type",
        "session_id",
        "group_id",
        "filename",
        "mime_type",
        "sha256",
        "size_bytes",
        "created_by",
        "created_at",
        "content_kind",
        "page_count",
        "extraction_status",
        "extracted_text_bytes",
        "text_available",
    }
    assert set(original) == manifest_keys
    assert [item["file_id"] for item in client.get("/admin/api/sessions/s1").json()["files"]["session"]] == [file_id]
    assert main.get_session_overview("s2")["files"]["group"] == []

    path = f"/admin/api/sessions/s1/files/{file_id}"
    moved_to_group = client.patch(path, json={"scope_type": "group"}, headers=headers)
    assert moved_to_group.status_code == 200
    assert moved_to_group.json()["file"]["file_id"] == file_id
    for session_id in ("s1", "s2"):
        assert [item["file_id"] for item in client.get(f"/admin/api/sessions/{session_id}").json()["files"]["group"]] == [file_id]
        assert [item["file_id"] for item in main.get_session_overview(session_id)["files"]["group"]] == [file_id]
    assert [item["file_id"] for item in main.list_session_files(session_id="s2")["files"]] == [file_id]

    moved_to_session = client.patch(path, json={"scope_type": "session"}, headers=headers)
    assert moved_to_session.status_code == 200
    assert moved_to_session.json()["file"]["file_id"] == file_id
    assert main.get_session_overview("s2")["files"]["group"] == []
    assert [item["file_id"] for item in main.get_session_overview("s1")["files"]["session"]] == [file_id]

    edited = client.patch(
        path,
        json={"content": "Updated draft", "expected_sha256": original["sha256"]},
        headers=headers,
    )
    assert edited.status_code == 200
    current = edited.json()["file"]
    assert set(current) == manifest_keys
    assert current["file_id"] == file_id
    assert current["sha256"] != original["sha256"]
    assert current["size_bytes"] == len("Updated draft".encode("utf-8"))
    assert main.download_session_file(session_id="s1", file_id=file_id)["file"]["content"] == "Updated draft"

    stale = client.patch(
        path,
        json={"content": "Stale overwrite", "expected_sha256": original["sha256"]},
        headers=headers,
    )
    assert stale.status_code == 409
    assert main.download_session_file(session_id="s1", file_id=file_id)["file"]["content"] == "Updated draft"

    assert client.patch(path, json={"scope_type": "group"}, headers=headers).status_code == 200
    assert [item["file_id"] for item in main.get_session_overview("s2")["files"]["group"]] == [file_id]
    deleted = client.delete(path, headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["file"]["file_id"] == file_id
    for session_id in ("s1", "s2"):
        admin_files = client.get(f"/admin/api/sessions/{session_id}").json()["files"]
        assert admin_files == {"session": [], "group": []}
        assert main.get_session_overview(session_id)["files"] == {"session": [], "group": []}
    assert main.list_session_files(session_id="s1")["files"] == []
    assert main.download_session_file(session_id="s1", file_id=file_id) == {
        "ok": False,
        "error": "File is unavailable for this session.",
    }


def test_restart_helper_uses_fixed_systemctl_command(load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)
    invocation = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_subprocess_exec(*args, **kwargs):
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", create_subprocess_exec)
    asyncio.run(main._request_service_restart())

    assert invocation == {
        "args": (
            "/bin/systemctl",
            "start",
            "--no-block",
            "mcp-session-bridge-restart.service",
        ),
        "kwargs": {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        },
    }


def test_managed_restart_writes_runtime_request_file(load_main, tmp_path, monkeypatch) -> None:
    request_file = tmp_path / "run" / "restart-request"
    request_file.parent.mkdir()
    main = load_main(
        graph_experimental=True,
        env={"BRIDGE_RESTART_REQUEST_FILE": str(request_file)},
    )

    asyncio.run(main._request_service_restart())

    assert request_file.exists()
    assert request_file.stat().st_mode & 0o777 == 0o600


def test_restart_helper_surfaces_nonzero_systemctl_result(load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"systemd rejected request"

    async def create_subprocess_exec(*args, **kwargs):
        return Process()

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", create_subprocess_exec)

    with pytest.raises(RuntimeError, match="systemd rejected request"):
        asyncio.run(main._request_service_restart())


def test_restart_helper_terminates_and_reaps_timed_out_systemctl(load_main, monkeypatch) -> None:
    main = load_main(graph_experimental=True)

    class Process:
        returncode = None
        terminated = False
        killed = False
        wait_calls = 0

        async def communicate(self):
            await asyncio.Event().wait()

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        async def wait(self):
            self.wait_calls += 1
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return self.returncode

    process = Process()

    async def create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(main, "RESTART_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(main, "RESTART_CLEANUP_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="timed out after 0.01 seconds"):
        asyncio.run(main._request_service_restart())

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2

def test_admin_search_settings_keys_and_basic_search_api(load_main) -> None:
    main = load_main(graph_experimental=True)
    main.store.create_session("search-session", "Searchable session", "manual-context")
    main.store.save_exchange(
        "search-session", "Codex", "The admin search contains a kumquat marker.", "Confirmed."
    )
    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    csrf = client.get("/admin/api/me").json()["csrf_token"]
    headers = {"x-csrf-token": csrf}

    settings = client.get("/admin/api/settings")
    assert settings.status_code == 200
    assert set(settings.json()["settings"]) == {
        "general", "search", "api", "groups", "index", "transcript", "tool_output"
    }
    assert settings.json()["settings"]["search"]["enabled"] is False
    assert settings.json()["settings"]["transcript"]["chunk_max_chars"] == 12000
    assert settings.json()["settings"]["transcript"]["chunk_max_lines"] == 180
    assert settings.json()["settings"]["tool_output"] == {
        "active_mode": "optimized",
        "configured_mode": "optimized",
        "restart_required": False,
        "restart_pending": False,
        "pending_mode": None,
    }

    service_status = client.get("/admin/api/service/status")
    assert service_status.status_code == 200
    assert service_status.json()["tool_output"] == settings.json()["settings"]["tool_output"]

    no_restart_needed = client.post("/admin/api/service/restart", headers=headers)
    assert no_restart_needed.status_code == 409

    invalid_tool_output = client.put(
        "/admin/api/settings/tool-output",
        json={"mode": "structured_only"},
        headers=headers,
    )
    assert invalid_tool_output.status_code == 400

    tool_output_update = client.put(
        "/admin/api/settings/tool-output",
        json={"mode": "maximum_compatibility"},
        headers=headers,
    )
    assert tool_output_update.status_code == 200
    assert set(tool_output_update.json()) == {"ok", "tool_output"}
    assert tool_output_update.json()["tool_output"]["active_mode"] == "optimized"
    assert tool_output_update.json()["tool_output"]["configured_mode"] == "maximum_compatibility"
    assert tool_output_update.json()["tool_output"]["restart_required"] is True

    anonymous_client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous_client.post("/admin/api/service/restart").status_code == 401
    assert client.post("/admin/api/service/restart").status_code == 403

    main.admin.restart_requester = None
    unavailable_restart = client.post("/admin/api/service/restart", headers=headers)
    assert unavailable_restart.status_code == 503
    assert "unavailable" in unavailable_restart.json()["error"].lower()
    assert client.get("/admin/api/service/status").json()["tool_output"]["restart_pending"] is False

    async def fail_restart_with_os_error() -> None:
        raise OSError("systemctl missing")

    main.admin.restart_requester = fail_restart_with_os_error
    os_error_restart = client.post("/admin/api/service/restart", headers=headers)
    assert os_error_restart.status_code == 503
    assert "systemctl missing" in os_error_restart.json()["error"]
    assert client.get("/admin/api/service/status").json()["tool_output"]["restart_pending"] is False

    async def reject_restart() -> None:
        raise RuntimeError("systemd unavailable")

    main.admin.restart_requester = reject_restart
    failed_restart = client.post("/admin/api/service/restart", headers=headers)
    assert failed_restart.status_code == 503
    assert client.get("/admin/api/service/status").json()["tool_output"]["restart_pending"] is False

    restart_requests = []

    async def request_restart() -> None:
        restart_requests.append(True)

    main.admin.restart_requester = request_restart
    restart = client.post("/admin/api/service/restart", headers=headers)
    assert restart.status_code == 202
    assert restart.json()["restart_requested"] is True
    assert restart.json()["tool_output"]["restart_pending"] is True
    assert restart.json()["tool_output"]["pending_mode"] == "maximum_compatibility"
    assert restart_requests == [True]

    pending_status = client.get("/admin/api/service/status").json()["tool_output"]
    assert pending_status["restart_pending"] is True
    assert pending_status["restart_required"] is True

    blocked_update = client.put(
        "/admin/api/settings/tool-output",
        json={"mode": "optimized"},
        headers=headers,
    )
    assert blocked_update.status_code == 409
    assert main.store.get_app_setting("mcp.tool_output_mode") == "maximum_compatibility"
    assert main.store.get_app_setting("mcp.tool_output_restart_pending_mode") == "maximum_compatibility"

    async def enter_app_lifespan() -> None:
        async with main.app_lifespan(None):
            assert main.store.get_app_setting("mcp.tool_output_restart_pending_mode") is None

    asyncio.run(enter_app_lifespan())

    transcript_update = client.put(
        "/admin/api/settings/transcript",
        json={"chunk_max_chars": 64_000, "chunk_max_lines": 600},
        headers=headers,
    )
    assert transcript_update.status_code == 200
    assert transcript_update.json()["settings"]["transcript"]["chunk_max_chars"] == 64_000
    assert main.store.get_app_setting("transcript.chunk_max_chars") == "64000"

    invalid_transcript_update = client.put(
        "/admin/api/settings/transcript",
        json={"chunk_max_chars": 999, "chunk_max_lines": 600},
        headers=headers,
    )
    assert invalid_transcript_update.status_code == 400

    started_probe = main.run_output_probe("chatgpt-web", 12_000, "transcript_markdown")
    stored_probe = main.store.get_output_probe_run(started_probe["run_id"])
    assert stored_probe is not None
    checkpoint_canaries = [
        block["canary"] for block in stored_probe["blocks"] if block["checkpoint_labels"]
    ]
    last_probe_block = stored_probe["blocks"][-1]
    main.submit_output_probe_observation(
        started_probe["run_id"],
        checkpoint_canaries,
        last_probe_block["index"],
        last_probe_block["canary"],
    )
    probe_settings = client.get("/admin/api/settings").json()["settings"]["transcript"]
    assert probe_settings["runs"][0]["result_class"] == "complete"
    assert probe_settings["runs"][0]["tool_output_mode"] == "optimized"
    assert probe_settings["recommendations"][0]["recommended_chunk_chars"] == 9_000
    assert "blocks" not in probe_settings["runs"][0]
    assert "observed_canaries" not in probe_settings["runs"][0]

    estimate_config = {
        **settings.json()["settings"]["search"],
        "enabled": True,
        "included_group_ids": ["uncategorized"],
    }
    estimate = client.post(
        "/admin/api/search/index/estimate", json=estimate_config, headers=headers
    )
    assert estimate.status_code == 200
    assert estimate.json()["estimate"]["document_count"] == 1
    assert estimate.json()["estimate"]["embedding_token_count"] > 0
    assert estimate.json()["estimate"]["estimated_cost_usd"] is not None

    with main.admin.search._connect() as conn:
        conn.execute("UPDATE search_index_state SET status='building' WHERE singleton=1")
    blocked_search_settings = {
        **settings.json()["settings"]["search"],
        "enabled": True,
        "included_group_ids": ["uncategorized"],
    }
    blocked = client.put(
        "/admin/api/settings/search", json=blocked_search_settings, headers=headers
    )
    assert blocked.status_code == 409
    with main.admin.search._connect() as conn:
        conn.execute("UPDATE search_index_state SET status='empty' WHERE singleton=1")

    key_value = "sk-test-secret-value"
    updated_keys = client.put(
        "/admin/api/settings/api",
        json={"openai": key_value, "cohere": "cohere-test-secret"},
        headers=headers,
    )
    assert updated_keys.status_code == 200
    key_payload = updated_keys.json()["settings"]["api"]
    assert key_payload["openai"]["configured"] is True
    assert key_payload["cohere"]["configured"] is True
    assert key_value not in updated_keys.text
    assert key_value not in (main.store.get_app_setting("providers.openai.api_key") or "")

    search = client.post("/admin/api/search", json={"query": "kumquat", "mode": "basic"})
    assert search.status_code == 200
    result = search.json()["results"][0]
    assert result["session_id"] == "search-session"
    assert result["pipeline"] == ["BM25"]
    assert result["group"]["group_id"] == "uncategorized"

    removed = client.delete("/admin/api/settings/api/cohere/key", headers=headers)
    assert removed.status_code == 200
    assert removed.json()["settings"]["api"]["cohere"]["configured"] is False


def test_admin_operational_status_is_authenticated_and_secret_free(load_main, tmp_path, monkeypatch) -> None:
    import app.admin as admin_module

    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overall": "attention",
                "secret": "test-secret",
                "version": {"current": "0.4.0", "database_schema": 1},
                "update": {
                    "state": "available",
                    "current": "0.4.0",
                    "latest": "0.4.1",
                    "release_url": "https://github.test/v0.4.1",
                },
                "checks": [
                    {"id": "service", "label": "Bridge service", "state": "pass", "message": "active"}
                ],
                "installation": {
                    "mode": "managed",
                    "public_base_url": "https://bridge.example.test",
                },
                "last_operation": {"operation": "setup", "state": "complete"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(admin_module, "BRIDGE_VERSION_LABEL", "2026.8.2-beta")
    main = load_main(
        graph_experimental=True,
        env={"BRIDGE_OPERATIONAL_STATUS_FILE": str(status_path)},
    )
    anonymous = TestClient(main.app, base_url="http://127.0.0.1:8787")
    assert anonymous.get("/admin/api/status").status_code == 401

    client = TestClient(main.app, base_url="http://127.0.0.1:8787")
    client.post(
        "/admin/login",
        data={"username": "owner", "password": "secret-admin-password", "next": "/admin/sessions"},
        follow_redirects=False,
    )
    response = client.get("/admin/api/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    status = response.json()["status"]
    assert status["format_version"] == 1
    assert "schema_version" not in status
    assert status["version"]["database_schema"] == 2
    assert status["version"]["current"] == "0.4.0"
    assert status["version"]["label"] == "2026.8.2-beta"
    assert status["update"]["state"] == "available"
    assert status["live"]["application"] == "pass"
    assert "test-secret" not in response.text

@requires_node
def test_search_snippets_highlight_matches_without_interpreting_markup() -> None:
    """Search snippets come from stored transcripts, so they must never be HTML."""
    source = slice_source("function appendHighlightedText", "function selectSearchResult")
    result = run_js(
        source
        + """
        const container = document.createElement("div");
        appendHighlightedText(container, input.text, input.ranges);
        emit({ tree: describe(container), text: flatText(container) });
        """,
        payload={
            "text": "before <img src=x onerror=alert(1)> after",
            "ranges": [{"start": 7, "end": 10}],
        },
    )

    assert result["text"] == "before <img src=x onerror=alert(1)> after"
    marks = [child for child in result["tree"]["children"] if child["tag"] == "mark"]
    assert [mark["text"] for mark in marks] == ["<im"]
    # Every node carries text, never markup: nothing can inject an element.
    assert all(child["innerHTML"] == "" for child in result["tree"]["children"])


@requires_node
def test_deleting_a_group_moves_its_files_to_the_destination_group() -> None:
    source = slice_source(
        'if (groupMatch && method === "DELETE")', 'if (path === "/admin/api/sessions")'
    )
    result = run_js(
        """
        const demo = input.demo;
        const jsonResponse = (value) => value;
        function handleDelete(body, method, groupMatch) {
        """
        + source
        + """
        }
        // groupMatch is the route regex match: [full, groupId]
        handleDelete(input.body, "DELETE", ["", input.groupId]);
        emit(demo.files);
        """,
        payload={
            "groupId": "research",
            "body": {"destination_group_id": "archive"},
            "demo": {
                "groups": [{"group_id": "research"}, {"group_id": "archive"}],
                "sessions": [],
                "files": [
                    {"file_id": "f1", "scope_type": "group", "group_id": "research"},
                    {"file_id": "f2", "scope_type": "group", "group_id": "other"},
                    {"file_id": "f3", "scope_type": "session", "group_id": "research"},
                ],
            },
        },
        dom=False,
    )

    moved = {file["file_id"]: file["group_id"] for file in result}
    assert moved["f1"] == "archive", "a group file must follow the group it belonged to"
    assert moved["f2"] == "other", "files of other groups must be left alone"
    assert moved["f3"] == "research", "session-scoped files are not group files"


@requires_node
def test_viewer_startup_script_has_no_use_before_definition() -> None:
    """The sensitive-icon pass runs at load time and reads SVG constants.

    Ordering the two wrongly throws a ReferenceError that blanks the whole page,
    which is invisible to any assertion about the source text.
    """
    source = viewer_source()
    constants = source.index("const GROUP_ICON_SVG_ATTRS")
    startup = source.index(
        'for (const icon of document.querySelectorAll('
        '".sensitive-overlay-icon, .sensitive-toggle-icon"))'
    )
    assert constants < startup, (
        "GROUP_ICON_SVG_ATTRS is declared after the startup pass that reads it; "
        "the const is in its temporal dead zone and the page will not render"
    )

    # Prove the ordering is what actually matters by running the two together.
    declaration = source[constants : source.index("\n", constants)]
    result = run_js(
        declaration
        + """
        try { emit({ ok: Boolean(GROUP_ICON_SVG_ATTRS) }); }
        catch (error) { emit({ ok: false, error: error.name }); }
        """,
        dom=False,
    )
    assert result["ok"] is True


def test_admin_page_serves_every_asset_it_references(admin_client) -> None:
    """A reference in the page is worthless if the route behind it is missing."""
    client, _ = admin_client(graph_experimental=True)
    page = client.get("/admin/sessions")
    assert page.status_code == 200

    referenced = sorted(set(re.findall(r'(?:href|src)="(/admin/assets/[^"]+)"', page.text)))
    assert referenced, "the admin page should reference its stylesheets and scripts"

    broken = {
        asset: client.get(asset).status_code
        for asset in referenced
        if client.get(asset).status_code != 200
    }
    assert broken == {}

    lockup = "/admin/assets/brand/svg/lockup-horizontal-dark.svg"
    assert lockup in referenced
    assert page.text.count(lockup) == 1, "the brand lockup should appear once"


FEATURE_CONTROLS = {
    "session list": ["sessionMoveDialog", "sessionMoveGroupList", "sessionMoveConfirmation", "sessionMoveConfirm"],
    "file workspace": ["fileWorkspaceDialog", "fileWorkspaceOpen", "fileWorkspaceCount", "fileWorkspaceListPane", "fileWorkspaceDetailPane", "fileWorkspaceBack"],
    "file editing": ["fileEditButton", "fileEditor", "fileDeletePane", "fileDeleteWarning", "fileGuardPane", "fileGuardSave", "fileGuardDiscard", "fileGuardKeepEditing"],
    "file upload": ["groupFileInput", "sessionFileInput"],
    "session export": ["exportHtmlButton"],
    "general settings": ["identity", "timezoneSelect"],
    "transcript settings": ["transcriptChunkMaxChars", "transcriptChunkMaxLines"],
    "tool output compatibility": ["toolOutputMaximumCompatibility", "toolOutputOptimized", "toolOutputRestart", "toolOutputRestartMessage", "toolOutputRestartRequired"],
    "output probe": ["probeHarnessLabel", "probeTargetChars", "probeContentProfile", "probePromptCopy", "probeResults"],
    "search": ["searchOpenButton", "searchDialog", "localSearchResults"],
    "rag settings": ["ragEnabled", "cohereEnabled", "ragGroupList"],
    "search index": ["indexRebuild", "indexReadyCheck", "indexBuiltAt", "indexCancel", "indexDelete", "indexEstimate", "indexEstimateDocuments", "indexEstimateTokens", "indexEstimateCost"],
    "bridge status": ["settingsUpdateDot", "statusUpdateDot", "bridgeStatusChecks"],
    "database export": ["databaseExportButton", "databaseExportResult"],
}


@pytest.mark.parametrize("feature", sorted(FEATURE_CONTROLS))
def test_admin_page_exposes_the_controls_each_feature_needs(admin_client, feature) -> None:
    """Element ids are the contract between the markup and the viewer script.

    This deliberately checks ids and nothing about styling or wording: a control
    that disappears breaks the feature, whereas a restyled one does not.
    """
    client, _ = admin_client(graph_experimental=True)
    page = client.get("/admin/sessions").text

    missing = [name for name in FEATURE_CONTROLS[feature] if f'id="{name}"' not in page]
    assert missing == [], f"{feature} lost its controls: {missing}"


def test_admin_page_does_not_ship_retired_controls(admin_client) -> None:
    client, _ = admin_client(graph_experimental=True)
    page = client.get("/admin/sessions").text

    # Codex was removed from the workspace: neither markup nor route may return.
    for retired in ('id="codexOpenButton"', 'id="codexDialog"', "/admin/api/codex"):
        assert retired not in page
    assert client.get("/admin/api/codex").status_code == 404

    # Replaced by the search dialog and the file workspace respectively.
    for retired in ('id="searchInput"', 'id="filesPanel"', 'id="fileDialog"'):
        assert retired not in page

    # Dialogs are declared once; a second copy makes getElementById ambiguous.
    for dialog in ("searchDialog", "aiSettingsDialog", "fileWorkspaceDialog"):
        assert page.count(f'<dialog id="{dialog}"') == 1

    for tab in ("general", "search", "api", "transcript", "database", "status"):
        assert f'data-settings-tab="{tab}"' in page
        assert f'data-settings-panel="{tab}"' in page


@requires_node
def test_session_export_produces_nothing_for_an_unrevealed_sensitive_thread() -> None:
    """Export is a download: a guarded conversation must not reach the disk."""
    source = slice_source(
        "function selectedThreadIsGuarded()", "function setLayout(layout)"
    ) + slice_source("function buildSessionExportHtml()", "function exportMessageHtml")

    def build(*, is_sensitive: bool, revealed: bool) -> str:
        return run_js(
            """
            const state = {
              selectedSession: { session_id: "s1", title: "Quarterly numbers", group_id: "g1" },
              groups: [{ group_id: "g1", is_sensitive: input.is_sensitive }],
              revealedSensitiveThreads: new Set(input.revealed ? ["g1"] : []),
              exchanges: [{ user_message: "Ask", assistant_response: "SECRET-PAYLOAD", is_deleted: 0 }],
            };
            const userDisplayName = () => "Owner";
            const formatDate = () => "date";
            const exportMessageHtml = (name, body) => `<p>${name}: ${body}</p>`;
            const escapeHtml = (value) => String(value).replace(/[&<>"]/g, (c) => (
              { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
            ));
            """
            + source
            + """
            emit(buildSessionExportHtml());
            """,
            payload={"is_sensitive": is_sensitive, "revealed": revealed},
            dom=False,
        )

    guarded = build(is_sensitive=True, revealed=False)
    assert guarded == ""

    revealed = build(is_sensitive=True, revealed=True)
    assert "SECRET-PAYLOAD" in revealed
    assert "Quarterly numbers" in revealed

    ordinary = build(is_sensitive=False, revealed=False)
    assert "SECRET-PAYLOAD" in ordinary
