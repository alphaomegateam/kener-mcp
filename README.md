# kener-mcp

An [MCP](https://modelcontextprotocol.io) server for [Kener](https://github.com/rajnandan1/kener) v4 status pages. It lets an AI agent (Claude Code, Claude Desktop, or any MCP client) read monitors and incidents, and create, update, and resolve incidents.

Tested against Kener 4.1.7.

## Tools

| Tool | What it does | Writes? |
|------|--------------|---------|
| `list_monitors` | Lists monitors: tag, name, status, type, category. Monitor config (`type_data`) is never returned, because it can hold target URLs and request headers. | No |
| `list_incidents` | Lists incidents, newest first. Only open incidents unless `include_resolved` is true. | No |
| `get_incident` | Gets one incident with its update timeline. | No |
| `create_incident` | Creates an incident that starts now. It can attach monitors and post a first update. | Yes |
| `post_incident_update` | Posts an update and sets the state (`INVESTIGATING`, `IDENTIFIED`, `MONITORING`, `RESOLVED`). | Yes |
| `resolve_incident` | Posts a `RESOLVED` update, which closes the incident and sets its end time. | Yes |

There is no delete tool. Delete incidents in the Kener dashboard.

The read tools have the MCP `readOnlyHint` annotation, so clients can allow them without a prompt and still ask before a write.

## Configuration

One server serves one Kener instance. To use more than one instance, register one server per instance, each with its own URL and key.

| Variable | Required | Description |
|----------|----------|-------------|
| `KENER_URL` | Yes | Base URL of the Kener instance, for example `https://status.example.com`. |
| `KENER_API_KEY` | One of these two | A Kener API key. Create one in the Kener dashboard under **Manage > API Keys**. |
| `KENER_API_KEY_CMD` | One of these two | A command that prints the API key on stdout, for example `op read op://Vault/Item/api_key`. See [API key command](#api-key-command). |
| `KENER_API_KEY_CMD_TIMEOUT` | No | Seconds to wait for the key command. Default `30`. |
| `KENER_NAME` | No | A short name for the instance. It is used in the server name and in error messages. |
| `KENER_INSTRUCTIONS` | No | Extra text for the agent, for example "This page is client-facing; updates also post to the client's Slack." |

The `--url`, `--api-key-cmd`, and `--name` flags override the matching variables. There is no `--api-key` flag, because process lists show command-line arguments.

### API key command

Use `KENER_API_KEY_CMD` (or `--api-key-cmd`) to get the key from a secret manager, so that the key is not in your MCP config:

- The command runs on the first tool call, not at server start. A slow or locked secret manager does not block the MCP handshake.
- The key is kept in memory. If Kener rejects it (HTTP 401), the next tool call runs the command again, so a rotated key is picked up.
- The command is split like a shell command line (`shlex`) but does not run in a shell. Pipes, `$VARS`, and `~` are not expanded. For those, point it at a script.
- The command must print only the key. If it fails, the error shows its exit code and stderr, never its stdout.

Examples:

```bash
# 1Password
KENER_API_KEY_CMD='op read "op://Infra/Kener API key/api_key"'
# macOS Keychain
KENER_API_KEY_CMD='security find-generic-password -s kener -w'
# pass
KENER_API_KEY_CMD='pass show kener/api-key'
```

## Install

```bash
uv tool install git+https://github.com/alphaomegateam/kener-mcp
```

Then check the URL and key:

```bash
KENER_URL=https://status.example.com KENER_API_KEY=... kener-mcp check
```

## Register with Claude Code

```bash
claude mcp add kener -s user \
  -e KENER_URL=https://status.example.com \
  -e KENER_API_KEY=your-key \
  -- kener-mcp
```

For two instances, add two servers:

```bash
claude mcp add kener-prod -s user -e KENER_URL=https://status.example.com -e KENER_API_KEY=... -- kener-mcp
claude mcp add kener-staging -s user -e KENER_URL=https://status-staging.example.com -e KENER_API_KEY=... -- kener-mcp
```

With a key command instead of a stored key:

```bash
claude mcp add kener -s user -- kener-mcp \
  --url https://status.example.com \
  --api-key-cmd 'op read "op://Infra/Kener API key/api_key"'
```

## Register with Claude Desktop (or another MCP client)

```json
{
  "mcpServers": {
    "kener": {
      "command": "kener-mcp",
      "env": {
        "KENER_URL": "https://status.example.com",
        "KENER_API_KEY": "your-key"
      }
    }
  }
}
```

## Kener API notes

These notes describe Kener 4.1.7 as read from its source:

- An update is `POST /api/v4/incidents/{id}/comments` with `{"comment": "...", "state": "..."}`. The Kener docs show `message` and `status`. The server rejects those names.
- There is no resolve endpoint. A comment with state `RESOLVED` sets the incident state and its end time. `PATCH /api/v4/incidents/{id}` cannot change the state.
- API-created incidents start in state `INVESTIGATING`.
- Updates posted through the API do not email subscribers ([rajnandan1/kener#886](https://github.com/rajnandan1/kener/issues/886)). Updates made in the dashboard do.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests use [respx](https://lundberg.github.io/respx/) to check the HTTP request shapes. They call no real Kener instance.

## License

MIT. See [LICENSE](LICENSE).
