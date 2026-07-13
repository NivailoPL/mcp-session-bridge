# Model Instructions

MCP Session Bridge now provides server-level MCP instructions through the standard `instructions` field. Those instructions give clients a short version of the tool protocol during tool selection.

For model clients that support project/system prompts, use the full prompt template:

- [project-prompt-template.md](project-prompt-template.md)

## Short Protocol

When a `session_id` is known:

1. Call `get_session_overview`.
2. Read `transcript_chunk_count`, `transcript_sha256`, exchange count, turn count, and chunk limits.
3. Call `get_last_speaker` with your own `model_name`. If it returns `should_fetch_transcript: false` (you saved the last turn) **and** you are still in the same chat window with that turn in your local context, you may skip the chunk fetch and answer from local context.
4. In every other case — different or unknown last speaker, a fresh window, or any doubt — fetch every `get_session_transcript_chunk` from `chunk_index=1` through `transcript_chunk_count`.
5. Only answer after the required transcript chunks are available, or after a confirmed same-model, same-window skip.
6. Before showing the final response to the user, call `save_exchange`. This is required on every turn, even when you skipped the chunk fetch.

When starting a new session, call `list_session_groups` first. If the user names a group, pass the matching `group_id` to `create_session`; otherwise let the session default to `uncategorized`.

## Response Timestamps

Read `response_display_timezone` from `get_session_overview` when you need to know the bridge display timezone before saving a response. `save_exchange` returns `assistant_created_at_display` and `assistant_created_at_timezone`; treat those returned values as authoritative for the user-visible response header. Do not convert them into the user's local timezone. MCP Session Bridge renders response display timestamps in the configured bridge display timezone, UTC by default.

If the user asks you to save a summary, plan, note, or reusable context, prepare the user-facing response, save it with `save_exchange`, then save the durable text with `upload_session_file` or `upload_group_file`.

## File Context And Reconciliation

MCP Session Bridge is not an automatic file-context delivery system. It does not automatically ingest or provide the user's external notes, project files, directories, PDFs, or other knowledge material to the model.

Files may be explicitly uploaded through the admin UI or saved with `upload_session_file` and `upload_group_file`. An owner may later move, edit, or permanently delete them. The current file manifest is authoritative: use `get_session_overview` or `list_session_files`, then `download_session_file` when a listed file is relevant. Models are not automatically notified about owner file mutations, so do not rely on an older manifest or cached file content.

## Response Storage

`save_exchange` should receive the full latest user message and the full assistant response exactly as the model is about to show it. This is what allows future models to continue from the transcript without reconstructing missing context.
