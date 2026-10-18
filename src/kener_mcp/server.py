"""FastMCP server that exposes incident tools for one Kener v4 instance.

Configure it with environment variables:

  KENER_URL                  Base URL of the Kener instance (required).
  KENER_API_KEY              Kener API key. Create one under Manage > API Keys.
  KENER_API_KEY_CMD          Or: a command that prints the API key, for example
                             "op read op://Vault/Item/api_key". It runs on first use.
  KENER_API_KEY_CMD_TIMEOUT  Seconds to wait for that command (default 30).
  KENER_NAME                 Short name for this instance, used in messages (optional).
  KENER_INSTRUCTIONS         Extra text for the agent, such as who reads the page.

Set exactly one of KENER_API_KEY and KENER_API_KEY_CMD. The --url, --api-key-cmd
and --name flags override the matching variables.

For more than one instance, register one server per instance.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .auth import DEFAULT_CMD_TIMEOUT, ApiKeyError, ApiKeyProvider
from .client import Impact, IncidentState, KenerClient, KenerError

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

NOTIFY_CAVEAT = (
    "On Kener 4.1.7 and earlier, updates posted through the API do not email "
    "subscribers (rajnandan1/kener#886)."
)


class SettingsError(Exception):
    """A required environment variable is missing or not valid."""


@dataclass(frozen=True)
class Settings:
    url: str
    api_key: str | None = None
    api_key_cmd: str | None = None
    api_key_cmd_timeout: float = DEFAULT_CMD_TIMEOUT
    name: str | None = None
    instructions: str | None = None

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise SettingsError("KENER_URL must be an https:// or http:// URL.")
        if parts.username or parts.password:
            raise SettingsError("KENER_URL must not contain a user name or password.")
        if bool(self.api_key) == bool(self.api_key_cmd):
            raise SettingsError("Set exactly one of KENER_API_KEY and KENER_API_KEY_CMD.")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **overrides: str | None) -> "Settings":
        env = os.environ if env is None else env

        def get(key: str) -> str | None:
            override = overrides.get(key.removeprefix("KENER_").lower())
            value = override if override is not None else env.get(key, "")
            return value.strip() or None

        url = get("KENER_URL")
        if not url:
            raise SettingsError("KENER_URL is not set.")
        timeout = get("KENER_API_KEY_CMD_TIMEOUT")
        try:
            timeout_s = float(timeout) if timeout else DEFAULT_CMD_TIMEOUT
        except ValueError:
            raise SettingsError("KENER_API_KEY_CMD_TIMEOUT must be a number.") from None
        api_key_cmd = get("KENER_API_KEY_CMD")
        return cls(
            url=url.rstrip("/"),
            # A command given as a flag wins over a key in the environment.
            api_key=None if overrides.get("api_key_cmd") else get("KENER_API_KEY"),
            api_key_cmd=api_key_cmd,
            api_key_cmd_timeout=timeout_s,
            name=get("KENER_NAME"),
            instructions=get("KENER_INSTRUCTIONS"),
        )

    def key_provider(self) -> ApiKeyProvider:
        return ApiKeyProvider(self.api_key, self.api_key_cmd, self.api_key_cmd_timeout)

    @property
    def label(self) -> str:
        return self.name or self.url


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _incident_summary(inc: dict) -> dict:
    return {
        "id": inc["id"],
        "title": inc["title"],
        "state": inc["state"],
        "started_at": _iso(inc.get("start_date_time")),
        "ended_at": _iso(inc.get("end_date_time")),
        "source": inc.get("incident_source"),
        "monitors": inc.get("monitors", []),
        "url": inc.get("url"),
    }


def _comment_summary(c: dict) -> dict:
    return {
        "id": c["id"],
        "state": c["state"],
        "comment": c["comment"],
        "at": _iso(c.get("timestamp")),
    }


def _monitor_summary(m: dict) -> dict:
    # Never return type_data or monitor_settings_json: they hold target URLs,
    # request headers and other monitor secrets.
    return {
        "tag": m["tag"],
        "name": m["name"],
        "status": m.get("status"),
        "monitor_type": m.get("monitor_type"),
        "category": m.get("category_name"),
        "hidden": m.get("is_hidden") == "YES",
        "description": m.get("description"),
    }


IncidentId = Annotated[int, Field(description="Kener incident ID.", ge=1)]


def create_server(settings: Settings, keys: ApiKeyProvider | None = None) -> FastMCP:
    keys = keys or settings.key_provider()
    instructions = (
        f"Manage incidents on the Kener status page at {settings.url}. "
        "Updates are public: they appear on the status page and reach any integration "
        "that reads Kener."
    )
    if settings.instructions:
        instructions += "\n\n" + settings.instructions
    mcp = FastMCP(f"kener-{settings.name}" if settings.name else "kener", instructions=instructions)

    @asynccontextmanager
    async def kener() -> AsyncIterator[KenerClient]:
        try:
            api_key = await keys.get()
        except ApiKeyError as e:
            raise ToolError(f"[{settings.label}] {e}") from None
        client = KenerClient(settings.url, api_key)
        try:
            yield client
        except KenerError as e:
            if e.status_code == 401 and keys.uses_command:
                # The key may have been rotated. Run the command again next time.
                keys.invalidate()
            raise ToolError(f"[{settings.label}] {e}") from None
        finally:
            await client.aclose()

    @mcp.tool(annotations=READ_ONLY)
    async def list_monitors(
        include_hidden: Annotated[bool, Field(description="Include hidden monitors.")] = False,
    ) -> list[dict]:
        """List the monitors on this Kener instance (tag, name, status, type, category).

        Use the tags with create_incident.
        """
        async with kener() as k:
            monitors = await k.list_monitors()
        out = [_monitor_summary(m) for m in monitors]
        return out if include_hidden else [m for m in out if not m["hidden"]]

    @mcp.tool(annotations=READ_ONLY)
    async def list_incidents(
        include_resolved: Annotated[
            bool, Field(description="Also return RESOLVED incidents.")
        ] = False,
        limit: Annotated[int, Field(description="Maximum incidents to return.", ge=1)] = 20,
    ) -> list[dict]:
        """List incidents, newest first. By default only open (not RESOLVED) incidents."""
        async with kener() as k:
            incidents = await k.list_incidents()
        if not include_resolved:
            incidents = [i for i in incidents if i["state"] != "RESOLVED"]
        incidents.sort(key=lambda i: i.get("start_date_time") or 0, reverse=True)
        return [_incident_summary(i) for i in incidents[:limit]]

    @mcp.tool(annotations=READ_ONLY)
    async def get_incident(incident_id: IncidentId) -> dict:
        """Get one incident with its update timeline (oldest first)."""
        async with kener() as k:
            incident = await k.get_incident(incident_id)
            comments = await k.list_comments(incident_id)
        out = _incident_summary(incident)
        comments.sort(key=lambda c: (c.get("timestamp") or 0, c["id"]))
        out["updates"] = [_comment_summary(c) for c in comments]
        return out

    @mcp.tool(
        annotations=WRITE,
        description=(
            "Post a public update on an incident and set its state. The state becomes "
            "the incident's current state. To close an incident, use resolve_incident. "
            + NOTIFY_CAVEAT
        ),
    )
    async def post_incident_update(
        incident_id: IncidentId,
        comment: Annotated[
            str, Field(description="Update text shown on the status page.", min_length=1)
        ],
        state: Annotated[IncidentState, Field(description="New incident state.")],
    ) -> dict:
        async with kener() as k:
            created = await k.add_comment(incident_id, comment.strip(), state)
            incident = await k.get_incident(incident_id)
        return {"update": _comment_summary(created), "incident": _incident_summary(incident)}

    @mcp.tool(
        annotations=WRITE,
        description=(
            "Resolve an incident. Kener has no separate resolve call: this posts an "
            "update with state RESOLVED, which closes the incident and sets its end "
            "time. " + NOTIFY_CAVEAT
        ),
    )
    async def resolve_incident(
        incident_id: IncidentId,
        comment: Annotated[str, Field(description="Closing update text.", min_length=1)] = (
            "This incident has been resolved."
        ),
    ) -> dict:
        async with kener() as k:
            created = await k.add_comment(incident_id, comment.strip(), "RESOLVED")
            incident = await k.get_incident(incident_id)
        return {"update": _comment_summary(created), "incident": _incident_summary(incident)}

    @mcp.tool(
        annotations=WRITE,
        description=(
            "Create a public incident, starting now, in state INVESTIGATING. Optionally "
            "attach monitors (tags from list_monitors) and post a first update. " + NOTIFY_CAVEAT
        ),
    )
    async def create_incident(
        title: Annotated[str, Field(description="Incident title.", min_length=1)],
        monitor_tags: Annotated[
            list[str] | None, Field(description="Monitor tags the incident affects.")
        ] = None,
        impact: Annotated[Impact, Field(description="Impact on each listed monitor.")] = "DOWN",
        first_update: Annotated[
            str | None, Field(description="Optional first update text.")
        ] = None,
        state: Annotated[
            IncidentState, Field(description="State for the first update.")
        ] = "INVESTIGATING",
    ) -> dict:
        monitors = [{"monitor_tag": t, "impact": impact} for t in monitor_tags or []]
        async with kener() as k:
            incident = await k.create_incident(title.strip(), _now(), monitors)
            out: dict = {"incident": _incident_summary(incident)}
            if first_update and first_update.strip():
                created = await k.add_comment(incident["id"], first_update.strip(), state)
                out["update"] = _comment_summary(created)
                out["incident"] = _incident_summary(await k.get_incident(incident["id"]))
        return out

    return mcp


async def _check(settings: Settings) -> int:
    try:
        api_key = await settings.key_provider().get()
        async with KenerClient(settings.url, api_key) as k:
            monitors = await k.list_monitors()
            incidents = await k.list_incidents()
    except (ApiKeyError, KenerError) as e:
        print(f"{settings.label}: FAILED - {e}", file=sys.stderr)
        return 1
    open_count = sum(1 for i in incidents if i["state"] != "RESOLVED")
    print(f"{settings.label}: ok ({len(monitors)} monitors, {open_count} open incidents)")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kener-mcp",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", help="Kener base URL (overrides KENER_URL).")
    parser.add_argument(
        "--api-key-cmd", help="Command that prints the API key (overrides KENER_API_KEY_CMD)."
    )
    parser.add_argument("--name", help="Short instance name (overrides KENER_NAME).")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="Test the URL and API key, then exit.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env(url=args.url, api_key_cmd=args.api_key_cmd, name=args.name)
    except SettingsError as e:
        print(f"kener-mcp: {e}", file=sys.stderr)
        sys.exit(2)

    if args.command == "check":
        sys.exit(asyncio.run(_check(settings)))
    create_server(settings).run()


if __name__ == "__main__":
    main()
