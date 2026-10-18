# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-18

### Added

- Stdio MCP server for one Kener v4 instance, configured with `KENER_URL` and `KENER_API_KEY`.
- Tools: `list_monitors`, `list_incidents`, `get_incident`, `create_incident`, `post_incident_update`, `resolve_incident`.
- `KENER_API_KEY_CMD` / `--api-key-cmd`: get the API key from a helper command (for example `op read`) on first use, with a timeout. A 401 makes the next call run the command again.
- `kener-mcp check` to test the URL and key.

[Unreleased]: https://github.com/alphaomegateam/kener-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/alphaomegateam/kener-mcp/releases/tag/v0.1.0
