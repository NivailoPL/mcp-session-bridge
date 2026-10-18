---
title: Topics v2 and the shared header - Plan
type: feat
date: 2026-10-18
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Topics v2 and the shared header - Plan

## Goal Capsule

- **Objective:** Turn the Topics lab into the agreed Topics workspace (one main view, a "Manage topics" overlay, a Graph view) and give every workspace the same header with Search and Settings; then reset the production taxonomy and test the whole flow from an empty map.
- **Design source:** canvas https://claude.ai/artifact/V3E5ukEhM415RHbmn89wKJ, page "Topics · v2" (boards 1–7) and page "Header · search + settings" (variant A, without Log out; Settings overlay with a Topics tab).
- **Authority:** this plan, then `docs/plans/2026-10-17-001-feat-topics-lab-plan.md` (phase A is live), then repo conventions (additive schema, CSRF guard, run/journal records, sensitive groups opt-in, token master switch).
- **Design rules:** one main view per workspace, everything else as closable overlays; headings by function, buttons carry the model badge from settings (e.g. "Sort new (Jev-1.14)", "Find topics" + Codex mark); no onboarding screens: an empty map is just the empty state of the same view; sessions with group pictograms lead, chapter titles stay secondary.
- **Stop conditions:** ask before changing `SCHEMA_VERSION`, adding any unattended run, exposing topics over MCP, sending transcript text (not titles) to Jev, or deleting chapters.
- **Tail:** commits on `release/*-beta`; each phase deployed and checked on production before the next; production DB backup before the reset.

## Progress

- 2026-10-18: phases 1, 2, 3 and 5 implemented and committed (31cf3d1, cb50477, c9e5565 and the docs commit). Settings opens on other workspaces as the Sessions overlay in a same-origin frame instead of a separate shared module (lower risk than moving ~200 settings handlers). A real Find topics run on the live chapter titles proposed 24 topics placing 449 of 490 chapters. Phase 4 (reset and live test) follows the deploy.

## Phases

### Phase 1 · Shared header with Search and Settings (whole Bridge)

- **H1. One header everywhere.** Sessions, Contexts, Topics and Lab render the same right side of the workspace bar: `Search ⌘K` button, gear (Settings, with the update dot), divider, owner name. "Log out" disappears from the bar (it stays as "Sign out" in Settings › General). Search and Settings leave the Sessions sidebar; groups start at the top.
- **H2. Settings as a shared overlay.** Move the Settings dialog markup and logic out of `web/admin-viewer.html` into shared assets (`web/admin-settings.js` + CSS, served like `admin-confirmation.js`) so every workspace opens the same overlay in place. It opens on the tab of the workspace it came from (Topics → Topics tab).
- **H3. Topics tab in Settings** (under "Uses tokens", after Chapters): model for Find/Refine (Codex model + effort), model for Sort/Test (Jev model + OpenRouter key: save, replace, remove), include sensitive groups, "Suggest Find topics when Unsorted reaches N". Backed by the existing `/admin/api/topics/settings` and `/admin/api/topics/key`. The lab's own Settings step is removed.
- **H4. Search.** Redesign is postponed: on Sessions the button opens today's search dialog; on other workspaces it opens Sessions with the search dialog open (`/admin/sessions?search=1`). `⌘K` works on every workspace.
- **Check:** all workspaces show the same bar at 1280 and 1180 px; Settings opens in place on Contexts and Topics; existing settings tests stay green.

### Phase 2 · Topics engine (backend, no UI change yet)

- **E1. Proposals store.** Additive tables `topic_proposals` (op `new_topic|split|redescribe|first_map`, payload, status `pending|accepted|rejected|outdated`, source run, test run, decision note) and a rejected-memory list fed to later runs.
- **E2. Find topics.** Codex reviews Unsorted. On an empty map Unsorted is every chapter, so the result is one `first_map` proposal (topics + families + descriptions + intended chapters); later it proposes `new_topic` items. Granularity hint (broad/balanced/fine) and a free-text note go into the prompt. Rule: a topic needs ≥3 chapters from ≥2 sessions.
- **E3. Refine topics.** Codex reviews classified chapters by topic and proposes `split`, `new_topic` (carved out) or `redescribe`; no merge; at most 5.
- **E4. Test (simulation).** Every proposal and every manual description edit is tested before it is shown: build the candidate map, sort all eligible chapters with the sorting model as a `test` run, compute agreement with the intended chapters, confidence before/after in the affected area, chapters pulled in from other topics, pool change. Results are stored with the proposal.
- **E5. Decisions and versions.** Accept creates and activates a new map version and adopts the test run's assignments; reject stores the note; other proposals on an older version become outdated. Restore any version. Import/export JSON stay.
- **E6. Activity.** Per topic: sessions per week for the last 12 weeks (from the first exchange time of each chapter) for the bar sparkline; per chapter: second topic and its probability.
- **E7. CLI.** `mcp-bridge topics proposals` and `run <id>` show proposals and tests; the journal records every step (actor, action, data) in a shape a future Bridge-wide digest can read.
- **Check:** unit tests with fake Codex and fake Jev for first map, new topic, split, redescribe, test metrics, accept/reject/outdated, restore; one real Find topics run on a copy of production data before deploying.

### Phase 3 · Topics workspace v2 (UI)

- **W1. Main view.** Strip: map status, `Manage topics` (badge = pending proposals; numbered step 1 on an empty map), arrow, `Sort new · N (Jev-1.14)` (step 2; disabled while there are no topics), `List | Graph` switch. Left: topics by family with the 12-week bars and session counts, Unsorted last. Right: selected topic as the session tree: group pictogram, title, topic dots, when, count; chapters with confidence bar and the second topic (dot + name + %). "Edit" opens Manage topics on that topic. "Add to a context" is deferred until the Context Builder rebuild.
- **W2. Sort new.** Confirm dialog (count, sessions, map version, cost, model), then progress inside the strip (spinner, n/N, cost, Pause); topic counts update live with "+n".
- **W3. Manage topics overlay.** Left column: Find topics and Refine topics (with their state: Unsorted n/15, last run), sections Proposals / Topics / Versions, Import/Export. Proposals: first-map review (checklist, selected topic with sessions and test, broad/balanced/fine, note, Find again, Accept) or change cards (kind, source, test verdict, Accept/Reject). Topics: list with weak counts and the edit form (name, family, color, description, Test taxonomy, propose split, merge, remove when empty, Save). Versions: list with restore.
- **W4. Graph view.** Port `topic-map.html` (topics as circles, chapters as dots, second-topic links, sensitive rings, bridges list) onto live data; click a chapter to open it in its session.
- **Check:** demo admin walkthrough of every canvas board state; no console errors; layout at 1180 px.

### Phase 4 · Reset and live test from zero

- **R1.** Back up the production database.
- **R2.** Delete every Topics record (map versions, runs, run items, proposals, journal); keep chapters, settings and the OpenRouter key.
- **R3.** The owner runs Manage topics › Find topics on the empty map, reviews, accepts, then Sort new. Claude follows each step with `mcp-bridge topics` and compares with the October 17 desktop and live results.

### Phase 5 · Docs and cleanup

- Changelog, limitations (what goes to Codex and to OpenRouter), operations (CLI), removal of lab-only code paths.

## Out of scope

Search redesign, Add to a context, automatic sorting or reviews (later, with a Bridge-wide digest), onboarding, MCP exposure of topics.
