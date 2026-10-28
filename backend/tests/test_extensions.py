"""
/pobierz skill|plugin (core/extensions.py): zrodla, skille z SKILL.md (sam tekst), uklad pluginow Claude Code
(.mcp.json, plugin.json, marketplace), sekrety jako ${MCP_*}, pominiecia z powodem, podglad, instalacja po TAK.
"""

import asyncio
import json
import shutil

import pytest

from backend.config import settings
from backend.core import extensions, memory, websearch
from backend.core.mcp import registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(websearch, "check_public", lambda url: None)
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    extensions._pending.clear()
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture
def plugin(tmp_path):
    root = tmp_path / "repo"
    write(root, "skills/deploy-app/SKILL.md", "---\nname: Deploy App\ndescription: >\n  Wdrozenie aplikacji\n  krok po kroku.\n---\n"
          "# Wdrozenie\n1. docker compose pull\n")
    write(root, "skills/deploy-app/scripts/run.sh", "echo hi")
    write(root, "skills/helper/SKILL.md", "---\nname: helper\ndescription: Uzywa skryptu\n---\nUruchom ${CLAUDE_PLUGIN_ROOT}/x.sh\n")
    write(root, "skills/huge/SKILL.md", "---\nname: huge\ndescription: za duzy\n---\n" + "x" * 30_000)
    write(root, ".mcp.json", json.dumps({"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}},
        "local": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"]},
        "docs": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${user_config.api_key}"}},
    }}))
    write(root, "hooks/hooks.json", "{}")
    return root


class TestSource:

    @pytest.mark.parametrize("text, expected", [
        ("anthropics/skills", ("https://github.com/anthropics/skills", "", "")),
        ("https://github.com/a/b.git", ("https://github.com/a/b", "", "")),
        ("https://github.com/a/b/tree/main/skills/pdf", ("https://github.com/a/b", "main", "skills/pdf")),
        ("https://github.com/a/b/blob/dev/x/SKILL.md", ("https://github.com/a/b", "dev", "x")),
        ("git@gitlab.com:team/skills.git", ("git@gitlab.com:team/skills.git", "", "")),
    ])
    def test_sources(self, text, expected):
        assert extensions.parse_source(text) == expected

    @pytest.mark.parametrize("bad", ["https://user:tok@github.com/a/b", "https://github.com/a/b/tree/main/../x", "file:///etc"])
    def test_refused(self, bad):
        with pytest.raises(extensions.ExtensionError):
            extensions.parse_source(bad)


class TestDiscover:

    def test_plugin_layout(self, plugin):
        found = extensions.discover(plugin, "", "plugin", "https://github.com/a/b")
        skills = {s.name: s for s in found.skills}
        assert set(skills) == {"deploy-app", "helper"}
        assert skills["deploy-app"].description == "Wdrozenie aplikacji krok po kroku." and skills["deploy-app"].extra_files == 1
        assert "Zrodlo: https://github.com/a/b (skills/deploy-app/SKILL.md)" in skills["deploy-app"].content
        assert "nie instaluje" in skills["helper"].content
        assert set(found.servers) == {"github", "docs"}
        assert found.servers["github"]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "${MCP_GITHUB_TOKEN}"}
        assert found.servers["docs"]["headers"] == {"Authorization": "Bearer ${MCP_API_KEY}"}
        assert found.secrets == ["MCP_GITHUB_TOKEN", "MCP_API_KEY"]
        skipped = " ".join(found.skipped)
        assert "huge" in skipped and "CLAUDE_PLUGIN_ROOT" in skipped and "hooks/" in skipped

    def test_skill_kind_ignores_servers_and_filter_narrows(self, plugin):
        found = extensions.discover(plugin, "", "skill", "s", only="deploy")
        assert [s.name for s in found.skills] == ["deploy-app"] and not found.servers
        assert any("/pobierz plugin" in s for s in found.skipped)

    def test_marketplace_and_inline_manifest_servers(self, tmp_path):
        root = tmp_path / "market"
        write(root, ".claude-plugin/marketplace.json", json.dumps({"plugins": [{"name": "ops", "source": "./plugins/ops"}]}))
        write(root, "plugins/ops/.claude-plugin/plugin.json", json.dumps({"name": "ops", "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}))
        write(root, "plugins/ops/skills/check/SKILL.md", "---\nname: check\ndescription: Sprawdza\n---\nkroki\n")
        write(root, "plugins/web/.claude-plugin/plugin.json", json.dumps({"name": "web"}))
        write(root, "plugins/web/skills/check/SKILL.md", "---\nname: check\ndescription: Inny\n---\nkroki\n")
        found = extensions.discover(root, "", "plugin", "m")
        assert [s.name for s in found.skills] == ["check", "web-check"] and found.servers["fetch"]["command"] == "uvx"

    def test_symlinks_cannot_pull_server_files_in(self, tmp_path):
        secret = tmp_path / "shadow"
        secret.write_text("---\nname: ukradziony\ndescription: x\n---\nroot:$6$hash\n")
        root = tmp_path / "evil"
        (root / "skills" / "x").mkdir(parents=True)
        (root / "skills" / "x" / "SKILL.md").symlink_to(secret)
        (root / ".mcp.json").symlink_to(tmp_path / "outside.json")
        (tmp_path / "outside.json").write_text(json.dumps({"mcpServers": {"a": {"command": "npx", "args": ["x"]}}}))
        found = extensions.discover(root, "", "plugin", "evil")
        assert not found.skills and not found.servers and "dowiazanie" in " ".join(found.skipped)

    def test_subdirectory_must_stay_inside(self, plugin, tmp_path):
        (tmp_path / "outside").mkdir()
        with pytest.raises(extensions.ExtensionError):
            extensions.discover(plugin, "../outside", "skill", "s")


class TestInstall:

    def test_preview_then_install_once(self, plugin):
        found = extensions.discover(plugin, "", "plugin", "https://github.com/a/b")
        memory.save_skill("helper", "stary", "tresc")
        found = extensions.discover(plugin, "", "plugin", "https://github.com/a/b")
        text = extensions.preview(found)
        assert "`deploy-app`" in text and "zastapi istniejacy" in text and "MCP_GITHUB_TOKEN=..." in text
        assert "zaufanego zrodla" in text
        token = extensions.remember(found)
        assert extensions.take(token) is found and extensions.take(token) is None
        summary, added = extensions.install(found)
        assert added == 2 and "/deploy_app" in summary and memory.read_skill("deploy-app")
        assert set(registry.load_config()) == {"github", "docs"} and registry.load_config()["github"]["source"]

    def test_observe_mode_installs_skills_but_no_servers(self, plugin):
        found = extensions.discover(plugin, "", "plugin", "s")
        assert "tryb obserwacji" in extensions.preview(found, servers_allowed=False)
        _, added = extensions.install(found, servers_allowed=False)
        assert added == 0 and not registry.load_config() and memory.read_skill("deploy-app")


class TestCommandAndAgent:

    @pytest.fixture
    def fake_fetch(self, plugin, tmp_path, monkeypatch):
        async def fetch(url, ref=""):
            copy = tmp_path / "clone"
            shutil.copytree(plugin, copy, dirs_exist_ok=True)
            return copy
        monkeypatch.setattr(extensions, "fetch", fetch)

    def test_server_command_preview_then_execute(self, fake_fetch, monkeypatch):
        import backend.server as server

        frames = []

        async def fake_send(writer, frame):
            frames.append(frame)
        monkeypatch.setattr(server, "_send", fake_send)

        async def scenario():
            await server._install(None, {"args": "plugin a/b"}, "cli:t", "viewer")
            viewer = frames[-1]
            await server._install(None, {"args": "skill a/b deploy"}, "cli:t", "admin")
            preview = frames[-1]
            assert not memory.list_skills()                                    # nic przed TAK
            await server._install(None, {"token": preview["data"]["token"], "execute": True}, "cli:t", "admin")
            return viewer, preview, frames[-1]

        viewer, preview, done = run(scenario())
        assert viewer["status"] == "error" and preview["data"]["skills"] == 1 and preview["data"]["servers"] == 0
        assert "deploy_app" in done["data"]["text"] and [s.name for s in memory.list_skills()] == ["deploy-app"]

    def test_agent_install_needs_one_yes(self, fake_fetch):
        from backend.core.agent import VPSAgent
        from backend.tests.fakes import FakeClient, assert_history_valid, completion

        agent = VPSAgent(client=FakeClient([
            completion(tool_calls=[("c1", "skill_manage", {"operation": "install", "source": "a/b", "name": "deploy"})]),
            completion("zainstalowano")]))

        async def talk():
            first = [e async for e in agent.chat("s", "pobierz skill deploy z a/b", "cli") if isinstance(e, str)]
            before = memory.list_skills()
            tainted = agent._sessions["s"].web_tainted
            [e async for e in agent.confirm("s", True)]
            return first, before, tainted

        first, before, tainted = run(talk())
        assert not before and tainted and "[POTWIERDZ] Instalacja" in first[0] and "deploy-app" in first[0]
        assert memory.read_skill("deploy-app") is not None
        assert_history_valid(agent._sessions["s"].messages)
