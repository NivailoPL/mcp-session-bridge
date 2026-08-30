# Versioning

MCP Session Bridge uses calendar versions in the form `YYYY.M.N` for releases made after the `0.x` series:

- `YYYY` is the release year;
- `M` is the release month without a leading zero;
- `N` is the sequence number of a Bridge release within that month, starting at `1`.

For example, `2026.8.2` is the second Bridge release planned for August 2026. The final component is a monthly sequence, not a day of the month and not an indicator of release size.

## Development And Stable Releases

A development branch is named `release/YYYY.M.N-beta`. While it is being tested, the package keeps the version of the latest stable release in `[project].version`, and `[tool.mcp-session-bridge.development]` records the target version and the label shown in **Settings → Status**.

On a `release/2026.9.1-beta` branch, for example, the package still reports the
last stable release while the development block names the version being built:

```toml
[project]
version = "2026.8.2"

[tool.mcp-session-bridge.development]
target-version = "2026.9.1"
label = "2026.9.1-beta"
```

**Settings → Status** shows `2026.9.1-beta` for that branch; the package itself
still answers `2026.8.2` until the release lands.

The beta suffix is a development label, not a stable Git tag. The managed updater continues to consume stable GitHub Releases only.

To publish the stable release:

1. Change `[project].version` and the locked package version to `YYYY.M.N`.
2. Remove or finish the development label by making `target-version` equal to the package version.
3. Merge the reviewed release branch into `main`.
4. Create the tag `vYYYY.M.N` on the release commit.
5. Let the release workflow test the tag and publish the matching archive and manifest.

The release workflow rejects versions that are not valid `YYYY.M.N` calendar versions. The updater still understands historical stable `0.x` versions so an installation can move directly from `0.5.0` to `2026.8.2`.

## What The Number Does Not Express

Calendar versions do not promise compatibility. Database and artifact formats have their own explicit version fields, while breaking changes and required operator actions belong in the changelog and GitHub Release notes.
