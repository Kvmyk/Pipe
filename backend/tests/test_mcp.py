"""
Testy MCP: Pipe jako serwer (dyspozytor obu er protokolu, narzedzia, zgody,
Streamable HTTP). Pipe jako klient MCP zostal usuniety w v0.35.
"""

import asyncio
import base64
import json

import pytest

from backend.config import settings
from backend.core import mcp
from backend.core.approvals import get_approvals
from backend.core.mcp import server as mcp_server
from backend.core.mcp.server import Caller

ADMIN = Caller("token:laptop", "admin")
VIEWER = Caller("viewer", "viewer")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPE_RUNTIME", "native")
    monkeypatch.setenv("HOST_ROOT", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "")
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    get_approvals()._items.clear()
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def rpc(method, params=None, *, id=1, modern=False):
    params = dict(params or {})
    if modern:
        params["_meta"] = {mcp.META_VERSION: mcp.MODERN_VERSION, mcp.META_CLIENT_INFO: {"name": "test", "version": "1"},
                           mcp.META_CLIENT_CAPABILITIES: {}}
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


# ─── Dyspozytor serwera ─────────────────────────────────────────────────────

class TestDispatcher:

    def test_legacy_initialize_and_listing(self):
        init = run(mcp_server.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}), ADMIN))
        assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "pipe"
        unknown = run(mcp_server.handle(rpc("initialize", {"protocolVersion": "2024-01-01"}), ADMIN))
        assert unknown["result"]["protocolVersion"] == "2025-11-25"
        assert run(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, ADMIN)) is None
        listing = run(mcp_server.handle(rpc("tools/list"), ADMIN))["result"]
        names = [t["name"] for t in listing["tools"]]
        assert names == sorted(names) and "run_command" in names and "resultType" not in listing

    def test_modern_discover_and_list(self):
        discover = run(mcp_server.handle(rpc("server/discover", modern=True), ADMIN))["result"]
        assert discover["supportedVersions"][0] == mcp.MODERN_VERSION and discover["resultType"] == "complete"
        assert discover["_meta"][mcp.META_SERVER_INFO]["name"] == "pipe" and discover["ttlMs"] > 0
        listing = run(mcp_server.handle(rpc("tools/list", modern=True), ADMIN))["result"]
        assert listing["cacheScope"] == "private" and listing["resultType"] == "complete"

    def test_errors(self):
        bad_version = rpc("tools/list")
        bad_version["params"]["_meta"] = {mcp.META_VERSION: "1900-01-01"}
        err = run(mcp_server.handle(bad_version, ADMIN))["error"]
        assert err["code"] == mcp.UNSUPPORTED_VERSION and err["data"]["requested"] == "1900-01-01"
        assert run(mcp_server.handle(rpc("resources/list"), ADMIN))["error"]["code"] == mcp.METHOD_NOT_FOUND
        assert run(mcp_server.handle(rpc("tools/call", {"name": "nie-ma"}), ADMIN))["error"]["code"] == mcp.INVALID_PARAMS
        assert run(mcp_server.handle([rpc("ping")], ADMIN))["error"]["code"] == mcp.INVALID_REQUEST
        assert run(mcp_server.handle(rpc("ping"), ADMIN))["result"] == {}


def call(name, arguments, caller=ADMIN):
    response = run(mcp_server.handle(rpc("tools/call", {"name": name, "arguments": arguments}, modern=True), caller))
    result = response["result"]
    return result["content"][0]["text"], result["isError"]


class TestServerTools:

    def test_safe_command_runs_and_forbidden_refused(self):
        text, is_error = call("run_command", {"command": "echo pipe-mcp"})
        assert "pipe-mcp" in text and not is_error
        text, is_error = call("run_command", {"command": "rm -rf /"})
        assert is_error and "zakazanych" in text

    def test_change_needs_approval_then_executes(self, isolated):
        from backend.core.watch import get_watcher

        target = isolated / "plik.txt"
        queue = get_watcher().notifier.subscribe()
        try:
            text, is_error = call("run_command", {"command": f"touch {target}", "reason": "test"})
            event = queue.get_nowait()
        finally:
            get_watcher().notifier.unsubscribe(queue)
        approval_id = event["id"]
        assert not is_error and approval_id in text and not target.exists()
        assert event["type"] == "approval" and event["requested_by"] == "token:laptop"
        assert "kopia przed zmiana" in event["plan"]
        status, _ = call("get_approval", {"id": approval_id})
        assert "[pending]" in status
        assert call("get_approval", {"id": approval_id}, Caller("token:obcy", "admin"))[1]   # cudza zgoda
        approval = run(get_approvals().decide(approval_id, True, "admin", cwd=str(isolated), sites_enabled=False))
        assert approval.status == "done" and target.exists() and "Dziennik zmian" in approval.result
        with pytest.raises(Exception):
            run(get_approvals().decide(approval_id, True, "admin"))

    def test_denied_and_viewer(self, isolated):
        text, _ = call("run_command", {"command": f"touch {isolated / 'x'}"})
        approval_id = text.split("id: ")[1].split(")")[0]
        assert run(get_approvals().decide(approval_id, False, "admin")).status == "denied"
        text, is_error = call("run_command", {"command": f"touch {isolated / 'y'}"}, VIEWER)
        assert is_error and "viewer" in text and not get_approvals().pending()

    def test_read_file(self, isolated):
        normal = isolated / "app.conf"
        normal.write_text("port=8080\napi_key=supertajnyklucz123\n")
        text, is_error = call("read_file", {"path": str(normal)})
        assert not is_error and "port=8080" in text and "supertajnyklucz123" not in text
        (isolated / ".env").write_text("X=1")
        text, is_error = call("read_file", {"path": str(isolated / ".env")})
        assert is_error and "sekretami" in text
        assert call("read_file", {"path": "wzgledna"})[1]

    def test_ask_pipe_runs_read_only(self, monkeypatch):
        from backend.core import agent as agent_module

        seen = {}

        class FakeAgent:
            async def chat(self, session_id, message, interface, **kwargs):
                seen.update(kwargs)
                yield "Dysk ma 40% wolnego."

        monkeypatch.setattr(agent_module, "get_agent", lambda: FakeAgent())
        text, _ = call("ask_pipe", {"question": "ile miejsca?"})
        assert text == "Dysk ma 40% wolnego." and seen["role"] == "viewer"


# ─── Streamable HTTP ────────────────────────────────────────────────────────

async def http(port, method="POST", body=None, headers=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    head = f"{method} /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(payload)}\r\n"
    for name, value in (headers or {}).items():
        head += f"{name}: {value}\r\n"
    writer.write(head.encode() + b"\r\n" + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    status = int(raw.split(b" ", 2)[1])
    rest = raw.split(b"\r\n\r\n", 1)[1]
    return status, json.loads(rest) if rest else None


def modern_headers(method, name=None):
    headers = {"MCP-Protocol-Version": mcp.MODERN_VERSION, "Mcp-Method": method,
               "Accept": "application/json, text/event-stream"}
    if name:
        headers["Mcp-Name"] = name
    return headers


class TestHttpTransport:

    def test_requests_and_validation(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "tok")

        async def scenario():
            server = await asyncio.start_server(mcp_server.McpHttpServer().handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            auth = {"Authorization": "Bearer tok"}
            call_body = rpc("tools/call", {"name": "server_status", "arguments": {}}, modern=True)
            results = {
                "ok": await http(port, body=rpc("tools/list", modern=True), headers={**auth, **modern_headers("tools/list")}),
                "noauth": await http(port, body=rpc("tools/list", modern=True), headers=modern_headers("tools/list")),
                "mismatch": await http(port, body=rpc("tools/list", modern=True), headers={**auth, **modern_headers("tools/call")}),
                "name": await http(port, body=call_body, headers={**auth, **modern_headers("tools/call", "inna")}),
                "b64name": await http(port, body=call_body, headers={
                    **auth, **modern_headers("tools/call", "=?base64?" + base64.b64encode(b"=?base64?x?=").decode() + "?=")}),
                "origin": await http(port, body=rpc("tools/list", modern=True),
                                     headers={**auth, **modern_headers("tools/list"), "Origin": "https://evil.example"}),
                "get": await http(port, method="GET", headers=auth),
                "legacy": await http(port, body=rpc("initialize", {"protocolVersion": "2025-11-25"}), headers=auth),
                "notify": await http(port, body={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                     headers={**auth, "MCP-Protocol-Version": "2025-11-25"}),
                "unknown": await http(port, body=rpc("prompts/list", modern=True),
                                      headers={**auth, **modern_headers("prompts/list")}),
            }
            server.close()
            await server.wait_closed()
            return results

        r = run(scenario())
        assert r["ok"][0] == 200 and r["ok"][1]["result"]["tools"]
        assert r["noauth"][0] == 401
        assert r["mismatch"][0] == 400 and r["mismatch"][1]["error"]["code"] == mcp.HEADER_MISMATCH
        assert r["name"][0] == 400 and r["b64name"][0] == 400
        assert r["origin"][0] == 403 and r["get"][0] == 405
        assert r["legacy"][0] == 200 and r["legacy"][1]["result"]["protocolVersion"] == "2025-11-25"
        assert r["notify"] == (202, None)
        assert r["unknown"][0] == 404

    def test_origin_rules(self):
        assert mcp_server.origin_allowed("") and mcp_server.origin_allowed("http://localhost:3000")
        assert not mcp_server.origin_allowed("http://localhost.evil.com")
        assert mcp_server.origin_allowed("https://app.example", {"https://app.example"})




# ─── Protokol Pipe i klienci ────────────────────────────────────────────────

class TestServerCommands:

    def test_bridge_and_approvals(self, monkeypatch, isolated):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent, exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "admintok")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewtok")
        monkeypatch.setattr(server, "get_agent", lambda: FakeAgent())
        target = isolated / "z-mostu"
        [[listing], [created]] = exchange([
            {"command": "mcp", "rpc": rpc("tools/list"), "token": "admintok"},
            {"command": "mcp", "rpc": rpc("tools/call", {"name": "run_command", "arguments": {"command": f"touch {target}"}}),
             "token": "admintok"},
        ])
        assert listing["data"]["rpc"]["result"]["tools"]
        approval_id = get_approvals().pending()[0].id
        assert approval_id in created["data"]["rpc"]["result"]["content"][0]["text"]
        [[pending], [viewer], [done]] = exchange([
            {"command": "approvals", "token": "admintok"},
            {"command": "approve", "id": approval_id, "decision": True, "token": "viewtok", "session_id": "v"},
            {"command": "approve", "id": approval_id, "decision": True, "token": "admintok"},
        ])
        assert pending["data"]["pending"][0]["id"] == approval_id
        assert viewer["status"] == "error" and done["data"]["status"] == "done" and target.exists()
        [[notification]] = exchange([{"command": "mcp", "rpc": {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                      "token": "admintok"}])
        assert notification["data"]["rpc"] is None


def test_telegram_approval_format():
    import sys as _sys
    from pathlib import Path

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))
    from tg_format import BUILTIN_COMMANDS, format_approval

    text = format_approval({"id": "ab12cd34", "requested_by": "token:<laptop>", "target": "local",
                            "command": "rm -f /tmp/<x>", "reason": "sprzatanie", "plan": "Bezpiecznik:\n- kopia"})
    assert "token:&lt;laptop&gt;" in text and "<pre>rm -f /tmp/&lt;x&gt;</pre>" in text and "wygasa" in text
    assert {"zgody", "mcp"} <= {c for c, _ in BUILTIN_COMMANDS}          # /mcp wrocilo w v0.42 (klient w izolacji)
