# Documentation

## Running a Bridge

| | |
| --- | --- |
| [managed-installation.md](managed-installation.md) | **The supported path.** `./mcp-bridge setup` on a Debian or Ubuntu VPS. The installer owns certificates, systemd, updates and rollback. Read this one first. |
| [client-setup.md](client-setup.md) | Adding the Bridge as a custom MCP connector, per client. |
| [operations.md](operations.md) | Running an installed Bridge: status, logs, export, audit, backups. |

## Working with models

| | |
| --- | --- |
| [project-prompt-template.md](project-prompt-template.md) | Paste-ready project instructions. This is the part of the setup worth getting right. |
| [model-instructions.md](model-instructions.md) | The tool protocol the server advertises to clients. |

## Reference

| | |
| --- | --- |
| [limitations.md](limitations.md) | What the Bridge deliberately does not do. |
| [security.md](security.md) | Threat model, credentials, what is stored where. |
| [versioning.md](versioning.md) | How releases are numbered. |

## Other install paths

Neither of these is the recommended way to run a persistent Bridge. Use
[managed-installation.md](managed-installation.md) for that.

| | |
| --- | --- |
| [installation.md](installation.md) | A local checkout for development and for trying the demo. No domain, no MCP client. |
| [deployment.md](deployment.md) | Uvicorn, systemd and Caddy templates, for a host that deliberately opts out of CLI-managed releases. |
