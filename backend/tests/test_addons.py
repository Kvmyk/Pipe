"""
Katalog gotowych dodatkow MCP (backend/addons, core/addons.py), sekrety wpisywane w oknie /mcp, wolumen /state,
komenda `mcp_servers` dla okna i narzedzie agenta `mcp_manage` z katalogiem.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import backend.server as server
from backend.config import settings
from backend.core import addons
from backend.core.mcp import registry

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "telegram"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PIPE_RUNTIME", "native")
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "")
    for name in [k for k in os.environ if k.startswith("MCP_")]:
        monkeypatch.delenv(name)
    from backend.core import audit
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    manager = registry.get_manager()
    manager.servers.clear()
    manager.functions.clear()

    async def no_reload():
        return None
    monkeypatch.setattr(manager, "reload", no_reload)        # bez Dockera: stan polaczen nie jest tu testowany
    yield tmp_path


def good(**changes):
    raw = json.loads((addons.ADDONS_DIR / "github.json").read_text(encoding="utf-8"))
    raw.update(changes)
    return raw


class TestCatalog:

    def test_every_file_is_valid_and_complete(self):
        found = addons.catalog()
        names = {a.name for a in found}
        assert {"github", "google", "outlook", "microsoft365"} <= names
        assert len(found) == len(list(addons.ADDONS_DIR.glob("*.json")))      # zaden plik nie zostal pominiety
        for addon in found:
            assert addon.title["pl"] and addon.title["en"] and addon.description["pl"] and addon.description["en"]
            registry.validate_server(addon.name, addon.server)
            for arg in addon.server.get("args", []):
                if arg.startswith(("@", "google-")) and not arg.startswith("--"):
                    assert "@" in arg.lstrip("@"), f"{addon.name}: przypnij wersje paczki ({arg})"

    def test_office_first_then_dev(self):
        assert [a.category for a in addons.catalog()][-1] in ("dev", "other")
        assert addons.catalog()[0].category == "office"

    @pytest.mark.parametrize("change,message", [
        ({"title": {"pl": "GitHub"}}, "title"),
        ({"secrets": []}, "MCP_GITHUB_TOKEN"),
        ({"server": {"command": "bash", "args": ["-c", "id"]}}, "npx"),
        ({"server": {"image": "x/y", "env": {"T": "ghp_" + "a" * 36}}}, "sekret"),
        ({"homepage": "http://example.com"}, "https"),
        ({"category": "games"}, "category"),
    ])
    def test_bad_files_are_refused(self, change, message):
        with pytest.raises(addons.AddonError, match=message):
            addons.parse(good(**change), "github.json")

    def test_file_name_must_match(self):
        with pytest.raises(addons.AddonError):
            addons.parse(good(), "inny.json")

    def test_to_data_never_contains_secret_values(self):
        registry.set_secret("MCP_GITHUB_TOKEN", "tajna-wartosc-123")
        data = addons.get("github").to_data()
        assert data["ready"] and data["secrets"][0]["set"] and "tajna-wartosc-123" not in json.dumps(data)


class TestSecrets:

    def test_store_resolve_forget(self):
        with pytest.raises(registry.McpConfigError, match="MCP_X"):
            registry.resolve({"A": "${MCP_X}"})
        registry.set_secret("mcp_x", "  wartosc  ")
        assert registry.resolve({"A": "pre-${MCP_X}"}) == {"A": "pre-wartosc"} and registry.has_secret("MCP_X")
        if os.name == "posix":
            assert registry.secrets_path().stat().st_mode & 0o777 == 0o600
        assert registry.forget_secret("MCP_X") and not registry.has_secret("MCP_X")

    def test_env_wins_over_store(self, monkeypatch):
        registry.set_secret("MCP_Y", "z-pliku")
        monkeypatch.setenv("MCP_Y", "z-env")
        assert registry.resolve({"A": "${MCP_Y}"}) == {"A": "z-env"}

    @pytest.mark.parametrize("name,value", [("GITHUB_TOKEN", "x"), ("MCP_", "x"), ("MCP_A", ""), ("MCP_A", "a\nb")])
    def test_bad_secrets(self, name, value):
        with pytest.raises(registry.McpConfigError):
            registry.set_secret(name, value)

    def test_state_volume_in_launch(self):
        cfg = registry.validate_server("outlook", addons.get("outlook").server)
        assert cfg["state"] is True and cfg["autoApprove"] == ["login", "verify-login", "list-accounts"]
        _, argv, _, _ = registry.launch("outlook", cfg)
        assert "pipe-mcp-state-outlook:/state" in argv and "--privileged" not in argv
        assert not any(a.startswith("/") and ":" in a for a in argv if a != "pipe-mcp-state-outlook:/state")
        with pytest.raises(registry.McpConfigError):
            registry.validate_server("x", {"url": "https://example.com/mcp", "state": True})


class TestWindowCommand:

    def call(self, monkeypatch, request, role="admin"):
        frames = []

        async def fake_send(writer, frame):
            frames.append(frame)
        monkeypatch.setattr(server, "_send", fake_send)
        asyncio.run(server._mcp_servers(None, request, role))
        return frames[-1]

    def test_view_has_servers_and_catalog(self, monkeypatch):
        frame = self.call(monkeypatch, {"args": ""})
        data = frame["data"]
        assert data["servers"] == [] and data["can_edit"]
        github = next(a for a in data["catalog"] if a["name"] == "github")
        assert not github["ready"] and github["secrets"][0]["url"].startswith("https://")
        assert "Gotowe do dodania" in data["text"] and "github" in data["text"]

    def test_key_then_add_from_catalog(self, monkeypatch):
        missing = self.call(monkeypatch, {"args": "dodaj github"})
        assert missing["status"] == "error" and "MCP_GITHUB_TOKEN" in missing["response"]
        saved = self.call(monkeypatch, {"args": "klucz", "name": "MCP_GITHUB_TOKEN", "key": "ghp-sekret-xyz"})
        assert "Zapisano MCP_GITHUB_TOKEN" in saved["data"]["text"] and "ghp-sekret-xyz" not in json.dumps(saved)
        added = self.call(monkeypatch, {"args": "dodaj github"})
        cfg = registry.load_config()["github"]
        assert "Dodano" in added["data"]["text"] and cfg["addon"] == "github"
        assert cfg["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${MCP_GITHUB_TOKEN}"     # w pliku tylko odwolanie

    def test_login_hint_disable_and_remove_with_volume(self, monkeypatch):
        removed_volumes = []

        async def fake_remove(name):
            removed_volumes.append(name)
        monkeypatch.setattr(registry, "remove_state", fake_remove)
        added = self.call(monkeypatch, {"args": "dodaj outlook"})
        assert "devicelogin" in added["data"]["text"]
        self.call(monkeypatch, {"args": "wylacz outlook"})
        assert registry.load_config()["outlook"]["enabled"] is False
        assert self.call(monkeypatch, {"args": ""})["data"]["servers"][0]["status"] == "disabled"
        self.call(monkeypatch, {"args": "usun outlook"})
        assert removed_volumes == ["outlook"] and "outlook" not in registry.load_config()

    def test_viewer_sees_but_cannot_change(self, monkeypatch):
        assert self.call(monkeypatch, {"args": "klucz", "name": "MCP_A", "key": "x"}, role="viewer")["status"] == "error"
        assert self.call(monkeypatch, {"args": "dodaj outlook"}, role="viewer")["status"] == "error"
        assert self.call(monkeypatch, {"args": ""}, role="viewer")["data"]["can_edit"] is False


class TestAgentTool:

    def test_catalog_and_add_by_name(self):
        from backend.core.handlers.mcp import handle_mcp_manage
        from backend.core.session import Session

        class Call:
            def __init__(self, i):
                self.id = i

        session = Session("s", "cli:kuba")

        async def go(args, i):
            return [e async for e in handle_mcp_manage(None, session, Call(i), args)]

        asyncio.run(go({"operation": "catalog"}, "c1"))
        listing = session.messages[-1]["content"]
        assert "outlook" in listing and "github" in listing and "MCP_GITHUB_TOKEN" in listing
        asyncio.run(go({"operation": "add", "name": "github"}, "c2"))
        assert "oknie /mcp" in session.messages[-1]["content"] and session.pending_confirmation is None
        events = asyncio.run(go({"operation": "add", "name": "outlook"}, "c3"))
        assert session.pending_confirmation is not None and any("[POTWIERDZ]" in e for e in events)
        assert "softeria" in "".join(events).lower()


class TestTelegram:

    def test_window_text_and_buttons(self):
        from tg_format import format_mcp, mcp_buttons
        data = {"servers": [{"name": "outlook", "status": "connected", "tools": [{}, {}]}],
                "catalog": [{"name": "github", "title": "GitHub", "description": "repo", "installed": False, "ready": False,
                             "secrets": [{"name": "MCP_GITHUB_TOKEN"}]},
                            {"name": "google", "title": "Google", "description": "g", "installed": False, "ready": True,
                             "secrets": [{"name": "MCP_G"}]}],
                "can_edit": True}
        text = format_mcp(data)
        assert "outlook" in text and "2 narzedzi" in text and "pipe web albo w CLI" in text
        assert mcp_buttons(data, True) == [("➕ Google", "mcp:add:google")] and mcp_buttons(data, False) == []


class TestCliWindow:
    """Okno /mcp w CLI (clients/cli/mcpwindow.py) z symulowana klawiatura."""

    def test_arrows_enter_and_escape(self):
        pytest.importorskip("prompt_toolkit")
        import io
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "cli"))
        import mcpwindow
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output.vt100 import Vt100_Output

        data = {"servers": [{"name": "outlook", "status": "connected", "tools": [], "enabled": True, "runs": "x",
                             "secrets": []}],
                "catalog": [{"name": "github", "title": "GitHub", "description": "repo", "installed": False, "ready": True,
                             "login": "", "runs": "y", "secrets": []}], "can_edit": True, "text": ""}
        sent = []

        class Client:
            async def send_command(self, command, **fields):
                sent.append(fields)
                return [{"status": "ok", "done": True, "data": {**data, "text": "Dodano serwer MCP github"}}]

        class Console:
            printed = []

            def print(self, *args, **kwargs):
                self.printed.append(" ".join(map(str, args)))

        console = Console()

        async def go():
            out = io.StringIO()
            with create_pipe_input() as pipe:
                output = Vt100_Output(out, lambda: Size(rows=40, columns=90), term="xterm")
                with create_app_session(input=pipe, output=output):
                    async def keys():
                        for key in ("\x1b[B", "\r", "\r", "\x1b"):    # w dol na GitHub, otworz, Dodaj, wyjdz
                            await asyncio.sleep(0.2)
                            pipe.send_text(key)
                    task = asyncio.ensure_future(keys())
                    await mcpwindow.run(Client(), console, lambda pl, en: pl, lambda frames: frames[-1]["data"])
                    await task
            return out.getvalue()

        screen = asyncio.run(go())
        assert {"args": "dodaj github"} in sent
        assert "Serwery MCP" in screen and "Gotowe do dodania" in screen and "❯" in screen
        assert any("Dodano serwer MCP github" in line for line in console.printed)
