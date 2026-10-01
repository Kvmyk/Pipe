"""
Kreator konfiguracji providera LLM dla Pipe.

Uruchom z katalogu glownego repozytorium:

    python3 -m backend.configure              # interaktywny kreator
    python3 -m backend.configure --check      # sprawdz obecna konfiguracje
    python3 -m backend.configure --models     # aktualne modele obecnego providera
    python3 -m backend.configure --providers  # lista dostepnych providerow
    python3 -m backend.configure --from-env   # bez pytan: zapisz .env ze zmiennych srodowiska
                                              # (cloud-init, CI, scripts/install-server.sh)

Kreator wybiera providera, pobiera aktualna liste modeli z jego endpointu
/models, sprawdza na zywo, czy wybrany model obsluguje tool calling (bez tego
agent nie dziala), i zapisuje backend/.env. Uzywa wylacznie biblioteki
standardowej — dziala na swiezym serwerze, przed `docker-compose up`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from backend.config.providers import (
    NO_KEY_PLACEHOLDER,
    LLMConfig,
    Provider,
    ProviderConfigError,
    all_providers,
    chat_model_ids,
    model_available,
    normalize_base_url,
    provider_notes,
    resolve_llm_config,
    save_user_provider,
    user_providers_path,
)
from backend.core.i18n import load_env_lang, tr

BACKEND_DIR = Path(__file__).resolve().parent
ENV_PATH = BACKEND_DIR / ".env"
ENV_EXAMPLE_PATH = BACKEND_DIR / ".env.example"

HTTP_TIMEOUT_SECONDS = 60
MAX_LISTED_MODELS = 15


# ─── Plik .env ──────────────────────────────────────────────────────────────

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """Parsuje KEY=VALUE, pomija komentarze i puste linie, zdejmuje cudzyslowy."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def render_env(text: str, updates: dict[str, str], remove: set[str] = frozenset()) -> str:
    """
    Podmienia wartosci w tekscie .env, zachowujac komentarze i kolejnosc.
    Klucze z `remove` sa usuwane, brakujace klucze z `updates` dopisywane na koncu.
    Zakomentowane linie (# KEY=...) nie sa ruszane.
    """
    pending = dict(updates)
    out: list[str] = []
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        key = match.group(1) if match else None
        if key in remove:
            continue
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(out) + "\n"


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    return parse_env(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_env_file(updates: dict[str, str], remove: set[str], path: Path = ENV_PATH) -> None:
    """Aktualizuje .env (tworzy go z .env.example, jesli nie istnieje). Uprawnienia 600."""
    if path.exists():
        base = path.read_text(encoding="utf-8")
    elif ENV_EXAMPLE_PATH.exists():
        base = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    else:
        base = ""
    path.write_text(render_env(base, updates, remove), encoding="utf-8")
    path.chmod(0o600)


def effective_env() -> dict[str, str]:
    """Zmienne tak, jak zobaczy je backend: .env, nadpisane przez srodowisko procesu."""
    return {**read_env_file(), **os.environ}


# ─── HTTP (urllib, bez zaleznosci) ──────────────────────────────────────────

class ApiError(Exception):
    """Blad komunikacji z API providera."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def host_side_url(base_url: str) -> str:
    """
    Adres widziany z hosta. Presety dla uslug lokalnych (Ollama) wskazuja
    host.docker.internal, bo tak widzi je kontener — kreator na hoscie
    musi zamiast tego uzyc 127.0.0.1.
    """
    if Path("/.dockerenv").exists():
        return base_url
    return base_url.replace("host.docker.internal", "127.0.0.1")


def _api_url(base_url: str, path: str) -> str:
    return f"{normalize_base_url(host_side_url(base_url))}/{path}"


def _request(url: str, api_key: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {api_key or NO_KEY_PLACEHOLDER}",
            "Content-Type": "application/json",
            "User-Agent": "Pipe-configure",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code}: {_error_message(exc.read())}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiError(tr(f"Brak polaczenia z {url}: {exc.reason}", f"Cannot connect to {url}: {exc.reason}")) from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(tr(f"Niepoprawna odpowiedz z {url}: {exc}", f"Invalid response from {url}: {exc}")) from exc


def _error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        error = json.loads(text).get("error", text)
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error)[:300]
    except (json.JSONDecodeError, AttributeError):
        return text[:300]


def fetch_models(base_url: str, api_key: str) -> list[str]:
    """Aktualne modele czatu providera, najnowsze najpierw."""
    response = _request(_api_url(base_url, "models"), api_key)
    entries = response.get("data", []) if isinstance(response, dict) else response
    return chat_model_ids([e for e in entries if isinstance(e, dict)])


_PING_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Testowe narzedzie. Wywolaj je, gdy uzytkownik o to poprosi.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def interpret_tool_test(response: dict[str, Any]) -> tuple[bool, str]:
    """Czy odpowiedz na zapytanie testowe zawiera wywolanie narzedzia."""
    choices = response.get("choices") or []
    if not choices:
        return False, tr("Provider zwrocil pusta odpowiedz.", "The provider returned an empty response.")
    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
        return True, tr("Model wywolal narzedzie — tool calling dziala.", "The model called the tool — tool calling works.")
    reply = (message.get("content") or "").strip().replace("\n", " ")
    return False, tr(f"Model odpowiedzial tekstem zamiast wywolac narzedzie: {reply[:120]!r}",
                     f"The model answered with text instead of calling the tool: {reply[:120]!r}")


def test_tool_calling(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Jestes testem integracji. Wykonuj polecenia doslownie."},
            {"role": "user", "content": "Wywolaj narzedzie ping."},
        ],
        "tools": [_PING_TOOL],
    }
    try:
        return interpret_tool_test(_request(_api_url(base_url, "chat/completions"), api_key, payload))
    except ApiError as exc:
        return False, str(exc)


# ─── Interakcja ─────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes_no(prompt: str, default: bool) -> bool:
    answer = ask(f"{prompt} ({tr('T/n', 'Y/n') if default else tr('t/N', 'y/N')})").lower()
    return default if not answer else answer in ("t", "tak", "y", "yes")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or tr("wlasny", "custom")


def choose_provider(providers: dict[str, Provider], current_id: str) -> Provider | None:
    """Zwraca wybranego providera albo None, gdy uzytkownik chce dodac wlasnego."""
    items = list(providers.values())
    print(tr("\nWybierz providera LLM:\n", "\nChoose an LLM provider:\n"))
    for i, p in enumerate(items, start=1):
        marker = tr("  (obecny)", "  (current)") if p.id == current_id else ""
        tag = "" if p.builtin else tr("  [wlasny]", "  [custom]")
        print(f"  {i:2}) {p.name:<22}{tag}{marker}")
        if provider_notes(p):
            print(f"      {provider_notes(p)}")
    custom_index = len(items) + 1
    print(tr(f"  {custom_index:2}) Inny — dowolny endpoint zgodny z OpenAI (vLLM, LM Studio, LiteLLM, Azure...)\n",
             f"  {custom_index:2}) Other — any OpenAI-compatible endpoint (vLLM, LM Studio, LiteLLM, Azure...)\n"))

    default_index = next((i for i, p in enumerate(items, start=1) if p.id == current_id), 1)
    while True:
        answer = ask(tr("Numer", "Number"), str(default_index))
        if answer.isdigit() and 1 <= int(answer) <= custom_index:
            index = int(answer)
            return None if index == custom_index else items[index - 1]
        print(tr("  Podaj numer z listy.", "  Enter a number from the list."))


def create_custom_provider(path: Path) -> Provider:
    print(tr("\nNowy provider — wystarczy adres API zgodnego z OpenAI (zwykle konczy sie na /v1).",
             "\nNew provider — the address of an OpenAI-compatible API is enough (it usually ends with /v1)."))
    name = ask(tr("Nazwa (np. Moj vLLM)", "Name (e.g. My vLLM)"), tr("Wlasny endpoint", "Custom endpoint"))
    while True:
        base_url = ask(tr("Adres API (np. http://192.168.1.10:8000/v1)", "API address (e.g. http://192.168.1.10:8000/v1)"))
        if base_url.startswith(("http://", "https://")):
            break
        print(tr("  Adres musi zaczynac sie od http:// lub https://", "  The address must start with http:// or https://"))
    provider = Provider(
        id=slugify(ask(tr("Identyfikator (do LLM_PROVIDER)", "Identifier (for LLM_PROVIDER)"), slugify(name))),
        name=name,
        base_url=base_url,
        requires_key=ask_yes_no(tr("Czy endpoint wymaga klucza API?", "Does the endpoint need an API key?"), True),
        builtin=False,
    )
    save_user_provider(provider, path)
    print(tr(f"  Zapisano providera '{provider.id}' w {path}", f"  Saved provider '{provider.id}' to {path}"))
    return provider


def ask_api_key(provider: Provider, current: dict[str, str]) -> str | None:
    """Zwraca nowy klucz, pusty string (brak klucza) albo None (zostaw obecny)."""
    existing = current.get("LLM_API_KEY", "") if current.get("LLM_PROVIDER") == provider.id else ""
    if not existing:
        existing = next((os.environ[n] for n in provider.key_envs if os.environ.get(n)), "")

    if provider.key_url:
        print(tr(f"\nKlucz API zdobedziesz tutaj: {provider.key_url}", f"\nGet an API key here: {provider.key_url}"))
    if existing:
        key = getpass.getpass(tr("Klucz API [Enter = zostaw obecny]: ", "API key [Enter = keep the current one]: ")).strip()
        return key or None
    if not provider.requires_key:
        return getpass.getpass(tr("Klucz API [Enter = brak]: ", "API key [Enter = none]: ")).strip()
    while True:
        key = getpass.getpass(tr("Klucz API: ", "API key: ")).strip()
        if key:
            return key
        print(tr("  Ten provider wymaga klucza.", "  This provider needs a key."))


def choose_model(provider: Provider, api_key: str) -> str:
    print(tr(f"\nPobieram aktualna liste modeli od {provider.name}...",
             f"\nFetching the current model list from {provider.name}..."))
    try:
        models = fetch_models(provider.base_url, api_key)
    except ApiError as exc:
        print(tr(f"  Nie udalo sie pobrac listy: {exc}", f"  Could not fetch the list: {exc}"))
        if exc.status in (401, 403):
            print(tr("  Wyglada na to, ze klucz API jest nieprawidlowy.", "  The API key looks invalid."))
        models = []

    default = provider.default_model
    if not models:
        while True:
            model = ask(tr("Podaj identyfikator modelu", "Enter the model identifier"), default)
            if model:
                return model

    if not default or not model_available(default, models):
        default = models[0]

    shown = models[:MAX_LISTED_MODELS]
    print(tr(f"\nModele z obsluga czatu ({len(models)}, najnowsze najpierw):\n",
             f"\nChat models ({len(models)}, newest first):\n"))
    for i, model in enumerate(shown, start=1):
        print(f"  {i:2}) {model}{tr('  (zalecany)', '  (recommended)') if model == default else ''}")
    if len(models) > len(shown):
        print(tr(f"      ... i {len(models) - len(shown)} wiecej — mozesz wpisac dowolny identyfikator",
                 f"      ... and {len(models) - len(shown)} more — you can type any identifier"))

    answer = ask(tr("\nNumer albo identyfikator modelu", "\nNumber or model identifier"), default)
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        return shown[int(answer) - 1]
    if not model_available(answer, models):
        print(tr(f"  Uwaga: {answer!r} nie ma na liscie providera — sprawdzimy go testem.",
                 f"  Note: {answer!r} is not on the provider's list — the test will check it."))
    return answer


def run_wizard() -> int:
    current = read_env_file()
    language = choose_language(current)

    print(tr("\nPipe — konfiguracja providera LLM", "\nPipe — LLM provider setup"))
    print("=" * 40)
    providers_file = user_providers_path(effective_env())
    try:
        providers = all_providers(providers_file)
    except ProviderConfigError as exc:
        print(f"[{tr('BLAD', 'ERROR')}] {exc}")
        return 1

    provider = choose_provider(providers, current.get("LLM_PROVIDER", ""))
    if provider is None:
        provider = create_custom_provider(providers_file)

    new_key = ask_api_key(provider, current)
    api_key = current.get("LLM_API_KEY", "") if new_key is None else new_key
    if new_key is None and not api_key:
        api_key = next((os.environ[n] for n in provider.key_envs if os.environ.get(n)), "")

    while True:
        model = choose_model(provider, api_key)
        print(tr(f"\nTestuje tool calling na modelu {model}...", f"\nTesting tool calling on model {model}..."))
        ok, message = test_tool_calling(provider.base_url, api_key, model)
        print(f"  {'OK' if ok else 'PROBLEM'}: {message}")
        if ok:
            break
        print(tr("\nBez dzialajacego tool callingu agent nie wykona zadnej komendy.",
                 "\nWithout working tool calling the agent cannot run any command."))
        choice = ask(tr("[w]ybierz inny model / [z]apisz mimo to / [a]nuluj",
                        "[c]hoose another model / [s]ave anyway / [a]bort"), tr("w", "c")).lower()
        if choice.startswith(("z", "s")):
            break
        if choice.startswith("a"):
            print(tr("Anulowano — .env bez zmian.", "Aborted — .env unchanged."))
            return 1

    updates = {"LLM_PROVIDER": provider.id, "LLM_MODEL": model, "PIPE_LANG": language}
    if new_key is not None:
        updates["LLM_API_KEY"] = new_key
    token = ensure_agent_token(current)
    if token:
        updates["AGENT_TOKEN"] = token
    # Adres pochodzi teraz z presetu — stare LLM_BASE_URL by go nadpisywalo.
    try:
        write_env_file(updates, remove={"LLM_BASE_URL"})
    except OSError as exc:
        print(tr(f"[BLAD] Nie moge zapisac {ENV_PATH}: {exc}", f"[ERROR] Cannot write {ENV_PATH}: {exc}"))
        print(tr("Uruchom kreator na hoscie, w katalogu repozytorium (w kontenerze .env jest tylko do odczytu).",
                 "Run the wizard on the host, in the repository directory (inside the container .env is read-only)."))
        return 1

    print(tr(f"\nZapisano {ENV_PATH}", f"\nSaved {ENV_PATH}"))
    print(f"  Provider: {provider.name}")
    print(f"  Model:    {model}")
    print(tr(f"  Jezyk:    {language}", f"  Language: {language}"))
    if token:
        print(tr("  AGENT_TOKEN: wygenerowany (ten sam podaj klientom: pipe --token ..., clients/telegram/.env)",
                 "  AGENT_TOKEN: generated (give the same one to clients: pipe --token ..., clients/telegram/.env)"))
    print(tr("\nDalej:", "\nNext:"))
    print("  cd backend && docker-compose up -d --build")
    print(tr("  (jesli agent juz dziala, wystarczy: docker-compose restart vps-agent)",
             "  (if the agent is already running: docker-compose restart vps-agent)"))
    return 0


def choose_language(current: dict[str, str]) -> str:
    """Pyta o jezyk Pipe (PIPE_LANG) i od razu przelacza na niego kreator."""
    existing = (os.environ.get("PIPE_LANG") or current.get("PIPE_LANG") or "pl").strip().lower()
    default = "2" if existing.startswith("en") else "1"
    print("Jezyk / Language:\n  1) polski\n  2) English")
    answer = ask("Numer / Number", default).lower()
    language = "en" if answer in ("2", "en", "english") else "pl"
    os.environ["PIPE_LANG"] = language
    return language


def ensure_agent_token(current: dict[str, str]) -> str | None:
    """
    Zwraca nowy AGENT_TOKEN do zapisania, gdy go jeszcze nie ma. Token chroni
    socket i port TCP; bez niego kazdy lokalny proces moze sterowac agentem.
    None = token juz jest (w .env albo w srodowisku), nic nie zmieniamy.
    """
    import secrets
    if current.get("AGENT_TOKEN", "").strip() or os.environ.get("AGENT_TOKEN", "").strip():
        return None
    return secrets.token_hex(24)


# ─── Tryby nieinteraktywne ──────────────────────────────────────────────────

def _load_config() -> LLMConfig | None:
    try:
        return resolve_llm_config(effective_env())
    except ProviderConfigError as exc:
        print(f"[{tr('BLAD', 'ERROR')}] {exc}")
        return None


def run_check() -> int:
    config = _load_config()
    if config is None:
        return 1

    print(f"Provider: {config.provider_name} ({config.provider_id})")
    print(tr(f"Adres:    {config.base_url}", f"Address:  {config.base_url}"))
    print(f"Model:    {config.model}")
    if config.requires_key and not config.api_key:
        print(tr("[BLAD] Brak klucza API. Uruchom: python3 -m backend.configure",
                 "[ERROR] No API key. Run: python3 -m backend.configure"))
        return 1

    try:
        models = fetch_models(config.base_url, config.api_key)
        if model_available(config.model, models):
            print(tr(f"[OK] Model jest na liscie providera ({len(models)} modeli czatu).",
                     f"[OK] The model is on the provider's list ({len(models)} chat models)."))
        elif models:
            print(tr(f"[UWAGA] Modelu nie ma na liscie providera — mogl zostac wycofany. Najnowsze: {', '.join(models[:5])}",
                     f"[WARNING] The model is not on the provider's list — it may have been retired. Newest: {', '.join(models[:5])}"))
    except ApiError as exc:
        print(tr(f"[UWAGA] Nie udalo sie pobrac listy modeli: {exc}", f"[WARNING] Could not fetch the model list: {exc}"))

    ok, message = test_tool_calling(config.base_url, config.api_key, config.model)
    print(f"[{'OK' if ok else tr('BLAD', 'ERROR')}] {message}")
    return 0 if ok else 1


# Zmienne, ktore --from-env przepisuje ze srodowiska do backend/.env.
FROM_ENV_KEYS = (
    "LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_REASONING_EFFORT", "LLM_TIMEOUT",
    "AGENT_TOKEN", "WORKER_MODEL", "PIPE_RUNTIME", "TCP_HOST", "WATCH_ENABLED", "WATCH_INTERVAL",
    "WATCH_DISK_PCT", "WATCH_MEM_PCT", "VIBE_EVERY", "PIPE_LANG",
)


def run_from_env(test: bool = False) -> int:
    """
    Nieinteraktywna konfiguracja: przepisuje ustawione zmienne srodowiska do
    backend/.env i sprawdza, czy da sie z nich zbudowac konfiguracje LLM.
    Klucz providera mozna tez podac jego wlasna zmienna (np. GEMINI_API_KEY).
    """
    updates = {key: os.environ[key] for key in FROM_ENV_KEYS if os.environ.get(key, "").strip()}
    if not updates.get("LLM_PROVIDER") and not updates.get("LLM_BASE_URL"):
        print(tr("[BLAD] Ustaw co najmniej LLM_PROVIDER (np. gemini, openai, openrouter, ollama) albo LLM_BASE_URL.",
                 "[ERROR] Set at least LLM_PROVIDER (e.g. gemini, openai, openrouter, ollama) or LLM_BASE_URL."))
        return 1
    try:
        config = resolve_llm_config({**read_env_file(ENV_PATH), **os.environ})
    except ProviderConfigError as exc:
        print(f"[{tr('BLAD', 'ERROR')}] {exc}")
        return 1
    if config.requires_key and not config.api_key:
        print(tr(f"[BLAD] Brak klucza API dla {config.provider_name} — ustaw LLM_API_KEY.",
                 f"[ERROR] No API key for {config.provider_name} — set LLM_API_KEY."))
        return 1
    # Wygeneruj AGENT_TOKEN, jesli nikt go nie podal — port i socket nie moga zostac bez ochrony.
    token = ensure_agent_token(read_env_file(ENV_PATH))
    if token:
        updates["AGENT_TOKEN"] = token
    try:
        write_env_file(updates, set(), ENV_PATH)
    except OSError as exc:
        print(tr(f"[BLAD] Nie moge zapisac {ENV_PATH}: {exc}", f"[ERROR] Cannot write {ENV_PATH}: {exc}"))
        return 1
    print(tr("Zapisano", "Saved") + f" {ENV_PATH}: {', '.join(sorted(k for k in updates if k != 'LLM_API_KEY'))}"
          + (" + LLM_API_KEY" if "LLM_API_KEY" in updates else ""))
    print(f"  Provider: {config.provider_name}, model: {config.model}")
    if token:
        print(tr("  AGENT_TOKEN: wygenerowany (podaj klientom, np. w clients/telegram/.env)",
                 "  AGENT_TOKEN: generated (give it to clients, e.g. in clients/telegram/.env)"))
    return run_check() if test else 0


def run_models() -> int:
    config = _load_config()
    if config is None:
        return 1
    try:
        models = fetch_models(config.base_url, config.api_key)
    except ApiError as exc:
        print(f"[{tr('BLAD', 'ERROR')}] {exc}")
        return 1
    print(tr(f"Modele czatu u {config.provider_name}, najnowsze najpierw:\n",
             f"Chat models at {config.provider_name}, newest first:\n"))
    for model in models:
        print(f"  {model}{tr('  <- obecny', '  <- current') if model_available(model, [config.model]) else ''}")
    return 0


def run_providers() -> int:
    try:
        providers = all_providers(user_providers_path(effective_env()))
    except ProviderConfigError as exc:
        print(f"[{tr('BLAD', 'ERROR')}] {exc}")
        return 1
    for p in providers.values():
        tag = "" if p.builtin else tr("  [wlasny]", "  [custom]")
        print(f"{p.id:<12} {p.name}{tag}")
        print(f"{'':<12} {p.base_url}")
        if p.default_model:
            print(f"{'':<12} {tr('domyslny model', 'default model')}: {p.default_model}")
        if provider_notes(p):
            print(f"{'':<12} {provider_notes(p)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env_lang(ENV_PATH)
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.configure",
        description=tr("Kreator konfiguracji providera LLM dla Pipe.", "LLM provider setup wizard for Pipe."),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help=tr("sprawdz obecna konfiguracje (lista modeli + test tool callingu)",
                              "check the current configuration (model list + tool-calling test)"))
    mode.add_argument("--models", action="store_true",
                      help=tr("pokaz aktualne modele obecnego providera", "show the current provider's models"))
    mode.add_argument("--providers", action="store_true",
                      help=tr("pokaz dostepnych providerow", "show the available providers"))
    mode.add_argument("--from-env", action="store_true",
                      help=tr("bez pytan: zapisz .env ze zmiennych LLM_PROVIDER, LLM_API_KEY, LLM_MODEL... (automatyzacja)",
                              "no questions: write .env from LLM_PROVIDER, LLM_API_KEY, LLM_MODEL... (automation)"))
    parser.add_argument("--test", action="store_true",
                        help=tr("z --from-env: od razu sprawdz polaczenie i tool calling",
                                "with --from-env: check the connection and tool calling right away"))
    args = parser.parse_args(argv)

    try:
        if args.check:
            return run_check()
        if args.models:
            return run_models()
        if args.providers:
            return run_providers()
        if args.from_env:
            return run_from_env(test=args.test)
        return run_wizard()
    except KeyboardInterrupt:
        print(tr("\nPrzerwano.", "\nInterrupted."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
