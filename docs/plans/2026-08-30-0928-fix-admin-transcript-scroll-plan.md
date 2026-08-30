---
title: Admin Transcript Scroll - Plan
type: fix
date: 2026-08-30
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Admin Transcript Scroll - Plan

## Goal Capsule

**Objective:** Keep the selected-session header fully visible below the global workspace navigation while a long conversation is being reviewed, and make vertical scrolling move only the conversation area.

**Means:** Make the transcript region the vertical scroll owner and keep the session header outside that scroll context, while updating transcript navigation to target the same scroll owner (KTD1).

**Authority:** NIV-17 defines the user-visible bug and expected behavior. Existing Admin Viewer layout and test contracts define compatibility constraints. Implementation details may change when live browser verification exposes a layout edge case.

**Stop conditions:** Do not change backend routes, persistence, session data, or the Graph workspace. Do not redesign transcript navigation or the three conversation layouts.

**Execution profile:** Lightweight local UI fix with focused regression coverage and a live browser smoke check. Tail ownership remains in this task: create a local commit on the current branch, with no push or merge.

## Product Contract

### Summary

The Admin Viewer currently lets the document scroll before the conversation body takes over. This causes the selected-session header to move under the global workspace bar and makes long transcript review awkward.

### Problem Frame

The session header is sticky relative to the page, but the transcript content expands the page instead of owning its own scroll viewport. A downward gesture from the top of a long conversation therefore scrolls the document first.

### Requirements

- R1. A long selected conversation scrolls vertically inside the conversation region rather than moving the document.
- R2. The selected-session header stays fully visible below the global workspace bar while the conversation region is scrolled.
- R3. Existing transcript navigation controls continue to reach the top, bottom, previous turn, and next turn within the conversation region.
- R4. Pairs, Bubbles, Thread, Markdown, sensitive-content, empty, and responsive states retain their current behavior outside the scroll ownership change.

### Acceptance Examples

- AE1. Given a selected session whose transcript is taller than the available viewport, when the user scrolls down from the transcript top, then the document scroll position does not advance and the conversation region scroll position does.
- AE2. Given the same long transcript at its top, middle, or bottom, when the user inspects the page, then the global workspace bar and selected-session header remain fully visible without overlap.
- AE3. Given any supported transcript layout with multiple turns, when the user activates the existing top, previous, next, or bottom control, then the requested position is reached inside the conversation region.
- AE4. Given a narrow viewport, when the user reviews a long transcript, then all conversation content remains reachable and the header does not become hidden under the global workspace bar.

### Scope Boundaries

- In scope: Admin Viewer layout containment, the scroll-target calculations used by its existing transcript controls, and focused regression coverage.
- Out of scope: backend/API changes, transcript data changes, Graph workspace changes, new navigation controls, and visual redesign of the session header or transcript cards.

### Sources / Research

- Linear issue NIV-17: [Przewijanie rozmowy w dół najpierw przewija jej TOP](https://linear.app/nivailo/issue/NIV-17/przewijanie-rozmowy-w-dol-najpierw-przewija-jej-top).
- `admin-viewer.html` contains the global workspace bar, session `.topbar`, `.thread-sensitive-content` transcript region, `.turn-nav`, and the existing transcript scroll helpers.
- `tests/test_admin.py` and `tests/viewer_harness.py` contain the current Admin Viewer contract and JavaScript smoke-test patterns.

## Planning Contract

### Key Technical Decisions

- KTD1. Give the transcript region exclusive ownership of vertical overflow and keep the session header outside that scroll surface. This fixes the root cause at the layout boundary and avoids compensating for page scroll in JavaScript.
- KTD2. Rebase the existing transcript navigation calculations on the transcript scroll container instead of the window. This keeps button navigation consistent with wheel and touch scrolling after the ownership change.

### Assumptions

- The current flex layout can provide a bounded transcript viewport by adding containment constraints around the existing main column.
- The inline demo dataset is sufficient for live browser validation because the change has no backend dependency.

### Implementation Constraints

- Reuse `--workspace-bar-height` for the relationship between the global workspace bar and the session header.
- Preserve the current `scrollTranscript` public behavior and all existing turn-selection offsets; change only the coordinate system and scroll target needed by the new container.
- Keep the CSS and JavaScript changes local to the existing Admin Viewer surface.

## Implementation Units

### U1. Contain Admin Transcript Scrolling

**Goal:** Make the conversation body the only vertical scroll surface while preserving the existing header, navigation rail, layouts, and responsive states.

**Requirements:** R1-R4; AE1-AE4; KTD1-KTD2.

**Dependencies:** None.

**Files:** `admin-viewer.html`, `tests/test_admin.py`.

**Approach:**

- Bound the main session column to the viewport below the global workspace bar and prevent page-level overflow from the session surface.
- Allow `.thread-sensitive-content` to consume the remaining main-column height and scroll its transcript content.
- Keep the session header positioned below the global workspace bar when an outer page scroll is possible at responsive widths.
- Update top, bottom, previous, and next calculations to use the transcript container's scroll geometry.

**Execution note:** Prove the layout in a real browser with a long demo transcript after the focused source-contract test passes.

**Patterns to follow:** Existing `.shell`, `.main`, `.topbar`, `.thread-sensitive-content`, `.content-wrap`, and `scrollTranscript` implementation; `tests/test_admin.py` source-contract assertions; `tests/viewer_harness.py` JavaScript extraction helpers.

**Test scenarios:**

1. Covers AE1. Assert the Admin Viewer declares a bounded main column, a transcript overflow container, and no window-based scroll target for transcript navigation.
2. Covers AE2. In a live browser with a long transcript, scroll the conversation region and assert the document remains at its initial scroll position while the session header stays below the global workspace bar.
3. Covers AE3. In the same browser state, activate top, previous, next, and bottom controls and assert the transcript container moves to the corresponding turn or boundary.
4. Covers AE4. Repeat the live scroll check at a narrow viewport and assert the transcript remains reachable without header overlap or clipped controls.
5. Verify empty, sensitive, Markdown, Pairs, Bubbles, and Thread render states still expose their current content and controls after the scroll-container change.

**Verification:** Focused Admin Viewer tests pass, the source has no whitespace errors, and live browser inspection confirms one document-level workspace scroll boundary plus a working transcript scroll boundary at desktop and narrow widths.

## Verification Contract

- Focused regression: the new Admin Viewer scroll contract and existing Admin Viewer tests pass together.
- Browser behavior: the inline demo page is exercised at desktop and narrow widths with a long transcript, including wheel/trackpad-style scrolling and all four transcript navigation controls.
- Diff quality: changed files contain no whitespace errors and no unrelated source or generated artifacts.

## Definition of Done

- R1-R4 and AE1-AE4 are satisfied by the implementation and evidence.
- The selected-session header never hides under the global workspace bar during transcript scrolling.
- Existing transcript navigation controls use the same scroll owner as manual scrolling.
- Focused tests and the browser smoke check pass.
- Only the intended plan, Admin Viewer, and regression-test changes are included in the new local commit; the pre-existing session-trash plan remains untouched and uncommitted.
