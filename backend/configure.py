"""
Kreator konfiguracji providera LLM dla Pipe.

Uruchom z katalogu glownego repozytorium:

    python3 -m backend.configure              # interaktywny kreator
    python3 -m backend.configure --check      # sprawdz obecna konfiguracje
    python3 -m backend.configure --models     # aktualne modele obecnego providera
    python3 -m backend.configure --providers  # lista dostepnych providerow

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
    resolve_llm_config,
    save_user_provider,
    user_providers_path,
)

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
        raise ApiError(f"Brak polaczenia z {url}: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(f"Niepoprawna odpowiedz z {url}: {exc}") from exc


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
        return False, "Provider zwrocil pusta odpowiedz."
    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
        return True, "Model wywolal narzedzie — tool calling dziala."
    reply = (message.get("content") or "").strip().replace("\n", " ")
    return False, f"Model odpowiedzial tekstem zamiast wywolac narzedzie: {reply[:120]!r}"


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
    answer = ask(f"{prompt} ({'T/n' if default else 't/N'})").lower()
    return default if not answer else answer in ("t", "tak", "y", "yes")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "wlasny"


def choose_provider(providers: dict[str, Provider], current_id: str) -> Provider | None:
    """Zwraca wybranego providera albo None, gdy uzytkownik chce dodac wlasnego."""
    items = list(providers.values())
    print("\nWybierz providera LLM:\n")
    for i, p in enumerate(items, start=1):
        marker = "  (obecny)" if p.id == current_id else ""
        tag = "" if p.builtin else "  [wlasny]"
        print(f"  {i:2}) {p.name:<22}{tag}{marker}")
        if p.notes:
            print(f"      {p.notes}")
    custom_index = len(items) + 1
    print(f"  {custom_index:2}) Inny — dowolny endpoint zgodny z OpenAI (vLLM, LM Studio, LiteLLM, Azure...)\n")

    default_index = next((i for i, p in enumerate(items, start=1) if p.id == current_id), 1)
    while True:
        answer = ask("Numer", str(default_index))
        if answer.isdigit() and 1 <= int(answer) <= custom_index:
            index = int(answer)
            return None if index == custom_index else items[index - 1]
        print("  Podaj numer z listy.")


def create_custom_provider(path: Path) -> Provider:
    print("\nNowy provider — wystarczy adres API zgodnego z OpenAI (zwykle konczy sie na /v1).")
    name = ask("Nazwa (np. Moj vLLM)", "Wlasny endpoint")
    while True:
        base_url = ask("Adres API (np. http://192.168.1.10:8000/v1)")
        if base_url.startswith(("http://", "https://")):
            break
        print("  Adres musi zaczynac sie od http:// lub https://")
    provider = Provider(
        id=slugify(ask("Identyfikator (do LLM_PROVIDER)", slugify(name))),
        name=name,
        base_url=base_url,
        requires_key=ask_yes_no("Czy endpoint wymaga klucza API?", True),
        builtin=False,
    )
    save_user_provider(provider, path)
    print(f"  Zapisano providera '{provider.id}' w {path}")
    return provider


def ask_api_key(provider: Provider, current: dict[str, str]) -> str | None:
    """Zwraca nowy klucz, pusty string (brak klucza) albo None (zostaw obecny)."""
    existing = current.get("LLM_API_KEY", "") if current.get("LLM_PROVIDER") == provider.id else ""
    if not existing:
        existing = next((os.environ[n] for n in provider.key_envs if os.environ.get(n)), "")

    if provider.key_url:
        print(f"\nKlucz API zdobedziesz tutaj: {provider.key_url}")
    if existing:
        key = getpass.getpass("Klucz API [Enter = zostaw obecny]: ").strip()
        return key or None
    if not provider.requires_key:
        return getpass.getpass("Klucz API [Enter = brak]: ").strip()
    while True:
        key = getpass.getpass("Klucz API: ").strip()
        if key:
            return key
        print("  Ten provider wymaga klucza.")


def choose_model(provider: Provider, api_key: str) -> str:
    print(f"\nPobieram aktualna liste modeli od {provider.name}...")
    try:
        models = fetch_models(provider.base_url, api_key)
    except ApiError as exc:
        print(f"  Nie udalo sie pobrac listy: {exc}")
        if exc.status in (401, 403):
            print("  Wyglada na to, ze klucz API jest nieprawidlowy.")
        models = []

    default = provider.default_model
    if not models:
        while True:
            model = ask("Podaj identyfikator modelu", default)
            if model:
                return model

    if not default or not model_available(default, models):
        default = models[0]

    shown = models[:MAX_LISTED_MODELS]
    print(f"\nModele z obsluga czatu ({len(models)}, najnowsze najpierw):\n")
    for i, model in enumerate(shown, start=1):
        print(f"  {i:2}) {model}{'  (zalecany)' if model == default else ''}")
    if len(models) > len(shown):
        print(f"      ... i {len(models) - len(shown)} wiecej — mozesz wpisac dowolny identyfikator")

    answer = ask("\nNumer albo identyfikator modelu", default)
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        return shown[int(answer) - 1]
    if not model_available(answer, models):
        print(f"  Uwaga: {answer!r} nie ma na liscie providera — sprawdzimy go testem.")
    return answer


def run_wizard() -> int:
    print("Pipe — konfiguracja providera LLM")
    print("=" * 40)

    current = read_env_file()
    providers_file = user_providers_path(effective_env())
    try:
        providers = all_providers(providers_file)
    except ProviderConfigError as exc:
        print(f"[BLAD] {exc}")
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
        print(f"\nTestuje tool calling na modelu {model}...")
        ok, message = test_tool_calling(provider.base_url, api_key, model)
        print(f"  {'OK' if ok else 'PROBLEM'}: {message}")
        if ok:
            break
        print("\nBez dzialajacego tool callingu agent nie wykona zadnej komendy.")
        choice = ask("[w]ybierz inny model / [z]apisz mimo to / [a]nuluj", "w").lower()
        if choice.startswith("z"):
            break
        if choice.startswith("a"):
            print("Anulowano — .env bez zmian.")
            return 1

    updates = {"LLM_PROVIDER": provider.id, "LLM_MODEL": model}
    if new_key is not None:
        updates["LLM_API_KEY"] = new_key
    # Adres pochodzi teraz z presetu — stare LLM_BASE_URL by go nadpisywalo.
    try:
        write_env_file(updates, remove={"LLM_BASE_URL"})
    except OSError as exc:
        print(f"[BLAD] Nie moge zapisac {ENV_PATH}: {exc}")
        print("Uruchom kreator na hoscie, w katalogu repozytorium (w kontenerze .env jest tylko do odczytu).")
        return 1

    print(f"\nZapisano {ENV_PATH}")
    print(f"  Provider: {provider.name}")
    print(f"  Model:    {model}")
    print("\nDalej:")
    print("  cd backend && docker-compose up -d --build")
    print("  (jesli agent juz dziala, wystarczy: docker-compose restart vps-agent)")
    return 0


# ─── Tryby nieinteraktywne ──────────────────────────────────────────────────

def _load_config() -> LLMConfig | None:
    try:
        return resolve_llm_config(effective_env())
    except ProviderConfigError as exc:
        print(f"[BLAD] {exc}")
        return None


def run_check() -> int:
    config = _load_config()
    if config is None:
        return 1

    print(f"Provider: {config.provider_name} ({config.provider_id})")
    print(f"Adres:    {config.base_url}")
    print(f"Model:    {config.model}")
    if config.requires_key and not config.api_key:
        print("[BLAD] Brak klucza API. Uruchom: python3 -m backend.configure")
        return 1

    try:
        models = fetch_models(config.base_url, config.api_key)
        if model_available(config.model, models):
            print(f"[OK] Model jest na liscie providera ({len(models)} modeli czatu).")
        elif models:
            print(f"[UWAGA] Modelu nie ma na liscie providera — mogl zostac wycofany. Najnowsze: {', '.join(models[:5])}")
    except ApiError as exc:
        print(f"[UWAGA] Nie udalo sie pobrac listy modeli: {exc}")

    ok, message = test_tool_calling(config.base_url, config.api_key, config.model)
    print(f"[{'OK' if ok else 'BLAD'}] {message}")
    return 0 if ok else 1


def run_models() -> int:
    config = _load_config()
    if config is None:
        return 1
    try:
        models = fetch_models(config.base_url, config.api_key)
    except ApiError as exc:
        print(f"[BLAD] {exc}")
        return 1
    print(f"Modele czatu u {config.provider_name}, najnowsze najpierw:\n")
    for model in models:
        print(f"  {model}{'  <- obecny' if model_available(model, [config.model]) else ''}")
    return 0


def run_providers() -> int:
    try:
        providers = all_providers(user_providers_path(effective_env()))
    except ProviderConfigError as exc:
        print(f"[BLAD] {exc}")
        return 1
    for p in providers.values():
        tag = "" if p.builtin else "  [wlasny]"
        print(f"{p.id:<12} {p.name}{tag}")
        print(f"{'':<12} {p.base_url}")
        if p.default_model:
            print(f"{'':<12} domyslny model: {p.default_model}")
        if p.notes:
            print(f"{'':<12} {p.notes}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.configure",
        description="Kreator konfiguracji providera LLM dla Pipe.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="sprawdz obecna konfiguracje (lista modeli + test tool callingu)")
    mode.add_argument("--models", action="store_true", help="pokaz aktualne modele obecnego providera")
    mode.add_argument("--providers", action="store_true", help="pokaz dostepnych providerow")
    args = parser.parse_args(argv)

    try:
        if args.check:
            return run_check()
        if args.models:
            return run_models()
        if args.providers:
            return run_providers()
        return run_wizard()
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
