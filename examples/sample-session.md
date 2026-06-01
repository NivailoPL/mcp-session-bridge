# MCP Session Bridge Session Transcript

## Metadata

- session_id: `sample-session`
- title: Sample multi-model session
- exchange_count: 2
- turn_count: 4
- session_created_at: 2026-06-01T08:00:00+00:00
- session_updated_at: 2026-06-01T08:02:00+00:00
- response_display_timezone: UTC
- response_display_format: HH:MM (weekday, Month D, YYYY)

## Turn Sequence

USER
Claude
USER
GPT

## Transcript

### USER

We are testing whether MCP Session Bridge can keep a shared transcript across models.

### Claude - 08:01 (Monday, June 1, 2026)

<!-- created_at_display=08:01 (Monday, June 1, 2026) -->

I will start the shared session by naming the goal: preserve enough conversation history for the next model to continue without guessing.

### USER

Please continue from Claude's note and explain what the bridge should do next.

### GPT - 08:02 (Monday, June 1, 2026)

<!-- created_at_display=08:02 (Monday, June 1, 2026) -->

The bridge should expose an overview, return the transcript in bounded chunks, and save my full response before it is shown to the user.
