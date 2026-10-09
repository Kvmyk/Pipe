"""
Providerzy LLM wybierani w trakcie pracy (ekran wyboru i przelacznik w `pipe web`).

`backend/.env` wyznacza providera BAZOWEGO — tego, z ktorym backend startuje. Ten modul
dodaje nad nim warstwe w DATA_DIR/llm_keys.json (0600):

    keys     klucze API dodane z interfejsu, po id providera
    models   model wybrany dla providera (gdy inny niz domyslny presetu)
    active   id providera, z ktorego agent korzysta teraz (pusty = bazowy)
    chosen   czy uzytkownik przeszedl juz ekran wyboru

Provider jest "gotowy", gdy ma klucz: dodany z interfejsu, z .env (LLM_API_KEY dla bazowego)
albo ze zmiennej presetu (GEMINI_API_KEY, OPENAI_API_KEY...). Wybor jest wspolny dla calego
agenta — po przelaczeniu z nowego providera korzystaja tez Telegram, CLI, rutyny i workery.

Klucze nigdy nie wracaja do klienta: `listing()` podaje tylko, czy klucz jest i skad.
Bez importu `settings` (testowalne przez DATA_DIR), jak core/memory.py.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from typing import Any, Mapping

from backend.config.providers import LLMConfig, Provider, all_providers, privacy_note, provider_notes, user_providers_path
from backend.core import memory, runtime
from backend.core.i18n import tr

STORE = "llm_keys.json"
MAX_KEY = 512
MAX_MODEL = 200
_MODEL = re.compile(r"^[\w./:@+-]+$")


class LlmError(ValueError):
    """Odrzucona zmiana providera (nieznany provider, brak klucza, zly model)."""


def store_path():
    return memory.data_dir() / STORE


def load() -> dict[str, Any]:
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    def strings(name: str) -> dict[str, str]:
        value = raw.get(name)
        return {str(k): str(v) for k, v in value.items() if v} if isinstance(value, dict) else {}

    return {"keys": strings("keys"), "models": strings("models"),
            "active": str(raw.get("active") or ""), "chosen": bool(raw.get("chosen"))}


def _save(state: dict[str, Any]) -> None:
    path = store_path()
    memory._write_atomic(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)      # klucze API


def _providers(env: Mapping[str, str]) -> dict[str, Provider]:
    return all_providers(user_providers_path(env))


def _key(provider_id: str, provider: Provider | None, base: LLMConfig | None, env: Mapping[str, str],
         state: dict[str, Any]) -> tuple[str, str]:
    """(klucz, zrodlo) — zrodlo: "web" (dodany z interfejsu), "env" (.env) albo ""."""
    stored = state["keys"].get(provider_id, "")
    if stored:
        return stored, "web"
    if base is not None and base.provider_id == provider_id and base.api_key:
        return base.api_key, "env"
    for name in (provider.key_envs if provider else ()):
        value = (env.get(name) or "").strip()
        if value:
            return value, "env"
    return "", ""


def _model(provider_id: str, provider: Provider | None, base: LLMConfig | None, state: dict[str, Any]) -> str:
    stored = state["models"].get(provider_id, "")
    if stored:
        return stored
    if base is not None and base.provider_id == provider_id:
        return base.model
    return provider.default_model if provider else ""


def _base_url(provider: Provider) -> str:
    # Presety uslug lokalnych (Ollama) wskazuja adres hosta widziany z kontenera — patrz settings.py.
    if "host.docker.internal" in provider.base_url and runtime.kind() == "native":
        return provider.base_url.replace("host.docker.internal", "127.0.0.1")
    return provider.base_url


def resolve(base: LLMConfig, env: Mapping[str, str] | None = None) -> LLMConfig:
    """Konfiguracja, z ktorej agent korzysta TERAZ. Niepelny albo nieznany wybor = provider bazowy."""
    env = os.environ if env is None else env
    state = load()
    active = state["active"] or base.provider_id
    if active == base.provider_id:
        key, _ = _key(active, None, base, env, state)
        return replace(base, api_key=key or base.api_key, model=_model(active, None, base, state))
    provider = _providers(env).get(active)
    if provider is None:
        return base
    key, _ = _key(active, provider, base, env, state)
    model = _model(active, provider, base, state)
    if not model or (provider.requires_key and not key):
        return base
    # reasoning_effort z .env dotyczy modelu bazowego — innemu providerowi go nie wysylamy.
    return LLMConfig(provider_id=provider.id, provider_name=provider.name, base_url=_base_url(provider), api_key=key,
                     model=model, requires_key=provider.requires_key, reasoning_effort=None, timeout=base.timeout)


def endpoint(provider_id: str, base: LLMConfig, env: Mapping[str, str] | None = None) -> tuple[str, str, str, bool]:
    """(nazwa, adres, zapisany klucz, czy wymaga klucza) providera — do pobrania listy modeli."""
    env = os.environ if env is None else env
    state = load()
    if provider_id == base.provider_id:
        return base.provider_name, base.base_url, _key(provider_id, None, base, env, state)[0], base.requires_key
    provider = _providers(env).get(provider_id)
    if provider is None:
        raise LlmError(tr(f"Nie znam providera {provider_id!r}.", f"Unknown provider {provider_id!r}."))
    return provider.name, _base_url(provider), _key(provider_id, provider, base, env, state)[0], provider.requires_key


def listing(base: LLMConfig, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Stan dla interfejsu: providerzy (bez kluczy), aktywny, czy wybor juz byl."""
    env = os.environ if env is None else env
    state = load()
    current = resolve(base, env)
    providers = dict(_providers(env))
    rows: list[dict[str, Any]] = []

    def row(provider_id: str, provider: Provider | None) -> dict[str, Any]:
        key, source = _key(provider_id, provider, base, env, state)
        requires_key = provider.requires_key if provider else base.requires_key
        return {
            "id": provider_id,
            "name": provider.name if provider else base.provider_name,
            "model": _model(provider_id, provider, base, state),
            "default_model": provider.default_model if provider else base.model,
            "requires_key": requires_key,
            "has_key": bool(key),
            "key_source": source,
            # lokalny provider bez klucza (Ollama) liczy sie jako gotowy dopiero po wybraniu modelu
            "ready": bool(key) if requires_key else bool(provider_id == base.provider_id or state["models"].get(provider_id)),
            "key_url": provider.key_url if provider else "",
            "notes": provider_notes(provider) if provider else "",
            "privacy": privacy_note(provider_id) if provider else "",
            "base": provider_id == base.provider_id,
            "active": provider_id == current.provider_id,
        }

    if base.provider_id not in providers and base.enabled:   # wlasny endpoint z LLM_BASE_URL, spoza presetow
        rows.append(row(base.provider_id, None))
    rows.extend(row(provider_id, provider) for provider_id, provider in providers.items())
    # gotowi na gorze, aktywny pierwszy; reszta w kolejnosci presetow
    rows.sort(key=lambda r: (not r["active"], not r["ready"]))
    return {"providers": rows, "chosen": state["chosen"],
            "active": {"id": current.provider_id, "name": current.provider_name, "model": current.model}}


def clean_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) > MAX_KEY or any(ch.isspace() or ord(ch) < 32 for ch in key):
        raise LlmError(tr("Klucz API ma niepoprawny format (spacje albo znaki sterujace).",
                          "The API key has an invalid format (whitespace or control characters)."))
    return key


def clean_model(model: str) -> str:
    model = (model or "").strip()
    if model and (len(model) > MAX_MODEL or not _MODEL.match(model)):
        raise LlmError(tr(f"Niepoprawna nazwa modelu: {model[:60]!r}.", f"Invalid model name: {model[:60]!r}."))
    return model


def select(provider_id: str, base: LLMConfig, *, key: str = "", model: str = "",
           env: Mapping[str, str] | None = None) -> LLMConfig:
    """
    Ustawia providera jako aktywnego (opcjonalnie zapisujac nowy klucz i model) i oznacza wybor jako dokonany.
    Klucz powinien byc juz sprawdzony u providera — tu jest tylko walidacja formatu.
    """
    env = os.environ if env is None else env
    key, model = clean_key(key), clean_model(model)
    provider = _providers(env).get(provider_id)
    if provider is None and provider_id != base.provider_id:
        raise LlmError(tr(f"Nie znam providera {provider_id!r}.", f"Unknown provider {provider_id!r}."))
    state = load()
    if key:
        state["keys"][provider_id] = key
    if model:
        state["models"][provider_id] = model
    name = provider.name if provider else base.provider_name
    requires_key = provider.requires_key if provider else base.requires_key
    if requires_key and not _key(provider_id, provider, base, env, state)[0]:
        raise LlmError(tr(f"Provider {name} nie ma jeszcze klucza API.", f"Provider {name} has no API key yet."))
    if not _model(provider_id, provider, base, state):
        raise LlmError(tr(f"Wybierz model dla providera {name}.", f"Pick a model for provider {name}."))
    state["active"] = "" if provider_id == base.provider_id else provider_id
    state["chosen"] = True
    _save(state)
    return resolve(base, env)


def forget(provider_id: str, base: LLMConfig, env: Mapping[str, str] | None = None) -> bool:
    """Usuwa klucz dodany z interfejsu. Gdy provider zostaje bez klucza, agent wraca do bazowego."""
    env = os.environ if env is None else env
    state = load()
    if provider_id not in state["keys"]:
        return False
    del state["keys"][provider_id]
    provider = _providers(env).get(provider_id)
    if state["active"] == provider_id and not _key(provider_id, provider, base, env, state)[0]:
        state["active"] = ""
    _save(state)
    return True
