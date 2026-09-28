"""
Settings — ładowanie konfiguracji z pliku .env
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from backend.config.providers import LLMConfig, ProviderConfigError, resolve_llm_config

# Załaduj .env z katalogu backendu (lub nadrzędnego)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

# ─── LLM ────────────────────────────────────────────────────────────────────
# Provider, adres, klucz i model wylicza backend/config/providers.py
# (LLM_PROVIDER + opcjonalne nadpisania LLM_BASE_URL / LLM_MODEL / LLM_API_KEY).
# Blad konfiguracji nie przerywa importu — zglasza go dopiero validate().
try:
    LLM: LLMConfig | None = resolve_llm_config(os.environ)
    _LLM_ERROR: str | None = None
except ProviderConfigError as exc:
    LLM = None
    _LLM_ERROR = str(exc)

# Presety lokalnych providerow (Ollama) wskazuja host.docker.internal — adres hosta
# widziany z kontenera. Poza kontenerem ta nazwa nie istnieje, wiec uzywamy localhost.
if LLM and "host.docker.internal" in LLM.base_url:
    from dataclasses import replace as _replace

    from backend.core import runtime as _runtime

    if _runtime.kind() == "native":
        LLM = _replace(LLM, base_url=LLM.base_url.replace("host.docker.internal", "127.0.0.1"))

LLM_BASE_URL: str = LLM.base_url if LLM else ""
LLM_API_KEY: str = LLM.api_key if LLM else ""
LLM_MODEL: str = LLM.model if LLM else ""

# ─── Security / Audit ───────────────────────────────────────────────────────
# Opcjonalny token autoryzacji. Jesli pusty — serwer nie wymaga tokenu
# (dostep chroniony wylacznie przez tunel SSH / uprawnienia do socketu).
AGENT_TOKEN: str = os.getenv("AGENT_TOKEN", "")

AUDIT_LOG_PATH: str = os.getenv("AUDIT_LOG_PATH", "/app/audit.log")

# ─── Socket ─────────────────────────────────────────────────────────────────
AGENT_SOCKET: str = os.getenv("AGENT_SOCKET", "/tmp/vps-agent.sock")

# TCP — nasłuchiwanie w wewnątrz kontenera (powinno być 0.0.0.0 by Docker mógł sproxy'ować z hosta)
TCP_HOST: str = os.getenv("TCP_HOST", "0.0.0.0")
TCP_PORT: int = int(os.getenv("TCP_PORT", "7379"))

# ─── Validation ─────────────────────────────────────────────────────────────
def validate() -> None:
    """Rzuca ValueError jeśli brakuje wymaganych ustawień."""
    if LLM is None:
        raise ValueError(_LLM_ERROR)
    if LLM.requires_key and not LLM.api_key:
        raise ValueError(
            f"Brak klucza API dla providera {LLM.provider_name}. "
            "Uruchom kreator z katalogu repozytorium: python3 -m backend.configure "
            "(albo ustaw LLM_API_KEY w backend/.env)."
        )
    # W Kubernetesie port jest osiagalny z kazdego poda (ClusterIP), wiec pusty token
    # oznaczalby, ze dowolny pod moze sterowac agentem. Na VPS chroni go tunel SSH.
    from backend.core import runtime
    if not AGENT_TOKEN and runtime.kind() == "kubernetes":
        raise ValueError(
            "Tryb kubernetes wymaga AGENT_TOKEN — bez niego kazdy pod w klastrze moze sterowac agentem. "
            "Ustaw go w sekrecie pipe-env (np. openssl rand -hex 24)."
        )


# ─── Petla agenta i komendy ─────────────────────────────────────────────────
def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


AGENT_MAX_ITERATIONS: int = _int("AGENT_MAX_ITERATIONS", 15)
# Limit dla komend zatwierdzonych przez uzytkownika (apt upgrade, docker build...).
CONFIRMED_COMMAND_TIMEOUT: int = _int("CONFIRMED_COMMAND_TIMEOUT", 900)
# Redakcja sekretow w wynikach narzedzi przed wyslaniem do providera LLM.
REDACT_SECRETS: bool = os.getenv("REDACT_SECRETS", "1").strip().lower() not in ("0", "false", "no", "nie")

# ─── Workery ────────────────────────────────────────────────────────────────
# Pusty WORKER_MODEL = ten sam model co agent. Tanszy model = tansze workery.
WORKER_MODEL: str = os.getenv("WORKER_MODEL", "").strip()
WORKER_MAX_ITERATIONS: int = _int("WORKER_MAX_ITERATIONS", 8)
WORKER_TIMEOUT: int = _int("WORKER_TIMEOUT", 240)
MAX_WORKERS: int = _int("MAX_WORKERS", 6)

# ─── VIBE ───────────────────────────────────────────────────────────────────
# Co ile wiadomosci uzytkownika agent odswieza notatke o jego stylu (0 = wylaczone).
VIBE_EVERY: int = _int("VIBE_EVERY", 6)

# ─── Czuwanie (watch) ───────────────────────────────────────────────────────
WATCH_ENABLED: bool = os.getenv("WATCH_ENABLED", "1").strip().lower() not in ("0", "false", "no", "nie")
WATCH_INTERVAL: int = _int("WATCH_INTERVAL", 120)
WATCH_DISK_PCT: int = _int("WATCH_DISK_PCT", 90)
WATCH_MEM_PCT: int = _int("WATCH_MEM_PCT", 92)
WATCH_LOAD_FACTOR: int = _int("WATCH_LOAD_FACTOR", 2)
