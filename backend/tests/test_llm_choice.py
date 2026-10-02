"""
Wybor providera LLM w trakcie pracy (core/llm.py): magazyn kluczy, przelaczanie w agencie,
komendy serwera dla `pipe web`.
"""

import asyncio
import json
import stat

import pytest

from backend import configure
from backend.config import settings
from backend.config.providers import resolve_llm_config
from backend.core import llm
from backend.core.agent import VPSAgent, _for_provider
from backend.core.security import classify_command
from backend.tests.fakes import FakeClient, completion

ENV = {"LLM_PROVIDER": "gemini", "LLM_API_KEY": "gem-key", "LLM_MODEL": "gemini-flash",
       "LLM_REASONING_EFFORT": "low", "OPENAI_API_KEY": "oai-key"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(tmp_path / "providers.json"))


@pytest.fixture
def base():
    return resolve_llm_config(ENV)


def rows(base):
    return {row["id"]: row for row in llm.listing(base, ENV)["providers"]}


class TestStore:

    def test_listing_shows_ready_providers_without_keys(self, base):
        state = llm.listing(base, ENV)
        found = rows(base)
        assert state["active"] == {"id": "gemini", "name": "Google Gemini", "model": "gemini-flash"} and not state["chosen"]
        assert found["gemini"]["ready"] and found["gemini"]["active"] and found["gemini"]["key_source"] == "env"
        assert found["openai"]["ready"] and not found["openai"]["active"]          # klucz z OPENAI_API_KEY
        assert not found["groq"]["ready"] and found["groq"]["key_url"].startswith("https://")
        assert not found["ollama"]["ready"] and not found["ollama"]["requires_key"]
        assert state["providers"][0]["id"] == "gemini" and state["providers"][1]["id"] == "openai"   # gotowi na gorze
        assert "gem-key" not in json.dumps(state) and "oai-key" not in json.dumps(state)

    def test_select_stores_key_and_switches(self, base):
        current = llm.select("groq", base, key="gsk-abc", model="llama-x", env=ENV)
        assert (current.provider_id, current.api_key, current.model) == ("groq", "gsk-abc", "llama-x")
        assert current.base_url == "https://api.groq.com/openai/v1" and current.reasoning_effort is None
        assert stat.S_IMODE(llm.store_path().stat().st_mode) == 0o600
        state = llm.listing(base, ENV)
        assert state["chosen"] and state["active"]["id"] == "groq" and state["providers"][0]["id"] == "groq"
        assert rows(base)["groq"]["key_source"] == "web" and "gsk-abc" not in json.dumps(state)

    def test_select_back_to_base_keeps_reasoning_effort(self, base):
        llm.select("openai", base, env=ENV)
        assert llm.resolve(base, ENV).model == "gpt-5.6-terra"
        current = llm.select("gemini", base, model="gemini-pro", env=ENV)
        assert (current.provider_id, current.model, current.reasoning_effort) == ("gemini", "gemini-pro", "low")

    def test_select_rejects_incomplete_or_malformed(self, base):
        with pytest.raises(llm.LlmError):
            llm.select("groq", base, env=ENV)                      # brak klucza
        with pytest.raises(llm.LlmError):
            llm.select("fireworks", base, key="fw-1", env=ENV)     # preset bez domyslnego modelu
        with pytest.raises(llm.LlmError):
            llm.select("nie-ma", base, key="x", env=ENV)
        with pytest.raises(llm.LlmError):
            llm.select("groq", base, key="ma spacje", model="m", env=ENV)
        with pytest.raises(llm.LlmError):
            llm.select("groq", base, key="ok", model="zly model\n", env=ENV)
        assert not llm.store_path().exists()

    def test_forget_returns_to_base(self, base):
        llm.select("groq", base, key="gsk-abc", model="llama-x", env=ENV)
        assert llm.forget("groq", base, ENV) and not llm.forget("groq", base, ENV)
        assert llm.resolve(base, ENV).provider_id == "gemini" and not rows(base)["groq"]["ready"]

    def test_broken_store_falls_back_to_base(self, base):
        llm.store_path().parent.mkdir(parents=True)
        llm.store_path().write_text("{nie json")
        assert llm.resolve(base, ENV) == base
        llm.store_path().write_text(json.dumps({"active": "usuniety-provider"}))
        assert llm.resolve(base, ENV) == base

    def test_key_store_is_a_sensitive_file(self):
        assert classify_command("cat /opt/pipe/backend/data/llm_keys.json") == "confirm"


class TestAgent:

    def test_agent_uses_selected_provider_and_model(self, base, monkeypatch):
        monkeypatch.setattr(settings, "LLM", base)
        monkeypatch.setattr(settings, "WORKER_MODEL", "gemini-lite")
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        agent = VPSAgent(client=FakeClient([completion("a"), completion("b"), completion("c")]))
        asyncio.run(agent.complete("s", "u", model="gemini-lite"))
        llm.select("openai", base)
        asyncio.run(agent.complete("s", "u", model="gemini-lite"))
        asyncio.run(agent.complete("s", "u"))
        first, second, third = agent._client.calls
        assert first["model"] == "gemini-lite" and first["extra_body"] == {"reasoning_effort": "low"}
        # model workera istnieje tylko u providera bazowego
        assert second["model"] == third["model"] == "gpt-5.6-terra" and second["extra_body"] is None

    def test_separate_client_per_selected_provider(self, base, monkeypatch):
        monkeypatch.setattr(settings, "LLM", base)
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        agent = VPSAgent()
        assert agent.active_llm()[1] is agent._client
        llm.select("openai", base)
        config, client = agent.active_llm()
        assert client is not agent._client and client is agent.active_llm()[1]
        assert str(client.base_url).startswith("https://api.openai.com") and client.api_key == "oai-key"

    def test_history_from_another_provider_is_reduced_to_the_standard(self):
        message = {"role": "assistant", "content": None, "pipe_provider": "gemini", "reasoning_content": "...",
                   "tool_calls": [{"id": "c1", "type": "function", "extra_content": {"google": {"thought_signature": "x"}},
                                   "function": {"name": "run", "arguments": "{}"}}]}
        same = _for_provider(message, "gemini")
        assert "pipe_provider" not in same and same["reasoning_content"] == "..." and "extra_content" in same["tool_calls"][0]
        other = _for_provider(message, "openai")
        assert other == {"role": "assistant", "content": "",
                         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]}
        tool = {"role": "tool", "tool_call_id": "c1", "content": "ok", "pipe_generated": True}
        assert _for_provider(tool, "openai") == {"role": "tool", "tool_call_id": "c1", "content": "ok"}


class TestServerCommands:

    @pytest.fixture
    def server_agent(self, base, monkeypatch):
        import backend.server as server
        from backend.tests.test_server_commands import FakeAgent

        monkeypatch.setattr(settings, "LLM", base)
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "")
        monkeypatch.setenv("AUDIT_LOG_PATH", "/dev/null")
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        fake = FakeAgent()
        monkeypatch.setattr(server, "get_agent", lambda: fake)
        return fake

    def test_check_key_then_switch(self, server_agent, monkeypatch):
        from backend.tests.test_server_commands import exchange

        seen = []

        def fake_models(base_url, key):
            seen.append((base_url, key))
            if key == "zly":
                raise configure.ApiError("HTTP 401: invalid key", 401)
            return ["llama-new", "llama-old"]

        monkeypatch.setattr(configure, "fetch_models", fake_models)
        [[listed], [no_key], [bad], [models], [refused], [switched]] = exchange([
            {"command": "providers"},
            {"command": "provider_models", "name": "groq"},
            {"command": "provider_models", "name": "groq", "key": "zly"},
            {"command": "provider_models", "name": "groq", "key": "gsk-dobry"},
            {"command": "provider_set", "name": "groq", "key": "zly", "model": "llama-new"},
            {"command": "provider_set", "name": "groq", "key": "gsk-dobry", "model": "llama-new"},
        ])
        assert listed["data"]["can_edit"] and listed["data"]["active"]["id"] == "gemini" and not listed["data"]["chosen"]
        assert no_key["status"] == "error" and bad["status"] == "error" and "401" in bad["response"]
        assert models["data"] == {"models": ["llama-new", "llama-old"], "total": 2}
        assert refused["status"] == "error" and "zly" not in refused["response"]
        assert switched["data"]["active"] == {"id": "groq", "name": "Groq", "model": "llama-new"} and switched["data"]["chosen"]
        assert "gsk-dobry" not in json.dumps(switched)
        assert seen[-1] == ("https://api.groq.com/openai/v1", "gsk-dobry")
        assert server_agent.messages == []                           # bez LLM

    def test_switch_between_ready_providers_and_forget(self, server_agent, base):
        from backend.tests.test_server_commands import exchange

        llm.select("groq", base, key="gsk-abc", model="llama-x")
        [[to_openai], [forgot], [unknown]] = exchange([
            {"command": "provider_set", "name": "openai"},          # klucz juz jest (z .env) — bez sprawdzania
            {"command": "provider_forget", "name": "groq"},
            {"command": "provider_set", "name": "nie-ma"},
        ])
        assert to_openai["data"]["active"]["id"] == "openai"
        assert not {r["id"]: r for r in forgot["data"]["providers"]}["groq"]["ready"]
        assert unknown["status"] == "error"

    def test_viewer_can_look_but_not_change(self, server_agent, monkeypatch):
        from backend.tests.test_server_commands import exchange

        monkeypatch.setattr(settings, "AGENT_TOKEN", "admtok")
        monkeypatch.setattr(settings, "AGENT_VIEWER_TOKEN", "viewtok")
        [[listed], [refused]] = exchange([{"command": "providers", "token": "viewtok"},
                                          {"command": "provider_set", "name": "openai", "token": "viewtok"}])
        assert listed["data"]["can_edit"] is False and listed["data"]["active"]["id"] == "gemini"
        assert refused["status"] == "error" and llm.listing(settings.LLM)["active"]["id"] == "gemini"
