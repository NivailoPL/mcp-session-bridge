---
title: "Context Visibility Controls - Plan"
date: 2026-08-06
type: feat
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Context Visibility Controls - Plan

## Goal Capsule

Add two reversible admin controls for shaping the transcript returned through MCP: excluding a complete user/model exchange, and masking only the model response while preserving an explicit placeholder. The owner-facing UI, labels, tooltips, and placeholder copy are English-only. The existing VPS repository and its established soft-delete, transcript rendering, admin API, and viewer patterns are authoritative.

## Product Contract

### Problem Frame

Brainstorming sessions sometimes need independent model opinions. Today, later models can anchor on earlier answers, while the existing Delete/Restore control looks destructive even though it already removes a complete exchange from MCP transcript chunks reversibly.

### Requirements

- R1. The owner can exclude a complete user/model exchange from MCP transcript chunks and include it again later.
- R2. An excluded exchange remains stored and visible as a compact excluded item in the admin panel, but contributes no heading, placeholder, or content to the model transcript.
- R3. The owner can mask and unmask the assistant response independently of exchange exclusion.
- R4. A masked response is hidden from every model retrieving transcript chunks; masking is not recipient-specific.
- R5. A masked exchange keeps the user message, model turn, speaker identity, and chronology in the transcript, replacing only the assistant content with `[MODEL RESPONSE MASKED - CONTENT HIDDEN FROM ALL MODELS]`.
- R6. The admin panel visually obscures masked assistant content and shows unambiguous `Excluded` and `Masked` states.
- R7. The actions use `Exclude` / `Include` and `Mask` / `Unmask`; every action has an English tooltip that explains its effect on MCP transcript chunks.
- R8. Exclusion and masking are persistent, reversible owner actions and are recorded in the existing exchange admin-event history.

### Acceptance Examples

- AE1. Given an active exchange, when the owner selects `Exclude`, then neither its user message nor its model answer appears in overview-derived transcript content or any transcript chunk; selecting `Include` restores both.
- AE2. Given an active exchange, when the owner selects `Mask`, then the real assistant response is absent from every transcript chunk, the user message remains, and the model turn contains the exact masking placeholder; selecting `Unmask` restores the answer.
- AE3. Given a masked exchange, when the owner excludes and later includes it, then it returns still masked until explicitly unmasked.
- AE4. Given an excluded or masked exchange, the admin panel communicates the state and each available reversal action has a descriptive tooltip.

### Scope Boundaries

In scope: persistent exchange-level exclusion, persistent assistant-response masking, MCP overview/chunk behavior, admin API controls, admin viewer states, tooltips, and automated regression coverage.

Deferred: per-model visibility, automatic expiry, schedules, bulk actions, masking user messages, and access-control/security guarantees for the visual blur.

## Planning Contract

### Key Technical Decisions

- KTD1. Reuse the existing soft-delete storage state as exchange exclusion while changing owner-facing language from deletion to context inclusion. This preserves the proven reversible filtering path and avoids parallel states with identical transcript behavior.
- KTD2. Add a separate persistent response-masked state. Exclusion and masking remain orthogonal so an excluded exchange can retain its masked status when included again.
- KTD3. Apply masking in the canonical transcript renderer before chunking. Overview hashes, counts, and every chunk therefore describe the same model-visible transcript without duplicating filtering logic.
- KTD4. Keep the stored response available to the authenticated admin API and obscure it in the viewer. This is a context-control feature, not an encryption or authorization boundary.

### Assumptions

- Existing deleted exchanges represent the same model-context exclusion semantics and may be presented as excluded without migrating their stored timestamps or reasons.
- Exports and admin-only raw views are owner-facing surfaces; their exact treatment should follow the active/excluded filter already used by the viewer rather than create a new public privacy guarantee.

## Implementation Units

### U1. Persist and render response masking

**Goal:** Add the independent masked state and make the canonical transcript renderer substitute the model-visible placeholder.

**Requirements:** R3-R5, R8; AE2-AE3.

**Dependencies:** None.

**Files:** `app/storage.py`, `app/session_package.py`, `tests/test_sessions.py`.

**Approach:** Extend the exchange record and additive SQLite initialization with a masking timestamp. Add reversible storage operations and event records. Render the placeholder only for masked assistant turns while preserving the real stored response for the admin surface.

**Execution note:** Start with failing storage and transcript-rendering tests that prove the real response is absent and the placeholder is present.

**Test scenarios:**

- Masking an active exchange persists the state, records an event, and substitutes the exact placeholder.
- Unmasking restores the original answer and records the inverse event.
- Exclude/include does not clear masking.
- Existing databases gain the new field with unmasked behavior by default.

**Verification:** Focused session tests prove storage migration, state transitions, transcript hashing/counting coherence, and chunk-safe content substitution.

### U2. Expose reversible admin controls

**Goal:** Add authenticated mutation routes and payload fields for exclude/include and mask/unmask.

**Requirements:** R1-R4, R7-R8; AE1-AE3.

**Dependencies:** U1.

**Files:** `app/admin.py`, `app/main.py`, `tests/test_admin.py`.

**Approach:** Keep the existing authenticated mutation and CSRF patterns. Retain the current soft-delete endpoints for compatibility while presenting them as exclude/include, and add dedicated mask/unmask endpoints.

**Test scenarios:**

- Unauthenticated requests to every new mutation route are rejected.
- Mask and unmask responses expose the updated state.
- Existing delete/restore routes continue to filter and restore exchanges.
- Admin session payloads distinguish excluded and masked exchanges.

**Verification:** Focused admin tests prove authorization, payload shape, and state transitions across the API/storage boundary.

### U3. Present context states in the admin viewer

**Goal:** Make exclusion and masking understandable and reversible from every transcript layout.

**Requirements:** R2, R6-R7; AE4.

**Dependencies:** U1-U2.

**Files:** `admin-viewer.html`, `tests/test_admin.py`.

**Approach:** Rename the owner-facing soft-delete actions to `Exclude` and `Include`, add `Mask` and `Unmask` beside them in both transcript layouts, and attach native English tooltips plus accessible labels. Collapse excluded content completely and blur only masked model content while keeping its state visible.

**Test scenarios:**

- Both layouts expose all four reversible actions with the required tooltips.
- Excluded exchanges render as compact state rows without their transcript content.
- Masked model content is visually obscured while the user turn remains readable.
- The raw admin transcript uses the exact masking placeholder.

**Verification:** Viewer contract tests cover labels, tooltips, state classes, and demo API behavior; the full suite validates the integrated storage, API, transcript, and UI contract.

## Verification Ladder

1. Focused storage and transcript tests for masking, migration, and exclusion behavior.
2. Focused admin tests for authentication, CSRF, payloads, and state transitions.
3. Full test suite and `git diff --check` on the VPS checkout.
4. Service restart followed by readiness and `/healthz` verification.
