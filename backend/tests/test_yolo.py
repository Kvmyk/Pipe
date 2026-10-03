"""
Tryb YOLO (/yolo): operacje wymagajace potwierdzenia wykonuja sie bez pytania — tylko admin, tylko
ta sesja, domyslnie wylaczony. Bezpiecznik i dziennik dzialaja dalej, zakazane sa nadal odrzucane.
"""

import asyncio
import json

import pytest

import backend.server as server
from backend.config import settings
from backend.core import audit, journal
from backend.core.agent import VPSAgent
from backend.core.events import Activity, Progress
from backend.core.session import Session
from backend.tests.fakes import FakeClient, assert_history_valid, completion


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path / "host"))
    (tmp_path / "host").mkdir()
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    return tmp_path


def run(gen):
    async def collect():
        return [e async for e in gen]
    return asyncio.run(collect())


def make_agent(script):
    return VPSAgent(client=FakeClient(script))


def texts(events):
    return [e for e in events if isinstance(e, str)]


class TestAgent:

    def test_off_by_default(self):
        session = Session()
        assert session.yolo is False and session.runs_yolo is False
        assert "YOLO" not in session.system_prompt

    def test_confirmed_command_runs_without_asking(self, tmp_path):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-dir"})]),
            completion("Zrobione"),
        ])
        agent.get_or_create_session("s").yolo = True
        events = run(agent.chat("s", "zrob katalog"))
        session = agent._sessions["s"]
        assert session.pending_confirmation is None
        assert not any("[POTWIERDZ]" in e for e in texts(events))
        assert texts(events)[-1] == "Zrobione"
        assert any(isinstance(e, Progress) and "YOLO" in e.text and "mkdir -p yolo-dir" in e.text for e in events)
        result = next(m for m in session.messages if m.get("tool_call_id") == "c1")
        assert "kod wyjscia" not in result["content"].lower() or "0" in result["content"]
        assert_history_valid(session.messages)
        assert "TRYB YOLO" in agent._client.calls[0]["messages"][0]["content"]

    def test_change_is_journaled_and_audited_as_yolo(self, tmp_path):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-dir"})]),
            completion("ok"),
        ])
        agent.get_or_create_session("s", "cli:kuba").yolo = True
        run(agent.chat("s", "zrob", "cli:kuba"))
        entries = journal.entries(5)
        assert entries and entries[0].interface == "cli:kuba [yolo]"
        assert "cli:kuba [yolo]" in (tmp_path / "audit.log").read_text(encoding="utf-8")

    def test_forbidden_still_refused(self):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "rm -rf /"})]),
            completion("Nie moge"),
        ])
        agent.get_or_create_session("s").yolo = True
        run(agent.chat("s", "usun wszystko"))
        session = agent._sessions["s"]
        result = next(m for m in session.messages if m.get("tool_call_id") == "c1")
        assert session.pending_confirmation is None
        assert not journal.entries(5)
        assert "rm -rf /" not in [e.command for e in journal.entries(5)]
        assert result["content"]
        assert_history_valid(session.messages)

    def test_write_file_runs_through_the_fuse(self, isolated):
        target = isolated / "host" / "srv" / "app.conf"
        target.parent.mkdir(parents=True)
        target.write_text("stare\n")
        agent = make_agent([
            completion(tool_calls=[("c1", "write_file", {"path": "/srv/app.conf", "content": "nowe\n"})]),
            completion("Zapisane"),
        ])
        agent.get_or_create_session("s").yolo = True
        events = run(agent.chat("s", "zmien config"))
        assert target.read_text() == "nowe\n"
        assert not any("[POTWIERDZ]" in e for e in texts(events))
        assert journal.entries(5)[0].tool == "write_file"         # kopia jest w dzienniku — /cofnij dziala
        assert_history_valid(agent._sessions["s"].messages)

    def test_viewer_cannot_run_yolo(self):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-dir"})]),
            completion("opisalem"),
        ])
        session = agent.get_or_create_session("s", owner="viewer", role="viewer")
        session.yolo = True
        events = run(agent.chat("s", "zrob", owner="viewer", role="viewer"))
        assert session.runs_yolo is False
        assert any(e.startswith("[ODMOWA]") for e in texts(events))
        assert not journal.entries(5)

    def test_web_activity_ends_once(self):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-dir"})]),
            completion("ok"),
        ])
        agent.get_or_create_session("s", "web:kuba").yolo = True
        events = run(agent.chat("s", "zrob", "web:kuba"))
        phases = [e.phase for e in events if isinstance(e, Activity)]
        assert phases == ["start", "end"]

    def test_other_sessions_still_ask(self):
        agent = make_agent([
            completion(tool_calls=[("c1", "execute_command", {"command": "mkdir -p yolo-dir"})]),
        ])
        agent.get_or_create_session("inna").yolo = True
        events = run(agent.chat("s", "zrob"))
        assert any("[POTWIERDZ]" in e for e in texts(events))
        assert agent._sessions["s"].pending_confirmation is not None


class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data: bytes):
        self.frames += [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    async def drain(self):
        pass


def command(agent, payload, role="admin"):
    writer = FakeWriter()
    request = {"command": "yolo", "session_id": "s", **payload}
    asyncio.run(server._handle_command(writer, agent, request, "s", "cli:kuba", "admin", role))
    return writer.frames[-1]


class TestCommand:

    def test_state_toggle_and_audit(self, isolated):
        agent = make_agent([])
        assert command(agent, {})["data"]["yolo"] is False
        frame = command(agent, {"args": "on"})
        assert frame["data"]["yolo"] is True and "/yolo off" in frame["data"]["text"]
        assert agent._sessions["s"].yolo is True
        assert command(agent, {})["data"]["yolo"] is True
        assert command(agent, {"args": "wylacz"})["data"]["yolo"] is False
        log = (isolated / "audit.log").read_text(encoding="utf-8")
        assert "yolo on" in log and "yolo off" in log

    def test_viewer_cannot_turn_it_on(self):
        agent = make_agent([])
        frame = command(agent, {"args": "on"}, role="viewer")
        assert frame["status"] == "error" and "administrator" in frame["response"]
        assert agent._sessions["s"].yolo is False

    def test_unknown_argument(self):
        frame = command(make_agent([]), {"args": "moze"})
        assert frame["status"] == "error" and "/yolo on" in frame["response"]


class TestClients:
    """/yolo i /providerzy w CLI, pipe web i na Telegramie."""

    def test_cli_commands_and_helpers(self):
        pytest.importorskip("rich")              # CI nie instaluje zaleznosci CLI — jak test_cli_completion.py
        from clients.cli import cli

        names = [entry[0] for entry in cli.COMMANDS]
        assert "yolo" in names and "providerzy" in names
        assert cli.COMMAND_ALIASES["providers"] == "providerzy" and cli.COMMAND_ALIASES["provider"] == "providerzy"
        assert "/yolo [on|off]" in cli.HELP_TEXT and "/yolo [on|off]" in cli.HELP_TEXT_EN
        data = {"providers": [{"id": "gemini", "name": "Google Gemini"}, {"id": "groq", "name": "Groq"}]}
        assert cli._find_provider(data, "2")["id"] == "groq"
        assert cli._find_provider(data, "Google Gemini")["id"] == "gemini" and cli._find_provider(data, "x") is None
        models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
        assert cli._pick_model(models, "2") == "llama-3.1-8b-instant"
        assert cli._pick_model(models, "gemma") == "gemma2-9b-it"            # jedyne pasujace
        assert cli._pick_model(models, "llama") == "llama"                   # niejednoznaczne — zostaje, jak wpisano

    def test_telegram_menu_and_formatting(self):
        from clients.telegram import tg_format

        assert "yolo" in dict(tg_format.BUILTIN_COMMANDS) and "providerzy" in dict(tg_format.BUILTIN_COMMANDS)
        assert "yolo" in dict(tg_format.BUILTIN_COMMANDS_EN) and "providers" in dict(tg_format.BUILTIN_COMMANDS_EN)
        assert tg_format.COMMAND_ALIASES["providers"] == "providerzy"
        data = {"active": {"id": "gemini", "name": "Google Gemini", "model": "gemini-2.5-flash"},
                "providers": [{"id": "gemini", "name": "Google Gemini", "model": "gemini-2.5-flash", "ready": True, "active": True},
                              {"id": "groq", "name": "Groq", "model": "llama", "ready": True},
                              {"id": "openai", "name": "OpenAI <x>", "ready": False}]}
        text = tg_format.format_providers(data, admin=True)
        assert "gemini-2.5-flash" in text and "OpenAI &lt;x&gt;" in text and "nie wysylaj" in text
        assert "tylko administrator" in tg_format.format_providers(data, admin=False)
        assert tg_format.provider_choices(data) == [("✓ Google Gemini", "gemini"), ("Groq", "groq")]

    def test_web_commands(self):
        from pathlib import Path

        static = Path(__file__).resolve().parents[2] / "clients" / "webui" / "static"
        app = (static / "app.js").read_text(encoding="utf-8")
        strings = (static / "i18n.js").read_text(encoding="utf-8")
        assert '["providerzy", "providers", "c_provider", true]' in app and '["yolo", "yolo", "c_yolo", true]' in app
        assert "providerzy: (args) => providersCommand(args)" in app and "openProvider(item.provider)" in app and "yolo: (args) => setYolo(args)" in app
        assert 'id="yolo-chip"' in (static / "index.html").read_text(encoding="utf-8")
        assert strings.count("c_yolo:") == 2 and strings.count("yoloChip:") == 2 and strings.count("unknownProvider:") == 2
