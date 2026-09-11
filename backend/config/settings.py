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
