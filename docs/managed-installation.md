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
- an owner username and a password of at least 10 characters for a new installation. An adopted installation can keep its current owner credentials.

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

Preview the detected installation and every setup section without writing to `/opt`, `/etc`, or `/var` (the `./mcp-bridge` bootstrap may still install `uv` for your login and create the checkout's local environment):

```bash
./mcp-bridge setup --dry-run
```

Run the guided setup:

```bash
./mcp-bridge setup
```

The command opens a resumable dashboard. Each numbered section shows its current state and can be selected independently:

- `DETECTED`: existing data or configuration can be adopted;
- `NEEDS INPUT`: required information is missing;
- `READY`: the section is configured or staged but not live;
- `ACTIVE`: the managed component is running;
- `WAITING`: configuration is complete but an external dependency such as DNS is pending;
- `ATTENTION` or `FAILED`: the dashboard prints the required next action.

The normal order is server inspection, adoption choice, release staging, database staging, administrator, public address, service preparation, activation, and verification. You can exit at any point and run `./mcp-bridge setup` or `mcp-bridge setup` later to continue.

Preparing a section does not replace the live systemd unit or Caddy configuration. **Activate installation** is a separate confirmation that:

1. creates a dated backup;
2. validates the intended Caddy configuration;
3. briefly stops the current Bridge;
4. takes the final SQLite copy so late writes are retained;
5. switches and explicitly restarts the managed service;
6. verifies the local health endpoint;
7. restores the previous service configuration automatically if activation fails.

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
| `/etc/mcp-session-bridge/bridge.env` | Active private runtime configuration |
| `/var/lib/mcp-session-bridge/state/pending/bridge.env` | Root-owned configuration staged by setup |
| `/var/lib/mcp-session-bridge` | SQLite database, context packs, and status state |
| `/var/backups/mcp-session-bridge` | Setup and update backups |
| `/usr/local/bin/mcp-bridge` | Daily CLI command |
| `/etc/systemd/system/mcp-session-bridge*.service` | Service, restart helper, and status refresh |
| `/etc/caddy/conf.d/mcp-session-bridge.caddy` | HTTPS reverse-proxy fragment |

Setup installs a bootstrap `/usr/local/bin/mcp-bridge` command immediately. Repeated setup runs keep the existing Bridge-owned launcher and skip this bootstrap step. Activation later replaces the bootstrap with the managed-release launcher.

Activation creates an unprivileged `mcp-session-bridge` service account, installs locked Python dependencies with `uv`, initializes or migrates SQLite, atomically promotes the staged environment and release, installs the staged systemd units, and configures or adopts Caddy. Setup keeps pending configuration and unit files root-owned; the service account owns only runtime data that the application must change.

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

Use the section menu instead of editing managed files:

```bash
mcp-bridge configure
```

Or open one section directly:

```bash
mcp-bridge configure domain
mcp-bridge configure administrator
```

Flag-based operation remains available for automation:

```bash
mcp-bridge configure --domain new-bridge.example.com
mcp-bridge configure --username owner
```

The command can preserve or rotate the owner password, validates the replacement Caddy configuration, reloads HTTPS, and restarts the Bridge. If the original domain was adopted from the main Caddyfile, its site address is updated in place so a duplicate proxy block is not created. If activation fails, the previous environment and Caddy configuration are restored.


## Adopt An Existing Checkout

When setup is run from an older checkout, it detects the checkout's `.env` and `data/bridge.sqlite3` when present. It shows the detected session count and owner/public configuration before asking whether to adopt them. Preparation uses SQLite's online backup API and leaves the source checkout untouched; activation creates the dated backup and takes a final copy while the old service is stopped.

Before adoption:

```bash
./mcp-bridge setup --dry-run
```

Then run normal setup and confirm:

```bash
mcp-bridge doctor
```

Do not remove the old checkout until the managed service, admin login, sessions, and one MCP client have all been verified.

## Terminal Color

Interactive headings use the Indygo accent (`#6366F1`) when stdout is a compatible terminal. Data, logs, redirected output, and JSON remain unstyled. Disable color with either:

```bash
NO_COLOR=1 mcp-bridge status
mcp-bridge --no-color setup
```

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
## Setup control center

Run `mcp-bridge setup` from an SSH terminal. The control center lets you inspect every section before choosing an action. Arrow navigation is used on a capable TTY; a numbered menu remains available for `TERM=dumb`. Opening a section or selecting Return does not acquire the operation lock and does not modify the server.

The Database section can verify, create a portable backup, import by complete replacement, migrate, optimize, or reset. Import never mutates the supplied source file. It stages and migrates a copy, makes a final verified backup of the current database after stopping Bridge, performs the replacement, verifies service-user WAL access, and rolls back on failure.

Portable database files contain conversations and other sensitive runtime rows. A DB-only restore preserves conversations, groups, and uploaded files, but credentials encrypted or hashed against a different installation secret may not remain usable. Reconnect harnesses and re-enter provider keys after restoring into a fresh configuration.

Preview removal before executing it:

```bash
mcp-bridge installation uninstall --dry-run --remove-data \
  --output /root/mcp-bridge-database-export.sqlite3
```

The uninstaller verifies the export before removing selected managed data. It does not remove the Git checkout, Caddy itself, unrelated Caddy files, or unrelated host services.
