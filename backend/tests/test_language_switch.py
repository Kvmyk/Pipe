"""
Zmiana jezyka Pipe w trakcie pracy (komenda `language`: /jezyk w CLI, pipe web i na Telegramie).

Wybor jest wspolny dla calego agenta: zapisuje sie w DATA_DIR/language.json, przelacza prompty
od razu i trafia jako zdarzenie `language` do subskrybentow (bot Telegram, pipe web).
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

import backend.server as server
from backend.config import settings
from backend.core import audit, i18n, watch
from backend.core.session import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients" / "webui"))
import server as web  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPE_LANG", "pl")           # set_lang() zmienia os.environ — monkeypatch przywroci
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(i18n, "_chosen", False)
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "", raising=False)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(watch, "_watcher", watch.Watcher())
    return tmp_path


def exchange(*payloads, subscribe=False):
    """Wysyla zadania po kolei; z `subscribe` zwraca tez zdarzenia, ktore dostal subskrybent."""
    async def run():
        sock = Path(tempfile.mkdtemp(prefix="p")) / "s.sock"
        srv = await asyncio.start_unix_server(server.handle_client, path=str(sock), limit=server.READ_LIMIT)
        events = []
        if subscribe:
            sub_reader, sub_writer = await asyncio.open_unix_connection(str(sock))
            sub_writer.write(b'{"command": "subscribe"}\n')
            await sub_writer.drain()
            events.append(json.loads(await sub_reader.readline())["event"])
        results = []
        for payload in payloads:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write((json.dumps({"session_id": "s", "interface": "cli:kuba", **payload}) + "\n").encode())
            await writer.drain()
            results.append(json.loads(await reader.readline()))
            writer.close()
        if subscribe:
            events.append(json.loads(await asyncio.wait_for(sub_reader.readline(), 2))["event"])
            sub_writer.close()
        srv.close()
        return results, events
    return asyncio.run(run())


class TestStore:

    @pytest.mark.parametrize("value,expected", [("EN", "en"), ("english", "en"), ("angielski", "en"), ("eng", "en"),
                                                ("pl", "pl"), ("Polski", "pl"), ("polish", "pl"), ("de", None), ("", None)])
    def test_normalize(self, value, expected):
        assert i18n.normalize(value) == expected

    def test_set_lang_switches_and_survives_restart(self, env, monkeypatch):
        assert i18n.set_lang("english") == "en" and i18n.lang() == "en" and i18n.chosen()
        assert json.loads((env / i18n.STORE).read_text()) == {"lang": "en", "env": "pl"}
        monkeypatch.setenv("PIPE_LANG", "pl")       # restart: proces znow startuje z .env
        monkeypatch.setattr(i18n, "_chosen", False)
        assert i18n.load_saved() == "en" and i18n.chosen()

    def test_switching_back_keeps_the_env_language(self, env):
        i18n.set_lang("en")
        i18n.set_lang("pl")
        assert json.loads((env / i18n.STORE).read_text()) == {"lang": "pl", "env": "pl"}

    def test_env_edited_later_wins(self, env, monkeypatch):
        i18n.set_lang("en")
        monkeypatch.setattr(i18n, "_chosen", False)
        monkeypatch.setenv("PIPE_LANG", "en")       # ktos przestawil PIPE_LANG w .env...
        (env / i18n.STORE).write_text('{"lang": "pl", "env": "pl"}')
        assert i18n.load_saved() == "en" and not i18n.chosen()      # ...wiec stary wybor juz nie obowiazuje

    def test_unknown_language_and_broken_store(self, env):
        with pytest.raises(ValueError):
            i18n.set_lang("de")
        (env / i18n.STORE).write_text("{zepsute")
        assert i18n.load_saved() == "pl" and not i18n.chosen()


class TestCommand:

    def test_state_without_arguments(self, env):
        (frame,), _ = exchange({"command": "language"})
        assert frame["data"]["lang"] == "pl" and frame["data"]["chosen"] is False and frame["data"]["can_edit"]
        assert frame["data"]["languages"] == ["pl", "en"] and "/jezyk en" in frame["data"]["text"]

    def test_switch_changes_prompt_and_notifies_subscribers(self, env):
        polish = Session(session_id="s", interface="cli").system_prompt
        (frame,), events = exchange({"command": "language", "args": "en"}, subscribe=True)
        assert frame["data"]["lang"] == "en" and frame["data"]["chosen"] and "English" in frame["data"]["text"]
        assert events[0]["type"] == "subscribed" and events[0]["lang"] == "pl" and events[0]["lang_chosen"] is False
        assert events[1]["type"] == "language" and events[1]["lang"] == "en"
        assert watch.get_watcher().notifier.history == []           # zdarzenie techniczne — nie trafia do /alerty
        assert Session(session_id="s", interface="cli").system_prompt != polish
        assert "language.json (lang=en)" in (env / "audit.log").read_text()

    def test_switch_and_back_from_another_channel(self, env):
        (first, second, state), _ = exchange({"command": "language", "args": "en"},
                                             {"command": "language", "args": "polski", "interface": "telegram:1"},
                                             {"command": "language"})
        assert first["data"]["lang"] == "en" and second["data"]["lang"] == "pl"
        assert state["data"]["lang"] == "pl" and state["data"]["chosen"] and "polski" in state["data"]["text"]

    def test_unknown_language_is_an_error(self, env):
        (frame,), _ = exchange({"command": "language", "args": "de"})
        assert frame["status"] == "error" and "[BLAD]" in frame["response"] and i18n.lang() == "pl"

    def test_viewer_cannot_switch(self, env, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "admin-token")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewer-token")
        (state, frame), _ = exchange({"command": "language", "token": "viewer-token"},
                                     {"command": "language", "args": "en", "token": "viewer-token"})
        assert state["data"]["can_edit"] is False
        assert frame["status"] == "error" and i18n.lang() == "pl" and not (env / i18n.STORE).exists()


class TestClients:

    def test_web_bridge_follows_the_server(self):
        bridge = web.WebBridge("127.0.0.1", 1, "tok", lang="pl")
        bridge._adopt_language("en")
        assert bridge.lang == "en"
        bridge._adopt_language("de")
        assert bridge.lang == "en"

    def test_every_client_has_the_command(self):
        root = Path(__file__).resolve().parents[2] / "clients"
        cli = (root / "cli" / "cli.py").read_text(encoding="utf-8")
        assert 'name == "jezyk"' in cli and "/jezyk [pl|en]" in cli and "/language [pl|en]" in cli
        app = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        assert '["jezyk", "language", "c_language", true]' in app and "jezyk: (args)" in app
        strings = (root / "webui" / "static" / "i18n.js").read_text(encoding="utf-8")
        assert strings.count("c_language:") == 2 and strings.count("languageReload:") == 2
        sys.path.insert(0, str(root / "telegram"))
        import tg_format
        assert tg_format.COMMAND_ALIASES["language"] == "jezyk"
        assert "jezyk" in dict(tg_format.BUILTIN_COMMANDS) and "language" in dict(tg_format.BUILTIN_COMMANDS_EN)
        assert 'CommandHandler(command_names(polish), handler)' in (root / "telegram" / "bot.py").read_text(encoding="utf-8")
