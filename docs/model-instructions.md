# Model Instructions

MCP Session Bridge now provides server-level MCP instructions through the standard `instructions` field. Those instructions give clients a short version of the tool protocol during tool selection.

For model clients that support project/system prompts, use the full prompt template:

- [project-prompt-template.md](project-prompt-template.md)

## Short Protocol

When a `session_id` is known:

1. Call `get_session_overview`.
2. Read `transcript_chunk_count`, `transcript_sha256`, exchange count, turn count, and chunk limits.
3. Fetch every `get_session_transcript_chunk` from `chunk_index=1` through `transcript_chunk_count`.
4. Only answer after the required transcript chunks are available.
5. Before showing the final response to the user, call `save_exchange`.

## Response Timestamps

Read `response_display_timezone` from `get_session_overview` when you need to know the bridge display timezone before saving a response. `save_exchange` returns `assistant_created_at_display` and `assistant_created_at_timezone`; treat those returned values as authoritative for the user-visible response header. Do not convert them into the user's local timezone. MCP Session Bridge renders response display timestamps in the configured bridge display timezone, UTC by default.

If the user asks for a summary:

1. Prepare the user-facing response.
2. Save the response with `save_exchange`.
3. Save the Markdown summary with `save_session_summary`.
4. Then show the response to the user.

## What The Bridge Is Not

MCP Session Bridge is not a file-context delivery system. It does not automatically provide the user's external notes, project files, PDFs, or other knowledge material to the model.

The user supplies those materials manually in the chat. The bridge supplies conversation history and summaries.

## Response Storage

`save_exchange` should receive the full latest user message and the full assistant response exactly as the model is about to show it. This is what allows future models to continue from the transcript without reconstructing missing context.
