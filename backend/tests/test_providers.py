"""
Testy dla backend/config/providers.py — presety, rozwiazywanie konfiguracji,
wlasni providerzy i filtrowanie list modeli.
"""

import json

import pytest

from backend.config.providers import (
    BUILTIN_PROVIDERS,
    DEFAULT_PROVIDER_ID,
    NO_KEY_PLACEHOLDER,
    Provider,
    ProviderConfigError,
    all_providers,
    chat_model_ids,
    load_user_providers,
    model_available,
    resolve_llm_config,
    save_user_provider,
)


@pytest.fixture
def no_user_file(tmp_path):
    """Sciezka do nieistniejacego pliku providerow — izoluje testy od dysku."""
    return tmp_path / "providers.json"


class TestBuiltinProviders:

    def test_ids_are_unique(self):
        ids = [p.id for p in BUILTIN_PROVIDERS]
        assert len(ids) == len(set(ids))

    def test_remote_providers_use_https(self):
        for p in BUILTIN_PROVIDERS:
            if p.requires_key:
                assert p.base_url.startswith("https://"), p.id

    def test_default_provider_exists_and_has_model(self):
        default = next(p for p in BUILTIN_PROVIDERS if p.id == DEFAULT_PROVIDER_ID)
        assert default.default_model

    def test_retired_gemini_model_is_not_default(self):
        """gemini-2.0-flash zostal wylaczony przez Google — nie moze byc domyslny."""
        assert all(p.default_model != "gemini-2.0-flash" for p in BUILTIN_PROVIDERS)


class TestResolveConfig:

    def test_empty_env_falls_back_to_default_provider(self, no_user_file):
        config = resolve_llm_config({}, no_user_file)
        assert config.provider_id == DEFAULT_PROVIDER_ID
        assert config.model

    def test_provider_preset_supplies_url_and_model(self, no_user_file):
        config = resolve_llm_config({"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-x"}, no_user_file)
        assert config.base_url == "https://api.openai.com/v1"
        assert config.model == "gpt-5.6-terra"
        assert config.api_key == "sk-x"

    def test_provider_id_is_case_insensitive(self, no_user_file):
        assert resolve_llm_config({"LLM_PROVIDER": " Groq "}, no_user_file).provider_id == "groq"

    def test_explicit_model_overrides_preset(self, no_user_file):
        config = resolve_llm_config({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.6-luna"}, no_user_file)
        assert config.model == "gpt-5.6-luna"

    def test_explicit_base_url_overrides_preset(self, no_user_file):
        config = resolve_llm_config(
            {"LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://proxy.example.com/v1"}, no_user_file
        )
        assert config.base_url == "https://proxy.example.com/v1"
        assert config.provider_id == "openai"

    def test_legacy_env_without_provider_is_matched_by_url(self, no_user_file):
        """Pliki .env sprzed v0.5.0 mialy tylko LLM_BASE_URL/LLM_API_KEY/LLM_MODEL."""
        env = {
            "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "LLM_API_KEY": "abc",
            "LLM_MODEL": "gemini-2.5-flash",
        }
        config = resolve_llm_config(env, no_user_file)
        assert config.provider_id == "gemini"
        assert config.model == "gemini-2.5-flash"

    def test_legacy_url_match_ignores_trailing_slash(self, no_user_file):
        config = resolve_llm_config({"LLM_BASE_URL": "https://api.groq.com/openai/v1/"}, no_user_file)
        assert config.provider_id == "groq"

    def test_unknown_url_is_custom_endpoint(self, no_user_file):
        config = resolve_llm_config(
            {"LLM_BASE_URL": "http://10.0.0.5:8000/v1", "LLM_MODEL": "qwen3"}, no_user_file
        )
        assert config.provider_id == "custom"
        assert config.requires_key is False

    def test_custom_endpoint_without_model_is_error(self, no_user_file):
        with pytest.raises(ProviderConfigError, match="LLM_MODEL"):
            resolve_llm_config({"LLM_BASE_URL": "http://10.0.0.5:8000/v1"}, no_user_file)

    def test_unknown_provider_lists_available(self, no_user_file):
        with pytest.raises(ProviderConfigError, match="gemini"):
            resolve_llm_config({"LLM_PROVIDER": "nie-ma-takiego"}, no_user_file)

    def test_provider_without_default_model_requires_llm_model(self, no_user_file):
        with pytest.raises(ProviderConfigError, match="LLM_MODEL"):
            resolve_llm_config({"LLM_PROVIDER": "ollama"}, no_user_file)

    def test_key_falls_back_to_provider_specific_env(self, no_user_file):
        config = resolve_llm_config({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-env"}, no_user_file)
        assert config.api_key == "sk-env"

    def test_llm_api_key_wins_over_provider_specific_env(self, no_user_file):
        env = {"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-main", "OPENAI_API_KEY": "sk-env"}
        assert resolve_llm_config(env, no_user_file).api_key == "sk-main"

    def test_other_providers_key_is_not_used(self, no_user_file):
        config = resolve_llm_config({"LLM_PROVIDER": "openai", "ANTHROPIC_API_KEY": "sk-ant"}, no_user_file)
        assert config.api_key == ""

    def test_local_provider_does_not_require_key(self, no_user_file):
        config = resolve_llm_config({"LLM_PROVIDER": "ollama", "LLM_MODEL": "qwen3"}, no_user_file)
        assert config.requires_key is False

    def test_optional_settings(self, no_user_file):
        env = {"LLM_PROVIDER": "openai", "LLM_REASONING_EFFORT": "low", "LLM_TIMEOUT": "300"}
        config = resolve_llm_config(env, no_user_file)
        assert config.reasoning_effort == "low"
        assert config.timeout == 300.0

    def test_empty_reasoning_effort_is_none(self, no_user_file):
        assert resolve_llm_config({"LLM_REASONING_EFFORT": " "}, no_user_file).reasoning_effort is None

    def test_invalid_timeout_is_error(self, no_user_file):
        with pytest.raises(ProviderConfigError, match="LLM_TIMEOUT"):
            resolve_llm_config({"LLM_TIMEOUT": "dlugo"}, no_user_file)

    def test_providers_file_taken_from_env(self, tmp_path):
        path = tmp_path / "moje.json"
        save_user_provider(Provider(id="lab", name="Lab", base_url="http://lab/v1", default_model="m", builtin=False), path)
        config = resolve_llm_config({"LLM_PROVIDER": "lab", "LLM_PROVIDERS_FILE": str(path)})
        assert config.base_url == "http://lab/v1"


class TestUserProviders:

    def test_missing_file_means_no_user_providers(self, no_user_file):
        assert load_user_providers(no_user_file) == []

    def test_save_and_load_roundtrip(self, no_user_file):
        provider = Provider(id="vllm", name="Moj vLLM", base_url="http://gpu:8000/v1",
                            requires_key=False, builtin=False)
        save_user_provider(provider, no_user_file)
        assert load_user_providers(no_user_file) == [provider]

    def test_save_upserts_by_id(self, no_user_file):
        save_user_provider(Provider(id="a", name="A", base_url="http://a/v1", builtin=False), no_user_file)
        save_user_provider(Provider(id="a", name="A2", base_url="http://a2/v1", builtin=False), no_user_file)
        loaded = load_user_providers(no_user_file)
        assert len(loaded) == 1 and loaded[0].base_url == "http://a2/v1"

    def test_user_entry_overrides_builtin(self, no_user_file):
        no_user_file.write_text(json.dumps({"providers": [
            {"id": "openai", "name": "OpenAI przez proxy", "base_url": "https://proxy/v1", "default_model": "x"}
        ]}))
        providers = all_providers(no_user_file)
        assert providers["openai"].base_url == "https://proxy/v1"
        assert providers["openai"].builtin is False

    def test_bare_list_format_is_accepted(self, no_user_file):
        no_user_file.write_text(json.dumps([{"id": "x", "base_url": "http://x/v1"}]))
        assert load_user_providers(no_user_file)[0].name == "x"

    def test_entries_without_required_fields_are_skipped(self, no_user_file, capsys):
        no_user_file.write_text(json.dumps({"providers": [{"name": "bez id"}, {"id": "ok", "base_url": "http://ok/v1"}]}))
        assert [p.id for p in load_user_providers(no_user_file)] == ["ok"]
        assert "Pomijam" in capsys.readouterr().err

    def test_unknown_fields_are_ignored(self, no_user_file):
        no_user_file.write_text(json.dumps([{"id": "x", "base_url": "http://x/v1", "kolor": "zielony"}]))
        assert load_user_providers(no_user_file)[0].id == "x"

    def test_invalid_json_is_clear_error(self, no_user_file):
        no_user_file.write_text("{ to nie json")
        with pytest.raises(ProviderConfigError, match="JSON"):
            load_user_providers(no_user_file)


class TestChatModelIds:

    def test_filters_non_chat_models(self):
        models = [{"id": i} for i in (
            "gpt-5.6-terra", "text-embedding-3-large", "gpt-tts-1", "whisper-large-v3",
            "gemini-3.1-flash-image", "llama-guard-4", "gpt-transcribe", "omni-moderation-latest",
        )]
        assert chat_model_ids(models) == ["gpt-5.6-terra"]

    def test_strips_gemini_models_prefix(self):
        assert chat_model_ids([{"id": "models/gemini-3.8-flash"}]) == ["gemini-3.8-flash"]

    def test_respects_supported_parameters_when_present(self):
        """OpenRouter podaje supported_parameters — bez 'tools' agent nie zadziala."""
        models = [
            {"id": "a/with-tools", "supported_parameters": ["tools", "temperature"]},
            {"id": "a/no-tools", "supported_parameters": ["temperature"]},
            {"id": "a/unknown"},
        ]
        assert chat_model_ids(models) == ["a/with-tools", "a/unknown"]

    def test_drops_batch_variants(self):
        assert chat_model_ids([{"id": "x/model"}, {"id": "x/model:batch"}]) == ["x/model"]

    def test_newest_first_when_created_is_known(self):
        models = [{"id": "old", "created": 100}, {"id": "new", "created": 300}, {"id": "mid", "created": 200}]
        assert chat_model_ids(models) == ["new", "mid", "old"]

    def test_keeps_api_order_without_created(self):
        assert chat_model_ids([{"id": "b"}, {"id": "a"}, {"id": "c"}]) == ["b", "a", "c"]

    def test_deduplicates(self):
        assert chat_model_ids([{"id": "m"}, {"id": "models/m"}]) == ["m"]


class TestModelAvailable:

    def test_exact_match(self):
        assert model_available("gpt-5.6-terra", ["gpt-5.6-terra"])

    def test_ollama_latest_tag_is_equivalent(self):
        assert model_available("qwen3", ["qwen3:latest"])
        assert model_available("qwen3:latest", ["qwen3"])

    def test_missing_model(self):
        assert not model_available("gemini-2.0-flash", ["gemini-3.8-flash"])
