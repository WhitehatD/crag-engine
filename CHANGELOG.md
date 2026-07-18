# Changelog

## Unreleased — 2026-07-18

### Changed

- **Rename: crag-engine is now crag Anchor.** The package is `crag-anchor`, the
  binaries are `crag-anchor`, `crag-anchor-mcp`, and `crag-anchor-cli`, the
  import package is `crag_anchor`, the Docker image/service is
  `crag-anchor` / `crag-anchor:local`, the systemd unit is
  `crag-anchor.service`, the launchd label is `sh.crag.anchor`, and env vars
  are `CRAG_ANCHOR_*`. The role is unchanged: crag Anchor — the verified-memory
  engine for crag. No compatibility aliases: the old name was never published
  to PyPI and had no external users.
