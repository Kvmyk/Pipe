"""
Rejestr providerow LLM — presety endpointow zgodnych z OpenAI Chat Completions.

Modul jest celowo oparty wylacznie o biblioteke standardowa: importuje go
kreator `python3 -m backend.configure`, ktory ma dzialac na swiezym serwerze,
zanim zainstalujesz jakiekolwiek zaleznosci.

Skad sie bierze konfiguracja (od najwyzszego priorytetu):
  1. LLM_BASE_URL / LLM_MODEL / LLM_API_KEY z .env — jawne nadpisanie
  2. preset wskazany przez LLM_PROVIDER (wbudowany albo z pliku uzytkownika)
  3. brak LLM_PROVIDER, ale jest LLM_BASE_URL — dopasowanie po adresie
     (kompatybilnosc ze starymi plikami .env sprzed v0.5.0)
  4. nic nie ustawione — domyslny provider (Gemini, darmowy tier)

LLM_PROVIDER=none — Pipe bez modelu jezykowego: czuwanie, snapshoty, raport, audyt, /cofnij
i narzedzia MCP dzialaja, rozmowa czeka na providera dodanego pozniej (pipe web, /providerzy).

Wlasni providerzy: plik JSON wskazany przez LLM_PROVIDERS_FILE
(domyslnie backend/data/providers.json). Wpis o id wbudowanego providera
nadpisuje wbudowany preset — mozna tak poprawic nieaktualny adres bez
czekania na nowa wersje Pipe.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from backend.core.i18n import tr

DEFAULT_PROVIDER_ID = "gemini"
DEFAULT_TIMEOUT_SECONDS = 120.0
# LLM_PROVIDER=none: bez modelu jezykowego (patrz docstring modulu).
NO_LLM_ID = "none"

# Klient OpenAI wymaga niepustego klucza nawet dla endpointow bez autoryzacji.
NO_KEY_PLACEHOLDER = "brak-klucza"

_BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_USER_PROVIDERS_PATH = _BACKEND_DIR / "data" / "providers.json"


class ProviderConfigError(ValueError):
    """Bledna lub niekompletna konfiguracja providera LLM."""


@dataclass(frozen=True)
class Provider:
    """Preset endpointu zgodnego z OpenAI."""

    id: str
    name: str
    base_url: str
    # Pusty = brak rozsadnego domyslnego modelu (np. Ollama) — trzeba wybrac.
    default_model: str = ""
    # Alternatywne zmienne srodowiskowe z kluczem, sprawdzane gdy LLM_API_KEY jest pusty.
    key_envs: tuple[str, ...] = ()
    key_url: str = ""
    requires_key: bool = True
    notes: str = ""
    builtin: bool = True


@dataclass(frozen=True)
class LLMConfig:
    """Rozwiazana, gotowa do uzycia konfiguracja polaczenia z LLM."""

    provider_id: str
    provider_name: str
    base_url: str
    api_key: str
    model: str
    requires_key: bool
    reasoning_effort: str | None
    timeout: float

    @property
    def enabled(self) -> bool:
        """False dla LLM_PROVIDER=none — Pipe dziala bez modelu."""
        return self.provider_id != NO_LLM_ID


# ─── Wbudowani providerzy ───────────────────────────────────────────────────
# Adresy i modele zweryfikowane w dokumentacji providerow (wrzesien 2026).
# Modele domyslne to tylko punkt startowy — kreator pobiera aktualna liste
# z endpointu /models, wiec nowe modele sa widoczne bez aktualizacji Pipe.
BUILTIN_PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-3.8-flash",
        key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        key_url="https://aistudio.google.com/apikey",
        notes="Darmowy tier dla modeli Flash (Google moze z niego uczyc swoje modele).",
    ),
    Provider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5.6-terra",
        key_envs=("OPENAI_API_KEY",),
        key_url="https://platform.openai.com/api-keys",
        notes="GPT-6 wymaga Responses API do tool callingu — przez Chat Completions uzyj rodziny GPT-5.6.",
    ),
    Provider(
        id="anthropic",
        name="Anthropic Claude",
        base_url="https://api.anthropic.com/v1/",
        default_model="claude-sonnet-5",
        key_envs=("ANTHROPIC_API_KEY",),
        key_url="https://platform.claude.com/settings/keys",
        notes="Przez warstwe zgodnosci z OpenAI SDK; LLM_REASONING_EFFORT jest ignorowany.",
    ),
    Provider(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="google/gemini-3.8-flash",
        key_envs=("OPENROUTER_API_KEY",),
        key_url="https://openrouter.ai/keys",
        notes="Jeden klucz, kilkaset modeli (m.in. DeepSeek, Qwen, GLM, Kimi, Llama).",
    ),
    Provider(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-120b",
        key_envs=("GROQ_API_KEY",),
        key_url="https://console.groq.com/keys",
        notes="Bardzo szybka inferencja modeli open-weight.",
    ),
    Provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        key_envs=("DEEPSEEK_API_KEY",),
        key_url="https://platform.deepseek.com/api_keys",
    ),
    Provider(
        id="mistral",
        name="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-large-latest",
        key_envs=("MISTRAL_API_KEY",),
        key_url="https://console.mistral.ai/api-keys",
        notes="Provider z UE.",
    ),
    Provider(
        id="xai",
        name="xAI Grok",
        base_url="https://api.x.ai/v1",
        default_model="grok-4.6",
        key_envs=("XAI_API_KEY",),
        key_url="https://console.x.ai",
    ),
    Provider(
        id="zai",
        name="Z.ai (GLM)",
        base_url="https://api.z.ai/api/paas/v4/",
        default_model="glm-5.3",
        key_envs=("ZAI_API_KEY",),
        key_url="https://z.ai",
    ),
    Provider(
        id="kimi",
        name="Moonshot Kimi",
        base_url="https://api.moonshot.ai/v1",
        default_model="kimi-k3",
        key_envs=("MOONSHOT_API_KEY",),
        key_url="https://platform.kimi.ai",
    ),
    Provider(
        id="together",
        name="Together AI",
        base_url="https://api.together.ai/v1",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        key_envs=("TOGETHER_API_KEY",),
        key_url="https://api.together.ai",
    ),
    Provider(
        id="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        default_model="gpt-oss-120b",
        key_envs=("CEREBRAS_API_KEY",),
        key_url="https://cloud.cerebras.ai",
    ),
    Provider(
        id="fireworks",
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        key_envs=("FIREWORKS_API_KEY",),
        key_url="https://fireworks.ai",
    ),
    Provider(
        id="ollama",
        name="Ollama (local)",
        # Z wnetrza kontenera host jest widoczny jako host.docker.internal
        # (patrz extra_hosts w docker-compose.yml).
        base_url="http://host.docker.internal:11434/v1",
        requires_key=False,
        notes="Ollama musi nasluchiwac na 0.0.0.0 (OLLAMA_HOST=0.0.0.0), zeby kontener ja widzial.",
    ),
)


# Angielskie uwagi do presetow (PIPE_LANG=en) — po id providera.
NOTES_EN: dict[str, str] = {
    "gemini": "Free tier for Flash models (Google may use it to improve its models).",
    "openai": "GPT-6 needs the Responses API for tool calling — over Chat Completions use the GPT-5.6 family.",
    "anthropic": "Through the OpenAI SDK compatibility layer; LLM_REASONING_EFFORT is ignored.",
    "openrouter": "One key, several hundred models (DeepSeek, Qwen, GLM, Kimi, Llama and more).",
    "groq": "Very fast inference of open-weight models.",
    "mistral": "EU-based provider.",
    "ollama": "Ollama must listen on 0.0.0.0 (OLLAMA_HOST=0.0.0.0) so the container can reach it.",
}


def provider_notes(provider: Provider) -> str:
    """Uwagi do providera w jezyku Pipe."""
    if provider.builtin and provider.id in NOTES_EN:
        return tr(provider.notes, NOTES_EN[provider.id])
    return provider.notes


# Ostrzezenia o prywatnosci — agent wysyla providerowi logi, konfiguracje i wyniki komend z serwera
# (sekrety sa redagowane, ale reszta trafia do modelu). Kreator pokazuje je po wyborze providera.
PRIVACY_NOTES: dict[str, tuple[str, str]] = {
    "gemini": (
        "Prywatnosc: w darmowym tierze Gemini API Google moze uzywac tresci zapytan (u Ciebie: logow, "
        "konfiguracji i wynikow komend z serwera) do ulepszania swoich uslug, a czytac je moga ludzie. "
        "Na serwerze produkcyjnym wlacz platnosci w Google AI Studio (wtedy obowiazuja warunki platnego "
        "tieru) albo wybierz innego providera, np. lokalna Ollame.",
        "Privacy: on the free tier of the Gemini API Google may use prompt content (here: logs, configs "
        "and command output from your server) to improve its services, and humans may read it. "
        "On a production server enable billing in Google AI Studio (paid-tier terms apply then) "
        "or choose another provider, e.g. a local Ollama.",
    ),
}


def privacy_note(provider_id: str) -> str:
    """Ostrzezenie o prywatnosci dla providera (po id) albo pusty tekst."""
    return tr(*PRIVACY_NOTES[provider_id]) if provider_id in PRIVACY_NOTES else ""


# ─── Pliki uzytkownika ──────────────────────────────────────────────────────

def user_providers_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    custom = (env.get("LLM_PROVIDERS_FILE") or "").strip()
    return Path(custom) if custom else DEFAULT_USER_PROVIDERS_PATH


def load_user_providers(path: Path) -> list[Provider]:
    """
    Wczytuje providerow z pliku JSON. Brak pliku = brak wlasnych providerow.

    Akceptowany format: {"providers": [ {...}, ... ]} albo sama lista.
    Wpisy bez `id` lub `base_url` sa pomijane z ostrzezeniem na stderr.
    """
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(tr(f"Plik providerow {path} nie jest poprawnym JSON-em: {exc}",
                                     f"Providers file {path} is not valid JSON: {exc}")) from exc

    entries = raw.get("providers", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ProviderConfigError(tr(f"Plik providerow {path}: oczekiwano listy pod kluczem 'providers'.",
                                     f"Providers file {path}: expected a list under the 'providers' key."))

    known_fields = {f.name for f in fields(Provider)}
    providers: list[Provider] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("base_url"):
            print(tr(f"[providers] Pomijam wpis #{i} w {path}: wymagane pola 'id' i 'base_url'.",
                     f"[providers] Skipping entry #{i} in {path}: fields 'id' and 'base_url' are required."), file=sys.stderr)
            continue
        data = {k: v for k, v in entry.items() if k in known_fields}
        data["id"] = str(data["id"]).strip().lower()
        data.setdefault("name", data["id"])
        data["key_envs"] = tuple(data.get("key_envs") or ())
        data["builtin"] = False
        providers.append(Provider(**data))
    return providers


def save_user_provider(provider: Provider, path: Path) -> None:
    """Dodaje lub aktualizuje (po `id`) providera w pliku uzytkownika."""
    existing = [p for p in load_user_providers(path) if p.id != provider.id]
    entries = []
    for p in [*existing, provider]:
        data = asdict(p)
        data.pop("builtin")
        data["key_envs"] = list(data["key_envs"])
        entries.append(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"providers": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def all_providers(path: Path | None = None) -> dict[str, Provider]:
    """Wbudowani + wlasni providerzy. Wlasny wpis nadpisuje wbudowany o tym samym id."""
    providers = {p.id: p for p in BUILTIN_PROVIDERS}
    for p in load_user_providers(path or user_providers_path()):
        providers[p.id] = p
    return providers


# ─── Rozwiazywanie konfiguracji ─────────────────────────────────────────────

def normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


def find_provider_by_url(base_url: str, providers: Mapping[str, Provider]) -> Provider | None:
    target = normalize_base_url(base_url)
    for p in providers.values():
        if normalize_base_url(p.base_url) == target:
            return p
    return None


def resolve_llm_config(env: Mapping[str, str], path: Path | None = None) -> LLMConfig:
    """Wylicza konfiguracje LLM ze zmiennych srodowiskowych (patrz docstring modulu)."""
    providers = all_providers(path or user_providers_path(env))

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    provider_id = get("LLM_PROVIDER").lower()
    explicit_url = get("LLM_BASE_URL")

    if provider_id == NO_LLM_ID:
        return no_llm_config()
    if provider_id:
        provider = providers.get(provider_id)
        if provider is None:
            raise ProviderConfigError(tr(
                f"Nieznany provider LLM_PROVIDER={provider_id!r}. "
                f"Dostepni: {', '.join(sorted(providers))}. "
                "Wlasnego providera dodasz kreatorem: python3 -m backend.configure",
                f"Unknown provider LLM_PROVIDER={provider_id!r}. "
                f"Available: {', '.join(sorted(providers))}. "
                "Add your own provider with the wizard: python3 -m backend.configure"
            ))
    elif explicit_url:
        provider = find_provider_by_url(explicit_url, providers)
    else:
        provider = providers[DEFAULT_PROVIDER_ID]

    base_url = explicit_url or (provider.base_url if provider else "")
    model = get("LLM_MODEL") or (provider.default_model if provider else "")
    if not model:
        name = provider.name if provider else base_url
        raise ProviderConfigError(tr(
            f"Brak modelu dla providera {name}. Ustaw LLM_MODEL w .env "
            "albo wybierz model kreatorem: python3 -m backend.configure",
            f"No model for provider {name}. Set LLM_MODEL in .env "
            "or pick a model with the wizard: python3 -m backend.configure"
        ))

    # Endpoint spoza presetow traktujemy jako niewymagajacy klucza — jesli go
    # jednak wymaga, provider zwroci czytelny blad 401 przy pierwszym zapytaniu.
    requires_key = provider.requires_key if provider else False
    api_key = get("LLM_API_KEY")
    if not api_key and provider:
        api_key = next((get(name) for name in provider.key_envs if get(name)), "")

    timeout_raw = get("LLM_TIMEOUT")
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError as exc:
        raise ProviderConfigError(tr(f"LLM_TIMEOUT musi byc liczba sekund, a jest: {timeout_raw!r}",
                                     f"LLM_TIMEOUT must be a number of seconds, got: {timeout_raw!r}")) from exc

    return LLMConfig(
        provider_id=provider.id if provider else "custom",
        provider_name=provider.name if provider else tr("Wlasny endpoint", "Custom endpoint"),
        base_url=base_url,
        api_key=api_key,
        model=model,
        requires_key=requires_key,
        reasoning_effort=get("LLM_REASONING_EFFORT") or None,
        timeout=timeout,
    )


def no_llm_config() -> LLMConfig:
    """Konfiguracja "bez modelu" — nic nie wysyla, call_llm odmawia (core/agent.py)."""
    return LLMConfig(provider_id=NO_LLM_ID, provider_name=tr("bez modelu", "no model"), base_url="", api_key="",
                     model="", requires_key=False, reasoning_effort=None, timeout=DEFAULT_TIMEOUT_SECONDS)


# ─── Listy modeli ───────────────────────────────────────────────────────────

# Fragmenty identyfikatorow modeli, ktore nie sa modelami czatu
# (embeddingi, mowa, obraz, moderacja...) — odfiltrowujemy je z list.
NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "tts", "whisper", "transcribe", "speech", "audio", "realtime",
    "-live", "image", "imagen", "dall-e", "moderation", "rerank", "guard",
    "veo", "sora", "flux", "aqa",
)


def clean_model_id(model_id: str) -> str:
    """Gemini zwraca identyfikatory w postaci 'models/<id>'."""
    return model_id.removeprefix("models/")


def chat_model_ids(models: list[dict[str, Any]]) -> list[str]:
    """
    Z surowej odpowiedzi GET /models wybiera modele czatu, najnowsze najpierw.

    Jesli provider podaje `supported_parameters` (np. OpenRouter), zostawiamy
    tylko modele z obsluga `tools` — bez tego agent nie zadziala.
    """
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in models:
        model_id = clean_model_id(str(m.get("id", "")))
        lowered = model_id.lower()
        if not model_id or model_id in seen or lowered.endswith(":batch"):
            continue
        params = m.get("supported_parameters")
        if params is not None and "tools" not in params:
            continue
        if any(marker in lowered for marker in NON_CHAT_MARKERS):
            continue
        seen.add(model_id)
        candidates.append((int(m.get("created") or 0), model_id))

    # sorted() jest stabilne — przy braku `created` zostaje kolejnosc z API.
    return [model_id for _, model_id in sorted(candidates, key=lambda c: -c[0])]


def model_available(model: str, available: list[str]) -> bool:
    """Czy model jest na liscie (Ollama: 'llama3' == 'llama3:latest')."""
    model = clean_model_id(model)
    return model in available or f"{model}:latest" in available or model.removesuffix(":latest") in available
