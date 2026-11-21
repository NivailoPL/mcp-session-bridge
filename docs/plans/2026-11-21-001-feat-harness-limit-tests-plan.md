---
title: Harness limit tests - Plan
type: feat
date: 2026-11-21
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Harness limit tests - Plan

## Goal Capsule

- **Objective:** Replace the manual output-probe panel in Settings with a one-prompt adaptive test. The owner types a harness name (e.g. `chatgpt.com`), copies one prompt, pastes it into that harness, and Bridge runs the whole test, then shows the latest result per harness in plain language. Add cleanup of old runs.
- **Authority:** this plan, then repo conventions (additive schema without a `SCHEMA_VERSION` bump, admin CSRF guard via `_require_admin_mutation`, terse UI with help behind "?", one main view with overlays).
- **Stop conditions:** ask before changing `SCHEMA_VERSION`, removing or renaming the existing `run_output_probe` / `submit_output_probe_observation` tools, adding any unattended or scheduled job, or raising `MAX_PROBE_TARGET_CHARS` above 250 000.
- **Execution profile:** backend test-first in `tests/`; UI checked in the demo admin (`scripts/demo_ui.py`); then a live run on at least chatgpt.com and claude.ai.
- **Tail ownership:** commits land directly on `release/2026.11.1-beta`.

## Progress

- 2026-11-21: phases 1 and 2 implemented (algorithm, suite storage, `next_harness_probe` / `report_harness_probe`). Deviations: `report_harness_probe` finishes the suite itself when no step is left and answers `done: true`, so the model can stop without one more call; suites are created only from the store for now (admin endpoint in phase 3).
- 2026-11-21: phase 3 implemented (endpoints from K6, `harness_tests` in the settings payload, `transcript.safe_chunk` from `transcript_markdown` only, and the current panel already reads it). "Keep latest per harness" and "Delete all" both also drop standalone manual runs; per-harness delete keeps them.
- 2026-11-21: phase 4 implemented and checked in the demo admin (desktop and 375 px; the admin has only a dark theme). The Advanced disclosure already holds the profile and start size for suites plus the old manual probe and its results, so U11 is reduced to "one step per conversation". `scripts/demo_ui.py` seeds four simulated suites (one still running).
- 2026-11-21: phase 6 first live runs: chatgpt.com drops the middle above ~44k (char-based: same boundary as the October canary runs); claude.ai truncates at 100 000 chars with a notice; grok.com passed 250k on canaries but could not recall a random block without tools: it reads big results from a temp file. Added the context vs delivery split: after every whole delivery in the context phase the model must recall 3 random non-checkpoint blocks (chosen after delivery) with `answer_recall_check`, no tools; a failed or skipped check is `spilled`. When the context limit comes from a spill, a delivery phase grows on canaries alone. `report_harness_probe` also takes a `delivery` self-report (context / file / unsure). The context warning counts only chars that stayed in the chat.

---

## Product Contract

### Summary

Today a harness test is one manual measurement per paste: the owner picks a label, a size and a content profile, copies an instruction, and later reads a flat list of the last 50 runs. Nothing tells them where the limit actually is or why a run failed.

The new test is a **suite**: a short, adaptive series of probe steps chosen by the server. The model in the harness only repeats "get next step, report what you saw" until the server says it is done. The server keeps all state, so a suite survives a broken conversation: pasting the same prompt into a fresh chat continues it.

### Decisions already made (2026-11-21 conversation)

- **Grow from a start point, adapt in both directions.** No overshoot to 250k at the start (it can fill a small context window in one call), no alternating low/high shots (cumulative context grows too fast and each high shot is a new risk). Start at 32 000 chars and let the results steer.
- **Read the cut point from truncation.** A truncated result is a measurement, not just a failure: the end of the last complete block is where the harness cut. One confirming step below it replaces several bisection steps.
- **Simple and advanced modes.** Simple mode always uses `transcript_markdown` and is the only one that feeds the chunk-size recommendation. `transport_canary`, manual size and "one step per conversation" live under Advanced.
- **The two profiles measure different things.** Same frame (1 024-char blocks, BEGIN/END, offsets, five checkpoints), different filler. `transcript_markdown` is repeated prose that tokenizes like real transcripts. `transport_canary` is random base64 that is incompressible and costs about 2-3x more tokens per char. Comparing the two shows whether a harness limits by chars or by tokens. Only the first one says what chunk size to use.

### Requirements

- **R1.** One input (harness name) and one button ("Get test prompt") start a suite and copy its prompt. The harness name comes from the owner, never from the model.
- **R2.** While a suite runs, the panel shows live progress ("Step 2 of ~4") by polling the suite state.
- **R3.** The list shows one row per harness with its latest suite: name, date, step count, a verdict pill ("No limit up to 250k", "Truncates above ~98k", "Rejects above ~12k", "Unfinished").
- **R4.** Expanding a row lists every step in one sentence each: size sent, what arrived (whole / cut at N chars / rejected with error / no report), what that means. It ends with the recommended chunk size and a "Use" button.
- **R5.** A suite continues after a dead conversation: the same prompt pasted in a new chat resumes at the next step.
- **R6.** Cleanup: "Keep latest per harness" and "Delete all", each confirmed; per-harness delete from the row; automatic retention of the last 5 suites per harness.
- **R7.** "Safe chunk size" at the top uses only `transcript_markdown` results. Today it takes the weakest across all profiles (`renderOutputProbeResults` in `web/admin-viewer.html`), so one canary run silently lowers it for everyone.
- **R8.** When a failure comes after a large cumulative payload in the same conversation, the report says the context window may be the cause and offers a fresh-chat retry.

### Non-goals

- No change to how transcript chunks are cut, only to how the limit is measured.
- No token counting on the server; tokens are shown only as an estimate next to chars.
- No automatic application of the recommendation; "Use" stays a click.

---

## Key Technical Decisions

### K1. The step algorithm is one pure function

`next_probe_step(steps, *, start, floor, ceiling, max_steps) -> int | None` in `app/output_probe.py`. Input: the suite's finished steps, each with `target_chars`, `outcome` (`complete | truncated | altered | rejected | no_report | verification_failed`; `altered` = head or middle removed) and, when truncated, `cut_at_chars`. Output: the next size, or `None` to stop.

State derived from steps:
- `lo` = largest `target_chars` with outcome `complete`.
- `hi` = smallest failing bound: `cut_at_chars` for a truncated step, `target_chars` for any other failure.

Rules, in order:
1. Stop at `max_steps` (7).
2. Stop if `lo == ceiling` (250 000 passed).
3. Stop if no `lo` and `hi <= floor` (4 096): the problem is not size.
4. Stop if the gap is tight (`hi - lo < max(4_000, lo * 0.10)`).
5. Last step truncated: confirm at about 95% of the cut point, rounded down to 1 000, if that is above `lo`. This comes before halving, so a cut on the very first step is confirmed, not halved.
6. No `lo` yet: `max(floor, hi / 2)`, rounded down to 1 000.
7. No `hi` yet: `min(ceiling, lo * 2)`.
8. Otherwise the midpoint of `lo` and `hi`, rounded down to 1 000 (stop if it is not above `lo`).

`verification_failed` (wrong or invented canaries) counts as a failure at that size but is marked in the report as "the model misread the payload". `no_report` is a conservative failure.

Worked traces (start 32k), kept as test cases:
- Large harness: 32k ✓, 64k ✓, 128k ✓, 250k ✓, stop. Verdict: no limit up to 250k.
- Silent cut at ~98k: 32k ✓, 64k ✓, 128k cut at 98k, 93k ✓, stop (gap 5k ≤ 9.3k).
- Rejects above ~12k: 32k ✗, 16k ✗, 8k ✓, 12k ✓, 14k ✗, stop.

Implemented in phase 1 together with the traces as tests.

### K2. Cut point from truncation

For a `tail_truncated` observation with a valid last complete block, `cut_at_chars = block.offset + block.char_count` of that block. This needs the block manifest already stored in `blocks_json`. `head_removed` and `middle_removed` count as failures without a cut point (the harness did something other than cut the tail), and the report says which.

### K3. Report by suite, not by run

If a harness rejects a result, the model never sees the `run_id` inside it. So reporting goes through the suite: the server knows which step is pending. Two new MCP tools:

- `next_harness_probe(suite_id)`: if a step is pending and unreported, it is first closed as `no_report`. Then it returns either the next payload (same shape as `run_output_probe` plus `suite_id`, `step`, `expected_steps`) or `{done: true, verdict, recommended_chunk_chars}`.
- `report_harness_probe(suite_id, observed_checkpoint_canaries, last_complete_block_index, last_complete_canary, harness_error="")`: closes the pending step. With `harness_error` and no canaries, the outcome is `rejected` and the error text (max 500 chars) is saved for the report.

The prompt then carries only the `suite_id` and the loop instruction. The existing `run_output_probe` / `submit_output_probe_observation` stay unchanged for Advanced and for older harness setups. After deploy, harnesses need a tool-list refresh or a connector reconnect, as with the tool output mode change.

### K4. Storage (additive)

- New table `harness_probe_suites`: `suite_id`, `harness_label`, `content_profile`, `mode` (`simple | advanced`), `status` (`running | done | abandoned`), `verdict`, `verdict_json` (lo, hi, recommended_chunk_chars, cumulative chars), `created_by`, `created_at`, `updated_at`.
- New nullable columns on `output_probe_runs`: `suite_id`, `step_index`, `outcome`, `cut_at_chars`, `harness_error`, `cumulative_chars`, added the same way `tool_output_mode` was added (additive column list, no `SCHEMA_VERSION` bump).
- Retention runs when a suite is created: keep the 5 newest suites per harness and delete their older runs; standalone runs keep today's 500 cap.
- A suite with no activity for 24 h is shown as "Unfinished" when it is read. No background job marks it.

### K5. Explanations are rendered from structured fields

The server returns per step: `target_chars`, `outcome`, `cut_at_chars`, `matched_checkpoints`, `harness_error`, `cumulative_chars`. The verdict is an enum plus numbers. The UI builds the sentences, so wording changes need no backend change and the API stays testable.

`context_suspect = true` on a failed step when `cumulative_chars` before it exceeds 150 000 (R8).

### K6. Admin API

- `POST /admin/api/settings/harness-tests` `{harness_label, mode?, content_profile?, start_chars?, one_step_per_chat?}` returns `{suite, prompt}`.
- `GET /admin/api/settings/harness-tests` returns the latest suite per harness plus the older-run count.
- `GET /admin/api/settings/harness-tests/{suite_id}` returns one suite with its steps (used for polling, every 3 s while `running` and the panel is open).
- `POST /admin/api/settings/harness-tests/cleanup` `{mode: "keep_latest" | "all"}` and `DELETE /admin/api/settings/harness-tests?harness=…`.

All mutations go through `_require_admin_mutation`.

---

## Phases

### Phase 1 · Algorithm (pure, test-first)

- **U1.** `next_probe_step` and `cut_point_from_observation` in `app/output_probe.py`, plus `suite_verdict(steps)` returning the enum and numbers.
- **U2.** Tests in `tests/test_output_probe.py`: the three traces above, floor failure, ceiling pass, `verification_failed`, `no_report`, max-steps stop, `head_removed` without a cut point.
- **Check:** `uv run pytest tests/test_output_probe.py`.

### Phase 2 · Storage and MCP tools

- **U3.** Table, columns, retention and store methods in `app/storage.py`.
- **U4.** `next_harness_probe` and `report_harness_probe` in `app/main.py`, reusing `build_output_probe` and `classify_probe_observation`.
- **U5.** Tests: a full simulated suite through the MCP functions (like `tests/test_admin.py` around the existing probe test), resume after an unreported step, rejected step with `harness_error`, retention.

### Phase 3 · Admin API and recommendation fix

- **U6.** Endpoints from K6 in `app/admin.py`; `_settings_payload` gains `harness_tests`.
- **U7.** `probe_recommendations` and the "Safe chunk size" value use only `transcript_markdown` (R7).
- **U8.** Tests for endpoints, CSRF and cleanup modes.

### Phase 4 · Settings UI (simple mode)

- **U9.** Replace the probe block in `web/admin-viewer.html` with the input, "Get test prompt", the copied-prompt status with progress, the per-harness list with expandable steps and the verdict box with "Use", and cleanup buttons with confirmation. Short labels; the profile explanation and the context-window note go behind "?".
- **U10.** Prompt text (one place, in the server's `prompt` field): use only the Bridge tools; loop `next_harness_probe` → read without summarizing → `report_harness_probe`; on a tool error report the error text; never infer canaries; start in a fresh chat; at the end reply with the verdict only.
- **Check:** demo admin at 1280 and 375 px, light and dark; a fake suite driven through the MCP functions shows live progress and the final report.

### Phase 5 · Advanced

- **U11.** Disclosure "Advanced": content profile, start size, "one step per conversation" (the server returns `done_for_this_chat` after each step and the prompt says to stop), and the old single-probe instruction for manual runs.
- **U12.** If a harness has both profiles finished, the row shows "limits by tokens" or "limits by chars" (the canary limit is clearly lower vs about the same).
- **U13.** Update `docs/limitations.md` and `docs/model-instructions.md` (new tools, refresh the tool list after deploy); CHANGELOG entry.

### Phase 6 · Live check

- **U14.** Deploy (see the VPS deploy workflow), refresh tools in harnesses, run simple suites on chatgpt.com and claude.ai, and one on a harness with a small window if one is at hand. Compare verdicts with the old manual results still in the DB.

---

## Risks

- **Context window vs tool limit.** Mitigated, not removed: start at 32k, read the cut point instead of bisecting, `context_suspect`, resume in a fresh chat, one-step-per-chat in Advanced.
- **Model drift over many large steps** (summarizing, recalled canaries). Canary validation catches it as `verification_failed`; at most 7 steps.
- **Harness hides tool errors from the model.** Then the step ends as `no_report` on the next call, which is a conservative failure, and the report says so.
- **Old harness tool lists.** New tools are invisible until the tool list is refreshed; the prompt fails clearly ("tool not found"), and the panel shows the refresh hint.
