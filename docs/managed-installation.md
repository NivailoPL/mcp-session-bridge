# Managed Server Installation

This is the supported onboarding path for a persistent MCP Session Bridge on a VPS. The installer owns the host lifecycle; the web admin remains a read-only operational view for service status and updates.

## Before You Start

Prepare:

- a 64-bit Debian or Ubuntu VPS with `systemd`, `apt`, a public IP address, and at least a small amount of persistent disk space;
- SSH access as `root`, or a user that can run `sudo`;
- a domain or subdomain dedicated to the Bridge;
- DNS access so you can create an `A` record pointing that hostname at the VPS;
- inbound TCP ports 22, 80, and 443;
- Git, curl, and CA certificates on the server;
- an owner username and a new password of at least 10 characters.

The setup does not connect ChatGPT, Claude, Codex, or another harness. Client connection is a separate step after the server is healthy.

## Install From Zero

Connect to the server and install the small bootstrap prerequisites:

```bash
apt-get update
apt-get install -y git curl ca-certificates
```

Clone the repository:

```bash
git clone https://github.com/NivailoPL/mcp-session-bridge.git
cd mcp-session-bridge
```

Optionally preview the managed-system plan without writing to `/opt`, `/etc`, or `/var` (the `./mcp-bridge` bootstrap may still install `uv` for your login and create the checkout's local environment):

```bash
./mcp-bridge setup --dry-run
```

Run the guided setup:

```bash
./mcp-bridge setup
```

The command asks for the public hostname, owner username, and owner password. It then reports every major stage and finishes in one of three states:

- `PASS`: installation is complete;
- `WAITING`: the local service is installed, but DNS is not resolving yet;
- `FAILED`: the command stopped and prints the failing stage.

If setup reports `WAITING`, create or correct the DNS record, wait for propagation, then run:

```bash
mcp-bridge status
```

The DNS check turns healthy automatically when the hostname resolves.

## What Setup Owns

The managed installation uses versioned releases and stable host paths:

| Path | Purpose |
| --- | --- |
| `/opt/mcp-session-bridge/releases/<version>` | Immutable application releases |
| `/opt/mcp-session-bridge/current` | Active release symlink |
| `/etc/mcp-session-bridge/bridge.env` | Private runtime configuration |
| `/var/lib/mcp-session-bridge` | SQLite database, context packs, and status state |
| `/var/backups/mcp-session-bridge` | Setup and update backups |
| `/usr/local/bin/mcp-bridge` | Daily CLI command |
| `/etc/systemd/system/mcp-session-bridge*.service` | Service, restart helper, and status refresh |
| `/etc/caddy/conf.d/mcp-session-bridge.caddy` | HTTPS reverse-proxy fragment |

Setup creates an unprivileged `mcp-session-bridge` service account, installs locked Python dependencies with `uv`, initializes or migrates SQLite, installs the systemd units, and configures Caddy.

Secrets are written only to the private environment file. Re-running setup preserves the existing Bridge secret so OAuth records and encrypted admin settings remain readable.

## Verify The Result

Run the fast report:

```bash
mcp-bridge status
```

Then run deeper read-only diagnostics:

```bash
mcp-bridge doctor
```

Useful final checks:

```bash
curl https://bridge.example.com/healthz
mcp-bridge logs
```

Open `https://bridge.example.com/admin/sessions`, sign in, and inspect **Settings → Status**. The page mirrors the cached CLI report and live application/database reachability. It does not install updates or run arbitrary server commands.

The MCP endpoint is:

```text
https://bridge.example.com/mcp
```

## Change The Domain Or Owner

Use the guided configuration command instead of editing managed files:

```bash
mcp-bridge configure --domain new-bridge.example.com --username owner
```

The command optionally rotates the owner password, validates the replacement Caddy configuration, reloads HTTPS, and restarts the Bridge. If activation fails, it restores the previous environment and Caddy fragment.


## Adopt An Existing Checkout

When setup is run from an older checkout, it detects the checkout's `.env` and `data/bridge.sqlite3` when present. It copies the database through SQLite's online backup API, creates a dated backup, and leaves the source checkout untouched.

Before adoption:

```bash
./mcp-bridge setup --dry-run
```

Then run normal setup and confirm:

```bash
mcp-bridge doctor
```

Do not remove the old checkout until the managed service, admin login, sessions, and one MCP client have all been verified.

## Updates And Recovery

Check without installing:

```bash
mcp-bridge update --check
```

Install the latest stable release:

```bash
mcp-bridge update
```

The updater accepts only stable semantic versions, downloads the matching GitHub release asset over HTTPS, verifies its GitHub-provided SHA-256 digest, preflights database migrations on a copy, creates a live backup, switches the release symlink, and verifies the restarted service. A failed activation restores the previous release and database.

Roll back the most recently completed update:

```bash
mcp-bridge rollback
```

After an update or rollback, reconnect MCP clients so they refresh their tool schemas.

## Compatibility Boundary For Agent Plugins

A future Agent Plugins adapter can be added without moving host management into a plugin. The compatibility boundary is deliberately narrow:

- `mcp-bridge setup`, `status`, `doctor`, `update`, and `rollback` remain stable host operations;
- installation and status metadata use versioned JSON files;
- MCP client or plugin registration stays separate from server installation;
- adapters may generate or validate client-facing configuration, but must not own the database, release symlink, systemd service, or raw secrets;
- provider-specific screenshots and harness instructions remain optional documentation.

This leaves room for a v2 adapter targeting the [Agent Plugins standard](https://agent-plugins.org/) while keeping v1 installations deterministic and recoverable.
