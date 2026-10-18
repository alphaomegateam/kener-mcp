import json
import shlex
import sys

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from kener_mcp.auth import ApiKeyError, ApiKeyProvider
from kener_mcp.server import Settings, SettingsError, create_server

BASE = "https://status.example.com"
API = f"{BASE}/api/v4"
KEY = "test-key-123"


def incident(id=7, state="INVESTIGATING", start=1_760_000_000, end=None, **kw):
    return {
        "id": id,
        "title": f"Incident {id}",
        "start_date_time": start,
        "end_date_time": end,
        "state": state,
        "incident_source": "API",
        "monitors": [],
        "created_at": "2026-10-18T00:00:00.000Z",
        "updated_at": "2026-10-18T00:00:00.000Z",
        "url": f"{BASE}/incidents/{id}",
        **kw,
    }


def comment(id=1, incident_id=7, text="Looking into it", state="INVESTIGATING", ts=1_760_000_060):
    return {
        "id": id,
        "incident_id": incident_id,
        "comment": text,
        "timestamp": ts,
        "state": state,
        "created_at": "2026-10-18T00:00:00.000Z",
        "updated_at": "2026-10-18T00:00:00.000Z",
    }


@pytest.fixture
def server():
    return create_server(Settings(url=BASE, api_key=KEY, name="test"))


@pytest.fixture
def api():
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        yield mock


async def call(server, tool, args=None):
    async with Client(server) as client:
        return (await client.call_tool(tool, args or {})).structured_content


def sent_json(route):
    return json.loads(route.calls.last.request.content)


# --- settings -------------------------------------------------------------


def test_settings_from_env():
    s = Settings.from_env(
        {"KENER_URL": "https://x.example.com/", "KENER_API_KEY": "k", "KENER_NAME": "cp"}
    )
    assert s.url == "https://x.example.com"
    assert s.name == "cp"


U = "https://x.example.com"


@pytest.mark.parametrize(
    "env, msg",
    [
        ({"KENER_API_KEY": "k"}, "KENER_URL is not set"),
        ({"KENER_URL": "x.example.com", "KENER_API_KEY": "k"}, "must be an https"),
        ({"KENER_URL": "https://u:p@x.example.com", "KENER_API_KEY": "k"}, "user name"),
        ({"KENER_URL": U}, "exactly one"),
        ({"KENER_URL": U, "KENER_API_KEY": "k", "KENER_API_KEY_CMD": "echo k"}, "exactly one"),
        (
            {"KENER_URL": U, "KENER_API_KEY_CMD": "echo k", "KENER_API_KEY_CMD_TIMEOUT": "x"},
            "must be a number",
        ),
    ],
)
def test_settings_errors(env, msg):
    with pytest.raises(SettingsError, match=msg):
        Settings.from_env(env)


def test_flags_override_env():
    s = Settings.from_env(
        {"KENER_URL": U, "KENER_API_KEY": "k"},
        url="https://y.example.com",
        api_key_cmd="echo k2",
        name="flag",
    )
    assert (s.url, s.api_key, s.api_key_cmd, s.name) == (
        "https://y.example.com",
        None,
        "echo k2",
        "flag",
    )


# --- API key command --------------------------------------------------------


def py_cmd(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


async def test_key_cmd_runs_once_and_caches(tmp_path):
    counter = tmp_path / "n"
    code = f"import pathlib; p=pathlib.Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('cmd-key')"  # noqa: E501
    keys = ApiKeyProvider(api_key_cmd=py_cmd(code))
    assert await keys.get() == "cmd-key"
    assert await keys.get() == "cmd-key"
    assert counter.read_text() == "x"
    keys.invalidate()
    await keys.get()
    assert counter.read_text() == "xx"


@pytest.mark.parametrize(
    "code, msg",
    [
        ("import sys; sys.stderr.write('locked'); sys.exit(3)", "exit code 3: locked"),
        ("print('')", "printed nothing"),
        ("print('a b')", "more than one value"),
    ],
)
async def test_key_cmd_errors(code, msg):
    with pytest.raises(ApiKeyError, match=msg):
        await ApiKeyProvider(api_key_cmd=py_cmd(code)).get()


async def test_key_cmd_failure_does_not_leak_stdout():
    code = "import sys; print('SECRET-PART'); sys.exit(1)"
    with pytest.raises(ApiKeyError) as exc:
        await ApiKeyProvider(api_key_cmd=py_cmd(code)).get()
    assert "SECRET-PART" not in str(exc.value)


async def test_key_cmd_timeout():
    keys = ApiKeyProvider(api_key_cmd=py_cmd("import time; time.sleep(5)"), timeout=0.3)
    with pytest.raises(ApiKeyError, match="did not finish"):
        await keys.get()


async def test_key_cmd_missing_binary():
    with pytest.raises(ApiKeyError, match="Could not run"):
        await ApiKeyProvider(api_key_cmd="no-such-binary-kener-mcp").get()


async def test_server_uses_cmd_key_and_reruns_after_401(api, tmp_path):
    counter = tmp_path / "n"
    code = f"import pathlib; p=pathlib.Path({str(counter)!r}); n=len(p.read_text()) if p.exists() else 0; p.write_text('x'*(n+1)); print(f'key{{n+1}}')"  # noqa: E501
    server = create_server(Settings(url=BASE, api_key_cmd=py_cmd(code), name="test"))
    route = api.get("/monitors")
    route.side_effect = [
        httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "Invalid API key"}}),
        httpx.Response(200, json={"monitors": []}),
    ]
    with pytest.raises(ToolError, match="Invalid API key"):
        await call(server, "list_monitors")
    await call(server, "list_monitors")
    auths = [c.request.headers["authorization"] for c in route.calls]
    assert auths == ["Bearer key1", "Bearer key2"]


# --- request shapes ---------------------------------------------------------


async def test_bearer_auth_and_user_agent(server, api):
    route = api.get("/monitors").respond(json={"monitors": []})
    await call(server, "list_monitors")
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["user-agent"].startswith("kener-mcp/")


async def test_list_monitors_hides_type_data_and_hidden(server, api):
    api.get("/monitors").respond(
        json={
            "monitors": [
                {
                    "tag": "web",
                    "name": "Website",
                    "status": "ACTIVE",
                    "monitor_type": "API",
                    "category_name": "Home",
                    "is_hidden": "NO",
                    "description": None,
                    "type_data": {"url": "https://secret", "headers": [{"key": "X-Token"}]},
                    "monitor_settings_json": {"x": 1},
                },
                {"tag": "h", "name": "Hidden", "is_hidden": "YES"},
            ]
        }
    )
    out = await call(server, "list_monitors")
    assert [m["tag"] for m in out["result"]] == ["web"]
    assert "type_data" not in json.dumps(out)
    out = await call(server, "list_monitors", {"include_hidden": True})
    assert [m["tag"] for m in out["result"]] == ["web", "h"]


async def test_list_incidents_open_only_newest_first(server, api):
    api.get("/incidents").respond(
        json={
            "incidents": [
                incident(1, start=100),
                incident(2, state="RESOLVED", start=200, end=300),
                incident(3, state="MONITORING", start=400),
            ]
        }
    )
    out = (await call(server, "list_incidents"))["result"]
    assert [i["id"] for i in out] == [3, 1]
    out = (await call(server, "list_incidents", {"include_resolved": True, "limit": 2}))["result"]
    assert [i["id"] for i in out] == [3, 2]


async def test_get_incident_includes_timeline(server, api):
    api.get("/incidents/7").respond(json={"incident": incident(7)})
    api.get("/incidents/7/comments").respond(
        json={"comments": [comment(2, ts=200), comment(1, ts=100)]}
    )
    out = await call(server, "get_incident", {"incident_id": 7})
    assert [u["id"] for u in out["updates"]] == [1, 2]
    assert out["started_at"].startswith("2025-")


async def test_post_update_uses_comment_and_state_fields(server, api):
    route = api.post("/incidents/7/comments").respond(
        201, json={"comment": comment(5, text="Found it", state="IDENTIFIED")}
    )
    api.get("/incidents/7").respond(json={"incident": incident(7, state="IDENTIFIED")})
    out = await call(
        server,
        "post_incident_update",
        {"incident_id": 7, "comment": "  Found it  ", "state": "IDENTIFIED"},
    )
    # Kener's docs say "message"/"status"; the 4.1.7 server reads "comment"/"state".
    assert sent_json(route) == {"comment": "Found it", "state": "IDENTIFIED"}
    assert out["incident"]["state"] == "IDENTIFIED"


async def test_post_update_rejects_bad_state(server, api):
    with pytest.raises(ToolError):
        await call(
            server, "post_incident_update", {"incident_id": 7, "comment": "x", "state": "FIXED"}
        )


async def test_resolve_posts_resolved_comment(server, api):
    route = api.post("/incidents/7/comments").respond(
        201, json={"comment": comment(6, state="RESOLVED")}
    )
    api.get("/incidents/7").respond(
        json={"incident": incident(7, state="RESOLVED", end=1_760_000_600)}
    )
    out = await call(server, "resolve_incident", {"incident_id": 7})
    assert sent_json(route) == {"comment": "This incident has been resolved.", "state": "RESOLVED"}
    assert out["incident"]["ended_at"] is not None


async def test_create_incident_with_monitors_and_first_update(server, api):
    create = api.post("/incidents").respond(201, json={"incident": incident(9)})
    add = api.post("/incidents/9/comments").respond(201, json={"comment": comment(1, 9)})
    api.get("/incidents/9").respond(json={"incident": incident(9)})
    out = await call(
        server,
        "create_incident",
        {
            "title": "API down",
            "monitor_tags": ["api", "web"],
            "impact": "DEGRADED",
            "first_update": "Looking into it",
        },
    )
    body = sent_json(create)
    assert body["title"] == "API down"
    assert isinstance(body["start_date_time"], int)
    assert body["monitors"] == [
        {"monitor_tag": "api", "impact": "DEGRADED"},
        {"monitor_tag": "web", "impact": "DEGRADED"},
    ]
    assert sent_json(add) == {"comment": "Looking into it", "state": "INVESTIGATING"}
    assert out["incident"]["id"] == 9 and out["update"]["id"] == 1


async def test_create_incident_minimal_sends_no_monitors_or_comment(server, api):
    create = api.post("/incidents").respond(201, json={"incident": incident(9)})
    add = api.post("/incidents/9/comments")
    await call(server, "create_incident", {"title": "Planned work"})
    assert "monitors" not in sent_json(create)
    assert not add.called


# --- errors -----------------------------------------------------------------


async def test_kener_error_message_is_surfaced(server, api):
    api.get("/incidents/99").respond(
        404, json={"error": {"code": "NOT_FOUND", "message": "Incident with id '99' not found"}}
    )
    api.get("/incidents/99/comments").respond(404, json={})
    with pytest.raises(ToolError, match=r"\[test\] Kener returned 404: Incident with id '99'"):
        await call(server, "get_incident", {"incident_id": 99})


async def test_network_error_does_not_leak_key(server, api):
    api.get("/monitors").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ToolError) as exc:
        await call(server, "list_monitors")
    assert KEY not in str(exc.value)
    assert "ConnectError" in str(exc.value)


async def test_write_tools_are_marked_not_read_only(server):
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["list_incidents"].annotations.read_only_hint is True
    assert tools["post_incident_update"].annotations.read_only_hint is False
    assert "incident_id" in tools["post_incident_update"].input_schema["required"]


async def test_server_reports_own_version(server):
    from kener_mcp import __version__

    async with Client(server) as client:
        info = client.server_info
    assert (info.name, info.version) == ("kener-test", __version__)
