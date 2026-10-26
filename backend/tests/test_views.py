"""
Widok Pipe: prosty (dla osob nietechnicznych) albo techniczny — komenda `view` (/widok w CLI, pipe web i na
Telegramie), wspolna dla calego agenta jak jezyk; prompt w widoku prostym; klienci ukrywaja te same komendy.
"""

import json
import re
import sys
from pathlib import Path

import pytest

import backend.server as server
from backend.config import settings
from backend.core import audit, views, watch
from backend.core.session import Session
from backend.tests.test_language_switch import exchange

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "clients" / "telegram"))
import tg_format  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPE_LANG", "pl")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "AGENT_TOKEN", "")
    monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "", raising=False)
    monkeypatch.setattr(audit, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(watch, "_watcher", watch.Watcher())
    return tmp_path


class TestStore:

    @pytest.mark.parametrize("value,expected", [("prosty", "simple"), ("Simple", "simple"), ("1", "simple"),
                                                ("techniczny", "tech"), ("TECHNICAL", "tech"), ("2", "tech"),
                                                ("zaawansowany", "tech"), ("cos", None), ("", None)])
    def test_normalize(self, value, expected):
        assert views.normalize(value) == expected

    @pytest.mark.parametrize("text,expected", [
        ("Chcę ogarniać maile, kalendarz i przypomnienia", "simple"),
        ("nie jestem techniczny, potrzebuję pomocy z tekstami", "simple"),
        ("I want help writing emails and notes", "simple"),
        ("pilnować serwera z Dockerem i nginxem", "tech"),
        ("Wdrożę aplikację z GitHuba na VPS", "tech"),
        ("monitor my Kubernetes cluster and patch vulnerabilities", "tech"),
        ("nie wiem", None),
    ])
    def test_guess_from_plain_words(self, text, expected):
        assert views.guess(text) == expected

    def test_default_is_technical_until_someone_chooses(self, env):
        assert views.current() == "tech" and not views.chosen()
        assert views.set_view("chce robic notatki i przypomnienia") == "simple"
        assert views.chosen() and views.is_simple()
        assert json.loads((env / views.STORE).read_text()) == {"view": "simple"}
        with pytest.raises(ValueError):
            views.set_view("hmm")
        assert views.current() == "simple"           # nieudana proba nie zmienia wyboru


class TestCommand:

    def test_state_switch_and_event_for_subscribers(self, env):
        (state, switched), events = exchange({"command": "view"}, {"command": "view", "args": "prosty"}, subscribe=True)
        assert state["data"]["view"] == "tech" and state["data"]["chosen"] is False and state["data"]["can_edit"]
        assert events[0]["type"] == "subscribed" and events[0]["view"] == "tech" and events[0]["view_chosen"] is False
        assert switched["data"]["view"] == "simple" and switched["data"]["chosen"]
        assert "/widok techniczny" in switched["data"]["text"]
        assert events[1]["type"] == "view" and events[1]["view"] == "simple"
        assert "view.json (view=simple)" in (env / "audit.log").read_text()

    def test_plain_words_pick_the_view(self, env):
        (reply,), _ = exchange({"command": "view", "args": "chcę pilnować serwera ze sklepem"})
        assert reply["data"]["view"] == "tech" and views.chosen()

    def test_unknown_answer_is_an_error_with_both_options(self, env):
        (reply,), _ = exchange({"command": "view", "args": "nie wiem"})
        assert reply["status"] == "error" and "/widok prosty" in reply["response"] and not views.chosen()

    def test_viewer_cannot_change_it(self, env, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "adm")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "view", raising=False)
        (state, change), _ = exchange({"command": "view", "token": "view"}, {"command": "view", "args": "prosty", "token": "view"})
        assert state["data"]["can_edit"] is False
        assert change["status"] == "error" and not views.chosen()


class TestPrompt:

    def test_simple_view_adds_plain_language_block(self, env):
        session = Session(interface="cli:kuba")
        assert "WIDOK PROSTY" not in session.system_prompt
        views.set_view("simple")
        prompt = session.system_prompt
        assert "WIDOK PROSTY" in prompt and prompt.index("WIDOK PROSTY") < prompt.index("--- NAWIGACJA ---")

    def test_english_block(self, env, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        views.set_view("simple")
        assert "SIMPLE VIEW" in Session(interface="cli:kuba").system_prompt


class TestClientsAgree:
    """CLI, Telegram i pipe web ukrywaja w widoku prostym te same komendy i zgaduja widok tymi samymi slowami."""

    def _js_list(self, source: str, name: str) -> list[str]:
        block = re.search(rf"const {name} = \[(.*?)\];", source, re.S).group(1)
        return re.findall(r'"([^"]+)"', block)

    def test_tech_commands_are_the_same_everywhere(self):
        pytest.importorskip("rich")
        sys.path.insert(0, str(ROOT / "clients" / "cli"))
        import cli
        app_js = (ROOT / "clients" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        assert set(self._js_list(app_js, "TECH_COMMANDS")) == set(cli.TECH_COMMANDS) == set(tg_format.TECH_COMMANDS)
        assert "widok" in {pl for pl, *_ in cli.COMMANDS} and cli.COMMAND_ALIASES["view"] == "widok"

    def test_web_guess_uses_the_backend_words(self):
        source = (ROOT / "clients" / "webui" / "static" / "onboard.js").read_text(encoding="utf-8")
        assert self._js_list(source, "TECH_WORDS") == list(views.TECH_WORDS)
        assert self._js_list(source, "SIMPLE_WORDS") == list(views.SIMPLE_WORDS)


class TestTelegram:

    def test_menu_and_help_follow_the_view(self):
        simple = [name for name, _ in tg_format.build_menu([], "simple")]
        tech = [name for name, _ in tg_format.build_menu([], "tech")]
        assert "zalataj" in tech and "zalataj" not in simple and {"skille", "widok", "pobierz"} <= set(simple)
        help_simple = tg_format.format_help([], "simple")
        assert "/podatnosci" not in help_simple and "/widok" in help_simple and "/widok techniczny" in help_simple
        assert "/podatnosci" in tg_format.format_help([], "tech")

    def test_simple_keyboard_buttons_map_to_commands(self):
        rows = tg_format.keyboard_rows("simple")
        assert tg_format.keyboard_command(rows[1][0]) == "skille"
        assert tg_format.keyboard_rows("tech") != rows

    def test_question_and_buttons(self, monkeypatch):
        monkeypatch.setenv("PIPE_LANG", "en")
        assert "What do you want to do" in tg_format.view_question()
        assert tg_format.view_buttons("tech") == [("Simple", "view:simple"), ("✓ Technical", "view:tech")]
