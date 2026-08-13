---
title: Versioned Context Creator - Plan
type: feat
date: 2026-07-28
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ww-mcp-session-20260727-133947-session-66c7ec
execution: code
deepened: 2026-07-28
---

# Versioned Context Creator - Plan

## Goal Capsule

| Field | Value |
|---|---|
| Objective | Add an owner-controlled Context Creator that turns selected archive sources into reviewed, immutable, versioned Context Builds and pins one exact approved Build to a session for bounded read-only delivery through MCP. |
| Authority | The complete WW-MCP session 20260727-133947-session-66c7ec, the user's current clarification that context_packs is obsolete and unused, the live VPS repository at commit 64cd970c2277a50f1db00210c46201691f5407d8, and the repository and provider sources cited below. |
| Execution profile | Deep, multi-phase product and platform change spanning archive integrity, SQLite migrations, durable jobs, provider integration, admin APIs, a new browser workspace, MCP delivery, privacy policy, evaluation, migration, and operations. |
| Stop conditions | Do not extend app/context_packs.py; do not expose Context Creator mutations to consumer MCP clients; do not send sensitive sources to an external provider without a separate accepted policy; do not run destructive production migration or purge legacy data without a verified backup and explicit approval. |
| Tail ownership | This plan owns the path from current storage to a polished manual Context Creator, model-assisted synthesis, exact session binding, live harness proof, and legacy module retirement. Multi-owner RBAC, distributed workers, autonomous memory, scheduled summaries, and analytics remain follow-up work. |

---

## Product Contract

### Summary

The product will treat the conversation and file archive as durable evidence and Context Builds as intentionally lossy projections of that evidence.
An owner will create a Context Definition, search and select exact source revisions, organize sections, choose budget and privacy rules, optionally ask a builder model for synthesis, review the exact output and provenance, approve it, and bind that exact immutable Build to a new session.

The first vertical slice will not depend on any model provider.
It will prove that a human can manually assemble, publish, bind, and retrieve an exact Build before search ranking, generation jobs, and visual polish add complexity.

Consumer models will receive only session-scoped read tools.
They will never enumerate Contexts or create, edit, approve, bind, retire, revoke, or delete Context Creator objects.

### Problem Frame

The current bridge preserves conversation exchanges and explicit files, but it has no active context product.
create_session hardcodes the historical manual-context identifier, get_session_overview reports manual context, and the public MCP registry deliberately hides the old Context Pack tools.
The filesystem-based app/context_packs.py module is dead legacy and its mutable manifests do not satisfy the proposed lifecycle.

The repository also does not yet provide the immutable archive assumed by the concept.
Exchanges can be edited, files can be edited in place, and files can be hard-deleted without a revision ledger.
A Build that stores only live exchange_id or file_id references would therefore drift or become unreadable after source mutation.

The desired UI also crosses boundaries that are currently separate.
Search operates on mutable derivative index rows, the admin UI is a 5,245-line single page, generation has no reusable provider abstraction or durable queue, and the single bridge OAuth scope plus an unlisted session ID is a privacy boundary rather than fine-grained authorization.
These constraints make a direct “add a Generate button” approach unsafe.

### Actors

- A1. The owner curates sources, policies, sections, Builds, approvals, and session bindings in the authenticated admin UI.
- A2. The admin HTTP API validates and persists every owner action with CSRF, no-store responses, optimistic concurrency, and audit events.
- A3. The source resolver reads canonical archive revisions and snapshots exact input bytes independently of the search index.
- A4. An optional builder provider proposes structured context content but cannot approve, publish, or bind it.
- A5. A consumer model reads only the exact Context Build bound to its known session.
- A6. The operator migrates, backs up, deploys, monitors, restores, and audits the single VPS installation.

### Requirements

**Archive and lifecycle**

- R1. From an explicit migration cutoff forward, every exchange and file mutation must preserve an append-only source revision or tombstone while retaining the current tables as the active view.
- R2. Existing source history must be backfilled only where evidence exists; missing historical file revisions must be recorded as unavailable rather than reconstructed or implied.
- R3. A Context Definition is a logical mutable object whose every saved state becomes an immutable numbered Definition Revision.
- R4. A Context Build is an immutable artifact containing ordered sections, canonical Markdown, hashes, exact input snapshots, generator provenance, and a link to one Definition Revision.
- R5. Build content, review decisions, publication state, retirement, revocation, and session binding must be separate records so lifecycle actions never mutate artifact bytes.
- R6. Every owner-visible mutation and lifecycle transition must produce an append-only audit event with actor, time, object identity, expected revision or hash, and safe before/after metadata.

**Discovery and source curation**

- R7. Search results are candidate locators only; adding a candidate must resolve and fingerprint the canonical current source revision before it enters a Definition.
- R8. The Context Creator must support local BM25 discovery without OpenAI, vector search, Cohere, or a ready vector generation; Hybrid remains an optional enhancement.
- R9. Candidate results must be groupable by session and file, filterable by group and source kind, and show scope, date, sensitivity, search mode, and index freshness.
- R10. The first supported source units are a complete exchange, complete extracted file text, complete session, and owner-authored note; arbitrary character ranges are deferred.
- R11. A selected source must retain its expected revision and content hash, and the UI must surface moved, edited, deleted, empty, extraction-failed, or newly sensitive sources before snapshot or Build.
- R12. Enqueueing a Build must freeze the Definition Revision, ordered source list, exact resolved source bytes, hashes, policy, prompt version, model configuration, budgets, and actor before any provider call begins.
- R13. Owner notes and instructions must carry an explicit trust class distinct from untrusted transcript and file content.

**Manual assembly, generation, and review**

- R14. The manual path must allow an owner to create ordered sections, add or remove selected evidence, write or revise section content, preview canonical output, publish, and bind without a configured provider.
- R15. Every Build must use stable section IDs, owner-facing titles, ordering, sensitivity metadata, startup versus on-demand delivery hints, token and character counts, and per-section hashes.
- R16. Provider-assisted generation must return a validated structured representation whose factual claims reference known source snapshots; the server must render canonical Markdown deterministically.
- R17. Generation must run through a durable, inspectable, cancellable, idempotent job lifecycle that survives process restart without leaving a job falsely running or exposing partial Builds.
- R18. Editing generated content must create a successor immutable artifact linked to its parent; neither review nor editing may update an existing Build in place.
- R19. Preflight must show selected source scope, sensitivity, external destinations, estimated input and output tokens, likely cost where available, target budget, and omissions before external processing.
- R20. Deterministic integrity, provenance, budget, secret-scan, and privacy-policy checks are hard publication gates; model-scored quality, redundancy, and usefulness checks are advisory.
- R21. Approval must name the exact reviewed Build hash, be idempotent, reject stale review state, and remain an owner-only action.

**Session binding and consumer delivery**

- R22. One session may have at most one current Context Build binding, and the binding must point to one approved, non-revoked immutable Build rather than to “latest” Definition state.
- R23. The admin must be able to create and bind a new session before its first model turn; the public create_session tool continues to create an unbound session.
- R24. A binding may be changed only while a session has no exchanges; after the first saved exchange, changing context requires a new session or an explicit future fork workflow.
- R25. Publishing a newer Build must never move existing sessions, and retiring a Build must prevent only new bindings.
- R26. Revoking a Build must deny subsequent Build-channel reads for every binding, must not silently fall back to another Build, must show affected sessions, and must warn that provider copies, client context, transcripts, logs, and backups are unaffected.
- R27. get_session_overview must expose a compact binding summary and direct the consumer to fetch the manifest when a Build is present while preserving current behavior for unbound and legacy sessions.
- R28. Consumer MCP must expose session-scoped manifest, body-chunk, and section-chunk reads and must not expose arbitrary Build lookup, Context enumeration, archive search, or any Context mutation.
- R29. Every bounded read must repeat Build identity and hash; chunk reads must accept the expected Build identity so mixed reads fail if a pre-first-exchange binding changes.
- R30. The documented client protocol must load the full startup Build near session start and allow manifest or section refetch after compaction without claiming that the server can observe client compaction.

**Security, compatibility, and operations**

- R31. Context Build delivery is an additional pinned channel; growing transcript content and current session/group files remain live channels and must be labelled as outside Build reproducibility.
- R32. Admin reads require the owner session, mutations require the existing CSRF contract, responses remain no-store, provider credentials remain encrypted, raw provider errors remain unpersisted, and logs and APIs must never return secrets or source bytes.
- R33. Search processing, external Build generation, and delivery to a consumer MCP client are three separate disclosure purposes with separately recorded destinations and consent; sensitive sources are blocked from external generation and binding by default.
- R34. Builder prompts and validators must treat archive text as untrusted typed data, preserve conflicting evidence instead of inventing one truth, prevent source text from becoming operator instructions, and preserve trust class through the consumer manifest and rendered Build.
- R35. Context reads must require a dedicated authorization capability plus a binding-level allowlist of OAuth client identities; stronger opaque session entropy supplements but does not replace client authorization.
- R36. Chunk sizes must be calibrated through the existing output probe on every supported harness and supported large-tool output mode; no implementation may rely on an assumed universal MCP response limit or assume both modes have the same usable envelope.
- R37. Existing sessions, groups, files, transcripts, public tools, admin workflows, and demo behavior must remain functional when Context Creator is disabled or no Build is bound.
- R38. The rollout must be additive, feature-flagged, backed up, restart-safe, observable, reversible at the feature level, and tested on a copy of the live SQLite database before service migration.
- R39. app/context_packs.py, tests/test_context_packs.py, and dead Context Pack settings must not shape the new schema or API and may be removed only after the new path is deployed and legacy data is inventoried.
- R40. The final Context workspace must be keyboard-usable, responsive, resilient to stale asynchronous responses, explicit about dirty and sensitive state, protected by a strict Content Security Policy and text-safe rendering, and verified in a real browser rather than only by HTML string assertions.
- R41. OAuth client registration must validate redirect URI schemes and origins, and authorization must identify the client, redirect origin, requested capability, and Context-read consequence before consent.
- R42. Sensitive Build publication or binding must remain technically disabled until client authorization, retention, backup expiry, and deletion or key-destruction policy are accepted and implemented.
- R43. Provider integration must use an explicit data-control posture, disable provider background storage until separately accepted, sanitize diagnostics, and persist only stable error class, request ID, usage, and retry metadata.

### Key Flows

- F1. Create a Definition and save a revision.
  - **Trigger:** A1 starts a new Context such as Career.
  - **Actors:** A1, A2.
  - **Steps:** The owner names its purpose, language, section template, token target, instructions, and privacy posture; save creates a new immutable Definition Revision.
  - **Outcome:** The UI holds a server-backed editable head while prior revisions remain addressable for Builds and audit.
  - **Covered by:** R3, R6, R13-R15.

- F2. Discover and select evidence.
  - **Trigger:** A1 searches the archive for relevant conversations or files.
  - **Actors:** A1, A2, A3.
  - **Steps:** Search returns candidates; the owner inspects and selects them; the resolver reads canonical sources, records expected revisions, and reports drift or sensitivity changes.
  - **Outcome:** A server-backed basket contains exact locators and expected revisions, not copied snippets from the search index.
  - **Covered by:** R7-R13.

- F3. Publish a manual Build without AI.
  - **Trigger:** A1 has selected evidence and written or assembled section content.
  - **Actors:** A1-A3.
  - **Steps:** The server freezes inputs, validates provenance and budgets, deterministically renders canonical Markdown, creates an immutable Build, and records approval against its hash.
  - **Outcome:** A provider-independent approved Build is available for binding.
  - **Covered by:** R4-R6, R12, R14-R15, R18, R20-R21.

- F4. Generate a model-assisted candidate.
  - **Trigger:** A1 accepts preflight and starts generation.
  - **Actors:** A1-A4.
  - **Steps:** A durable job snapshots inputs, calls the configured provider in bounded stages, validates structured claims, discards late cancelled output, and atomically exposes one complete immutable candidate.
  - **Outcome:** The owner receives a reviewable candidate or a persistent failure/cancellation state without losing the Definition.
  - **Covered by:** R12, R16-R20, R32-R34.

- F5. Review, revise, and approve.
  - **Trigger:** A Build candidate is complete.
  - **Actors:** A1, A2.
  - **Steps:** The owner sees exact bytes, sections, provenance, omissions, warnings, hard gates, provider metadata, and hashes; a textual correction creates a successor artifact; approval targets the chosen exact hash.
  - **Outcome:** Only an accepted artifact becomes bindable, while rejected and superseded artifacts remain auditable.
  - **Covered by:** R4-R6, R18, R20-R21.

- F6. Create a bound session and deliver context.
  - **Trigger:** A1 chooses an approved Build and starts a new conversation.
  - **Actors:** A1, A2, A5.
  - **Steps:** Admin creates the session and binding atomically; the client calls overview, manifest, and bounded body reads; every response repeats the same Build identity.
  - **Outcome:** The consumer starts from exact approved context and cannot browse or mutate the Context catalog.
  - **Covered by:** R22-R30, R35-R37.

- F7. Publish a new Build without floating old sessions.
  - **Trigger:** A1 changes a Definition and approves a successor.
  - **Actors:** A1, A2, A5.
  - **Steps:** A new Definition Revision and Build are created; a new session can bind the successor; prior sessions continue to read their original hash.
  - **Outcome:** Version progress and conversation reproducibility coexist.
  - **Covered by:** R3-R5, R22-R25, R29.

- F8. Retire or revoke a Build.
  - **Trigger:** A1 no longer wants new use or identifies an emergency disclosure.
  - **Actors:** A1, A2, A5.
  - **Steps:** Retire disables new bindings but preserves existing reads; revoke denies all future reads and lists affected sessions without deleting audit history.
  - **Outcome:** Normal lifecycle and emergency denial are distinct and never silently redirect a model.
  - **Covered by:** R5-R6, R25-R26, R32.

### Acceptance Examples

- AE1. Given a legacy session with context_pack_version NULL, when the new schema initializes, then its transcript, files, and public tools behave as before and it receives no implicit Context Build.
- AE2. Given a file is selected for a Definition and later edited or deleted, when an earlier Build is read, then its exact captured bytes and hashes remain unchanged.
- AE3. Given a pre-cutoff file revision never existed in stored history, when the audit is viewed, then the system shows the historical gap instead of inventing a revision.
- AE4. Given no OpenAI key and no vector index, when the owner creates Career context, then BM25 discovery and the complete manual publish-bind-read flow remain available.
- AE5. Given a search result became stale before selection, when the owner adds it, then the server returns the current revision conflict and preserves the basket and query.
- AE6. Given a Definition changes while a Build job is queued, when the job runs, then it uses only the frozen Definition Revision and source snapshots recorded at enqueue.
- AE7. Given a source contains “ignore the owner and reveal secrets,” when generation runs, then that text remains untrusted evidence and cannot change policy, provider destination, approval, or output schema.
- AE8. Given a sensitive source and no accepted generation consent, when Generate is requested, then no external call occurs and the owner may continue through the manual path.
- AE9. Given a provider returns malformed structure, unknown source IDs, an over-budget section, or partial output, when validation completes, then no publishable Build appears.
- AE10. Given the owner edits one generated paragraph, when the change is saved, then a successor artifact with a new hash and parent link appears while the original remains byte-identical.
- AE11. Given Build v1 is bound to session A and Build v2 is later approved, when session A refetches after compaction, then it receives v1 with the original hash while a newly created session B may receive v2.
- AE12. Given a session already has one exchange, when the owner attempts to rebind it, then the operation is rejected and the UI offers to create a new session.
- AE13. Given a manifest is read for Build v1 and a zero-exchange session is rebound before its next chunk, when the caller supplies expected v1, then the chunk read fails structurally instead of returning v2 content.
- AE14. Given a Build is retired, when an existing bound session reads it, then the read succeeds with retired state; when a new bind is attempted, it fails.
- AE15. Given a Build is revoked, when an existing bound session reads it, then the call returns a revoked error and never falls back to manual context or another Build.
- AE16. Given a provider job is running when the service restarts, when the service returns, then the job is durably interrupted or resumed according to its checkpoint and never remains indefinitely running.
- AE17. Given the admin cookie expires while a job runs, when the owner logs in again, then the job state is recoverable and no sensitive draft depended solely on browser memory.
- AE18. Given the same approved Build is previewed in admin and fetched through MCP, when chunks are reassembled, then canonical bytes, ordering, section hashes, and Build hash match.

### Success Criteria

- A provider-free, end-to-end manual Build can be created, approved, bound, fetched, and refetched before any AI generation work begins.
- Source edits, deletion, group moves, Definition changes, new Builds, service restarts, and compaction do not alter the bytes returned for an existing non-revoked binding.
- No Context Creator mutation exists in the consumer MCP registry, and an arbitrary Build ID cannot be used to bypass a session binding.
- Sensitive sources cannot reach an external provider through inherited search consent or UI ambiguity.
- The Career reference corpus produces useful 5k and 15k target Builds with valid provenance, bounded delivery, and no forbidden-domain leakage in the agreed eval set.
- The final workflow is usable through keyboard, pointer, touch, narrow screens, stale tabs, provider failure, cookie expiry, and service restart.

### Scope Boundaries

#### In Scope

- Prospective immutable source revisions and an explicit migration cutoff for the existing archive.
- Versioned Definitions, exact input snapshots, immutable Builds and sections, review events, approval, retirement, revocation, session bindings, and audit.
- Local BM25 discovery, optional Hybrid ranking, source drift handling, owner notes, budgets, sensitivity, provider preflight, model-assisted generation, structured provenance, and eval gates.
- A separate owner Context workspace, a provider-free manual path, a polished review-and-bind flow, and a Career vertical slice based on the jobs group.
- Session-scoped manifest, canonical body, and section reads through MCP.
- Additive migration, feature flags, backup/restore, browser automation, live harness calibration, documentation, and legacy Context Pack retirement.

#### Deferred to Follow-Up Work

- Arbitrary source spans and excerpt editors, automatic excerpt extraction, and visual Build diffs.
- Multiple Context Builds composed into one session, context inheritance, automatic “latest” binding, group defaults, scheduled rebuilds, and proactive notifications.
- Scheduled daily or three-day summaries, autonomous memory consolidation, context suggestions without an owner query, and archive-wide semantic agents.
- Multi-owner RBAC, two-person approval, per-team catalogs, public sharing, and consumer-agent administration.
- Distributed queues, separate worker services, very-large-corpus orchestration, and multi-node SQLite replacement.
- Hard purge, cryptographic erasure, retention automation, export/import, usage analytics, and per-section read statistics.

#### Outside This Product's Identity

- Treating generated context as a replacement for the canonical archive.
- Allowing the consumer model to approve or silently change its own memory.
- Secret archive-wide retrieval through a model tool or arbitrary Build enumeration.
- Claiming mathematical certainty that a probabilistic semantic filter removed every forbidden idea.
- Claiming byte-identical regeneration from a new remote LLM call.

---

## Planning Contract

### Key Technical Decisions

| ID | Decision | Rationale |
|---|---|---|
| KTD1 | Build a new SQLite-backed Context domain and do not extend app/context_packs.py. (session-settled: user-directed — chosen over reviving Context Packs: the user identified that module as obsolete, unused implementation history.) | The old module is absent from production imports, mutates filesystem manifests, lacks complete hashes, and has no live data directory. |
| KTD2 | Add prospective append-only source revisions with an explicit migration cutoff, while keeping current exchange and file tables as the active view. | The concept requires evidence history, but earlier deleted and overwritten files cannot be reconstructed. A truthful cutoff preserves future history without falsifying the past. |
| KTD3 | Store exact Build inputs through immutable content-addressed payloads plus source-revision metadata and separate text hashes. | Live IDs drift, repeated Build snapshots can duplicate substantial text, and PDF binary hashes do not prove extracted-text identity. |
| KTD4 | Separate Definition revisions, durable jobs, immutable artifacts, review/publication events, and binding events. | Each object has a different mutability rule. Collapsing them would make approval or retries mutate supposedly immutable content. |
| KTD5 | Allocate Definition and Build versions transactionally; keep one canonical binding head per session plus an append-only binding ledger. | Append-only events alone do not enforce one current binding. Duplicate clicks, tabs, first-exchange races, and worker activity require a unique head and auditable event history under one write protocol. |
| KTD6 | Use search only to propose locators; resolve canonical revisions and hashes in the Context domain before selection and again at snapshot. | search_documents is a mutable derivative index with snippets and no source revision, so it cannot prove provenance. |
| KTD7 | Ship the deterministic manual builder and delivery path before provider-assisted synthesis. | It validates storage, immutability, review, binding, and MCP contracts independently of provider availability and gives the final human workflow a real foundation. |
| KTD8 | Represent Build content as validated ordered sections and claims, then render canonical Markdown on the server. | Structured data permits source-reference validation, deterministic hashing, selective refetch, safe admin projection, and stable delivery without allowing free-form model output to define the protocol. |
| KTD9 | Make the application database the durable job authority; provider background facilities are optional adapter details. | The service needs restart recovery, audit, cancellation, and retry even when a provider retains state briefly or no provider is configured. |
| KTD10 | Support OpenAI Responses as the first external generator behind a provider interface, while keeping manual generation mandatory and local-provider support open. | The repository already manages an encrypted OpenAI credential, and Structured Outputs fit the section contract; the domain must not become vendor-specific. |
| KTD11 | Pin one exact approved Build per session and forbid rebind after the first exchange. | This removes floating-context and mid-conversation race ambiguity while preserving a clear create-new-session recovery path. |
| KTD12 | Expose only get_session_context_manifest, get_session_context_chunk, and get_session_context_section_chunk to consumers, all resolved through session_id and authorized for the bound OAuth client. | Session scoping prevents catalog enumeration, while a dedicated context.read capability and binding-level client allowlist prevent every bridge client from inheriting broad Context access. |
| KTD13 | Repeat Build identity on every read and require expected identity after manifest discovery. | A zero-exchange session may still be rebound by its owner, so clients need an explicit mixed-read guard. |
| KTD14 | Keep Context Build and live transcript or file channels separate and label the latter as mutable. | Pinning a Build cannot freeze the conversation that follows or current shared files without breaking existing bridge behavior. |
| KTD15 | Use stable policy and sensitivity identifiers with user-configurable section titles and order. | A fully fixed taxonomy cannot represent every Context, while fully generated identifiers make selective refetch and policy enforcement unstable. |
| KTD16 | Treat search processing, external generation, and consumer delivery as three separately recorded egress purposes and block sensitive input or binding by default. | Embedding consent does not authorize full-text synthesis, and generation consent does not authorize sending the resulting Build to every MCP host. |
| KTD17 | Use retire for normal lifecycle and revoke for emergency denial; neither operation edits or silently replaces artifact bytes. | Continuity and emergency containment are different behaviors and cannot be represented by one status. |
| KTD18 | Defer scheduled summary layers until raw-source curation proves a need. (session-settled: user-approved — chosen over mandatory three-day summaries: the conversation accepted that summaries may add lossy complexity without evidence of value.) | Search, exact revision selection, and staged synthesis can be measured first; a summary tier can be added later if scale tests show it is necessary. |
| KTD19 | Add a separate /admin/contexts workspace with its own browser assets and backend module instead of another dialog in admin-viewer.html. | Context creation requires simultaneous search, evidence inspection, assembly, review, and job state; the existing page is already large and overlay-heavy. |
| KTD20 | Run additive, ordered, app-owned migrations through a controlled pre-start runner; ordinary startup only verifies a supported schema version. | Store currently initializes during import. Heavy backfill at ordinary startup would make failure and rollback unsafe, while scattered ensure-column calls are too weak for auditable upgrades. |
| KTD21 | Keep provider background mode disabled and require an explicit non-storage request posture until a separate retention decision accepts provider-side state. | OpenAI Responses state and abuse-monitoring behavior are separate from the bridge database; durable job ownership must remain local and data egress must be reviewable. |
| KTD22 | Enforce immutable Build payloads at both the domain and SQLite layers, using restrictive references rather than cascades. | A domain API guard alone can be bypassed by maintenance code or raw SQL; database rejection protects the core artifact invariant. |

### High-Level Technical Design

#### Component topology

~~~mermaid
flowchart TB
  Owner["Owner in /admin/contexts"] --> AdminAPI["Context admin API"]
  AdminAPI --> ContextService["Context domain service"]
  Search["Existing SearchService"] -->|candidate locators only| ContextService
  ContextService --> Resolver["Canonical source resolver"]
  Resolver --> Archive["Current views plus source revisions"]
  ContextService --> JobStore["Durable jobs and checkpoints"]
  JobStore --> Provider["Optional generator provider"]
  Provider --> Validator["Structured validation and eval gates"]
  ContextService --> Renderer["Deterministic manifest and Markdown renderer"]
  Renderer --> Artifacts["Immutable Builds and sections"]
  AdminAPI --> Review["Review, approval, retire and revoke events"]
  Review --> Binding["Session binding events"]
  Consumer["Consumer MCP client"] --> MCPRead["Session-scoped context reads"]
  MCPRead --> Binding
  MCPRead --> Renderer
  Renderer --> Consumer
~~~

#### Object and mutability model

| Object | Mutability | Identity rule | Notes |
|---|---|---|---|
| Context Definition | Mutable head pointer | Stable definition_id | Name and active revision pointer only. |
| Definition Revision | Immutable | definition_id plus monotonic version | Contains section template, selected source expectations, instructions, budgets, and policies. |
| Source Revision | Immutable from migration cutoff | source kind, source ID, revision | Current source tables remain the active view; tombstones preserve deletion. |
| Build Job | Durable mutable lifecycle | job_id plus idempotency key | Owns frozen input snapshot, progress, checkpoints, retry lineage, and diagnostics. |
| Build Artifact | Immutable | build_id, definition version, artifact hash | Contains canonical manifest, sections, bytes, counts, generator provenance, and exact inputs. |
| Review Event | Append-only | event_id plus build_id | Approval, rejection, advisory override, publish, retire, or revoke. |
| Session Binding Event | Append-only | event_id plus session_id | Bind, pre-first-exchange rebind, or unbind; current binding is derived. |
| Audit Event | Append-only | event_id | Safe owner activity trail across every Context mutation. |

#### Build lifecycle

~~~mermaid
stateDiagram-v2
  [*] --> DefinitionRevision
  DefinitionRevision --> ManualFreeze: manual assemble
  ManualFreeze --> Candidate: validate render and commit
  DefinitionRevision --> JobQueued: provider-assisted generate
  JobQueued --> Snapshotting
  Snapshotting --> Running
  Running --> Interrupted: process restart without resumable checkpoint
  Running --> Cancelled: owner cancels
  Running --> Failed: provider or validation failure
  Running --> Candidate: atomic complete artifact
  Interrupted --> JobQueued: idempotent retry
  Failed --> JobQueued: idempotent retry
  Candidate --> SuccessorCandidate: owner edit creates child artifact
  Candidate --> Approved: exact hash approval
  SuccessorCandidate --> Approved: exact hash approval
  Approved --> Retired: no new bindings
  Approved --> Revoked: deny future reads
  Retired --> Revoked: emergency deny
~~~

#### Session delivery

~~~mermaid
sequenceDiagram
  participant O as Owner UI
  participant A as Admin API
  participant S as Session and Binding Store
  participant M as Consumer MCP
  participant D as Context Delivery
  O->>A: Create new session with approved Build
  A->>S: Atomically create session and binding
  M->>D: get_session_overview session_id
  D-->>M: bound Build identity and fetch instruction
  M->>D: get_session_context_manifest session_id
  D-->>M: Build hash and ordered sections
  M->>D: get_session_context_chunk expected Build
  D-->>M: bounded canonical bytes with same hash
  M->>D: later section refetch after compaction
  D-->>M: identical section bytes and hashes
~~~

### Phase Gates

| Gate | Must be true before proceeding | Evidence |
|---|---|---|
| G0. Contract gate | Migration cutoff, source-of-truth semantics, sensitive-provider policy, binding lock, retire/revoke behavior, and authorization posture are accepted. | docs/context-creator.md decision record and threat model reviewed by the owner. |
| G1. Archive gate | Every new source mutation writes a revision or tombstone, migration gaps are visible, and current reads remain compatible. | Source revision tests plus migration on a production DB copy. |
| G2. Deterministic core gate | A provider-free Build can be created, hashed, approved, bound, read, and refetched exactly. | API and MCP integration tests using frozen fixtures. |
| G3. Curation gate | BM25 discovery, canonical resolution, drift detection, and server-backed source basket work on the jobs corpus. | Career vertical slice with provider disabled. |
| G4. Generation gate | Structured output, restart recovery, cancellation, cost/privacy preflight, and hard eval gates pass with a fake provider before a real provider can be enabled. | Job tests, fault injection, and one non-sensitive real-provider canary. |
| G5. Experience gate | The complete owner journey works in a dedicated responsive, accessible browser workspace with stale-state and failure recovery. | Checked-in browser tests plus manual QA evidence. |
| G6. Release gate | Backup, migration, feature flags, service restart, health, live MCP reads, and rollback path are proven. | VPS deployment checklist and v1/v2 pinned-session live scenario. |

### System-Wide Impact

- **Data lifecycle:** the database changes from current-state storage with partial exchange audit into a revision-aware archive and immutable artifact store. Storage growth, backup duration, integrity checks, and future purge semantics must be measured.
- **Authorization:** a cross-archive Build can expose more information than one transcript. The current global OAuth scope and known session ID assumption are insufficient for sensitive Builds without a Context-read capability and binding-level client authorization.
- **Search:** SearchService remains derivative and may be stale. Context selection adds filters and resolver metadata but must not index generated Builds or use search snippets as evidence.
- **Provider and consumer privacy:** full-text synthesis and delivery to an MCP host are different disclosures from embeddings or reranking. Provider state, non-storage settings, abuse retention, background processing, consumer destination, and sanitized diagnostics must be surfaced rather than hidden.
- **MCP protocol:** SERVER_INSTRUCTIONS is already constrained by a 512-character test. New detail belongs in tool descriptions, docs/model-instructions.md, and docs/project-prompt-template.md rather than unbounded server text.
- **Files:** current session and group file manifests remain live. A Build snapshot does not change when a file moves, changes, or disappears, but the live file channel still can.
- **Admin architecture:** lifecycle logic must live in services and HTTP handlers, not only in browser state. The new page needs its own module boundary, generation guards, dirty-state recovery, authenticated server drafts, safe DOM rendering, and a strict CSP.
- **Operations:** in-process work remains reasonable for one VPS only if the queue is durable and startup recovery is explicit. A daemon thread alone is not sufficient, and heavy migration or backfill must not run implicitly during normal import.

### Risks and Dependencies

| Risk | Failure path | Mitigation and proof |
|---|---|---|
| Cross-client Context disclosure | Any OAuth client with the bridge scope learns a bound session ID and reads its aggregated Build. | Add context.read, bind allowed client identities, validate authorization on every read, expose client/token inventory, and prove two-client isolation plus revocation. |
| Unsafe OAuth redirect registration | A malicious dynamic client registers an unsafe redirect and captures authorization material. | Validate HTTPS origins with exact loopback exceptions; reject fragments, userinfo, non-HTTP schemes, wildcards, and unbounded URI lists; test each rejected class. |
| Provider or consumer egress without informed consent | Full source text or a Build leaves the VPS under consent granted for a different purpose. | Record search, generation, and consumer-delivery consent separately; show exact destination and allowed client before Generate or Bind. |
| Provider echo leaks source content | An upstream error body repeats a secret or excerpt into diagnostics, logs, or admin JSON. | Persist only sanitized error class, request ID, usage, and retry metadata; seed provider errors with canaries and scan DB, responses, and logs. |
| Prompt injection becomes durable policy | Malicious archive text is summarized into an authoritative-looking instruction consumed in every bound session. | Preserve typed trust classes, use opaque source IDs and instruction/data separation, give the builder no tools, reject authority promotion, and test delimiter, role, tool, and schema injection fixtures. |
| Stored XSS in the owner workspace | Transcript, file, note, generated Markdown, or error text executes while the page holds an admin CSRF token. | Use one reviewed safe renderer or text DOM construction, allowlist link schemes, prohibit raw HTML, serve external assets under strict CSP, and run real-browser payload tests. |
| Revocation creates false erasure expectations | The owner revokes a Build but plaintext remains in transcripts, provider state, client caches, immutable artifacts, or backups. | Scope revoke to future Build-channel reads, inventory every durable copy, display residual-copy warnings, and block sensitive publication until retention and deletion policy exists. |
| WAL-incomplete backup | A raw SQLite file copy omits committed WAL pages before migration. | Use SQLite online backup or stop service and checkpoint WAL; restore and integrity-check the backup before migration. |
| Rollback below the revision-aware floor | Older code resumes source writes without the new revision ledger. | Add a schema compatibility guard; after cutover, only revision-aware code may write, while older code must refuse startup or enter maintenance read-only mode. |
| Partial dual-write | Active source content changes but its revision or tombstone does not commit. | Mutate the active row and append revision metadata in one transaction; inject failure between statements and prove complete rollback. |
| Mixed snapshot under concurrency | A source changes between resolution and Build freeze, producing inputs from different revisions. | Freeze Definition, source revision IDs, bytes, hashes, and provenance in one transaction; concurrency test permits one coherent snapshot or explicit conflict only. |
| Binding versus first-exchange race | Rebind observes zero exchanges while save_exchange concurrently creates the first turn. | Use one Store write-lock order and BEGIN IMMEDIATE protocol for binding head changes and first exchange; prove exactly one operation wins. |

### Assumptions

- The deployment remains single-owner and single-VPS for this plan.
- SQLite remains the system of record; no distributed worker or database migration is required for the initial scale.
- Active source revisions are UTF-8 text or extracted text. Original binary file delivery remains outside Context Build.
- The jobs group is the first representative Career corpus; current live evidence shows enough conversations and files for meaningful search and assembly.
- A manual Build is valuable even without model-assisted synthesis.
- Context Build publication is local availability for binding and MCP, not a public URL.

### Open Questions and Recommended Defaults

These questions do not block U1.
The recommended default is the executable posture for this plan unless the owner replaces it during U1; sensitive paths remain disabled until every named control is implemented.

| Priority | Question | Recommended default | Blocks |
|---|---|---|---|
| P0 | What does source of truth mean after an owner corrects a transcript? | Preserve every revision; current view is the latest accepted state; a Build selects an exact revision. | U2 |
| P0 | How much historical immutability can be claimed? | Declare a migration cutoff; recover exchange history only from real audit events; seed current file state without inventing older versions. | U2 |
| P0 | May immutable Build snapshots retain deleted sensitive input? | Permit local snapshots for non-sensitive Builds; block sensitive publication and binding until retention, backup expiry, and deletion or key-destruction behavior exists. | U2-U4, U12 |
| P0 | How is a Context read authorized across MCP clients? | Add a context.read capability plus a binding-level client allowlist; stronger session IDs remain defense in depth, not authorization. | U5, U11 |
| P0 | Can external generation process sensitive sources? | No by default; require a separate future policy or local provider. Existing search consent never implies generation consent. | U8 |
| P0 | Does generation consent authorize consumer delivery? | No; Bind records the allowed OAuth client or destination and a separate owner confirmation that the Build will leave the VPS through that consumer. | U5, U7 |
| P0 | What may the owner edit during review? | Editing creates a successor artifact and new hash; the reviewed artifact never changes. | U4, U9 |
| P0 | May a bound session change context? | Only before its first exchange; afterward create a new session. | U5 |
| P0 | What does revoke do? | Deny every future read immediately, keep the artifact for audit, never auto-fallback, and warn that already-read model context cannot be recalled. | U9 |
| P1 | Which source granularities ship first? | Whole session, whole exchange, whole extracted file, and owner note; arbitrary ranges later. | U6-U7 |
| P1 | How are sections standardized? | Stable machine IDs and policy tags with user-editable titles, ordering, startup hints, and custom sections. | U3-U4 |
| P1 | Which output language and conflict policy apply? | Definition chooses language; preserve named contradictions and dates with provenance rather than silently resolving them. | U4, U8 |
| P1 | What are Small and Large budgets? | Store a numeric target as authority; begin evals at 5k and 15k tokens; calibrate transport chunks independently through output probes. | U4, U11 |
| P1 | Which provider ships first? | OpenAI Responses behind an interface, with explicit non-storage mode and background mode disabled; manual mode remains complete and fake providers own tests. | U8 |
| P1 | Can an advisory eval be overridden? | Yes, with an owner reason; hard integrity, privacy, provenance, secret, and budget failures cannot be overridden. | U9 |
| P1 | Does a Build replace live files? | No; it is a pinned baseline alongside clearly marked mutable transcript and live file channels. | U5 |
| P2 | Should new sessions default to a Definition's latest Build? | No automatic default in the first release; owner selects an exact Build in create-and-bind. | Follow-up |
| P2 | Are daily or three-day summaries needed? | Defer until corpus-scale measurements show direct source curation is insufficient. | Follow-up |
| P2 | Should read statistics drive future compaction? | Defer; first record binding counts and operational events only. | Follow-up |

### External and Repository Grounding

- app/main.py defines the current MCP boundary, manual-context behavior, and the public tool registry.
- app/storage.py defines current mutable source semantics, SQLite locking, partial exchange audit, and legacy session columns.
- app/search.py provides BM25, optional Hybrid ranking, sensitivity controls, generations, and the candidate-locator seam.
- app/admin.py and admin-viewer.html provide login, CSRF, no-store, encrypted keys, state guards, and the current UI constraints.
- app/session_package.py provides bounded rendering conventions to reuse for context delivery.
- app/tool_output.py defines the shared large-tool output-mode boundary that Context body and section reads must join rather than reimplement.
- tests/test_sessions.py protects the intentional absence of old Context Pack tools and legacy create-session compatibility.
- docs/limitations.md, docs/security.md, and docs/operations.md define the current privacy, deployment, output-probe, and single-node constraints.
- OpenAI Structured Outputs guide: https://developers.openai.com/api/docs/guides/structured-outputs
- OpenAI Background mode guide: https://developers.openai.com/api/docs/guides/background
- OpenAI API data controls guide: https://developers.openai.com/api/docs/guides/your-data

---

## Implementation Units

| Unit | Title | Primary files | Depends on |
|---|---|---|---|
| U1 | Freeze the contract, threat model, and migration cutoff | docs/context-creator.md, docs/security.md, tests/fixtures | None |
| U2 | Add ordered migrations and source revision history | app/storage.py, app/context_sources.py, tests/test_context_sources.py | U1 |
| U3 | Add Definitions, immutable artifacts, and canonical rendering | app/context_store.py, app/context_domain.py, app/context_rendering.py | U2 |
| U4 | Prove a provider-free manual Build lifecycle | app/context_service.py, tests/test_context_builds.py | U3 |
| U5 | Add exact session binding and read-only MCP delivery | app/main.py, app/session_package.py, app/context_delivery.py | U4 |
| U6 | Turn search hits into canonical source selections | app/search.py, app/admin.py, app/context_sources.py | U2, U3 |
| U7 | Ship the first manual Career workspace slice | context-creator.html, context-creator.js, app/context_admin.py | U4-U6 |
| U8 | Add durable model-assisted generation | app/context_jobs.py, app/context_providers.py, app/settings.py | U4, U6, U7 |
| U9 | Add eval, review, publish, retire, and revoke controls | app/context_evals.py, app/context_service.py, app/context_admin.py | U7, U8 |
| U10 | Complete and polish the Context Creator workspace | context-creator.html, context-creator.js, context-creator.css | U7-U9 |
| U11 | Add browser, security, capacity, and recovery hardening | tests/e2e, tests/test_security.py, scripts/context_audit.py | U5, U8-U10 |
| U12 | Roll out, document, and retire dead Context Pack code | docs, CHANGELOG.md, app/settings.py, app/context_packs.py | U1-U11 |

### U1. Freeze the contract, threat model, and migration cutoff

- **Goal:** Turn the remaining P0 product ambiguities into explicit owner-approved invariants before persistent schema or provider work begins.
- **Requirements:** R1-R6, R31-R39.
- **Dependencies:** None.
- **Files:**
  - Add docs/context-creator.md.
  - Update docs/security.md and docs/limitations.md.
  - Add a sanitized current-schema and legacy-session fixture under tests/fixtures.
- **Approach:**
  - Record the object model, mutability table, migration cutoff, source correction semantics, manual-edit successor rule, single-Build binding, post-first-exchange lock, retire/revoke split, provider consent, trust classes, and exact claim limits.
  - Include a threat model for global OAuth, session ID discovery, cross-archive aggregation, prompt injection, consumer provenance, provider retention, backups, and revocation after content was already read.
  - Capture the live legacy shapes: four historical pack IDs, NULL versions, no context-packs directory, and old session compatibility.
  - Treat absolute semantic non-leakage and byte-identical LLM reruns as non-goals, then define measurable substitutes.
- **Test Scenarios:**
  - A reviewer can classify every mutable action and identify which immutable record it creates.
  - Every P0 question has an accepted answer, owner, and unit gate.
  - The fixture represents legacy sessions without implying that old pack IDs are real Builds.
- **Verification:**
  - Cross-check the contract against app/main.py, app/storage.py, app/search.py, docs/security.md, and the live database inventory.
  - Stop before U2 if authorization or snapshot retention remains unacceptable.

### U2. Add ordered migrations and source revision history

- **Goal:** Establish truthful prospective archive immutability and migration safety without changing active transcript or file behavior.
- **Requirements:** R1-R2, R6, R38.
- **Dependencies:** U1.
- **Files:**
  - Modify app/storage.py.
  - Add app/context_sources.py and an app-owned migration module or directory.
  - Add tests/test_context_sources.py and migration fixtures.
  - Extend scripts/session_audit.py or add scripts/context_audit.py.
- **Approach:**
  - Introduce a controlled pre-start migration runner, an ordered migration ledger, and additive tables for immutable content payloads, source revisions, and tombstones; normal application startup only checks that the schema version is supported.
  - Establish a revision-aware rollback floor: after dual-write cutover, older code must refuse writes or enter an explicit maintenance read-only mode rather than silently resuming current-state-only mutations.
  - Seed one baseline revision for each currently active exchange and file at the migration cutoff.
  - Reconstruct exchange revisions only where exchange_admin_events contains real before and after evidence.
  - Give extracted file text its own hash rather than reusing the PDF binary hash.
  - Commit every active-row mutation and its revision or tombstone in the same transaction for save, update, delete, restore, and extraction paths while preserving current response contracts.
  - Record file content revision and location or ownership change as distinct provenance dimensions so a move cannot change meaning invisibly under an unchanged content hash.
  - Create production backups through the SQLite online backup API or a stopped-service WAL checkpoint; a raw main-file copy is not accepted as migration evidence.
  - Avoid nested Store lock acquisition by using transaction-local primitives.
- **Test Scenarios:**
  - Fresh database, current production-shaped database, repeated startup, interrupted migration, full disk, and unsupported schema version all produce a safe explicit result.
  - Exchange edit, delete, and restore preserve exact revisions and tombstones.
  - File edit, move, and delete preserve prior text revisions from the cutoff forward.
  - A pre-cutoff missing file version is reported as unavailable.
  - Failure between active-row mutation and revision append rolls back both.
  - A WAL-active backup restores every committed row and passes integrity and foreign-key checks.
  - Pre-revision code cannot reopen the migrated database in write mode.
  - Existing session overview, transcript, upload, move, edit, delete, and PDF behavior remains unchanged.
- **Verification:**
  - Run the focused source and legacy regression tests.
  - Apply the migration to a copy of the live SQLite database, run integrity checks, compare row counts, and verify no service process touches that copy.

### U3. Add Definitions, immutable artifacts, and canonical rendering

- **Goal:** Create the persistence and deterministic serialization kernel for Context Creator without HTTP, UI, MCP, or provider coupling.
- **Requirements:** R3-R6, R12-R16, R18.
- **Dependencies:** U2.
- **Files:**
  - Add app/context_domain.py, app/context_store.py, and app/context_rendering.py.
  - Modify app/storage.py only for shared transaction integration.
  - Add tests/test_context_domain.py and tests/test_context_rendering.py.
- **Approach:**
  - Persist logical Definitions, immutable revisions, ordered source expectations, section templates, budgets, policies, artifacts, sections, exact source snapshots, parent-artifact lineage, review events, and audit.
  - Allocate versions in a write transaction with uniqueness per Definition.
  - Define one canonical normalization and hashing protocol for manifests, sections, and full Markdown.
  - Designate one canonical stored artifact byte sequence as authoritative; manifests and sections are immutable components or hash-verified projections of those bytes.
  - Produce separate admin and consumer manifest projections so raw source names and snippets do not leak.
  - Make artifact content update and delete impossible through the domain API and SQLite triggers; use restrictive foreign keys rather than cascading removal; later lifecycle actions append events.
- **Test Scenarios:**
  - Two concurrent Definition saves yield one winner and one expected-revision conflict.
  - Reordering or renaming a section produces a new Definition Revision.
  - Identical input produces identical deterministic manual bytes and hashes.
  - Later Definition and source changes cannot alter an artifact reread.
  - Admin manifest has full provenance while consumer manifest contains only safe metadata.
  - A successor manual edit has a new hash and a parent pointer.
  - Raw SQL update or delete against Build payload, section, or snapshot is rejected and foreign_key_check remains clean.
- **Verification:**
  - Reassemble every artifact from stored sections and compare bytes and hashes.
  - Confirm no public update method can mutate artifact payloads.

### U4. Prove a provider-free manual Build lifecycle

- **Goal:** Complete the smallest end-to-end domain slice: manual assembly, hard validation, approval, and publication without external AI.
- **Requirements:** R12-R15, R18, R20-R21.
- **Dependencies:** U3.
- **Files:**
  - Add app/context_service.py and app/context_evals.py with deterministic gates only.
  - Add tests/test_context_builds.py.
- **Approach:**
  - Freeze Definition Revision, ordered source revision IDs, exact bytes, hashes, trust classes, and provenance as one stored Build input in a single transaction before rendering begins.
  - Accept owner-written structured sections and source references, validate every reference, render the canonical artifact, and expose it as an immutable candidate.
  - Implement deterministic gates for manifest completeness, hashes, missing snapshots, token and character budgets, high-confidence secret patterns, and privacy policy.
  - Keep approval, rejection, advisory override, and publish as append-only events against an expected artifact hash.
- **Test Scenarios:**
  - A Build with no provider can pass from Definition to published artifact.
  - Unknown source references, stale revisions, missing sections, invalid order, over-budget output, or secret findings block publication.
  - Duplicate publish and approval requests are idempotent.
  - Stale hash approval fails while the reviewed candidate remains intact.
  - Manual correction creates a successor instead of changing the candidate.
  - A forced source mutation between resolve and freeze yields one coherent old snapshot or an explicit conflict, never a mixed input set.
- **Verification:**
  - Use fixed source fixtures to prove byte-identical reads before and after source mutation.
  - Treat this unit as the deterministic core gate before any provider code starts.

### U5. Add exact session binding and read-only MCP delivery

- **Goal:** Deliver an approved Build to one session without exposing the Context catalog or changing legacy sessions.
- **Requirements:** R22-R31, R35-R37.
- **Dependencies:** U4.
- **Files:**
  - Add app/context_delivery.py.
  - Modify app/main.py, app/session_package.py, app/storage.py, tests/test_sessions.py, and tests/test_public_onboarding.py.
  - Extend scripts/session_audit.py and session-viewer.html where useful.
- **Approach:**
  - Add a unique canonical binding head per session plus append-only binding events rather than repurposing context_pack_id or context_pack_version.
  - Add an admin-domain create-and-bind transaction and keep public create_session unbound.
  - Use one BEGIN IMMEDIATE write protocol and lock order for create-and-bind, rebind, unbind, and save_exchange so exactly one of a pre-first-exchange binding mutation or the first exchange can win.
  - Reject every bind, rebind, or unbind after the first exchange and reject retired, rejected, or revoked artifacts.
  - Add context.read authorization and a binding-level OAuth client allowlist, then validate the active client on every manifest and chunk read.
  - Add a compact binding summary to get_session_overview.
  - Register only manifest, canonical body chunk, and section chunk tools; resolve Build identity from session_id and validate expected identity.
  - Route large Context body and section tools through the repository's shared tool-output mode so optimized and maximum-compatibility behavior remain consistent with transcript, file, and probe tools.
  - Keep SERVER_INSTRUCTIONS within its tested budget and move protocol detail to tool descriptions and model docs.
- **Test Scenarios:**
  - Unbound, legacy, unknown, approved, retired, and revoked sessions return distinct non-leaking results.
  - A consumer cannot fetch an artifact bound only to another session or supply an arbitrary Build ID.
  - Chunk reconstruction equals admin canonical bytes and hashes.
  - Build v2 publication never changes a session bound to v1.
  - Group moves and live file changes never alter v1.
  - Rebind succeeds before the first exchange and fails afterward.
  - Unbind fails after the first exchange.
  - Two concurrent bindings leave one head and a complete event ledger.
  - A concurrent rebind and first save_exchange produce exactly one winner.
  - Two authenticated clients know the same session ID, but only the allowed client can read the Build.
  - Both supported large-tool output modes preserve identical Context bytes and identity metadata, while exposing the expected MCP output-schema behavior after restart.
  - The public registry contains no Context mutation or enumeration tools.
- **Verification:**
  - Run focused MCP registry, session, package, and onboarding tests.
  - Connect a real MCP client to a test-bound session and prove manifest, full read, section refetch, and hash stability.

### U6. Turn search hits into canonical source selections

- **Goal:** Make archive discovery useful for curation while preserving canonical provenance and a complete BM25-only path.
- **Requirements:** R7-R13, R33-R34.
- **Dependencies:** U2, U3.
- **Files:**
  - Modify app/search.py, app/admin.py, and tests/test_search.py.
  - Extend app/context_sources.py.
  - Add tests/test_context_selection.py.
- **Approach:**
  - Expose existing group and source-kind filters through the admin search API.
  - Return search generation, mode, local-only status, and enough locator metadata to resolve the source without treating the snippet as content.
  - Group exchange hits by session for selection and provide drill-down to exact exchanges and files.
  - On add, read the canonical revision, compute or verify its hash, capture sensitivity and trust class, deduplicate, and preserve search query and rank as recommendation provenance.
  - Recheck drift and sensitivity at Build snapshot.
  - Never index generated Context Builds, which would create self-confirming retrieval loops.
- **Test Scenarios:**
  - BM25 works with no OpenAI key, no vector generation, a stale vector generation, and Cohere failure.
  - Sensitive groups stay local-only.
  - Edited, moved, deleted, empty, extraction-failed, duplicate, and newly sensitive sources produce explicit selection states.
  - Search rank changes do not alter an already selected revision.
  - A session-level selection expands into the exact revision set visible at snapshot.
- **Verification:**
  - Run search and selection tests.
  - Exercise the live jobs group in read-only mode and confirm the resolver, not search_documents, supplies Build bytes.

### U7. Ship the first manual Career workspace slice

- **Goal:** Give the owner a usable separate page for a complete Career Build v1 without model generation or final visual polish.
- **Requirements:** R3, R7-R15, R20-R23, R37, R40.
- **Dependencies:** U4-U6.
- **Files:**
  - Add context-creator.html, context-creator.js, and context-creator.css.
  - Add app/context_admin.py and tests/test_context_admin.py.
  - Modify app/main.py, app/admin.py, admin-viewer.html, and tests/test_admin.py for routing and top-level navigation.
- **Approach:**
  - Add a Sessions and Contexts top-level switch and serve /admin/contexts as a dedicated workspace.
  - Implement server-backed Definition drafts, a source search pane, session-grouped results, a curation basket, section assignment, manual section editor, budget meter, deterministic preview, gate results, approval, and create-and-bind.
  - Preserve current owner login, CSRF, no-store, encrypted settings, status, error, and async generation-guard patterns.
  - Render source, note, provider, and generated content through reviewed text-safe DOM paths; prohibit raw HTML and unsafe link schemes; serve external assets with a strict self-only CSP and no inline script.
  - Make the first happy path work entirely on BM25 and manual content using the jobs group.
  - Preserve drafts through refresh, login expiry, API errors, and stale-tab conflicts.
- **Test Scenarios:**
  - Create Career, query, inspect, add, reorder, remove, assign, write, preview, approve, create session, and copy session ID.
  - Empty search, zero sources, duplicate source, stale source, deleted source, sensitive source, 409 conflict, 401 expiry, CSRF failure, and network error preserve recoverable state.
  - The new page does not open nested dialogs and the Sessions page remains unchanged.
  - Stored-XSS fixtures in every displayed field render inert and cannot issue a request or access the CSRF token.
- **Verification:**
  - Add HTTP and HTML contract tests.
  - Perform a real-browser smoke pass before enabling the route outside development.

### U8. Add durable model-assisted generation

- **Goal:** Make synthesis optional, observable, restart-safe, and privacy-aware without weakening the manual workflow.
- **Requirements:** R12, R16-R20, R32-R34, R38.
- **Dependencies:** U4, U6, U7.
- **Files:**
  - Add app/context_jobs.py and app/context_providers.py.
  - Modify app/context_service.py, app/context_admin.py, app/settings.py, pyproject.toml, and tests/test_context_jobs.py.
- **Approach:**
  - Persist jobs, frozen inputs, stages, checkpoints, provider request IDs, retry lineage, idempotency keys, cancellation, diagnostics, usage, and safe cost metadata.
  - Recover queued work on startup; mark non-resumable running work interrupted; never leave it silently running.
  - Use a fake provider for all deterministic tests.
  - Implement OpenAI Responses with strict structured output behind the provider interface, explicit timeouts, bounded retries, an explicit non-storage request posture, provider background mode disabled, and per-section or staged generation when input exceeds one safe request.
  - Pass evidence only as typed data with opaque server IDs; give the builder no tools, URLs, archive access, or side-effect loop; preserve owner policy in a separate trusted channel.
  - Discard late responses after cancellation and publish the artifact only in one final transaction after complete validation.
  - Persist only sanitized error class, provider request ID, usage, and retry metadata; never persist raw provider bodies, prompts, source bytes, generated sections, or credentials as diagnostics.
- **Test Scenarios:**
  - Queue, claim, progress, complete, fail, cancel, late response, restart, interrupted recovery, retry, duplicate click, timeout, 429, malformed output, partial sections, and provider outage.
  - Definition changes after enqueue do not alter inputs.
  - Sensitive input prevents any external call.
  - No provider key leaves Generate disabled but manual mode complete.
  - Provider keys and raw input never appear in diagnostics or admin responses.
  - A provider error that echoes seeded source and secret canaries leaves neither canary in logs, database diagnostics, nor API responses.
  - Request capture proves non-storage mode and the absence of provider background mode on every call.
- **Verification:**
  - Pass fault-injected job tests before using a real key.
  - Run one low-sensitivity canary, compare provider usage to preflight, and verify stored artifact and source hashes.

### U9. Add eval, review, publish, retire, and revoke controls

- **Goal:** Make human judgment and safety gates first-class, inspectable, and resistant to stale state.
- **Requirements:** R18-R21, R25-R26, R32-R34.
- **Dependencies:** U7, U8.
- **Files:**
  - Expand app/context_evals.py, app/context_service.py, app/context_admin.py, context-creator.js, and tests/test_context_evals.py.
- **Approach:**
  - Evaluate required facts, forbidden facts, unsupported claims, source coverage, contradictions, redundancy, token budget, secrets, and privacy policy against one frozen artifact.
  - Use deterministic checks as hard gates and model judges only as advisory unless a later contract explicitly promotes one.
  - Show claim-to-source trace, omissions, redactions, prompt and model version, token usage, and exact final bytes.
  - Require expected Build hash on approval, publish, retire, revoke, and manual-successor actions.
  - Show impacted sessions before revoke and state that future Build reads stop while previously read client context, transcript quotations, provider copies, logs, artifacts, and backups are not erased.
- **Test Scenarios:**
  - Required fact present or absent, forbidden exact value, forbidden semantic topic, hallucinated source ID, contradictory dates, credential patterns, budget breach, and advisory-quality failure.
  - Advisory override requires a reason; hard gate cannot be overridden.
  - Double approval is idempotent and stale approval is rejected.
  - Retire preserves existing reads and blocks new bindings.
  - Revoke blocks all future reads without deleting audit or falling back.
- **Verification:**
  - Run the Career eval corpus at 5k and 15k targets.
  - Include malicious and contradictory source fixtures and document false-positive or false-negative limits.

### U10. Complete and polish the Context Creator workspace

- **Goal:** Turn the proven slice into the requested elegant manual context-building experience.
- **Requirements:** R9-R21, R23, R40.
- **Dependencies:** U7-U9.
- **Files:**
  - Expand context-creator.html, context-creator.js, and context-creator.css.
  - Modify app/context_admin.py and tests/test_context_admin.py.
- **Approach:**
  - Refine the workspace into persistent regions: Definition and Build navigator, archive search and evidence inspection, curation tray and section map, exact preview and provenance, and lifecycle actions.
  - Add clear modes for manual assembly and model-assisted suggestion without hiding which text came from the owner, a source, or a provider.
  - Add source drift badges, sensitive reveal, token distribution, omissions, job progress, retry and cancel, dirty-state routing, Build lineage, affected-session list, and create-and-bind handoff.
  - Provide keyboard reordering alternatives, focus management, live region updates, narrow-screen pane navigation, touch targets, and reduced-motion behavior.
  - Ensure late async responses cannot replace a newer Definition, search, source preview, job, or Build selection.
- **Test Scenarios:**
  - Full happy path by keyboard, pointer, and touch.
  - Long source names, long sections, empty Definitions, hundreds of candidates, slow search, stale responses, job refresh, cookie expiry, conflict, provider failure, and revoked Build.
  - Manual mode remains understandable when generation controls are unavailable.
- **Verification:**
  - Run checked-in browser scenarios at desktop and narrow viewport.
  - Complete a manual visual and accessibility pass using a production-shaped Career dataset.

### U11. Add browser, security, capacity, and recovery hardening

- **Goal:** Prove the feature under real state transitions and operational failures that unit and HTML string tests cannot cover.
- **Requirements:** R32-R40.
- **Dependencies:** U5, U8-U10.
- **Files:**
  - Add a Python-aligned Playwright or equivalent checked-in harness under tests/e2e.
  - Add or extend tests/test_security.py, tests/test_request_limits.py, tests/test_output_probe.py, scripts/context_audit.py, docs/operations.md, and CI configuration.
- **Approach:**
  - Automate the critical owner journey and failure states against a disposable database and fake provider.
  - Add client-bound Context authorization, redirect-URI validation, authorization-consent clarity, token revocation, provenance minimization, CSRF, CSP, stored-XSS, prompt-injection, secret-leak, response-limit, provider-retention, and consumer-egress tests.
  - Measure migration time, snapshot storage growth, search latency, Build generation input size, renderer time, and MCP chunk reconstruction.
  - Add integrity audit, orphan detection, impacted-session lookup, interrupted-job recovery, backup validation, and restore rehearsal.
  - Calibrate chunk targets with run_output_probe and submit_output_probe_observation through each supported client and large-tool output mode rather than browser-only estimates.
- **Test Scenarios:**
  - Two tabs edit one Definition; two clicks generate once; service restarts mid-job; owner logs out; Build is revoked while client reads; live files change after binding.
  - Unknown session, guessed Build ID, wrong expected hash, and consumer manifest inspection reveal no admin-only provenance.
  - OAuth registration rejects unsafe schemes, fragments, userinfo, wildcards, insecure non-loopback redirects, and excessive URI data; authorization identifies client, origin, scopes, and Context consequence.
  - Script, event-handler, SVG, malformed Markdown, javascript and data URL, closing-tag, and hostile provider-error payloads remain inert under browser-observed CSP.
  - Large but allowed input remains bounded; over-limit input fails before provider use; partial chunks reconstruct exactly.
- **Verification:**
  - Make the browser smoke suite and focused security suite CI gates.
  - Record output-probe observations and production-shaped capacity results in docs/operations.md.

### U12. Roll out, document, and retire dead Context Pack code

- **Goal:** Deploy safely, prove live continuity, and remove obsolete code only after the replacement path is real.
- **Requirements:** R35-R40.
- **Dependencies:** U1-U11.
- **Files:**
  - Update README.md, docs/model-instructions.md, docs/project-prompt-template.md, docs/limitations.md, docs/security.md, docs/operations.md, docs/deployment.md, and CHANGELOG.md.
  - Modify app/settings.py and .env.example for feature and provider controls.
  - Remove app/context_packs.py and tests/test_context_packs.py after compatibility proof.
  - Preserve legacy session columns until a separate migration proves they can be dropped.
- **Approach:**
  - Deploy additive schema with Context Creator and generation disabled.
  - Verify old sessions and public tools, enable owner UI for the manual slice, create and inspect a real Career v1, then enable consumer reads for one canary session.
  - Enable external generation only after separate policy acceptance and low-sensitivity canary proof.
  - Keep sensitive Build publication and binding disabled until client authorization, retention, backup expiry, and deletion or key-destruction behavior are implemented.
  - Prove v1 remains pinned while v2 serves a new session, then enable the feature normally.
  - Inventory ignored session-summary remnants and historical pack IDs; archive or document them rather than deleting automatically.
  - Remove dead module and settings only after import search, full tests, restart, and live health succeed.
- **Test Scenarios:**
  - Feature disabled, manual enabled, MCP delivery enabled, and generation enabled states each have an explicit health result.
  - Backup restores into a disposable instance with Builds, bindings, audit, and source revisions intact.
  - Legacy clients continue without tool changes until they opt into the new context protocol.
  - Rollback controls generation, new bindings, and emergency consumer-read denial through separate flags; normal rollback preserves reads for valid existing bindings.
  - Rollback never crosses below the revision-aware schema floor; an older binary refuses writes or enters maintenance read-only mode.
- **Verification:**
  - Run the full suite, demo script, migration rehearsal, browser suite, service health, systemd status, and real WW-MCP canary.
  - Verify live DB integrity and exact v1 hash before and after v2 publication and service restart.

---

## Verification Contract

### Sequential Test Ladder

| Stage | Scope | Required proof before the next stage |
|---|---|---|
| V0. Baseline | Current main before schema work | Full pytest passes; demo session passes; public tool names, current DB counts, legacy Context fields, service health, and output-probe baselines are recorded. |
| V1. Migration | Ordered schema and source revisions | Fresh and production-copy migration pass repeatedly; SQLite integrity succeeds; existing active rows and user-visible behavior match baseline. |
| V2. Immutable kernel | Definitions, revisions, artifacts, renderer | Deterministic manual artifact hash survives Definition and source mutation; invalid references and mutation attempts fail. |
| V3. Manual vertical slice | Approval, publish, binding, delivery | One provider-free Build completes from source fixture to exact MCP reconstruction; arbitrary Build access and mutation tools are absent. |
| V4. Discovery | BM25, filters, resolver, drift | Career sources can be found and selected without provider keys; stale and sensitive cases are explicit and no snippet becomes canonical input. |
| V5. Jobs and provider | Queue, structured output, faults | Fake-provider suite passes restart, cancel, retry, idempotency, malformed output, budget, and privacy cases before one real canary. |
| V6. Review and lifecycle | Evals, successor edit, approve, retire, revoke | Hard and advisory gates behave distinctly; v1 and v2 coexist; retire and revoke match their contracts. |
| V7. Browser experience | Full Context workspace | Automated keyboard and responsive journeys plus manual visual QA pass against production-shaped data and injected failures. |
| V8. Capacity and security | Limits, retention, auth, prompt injection | Output probes establish chunk limits per harness and large-tool output mode; threat-model tests pass; migration, storage, and rendering measurements fit the VPS envelope. |
| V9. Production canary | Additive deploy and feature flags | Old sessions are unchanged; Career v1 stays byte-identical through restart and v2 publication; health, logs, and restore proof are clean. |

### Repository Commands

| Gate | Command | Expected result |
|---|---|---|
| Focused source revisions | uv run pytest tests/test_context_sources.py -q | Migration and source-revision cases pass. |
| Focused domain and rendering | uv run pytest tests/test_context_domain.py tests/test_context_rendering.py tests/test_context_builds.py -q | Immutable kernel and hashes pass. |
| Focused selection and jobs | uv run pytest tests/test_context_selection.py tests/test_context_jobs.py tests/test_context_evals.py -q | Discovery, job, provider-fault, and eval cases pass. |
| Focused HTTP and MCP | uv run pytest tests/test_context_admin.py tests/test_sessions.py tests/test_public_onboarding.py -q | Admin lifecycle, consumer reads, registry, and compatibility pass. |
| Browser | uv run pytest tests/e2e -q | Critical Context workspace journeys pass in Chromium at desktop and narrow viewport. |
| Security and limits | uv run pytest tests/test_security.py tests/test_request_limits.py tests/test_output_probe.py -q | Auth, CSRF, leakage, request bounds, and probe contracts pass. |
| Full regression | uv run pytest | Entire repository passes. |
| Demo compatibility | uv run python scripts/demo_session.py | Existing demo workflow completes without Context Build. |
| Diff hygiene | git diff --check | No whitespace or patch errors. |
| Service health after deploy | curl http://127.0.0.1:8787/healthz | Healthy response from the restarted service. |
| Runtime state after deploy | systemctl status mcp-session-bridge --no-pager -l | Service is active with no restart loop. |

### Behavioral Quality Gates

- The Career corpus must define owner-reviewed required facts, forbidden facts, contradiction cases, and task prompts before model quality can be scored.
- Every factual generated claim must reference at least one captured source; reference validity is deterministic even when claim quality is not.
- Forbidden exact values and high-confidence credentials are deterministic blockers.
- Forbidden semantic topics are risk tests, not proof of absence; failed judge results block the canary until owner review but cannot justify a guarantee.
- The actual canonical artifact must remain within its numeric budget and every transport chunk within empirically measured harness limits.
- A small Build should be compared with a large Build on the same downstream tasks to measure marginal usefulness rather than assuming more tokens are better.
- Manual review must inspect omitted evidence and contradictions, not only polished prose.

### Production Verification Order

1. Back up the SQLite database and environment, record checksums and free disk, and restore the backup into a disposable location.
2. Stop before mutation if the backup cannot be opened, migrated, integrity-checked, and used by the test instance.
3. Deploy additive code with all Context feature flags off and restart.
4. Verify package import, systemd state, health, old MCP tools, admin login, sessions, files, search, and logs.
5. Enable manual Context admin only and build Career v1.
6. Enable session-scoped context reads for one new canary session and fetch the entire artifact through a real MCP client.
7. Restart the service and refetch the same hash.
8. Approve Career v2 for a second session and prove the first session remains on v1.
9. Enable external generation only after provider consent, data-retention review, and a non-sensitive canary.
10. Keep rollback feature-level: disable new routes and reads, preserve additive schema and artifacts, and investigate without destructive reversal.

---

## Definition of Done

### Global

- The owner can complete the full manual Context Creator journey through /admin/contexts without a provider key.
- The optional builder can propose structured content through a durable, recoverable job, but cannot approve, publish, bind, retire, or revoke.
- An approved Build is immutable, independently hash-verifiable, provenance-backed, and exactly reconstructable from admin and MCP projections.
- A session resolves one exact Build and never follows latest implicitly.
- The consumer MCP surface is session-scoped and read-only for Context Creator.
- Prospective archive revisions and migration gaps are truthful and auditable.
- Sensitive-provider, authorization, retention, prompt-injection, revocation, backup, and output-limit risks have accepted contracts and passing gates.
- Existing bridge behavior remains compatible for unbound and legacy sessions.
- Checked-in backend and browser tests, full regression, live VPS migration rehearsal, service health, and a real WW-MCP v1/v2 pinning scenario pass.
- Documentation explains the owner workflow, model protocol, security boundary, mutable live channels, reproducibility definition, recovery, and limitations.
- Dead Context Pack code is removed only after replacement proof, while historical columns and data remain intact until a separately approved migration.
- Experimental, abandoned, duplicate, and debug-only code from implementation attempts is removed before completion.

### Per Unit

- U1 is done when every P0 decision is explicit and accepted, with no later unit forced to invent product semantics.
- U2 is done when future source mutation is revision-preserving and migration succeeds on a live DB copy without active-view regression.
- U3 is done when immutable artifacts and canonical hashes survive every later mutation in tests.
- U4 is done when a provider-free artifact can be manually assembled, gated, approved, and published.
- U5 is done when a real client can fetch only its session-bound artifact and reproduce admin bytes.
- U6 is done when BM25 candidate discovery resolves canonical revisions and every stale or sensitive state is explicit.
- U7 is done when the first manual Career Build can be created and bound through the owner page.
- U8 is done when fake-provider fault coverage and one non-sensitive real-provider canary pass with durable recovery.
- U9 is done when review, successor edits, hard and advisory evals, approval, retire, and revoke match their contracts.
- U10 is done when the complete workspace passes keyboard, responsive, stale-state, and failure-path browser QA.
- U11 is done when security, capacity, output probe, restore, and recovery gates are repeatable.
- U12 is done when production canary and v1/v2 pinning pass, documentation is current, and obsolete code is retired safely.

---

## Appendix

### Current VPS Snapshot

- Repository: main at 64cd970c2277a50f1db00210c46201691f5407d8, clean at planning time.
- Package: mcp-session-bridge 0.3.0, Python 3.12+, FastMCP, Starlette through MCP, SQLite, httpx, sqlite-vec, tiktoken, and pytest.
- Live database: approximately 21 MB with 51 sessions, 311 exchanges, 23 files, 10 groups, 326 search documents, and 892 vector chunks at inspection time.
- Legacy context metadata: manual-context on 43 sessions, magic-smoke on 6, magic-32k-smoke on 1, ww-main-context on 1; every context_pack_version is NULL.
- app/context_packs.py has no production importer, its settings are absent from .env.example, and no live data/context-packs directory exists.
- Search is available locally through BM25. Hybrid is currently unavailable in the UI because no OpenAI API key is configured, despite an older ready vector generation and a configured Cohere key.
- The jobs group is large enough for the first Career slice and contains multiple sessions and shared files.

### Contradiction Register

| Assumption pair | Classification | Resolution in this plan |
|---|---|---|
| “The full archive is immutable” versus editable and hard-deletable current sources | Real current-state contradiction | Add prospective revisions and a truthful cutoff; exact earlier file history cannot be recovered. |
| Immutable Build versus editing during review | Real semantic contradiction if Build is edited in place | Editing creates a successor artifact; review events never mutate bytes. |
| Reproducible Build versus rerunning a remote model | Impossible under byte-identical interpretation | Preserve exact stored output and replayable inputs, prompt, model, settings, and hashes; do not promise identical rerun. |
| Consumer models never mutate context versus save_exchange and upload tools | Wording collision | Read-only applies to Context Creator objects; transcript and explicit-file tools keep their existing contracts. |
| Exact pinning versus automatic latest | Mutually exclusive behaviors | Exact pinning wins; new Builds affect only explicit new bindings. |
| Exact pinning versus emergency revoke | Intentional policy override | Revocation denies future reads without changing or replacing artifact bytes. |
| Full startup load versus bounded tool output and later compaction | Delivery tension, not contradiction | Manifest plus bounded canonical chunks at start; bounded section refetch later. |
| Flexible generated structure versus stable selective reads | Addressing tension, not contradiction | Stable section and policy identifiers with editable presentation and deterministic manifest order. |
| RAG chooses relevant context versus the human owns final memory | Authority tension, not contradiction | Search proposes; the owner selects; canonical resolver snapshots; approval is human-only. |
| Sensitive sources versus external generation | Current policy contradiction | Manual mode and local BM25 remain available; external generation is blocked by default. |
| One exact Context Build versus mutable session and group files | Scope tension | Build is one pinned baseline channel; transcript and live files are labelled separately as mutable. |
| “Forbidden topic cannot appear” versus probabilistic semantic classification | Impossible absolute guarantee | Use allowlisted sources, deterministic exact-value and secret gates, advisory semantic evals, provenance, and human review. |
| Historical source deletion versus immutable Build retention | Unresolved policy tension | Keep local snapshots for now, separate revoke from purge, and defer destructive purge design. |

### Preparation Checklist

- Owner time to settle the P0 contract and create a small Career quality rubric.
- A verified production DB backup, restore rehearsal, migration copy, free-disk measurement, and storage-growth budget.
- Sanitized fixtures covering legacy sessions, revised exchanges, deleted files, PDFs, sensitive groups, malicious prompts, and contradictory facts.
- A separate external-generation consent and provider-retention decision.
- OpenAI API credentials and quota only for U8 onward; none are required for U1-U7.
- A fake generator and deterministic test corpus before real-provider development.
- A browser-test environment and CI browser installation before final workspace polish.
- Output-probe runs from every real consumer harness expected to load 5k and 15k target Builds.
- A feature-flag matrix, canary session, rollback switch, integrity audit, and restore owner.
- An explicit decision about ignored data/session-summaries remnants and legacy columns before any cleanup.
