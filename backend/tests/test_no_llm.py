"""
Pipe bez modelu jezykowego (LLM_PROVIDER=none): backend startuje, nic nie idzie do providera,
rozmowa dostaje jasny komunikat, a provider dodany pozniej z interfejsu wlacza ja bez restartu.
"""

import asyncio

import pytest

from backend import configure
from backend.config import settings
from backend.config.providers import NO_LLM_ID, resolve_llm_config
from backend.core import llm
from backend.core.agent import NoLLM, VPSAgent
from backend.tests.fakes import FakeClient, completion

NONE_ENV = {"LLM_PROVIDER": "none"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    monkeypatch.setattr(settings, "VIBE_EVERY", 0)


@pytest.fixture
def none_base(monkeypatch):
    base = resolve_llm_config(NONE_ENV)
    monkeypatch.setattr(settings, "LLM", base)
    return base


async def collect(gen):
    return [event async for event in gen]


class TestConfig:

    def test_none_resolves_without_key_or_model(self, none_base):
        assert none_base.provider_id == NO_LLM_ID and not none_base.enabled
        assert none_base.model == "" and none_base.base_url == "" and not none_base.requires_key

    def test_validate_accepts_none(self, none_base, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "")
        settings.validate()

    def test_regular_provider_is_enabled(self):
        assert resolve_llm_config({"LLM_PROVIDER": "gemini", "LLM_API_KEY": "k"}).enabled


class TestAgent:

    def test_chat_answers_without_calling_anyone(self, none_base):
        agent = VPSAgent()
        assert agent._client is None and not agent.llm_enabled()
        events = asyncio.run(collect(agent.chat("s1", "czesc")))
        assert len(events) == 1 and events[0].startswith("[BLAD]") and "LLM_PROVIDER=none" in events[0]
        assert "s1" not in agent._sessions                    # historia nie rosnie od wiadomosci bez odpowiedzi

    def test_call_llm_refuses_before_budget_or_network(self, none_base):
        agent = VPSAgent()
        with pytest.raises(NoLLM):
            asyncio.run(agent.complete("system", "pytanie"))

    def test_verify_model_is_silent(self, none_base):
        assert asyncio.run(VPSAgent().verify_model()) is None

    def test_provider_added_later_turns_chat_on(self, none_base, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "")
        llm.select("groq", none_base, key="gsk-abc", model="llama-x", env={})
        client = FakeClient([completion("Dzien dobry")])
        agent = VPSAgent(client)
        assert agent.llm_enabled()
        events = asyncio.run(collect(agent.chat("s1", "czesc")))
        assert events == ["Dzien dobry"] and client.calls[0]["model"] == "llama-x"


class TestListing:

    def test_none_is_not_a_row_and_active_says_no_model(self, none_base):
        state = llm.listing(none_base, {})
        assert NO_LLM_ID not in {row["id"] for row in state["providers"]}
        assert state["active"]["id"] == NO_LLM_ID and state["active"]["model"] == ""


class TestWatcher:

    def test_external_alert_is_not_investigated(self):
        from backend.core.watch import Alert, Watcher

        class Agent:
            def llm_enabled(self):
                return False

        watcher = Watcher(agent=Agent())
        alert = Alert(id="1", key="ext:x", severity="warning", title="t", detail="d", since="now")
        asyncio.run(watcher.investigate_external(alert))
        assert not [e for e in watcher.notifier.history if e.get("type") == "investigation"]


class TestWizard:

    def test_wizard_option_writes_none(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(configure, "ENV_PATH", tmp_path / ".env")
        (tmp_path / ".env").write_text("LLM_PROVIDER=gemini\nLLM_MODEL=gemini-flash\nAGENT_TOKEN=abc\n")
        assert configure.save_without_llm(configure.read_env_file(tmp_path / ".env"), "pl") == 0
        env = configure.read_env_file(tmp_path / ".env")
        assert env["LLM_PROVIDER"] == "none" and "LLM_MODEL" not in env and env["AGENT_TOKEN"] == "abc"

    def test_choose_provider_offers_no_model(self, monkeypatch):
        from backend.config.providers import all_providers
        providers = all_providers(None)
        answers = iter([str(len(providers) + 2)])
        monkeypatch.setattr(configure, "ask", lambda *_a, **_k: next(answers))
        assert configure.choose_provider(providers, "") == NO_LLM_ID

    def test_from_env_and_check(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(configure, "ENV_PATH", tmp_path / ".env")
        monkeypatch.setenv("LLM_PROVIDER", "none")
        assert configure.run_from_env(test=True) == 0
        assert "LLM_PROVIDER=none" in (tmp_path / ".env").read_text()
