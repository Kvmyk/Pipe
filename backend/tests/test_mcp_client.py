"""
Pipe jako klient MCP (v0.42): protokol (stdio obu er, HTTP), izolacja serwera stdio w kontenerze, sekrety tylko
jako ${MCP_*}, polityka zgod (odczyt bez pytania, reszta z TAK), wynik jako tresc z zewnatrz (bez YOLO),
narzedzia w grupach ladowanych na zadanie, /mcp w klientach.
"""

import asyncio
import json
import sys
import textwrap

import pytest

from backend.config import settings
from backend.core import toolsets
from backend.core.mcp import client as mcp_client
from backend.core.mcp import registry, server as mcp_server

FAKE_SERVER = textwrap.dedent('''
    import json, os, sys
    MODERN = sys.argv[1] == "modern"
    TOOLS = [
        {"name": "echo", "description": "Zwraca tekst", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "env", "description": "Zmienne srodowiskowe", "annotations": {"readOnlyHint": True},
         "inputSchema": {"type": "object"}},
        {"name": "delete_everything", "description": "Niszczy", "inputSchema": {"type": "object"}},
    ]
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        method, params = m["method"], m.get("params") or {}
        if method == "server/discover" and MODERN:
            res = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}},
                   "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "fake-modern", "version": "2"}}}
        elif method == "initialize" and not MODERN:
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-legacy", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = params["name"], params.get("arguments") or {}
            text = args.get("text", "") if name == "echo" else ",".join(sorted(os.environ)) if name == "env" else "zniszczone"
            res = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            res = None
        out = {"jsonrpc": "2.0", "id": m["id"], **({"result": res} if res is not None else
                                                  {"error": {"code": -32601, "message": "nieznana metoda"}})}
        print(json.dumps(out), flush=True)
''')


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
    manager = registry.get_manager()
    manager.servers.clear()
    manager.functions.clear()
    yield tmp_path
    manager.servers.clear()
    manager.functions.clear()


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """Serwer stdio uruchamiany wprost (w testach bez Dockera) — zamiast kontenera z registry.launch()."""
    path = tmp_path / "fake_mcp.py"
    path.write_text(FAKE_SERVER)

    def direct(name, cfg):
        return sys.executable, [str(path), cfg["args"][-1]], registry.resolve(cfg.get("env") or {}), ""
    monkeypatch.setattr(registry, "launch", direct)

    async def present(image):
        return None
    monkeypatch.setattr(registry, "ensure_image", present)
    return path


def run(coro):
    return asyncio.run(coro)


# ─── Protokol ───────────────────────────────────────────────────────────────

class TestClient:

    @pytest.mark.parametrize("era", ["legacy", "modern"])
    def test_stdio_era_detection_and_calls(self, fake_server, era, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-tajnyklucz")

        async def scenario():
            client = mcp_client.McpClient(mcp_client.StdioTransport(sys.executable, [str(fake_server), era],
                                                                    env={"MOJA": "1"}))
            await client.connect()
            try:
                return client.era, client.server_info, [t["name"] for t in client.tools], \
                    await client.call_tool("echo", {"text": "czesc"}), await client.call_tool("env", {})
            finally:
                await client.close()

        detected, info, tools, echo, env = run(scenario())
        assert detected == era and info["name"] == f"fake-{era}" and tools == ["echo", "env", "delete_everything"]
        assert mcp_client.result_text(echo) == ("czesc", [], False)
        names = mcp_client.result_text(env)[0].split(",")
        assert "MOJA" in names and "LLM_API_KEY" not in names           # klucze Pipe nie trafiaja do serwera MCP

    def test_http_client_against_pipe_server(self):
        async def scenario():
            server = await asyncio.start_server(mcp_server.McpHttpServer().handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            client = mcp_client.McpClient(mcp_client.HttpTransport(f"http://127.0.0.1:{port}/mcp"))
            await client.connect()
            try:
                return client.era, await client.call_tool("run_command", {"command": "echo przez-http"})
            finally:
                await client.close()
                server.close()
                await server.wait_closed()

        era, result = run(scenario())
        assert era == "modern" and "przez-http" in mcp_client.result_text(result)[0]


# ─── Konfiguracja i izolacja ────────────────────────────────────────────────

class TestRegistry:

    def test_only_npx_uvx_and_docker_images(self):
        assert registry.validate_server("gh", {"command": "npx -y @mcp/server-github"})["args"] == ["-y", "@mcp/server-github"]
        assert registry.validate_server("f", {"command": "/usr/bin/uvx", "args": ["mcp-server-fetch"]})["command"] == "uvx"
        for bad in ({"command": "python3", "args": ["-m", "x"]}, {"command": "bash", "args": ["-c", "x"]}, {},
                    {"url": "ftp://x"}):
            with pytest.raises(registry.McpConfigError):
                registry.validate_server("x", bad)
        with pytest.raises(registry.McpConfigError):
            registry.validate_server("Zla Nazwa", {"command": "npx"})

    def test_docker_run_from_other_apps_keeps_only_harmless_flags(self):
        cfg = registry.validate_server("gh", {"command": "docker", "args": [
            "run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server", "stdio"]})
        assert cfg["image"] == "ghcr.io/github/github-mcp-server" and cfg["args"] == ["stdio"]
        assert cfg["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "${MCP_GITHUB_PERSONAL_ACCESS_TOKEN}"}
        for flag in (["-v", "/:/host"], ["--network", "host"], ["--privileged"]):
            with pytest.raises(registry.McpConfigError):
                registry.validate_server("x", {"command": "docker", "args": ["run", "-i", *flag, "img"]})

    def test_secrets_only_through_mcp_variables(self, monkeypatch):
        ok = registry.validate_server("gh", {"command": "npx", "args": ["x"], "env": {"T": "${MCP_GH}", "LOG": "info"}})
        assert ok["env"]["T"] == "${MCP_GH}"
        with pytest.raises(registry.McpConfigError, match="MCP_"):
            registry.validate_server("gh", {"command": "npx", "env": {"T": "${LLM_API_KEY}"}})
        with pytest.raises(registry.McpConfigError, match="sekret"):
            registry.validate_server("gh", {"command": "npx", "env": {"T": "ghp_" + "a" * 36}})
        monkeypatch.setenv("MCP_GH", "ghp_prawdziwy")
        assert registry.resolve({"T": "${MCP_GH}"}) == {"T": "ghp_prawdziwy"}
        monkeypatch.delenv("MCP_GH")
        with pytest.raises(registry.McpConfigError, match="MCP_GH"):
            registry.resolve({"T": "${MCP_GH}"})

    def test_container_is_isolated_and_secret_values_stay_out_of_argv(self, monkeypatch):
        monkeypatch.setenv("MCP_GH", "ghp_prawdziwy")
        program, argv, env, container = registry.launch("gh", registry.validate_server(
            "gh", {"command": "npx", "args": ["-y", "@mcp/server-github"], "env": {"TOKEN": "${MCP_GH}"}}))
        joined = " ".join(argv)
        assert program == "docker" and argv[:2] == ["run", "-i"] and container.startswith("pipe-mcp-gh-")
        for flag in ("--cap-drop ALL", "no-new-privileges", "--user 65534:65534", "--network bridge", "-e TOKEN"):
            assert flag in joined
        assert "-v" not in argv and "docker.sock" not in joined and "--privileged" not in joined
        assert "ghp_prawdziwy" not in joined and env == {"TOKEN": "ghp_prawdziwy"}
        assert argv[-4:] == [registry.NODE_IMAGE, "npx", "-y", "@mcp/server-github"]

    def test_storage_and_command_line(self):
        assert registry.add_server("gh", {"command": "npx", "args": ["x"]})
        assert oct(registry.config_path().stat().st_mode & 0o777) == "0o600"
        name, cfg = registry.parse_line("github npx -y @modelcontextprotocol/server-github "
                                        "GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN}")
        assert name == "github" and cfg["command"] == "npx" and cfg["env"] == {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "${MCP_GITHUB_TOKEN}"}
        assert registry.parse_line("docs https://mcp.example.com/mcp TOKEN=${MCP_DOCS}")[1] == {
            "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${MCP_DOCS}"}}
        text = registry.describe_server("github", registry.validate_server(name, cfg))
        assert "kontener" in text and "${MCP_GITHUB_TOKEN}" in text
        assert registry.remove_server("gh") and not registry.load_config()

    def test_policy(self):
        read = {"name": "get_issue", "annotations": {"readOnlyHint": True}}
        write = {"name": "create_issue", "annotations": {}}
        assert not registry.needs_confirmation({}, read) and registry.needs_confirmation({}, write)
        assert registry.needs_confirmation({"askAlways": True}, read)
        assert registry.server_of("mcp__github__list_issues") == "github" and registry.server_of("execute_command") == ""


# ─── Agent ──────────────────────────────────────────────────────────────────

def agent_with(fake_server, calls, *, era="legacy"):
    from backend.core.agent import VPSAgent
    from backend.tests.fakes import FakeClient

    registry.add_server("fake", {"command": "npx", "args": [era]})
    return VPSAgent(client=FakeClient(calls))


class TestAgent:

    def test_tools_load_on_demand_reads_run_and_writes_ask(self, fake_server, monkeypatch):
        from backend.tests.fakes import assert_history_valid, completion

        monkeypatch.setattr(settings, "TOOLS_MODE", "lazy")
        agent = agent_with(fake_server, [
            completion("bez mcp"),
            completion(tool_calls=[("c1", "mcp__fake__echo", {"text": "hej"}), ("c2", "mcp__fake__delete_everything", {})]),
            completion("czekam"), completion("gotowe")])

        async def talk():
            await registry.get_manager().reload()
            try:
                plain = [e async for e in agent.chat("s", "ile mam miejsca na dysku?", "cli")]
                first = [e async for e in agent.chat("s", "uzyj fake do echo", "cli")]
                tainted = agent._sessions["s"].web_tainted
                confirmed = [e async for e in agent.confirm("s", True)]
                return plain, first, tainted, confirmed
            finally:
                await registry.get_manager().close()

        _, first, tainted, _ = run(talk())
        offered = [[t["function"]["name"] for t in call["tools"]] for call in agent._client.calls]
        assert not any(n.startswith("mcp__") for n in offered[0])              # zwykla rozmowa — zero tokenow MCP
        assert {"mcp__fake__echo", "mcp__fake__delete_everything"} <= set(offered[1])   # nazwa serwera wlacza grupe
        messages = agent._sessions["s"].messages
        results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
        assert results["c1"] == "hej" and tainted                                 # odczyt bez pytania, tresc z zewnatrz
        assert any("[POTWIERDZ] Narzedzie MCP" in e and "fake.delete_everything" in e for e in first if isinstance(e, str))
        assert "zniszczone" in results["c2"]
        assert_history_valid(messages)

    def test_secret_argument_and_observe_mode_are_refused(self, fake_server, monkeypatch):
        from backend.tests.fakes import completion

        agent = agent_with(fake_server, [
            completion(tool_calls=[("c1", "mcp__fake__echo", {"text": "AKIAIOSFODNN7EXAMPLE"})]), completion("ok"),
            completion(tool_calls=[("c2", "mcp__fake__delete_everything", {})]), completion("ok")])
        monkeypatch.setenv("PIPE_OBSERVE", "1")

        async def talk():
            await registry.get_manager().reload()
            try:
                [e async for e in agent.chat("s", "fake echo", "cli")]
                [e async for e in agent.chat("s", "fake usun", "cli")]
            finally:
                await registry.get_manager().close()

        run(talk())
        results = {m["tool_call_id"]: m["content"] for m in agent._sessions["s"].messages if m.get("role") == "tool"}
        assert results["c1"].startswith("ODMOWA") and "sekret" in results["c1"]
        assert results["c2"].startswith("ODMOWA") and agent._sessions["s"].pending_confirmation is None

    def test_adding_a_server_needs_yes_and_validates(self, fake_server):
        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient, completion

        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "mcp_manage", {"operation": "add", "name": "fake", "command": "npx",
                                                         "args": ["modern"]})]), completion("dodano"),
            completion(tool_calls=[("c2", "mcp_manage", {"operation": "add", "name": "zly", "command": "bash",
                                                         "args": ["-c", "id"]})]), completion("nie")]))

        async def talk():
            first = [e async for e in agent.chat("s", "dodaj serwer mcp", "cli")]
            before = registry.load_config()
            [e async for e in agent.confirm("s", True)]
            state = registry.get_manager().servers.get("fake")
            [e async for e in agent.chat("s", "dodaj mcp bash", "cli")]
            await registry.get_manager().close()
            return first, before, state

        first, before, state = run(talk())
        assert before == {} and "[POTWIERDZ] Nowy serwer MCP" in first[0] and "kontenerze" in first[0]
        assert state.status == "connected" and state.client.era == "modern"
        results = {m["tool_call_id"]: m["content"] for m in agent._sessions["s"].messages if m.get("role") == "tool"}
        assert "npx, uvx" in results["c2"] and "zly" not in registry.load_config()

    def test_prompt_lists_servers_and_load_tools_accepts_them(self, fake_server, monkeypatch):
        from backend.core.session import Session

        async def scenario():
            registry.add_server("fake", {"command": "npx", "args": ["legacy"], "description": "testowy"})
            await registry.get_manager().reload()
            try:
                session = Session(session_id="s", interface="cli")
                prompt = session.system_prompt
                toolsets.activate(session, ["mcp:fake"])
                return prompt, toolsets.select(registry.get_manager().tool_schemas(), session, lazy=True)
            finally:
                await registry.get_manager().close()

        prompt, selected = run(scenario())
        assert "mcp:fake — 3 narzedzi (testowy)" in prompt and len(selected) == 3


# ─── /mcp w klientach ───────────────────────────────────────────────────────

class TestCommand:

    def test_add_list_remove_admin_only(self, fake_server, monkeypatch):
        import backend.server as server

        frames = []

        async def fake_send(writer, frame):
            frames.append(frame)
        monkeypatch.setattr(server, "_send", fake_send)

        async def call(args, role="admin"):
            await server._mcp_servers(None, {"args": args}, role)
            return frames[-1]

        async def scenario():                   # jedna petla — polaczenie z serwerem MCP zyje w petli, w ktorej powstalo
            try:
                return [await call("dodaj fake npx legacy", role="viewer"), await call("dodaj fake npx legacy"),
                        await call("dodaj zly bash -c id"), await call(""), await call("usun fake")]
            finally:
                await registry.get_manager().close()

        viewer, added, bad, listed, removed = run(scenario())
        assert viewer["status"] == "error" and bad["status"] == "error"
        assert "Dodano serwer MCP fake [connected]" in added["data"]["text"]
        assert added["data"]["servers"][0]["status"] == "connected" and "fake:" in listed["data"]["text"]
        assert "Usunieto" in removed["data"]["text"] and not registry.load_config()
