"""
Wybor providera prowadzi do wyboru modelu z jego listy — w CLI, `pipe web` i na Telegramie.
Obecny model jest zaznaczony i domyslny; fragment nazwy zaweza liste, a jedno trafienie wybiera od razu.
"""

import asyncio
from pathlib import Path

import pytest

MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it", "qwen-qwq-32b"]
GROQ = {"id": "groq", "name": "Groq", "model": "llama-3.1-8b-instant", "requires_key": True, "has_key": True, "ready": True}


class TestTelegramHelpers:

    def test_model_page_starts_at_current_and_wraps(self):
        from clients.telegram import tg_format

        models = [f"m{i}" for i in range(20)]
        buttons, page, pages = tg_format.model_page(models, "m17")
        assert (page, pages) == (2, 3) and ("✓ m17", 17) in buttons and len(buttons) == 4
        buttons, page, _ = tg_format.model_page(models, "inny", 0)
        assert page == 0 and buttons[0] == ("m0", 0) and len(buttons) == tg_format.MODELS_PER_PAGE
        assert tg_format.model_page(models, "", 99)[1] == 2          # strona spoza zakresu — ostatnia

    def test_long_names_are_shortened_and_callbacks_stay_small(self):
        from clients.telegram import tg_format

        long = "meta-llama/" + "x" * 120
        (label, index), = tg_format.model_page([long], long)[0]
        assert len(label) <= tg_format.MAX_MODEL_LABEL and label.startswith("✓ ") and index == 0

    def test_match_and_header(self):
        from clients.telegram import tg_format

        assert tg_format.match_models(MODELS, "LLAMA") == MODELS[:2] and tg_format.match_models(MODELS, "") == MODELS
        text = tg_format.format_model_picker("Groq <x>", "gemma2-9b-it", 4, 2, "llama")
        assert "Groq &lt;x&gt;" in text and "2 z 4" in text and "gemma2-9b-it" in text


class FakeClient:
    def __init__(self, models=MODELS, error=""):
        self.models, self.error, self.sent = models, error, []

    async def send_command(self, command, **fields):
        self.sent.append((command, fields))
        if command == "provider_models":
            if self.error:
                return [{"response": f"[BLAD] {self.error}", "status": "error", "done": True}]
            return [{"done": True, "data": {"models": self.models}}]
        if command == "provider_set":
            return [{"done": True, "data": {"active": {"id": fields["name"], "name": "Groq", "model": fields["model"]}}}]
        raise AssertionError(command)


@pytest.fixture
def cli(monkeypatch):
    pytest.importorskip("rich")                  # CI nie instaluje zaleznosci CLI
    from clients.cli import cli as module
    return module


def answers(monkeypatch, cli, *replies):
    queue = list(replies)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: queue.pop(0) if queue else k.get("default", ""))
    monkeypatch.setattr(cli.Confirm, "ask", lambda *a, **k: False)


class TestCli:

    def test_enter_keeps_the_current_model(self, monkeypatch, cli):
        answers(monkeypatch, cli)                                    # Enter -> domyslny = obecny
        client = FakeClient()
        asyncio.run(cli._setup_provider(client, GROQ, ask_key=False))
        assert client.sent[-1] == ("provider_set", {"name": "groq", "key": "", "model": "llama-3.1-8b-instant"})

    def test_number_and_narrowing_by_fragment(self, monkeypatch, cli):
        answers(monkeypatch, cli, "llama", "1")                      # "llama" — dwa trafienia, potem numer z zawezonej listy
        client = FakeClient()
        asyncio.run(cli._setup_provider(client, GROQ, ask_key=False))
        assert client.sent[-1][1]["model"] == "llama-3.3-70b-versatile"

    def test_query_with_one_hit_switches_without_asking(self, monkeypatch, cli):
        monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: pytest.fail("nie powinno pytac"))
        client = FakeClient()
        asyncio.run(cli._setup_provider(client, GROQ, "gemma", ask_key=False))
        assert client.sent[-1][1]["model"] == "gemma2-9b-it"

    def test_model_outside_the_list_needs_confirmation(self, monkeypatch, cli):
        answers(monkeypatch, cli, "nie-ma-takiego", "")               # Confirm -> nie; potem Enter = obecny
        client = FakeClient()
        asyncio.run(cli._setup_provider(client, GROQ, ask_key=False))
        assert client.sent[-1][1]["model"] == "llama-3.1-8b-instant"

    def test_unavailable_list_allows_typing_a_name(self, monkeypatch, cli):
        answers(monkeypatch, cli, "moj-model")
        client = FakeClient(error="Ollama nie odpowiada")
        asyncio.run(cli._setup_provider(client, {**GROQ, "id": "ollama", "requires_key": False}, ask_key=False))
        assert client.sent[-1][1]["model"] == "moj-model"


def test_web_menu_leads_to_the_model_step():
    static = Path(__file__).resolve().parents[2] / "clients" / "webui" / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    strings = (static / "i18n.js").read_text(encoding="utf-8")
    assert "openProvider(item.provider)" in app and "switchProvider" not in app
    assert "showProvider(provider, query)" in app and "function showProvider(p, query)" in app
    assert strings.count("pickModel:") == 2 and strings.count("changeModel:") == 2
