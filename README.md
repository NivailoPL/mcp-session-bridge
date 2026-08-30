<p align="center">
  <img src="https://panchmurka.wtf/lockup-vertical-dark.png" width="300" alt="">
</p>


<h1 align="center">MCP Session Bridge</h1>
<p align="center"><strong>The conversation continues.</strong></p>

MCP Session Bridge is a shared notebook between models. It keeps one conversation
in one durable place, so any assistant you connect can read the whole thread and
add to it.

Nothing is summarised. Nothing leaves your server.

![Version](https://img.shields.io/badge/version-2026.8.2-6C5CF2)
![License](https://img.shields.io/badge/license-MIT-6C5CF2)

---

## Is it for you?

If you find yourself copy-pasting between models, this might help.

**You want a second opinion on a long thread.**
You have been working through something with GPT for forty minutes. You want
Claude's read on it — not on a summary of it, on the actual thread. You paste
one ID and Claude has all of it.

**You brainstorm in one place and build in another.**
You design a feature with GPT and Grok over several turns, then hook Claude Code
to the same session for the implementation. The implementer sees the arguments,
not just the decision.

**You want the record to outlive the window.**
Chat windows get compacted, expire, or disappear into a sidebar. A session has an
address. Come back in three weeks and the thread is exactly as you left it —
every exchange, in full.

It runs on the subscriptions you already pay for. The Bridge adds no model costs
of its own; it stores what your assistants say and hands it to the next one.

It is single-user and private by design. You run it on your own machine and
authorise only the harnesses you choose to trust.

---

## How it works

1. You ask an assistant to start a session. It connects to your server over MCP
   and creates one.
2. It hands you a session ID — `20260826-185134-session-ffde79`. That ID is the
   key to the thread.
3. You paste the ID into another assistant. It pulls the transcript in chunks,
   reads the history, answers you, and saves its answer back.

Sessions are not listable from outside. They cannot be enumerated or guessed. If
you have the ID you have the thread; if you do not, there is nothing to find.

Set up a project in your harness with a prompt template for these conversations.
One ships with the repo — `project-prompt-template.md`. It tells the model when
to fetch the transcript and when to save, so you do not have to ask each time.

---

## Quick start

You will need a Debian or Ubuntu server with SSH, a public IP, a hostname you
control, and inbound ports 22, 80 and 443.

```bash
apt-get update
apt-get install -y git curl ca-certificates
git clone https://github.com/NivailoPL/mcp-session-bridge.git
cd mcp-session-bridge
./mcp-bridge setup
```

`setup` asks for your domain, issues a certificate, and starts the service.
Afterwards, `mcp-bridge doctor` checks a running install and
`mcp-bridge status` shows what is up.

---

## Connect a harness

Any client that supports custom MCP connectors will work. The steps are the same
everywhere:

1. Add a custom connector pointing at your domain — `https://mcp.example.com`
2. Authorise it. The Bridge uses OAuth 2.1 with PKCE; your harness runs the flow.
3. Create a project and paste `project-prompt-template.md` into its instructions.

Verified against: chatgpt.com, claude.ai, grok.com, Codex App, Claude Code App.

---

## What's in the box

|                         |                                                              |
| ----------------------- | ------------------------------------------------------------ |
| **Sessions**            | One thread each, addressed by ID, titled automatically.      |
| **Exchanges**           | A complete user ↔ model pair. Stored whole, never distilled. |
| **Chunked transcripts** | Long threads come back in bounded pieces, so nothing arrives as one oversized result. |
| **Groups**              | Sets of sessions that share files and context.               |
| **Files**               | Text and PDF, attached to one session or to a whole group.   |
| **Masking**             | Hide a turn, or an entire exchange, from what the models see. |
| **Admin UI**            | Browse sessions, read transcripts, manage files and groups.  |
| **CLI**                 | `setup`, `config`, `doctor`, `status`.                       |

Files enter the Bridge only when you upload them. Nothing on your machine is
scanned, indexed, or ingested in the background.

---

## Your data

Everything sits on your server in a SQLite database. You can copy it, back it up,
and open it with any SQLite tool — there is no proprietary format in the way.

Nothing is distilled or compacted automatically. The transcript stays raw, which
means you can point something else at it — a memory layer, an index, your own
notes — without first unpicking somebody's summary. I leave the raw data for you
to manage.

Bulk export to Markdown is operational. Runyourself or automate for agentic use with `mcp-bridge export`.

---

## Limits

**No OCR.** A PDF without a text layer uploads fine and comes back empty. The
Bridge says so rather than pretending.

**Single user.** No accounts, no sharing, no roles. One person, one server.

**Saving is deliberate.** A turn enters the record when a model calls
`save_exchange`. If it does not, that turn is not in the thread. The prompt
template exists to make this dependable — it is the part of the setup worth
getting right.

**A server to look after.** Certificates, updates and backups are yours.

---

## Status

Version 2026.8.2. Built and used daily by one person.
Expect rough edges, and open an issue when you find one.

---

## License

MIT.