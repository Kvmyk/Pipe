"""
Testy dla kreatora backend/configure.py — edycja .env i interpretacja
odpowiedzi providera. Kreator nie laczy sie tu z zadnym API.
"""

import pytest

from backend import configure
from backend.configure import host_side_url, interpret_tool_test, parse_env, render_env


class TestParseEnv:

    def test_basic_values_comments_and_quotes(self):
        text = '# komentarz\nA=1\n\nB = "dwa"\nC=\'trzy\'\n# D=zakomentowane\n'
        assert parse_env(text) == {"A": "1", "B": "dwa", "C": "trzy"}

    def test_empty_value(self):
        assert parse_env("LLM_MODEL=\n") == {"LLM_MODEL": ""}


class TestRenderEnv:

    def test_updates_in_place_and_keeps_comments(self):
        text = "# Provider\nLLM_PROVIDER=gemini\n# klucz\nLLM_API_KEY=\nAGENT_TOKEN=tajny\n"
        result = render_env(text, {"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-1"})
        assert result == "# Provider\nLLM_PROVIDER=openai\n# klucz\nLLM_API_KEY=sk-1\nAGENT_TOKEN=tajny\n"

    def test_appends_missing_keys(self):
        assert render_env("A=1\n", {"B": "2"}) == "A=1\nB=2\n"

    def test_removes_keys(self):
        text = "LLM_BASE_URL=https://stary/v1\nLLM_MODEL=x\n"
        assert render_env(text, {}, remove={"LLM_BASE_URL"}) == "LLM_MODEL=x\n"

    def test_commented_keys_are_untouched(self):
        text = "# LLM_BASE_URL=https://przyklad/v1\n"
        assert render_env(text, {"LLM_BASE_URL": "nowy"}, remove=set()) == (
            "# LLM_BASE_URL=https://przyklad/v1\nLLM_BASE_URL=nowy\n"
        )

    def test_legacy_env_migration(self):
        """Stary .env (bez LLM_PROVIDER) po kreatorze: provider dopisany, stary URL usuniety."""
        legacy = "LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\nLLM_API_KEY=abc\nLLM_MODEL=gemini-2.0-flash\n"
        result = parse_env(render_env(legacy, {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-3.8-flash"}, {"LLM_BASE_URL"}))
        assert result == {"LLM_API_KEY": "abc", "LLM_MODEL": "gemini-3.8-flash", "LLM_PROVIDER": "gemini"}


class TestWriteEnvFile:

    def test_creates_from_example_with_private_permissions(self, tmp_path, monkeypatch):
        example = tmp_path / ".env.example"
        example.write_text("# naglowek\nLLM_PROVIDER=gemini\nLLM_API_KEY=\nAGENT_TOKEN=\n")
        monkeypatch.setattr(configure, "ENV_EXAMPLE_PATH", example)
        target = tmp_path / ".env"

        configure.write_env_file({"LLM_API_KEY": "sekret"}, set(), path=target)

        assert parse_env(target.read_text())["LLM_API_KEY"] == "sekret"
        assert "# naglowek" in target.read_text()
        assert target.stat().st_mode & 0o777 == 0o600


class TestInterpretToolTest:

    def test_tool_call_means_success(self):
        response = {"choices": [{"message": {"tool_calls": [{"function": {"name": "ping"}}]}}]}
        ok, _ = interpret_tool_test(response)
        assert ok

    def test_text_reply_means_failure(self):
        response = {"choices": [{"message": {"content": "Nie mam dostepu do narzedzi."}}]}
        ok, message = interpret_tool_test(response)
        assert not ok
        assert "Nie mam dostepu" in message

    def test_empty_response_means_failure(self):
        ok, _ = interpret_tool_test({})
        assert not ok


class TestHostSideUrl:

    def test_docker_host_alias_is_translated_on_host(self, monkeypatch):
        monkeypatch.setattr(configure.Path, "exists", lambda self: False)
        assert host_side_url("http://host.docker.internal:11434/v1") == "http://127.0.0.1:11434/v1"

    def test_remote_urls_are_untouched(self):
        assert host_side_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


class TestSlugify:

    @pytest.mark.parametrize("name,expected", [
        ("Moj vLLM", "moj-vllm"),
        ("  LM Studio (laptop) ", "lm-studio-laptop"),
        ("!!!", "wlasny"),
    ])
    def test_slugify(self, name, expected):
        assert configure.slugify(name) == expected
