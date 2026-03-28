"""
Settings — ładowanie konfiguracji z pliku .env
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Załaduj .env z katalogu backendu (lub nadrzędnego)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

# ─── LLM ────────────────────────────────────────────────────────────────────
LLM_BASE_URL: str = os.getenv(
    "LLM_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
)
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.0-flash")

# ─── Security / Audit ───────────────────────────────────────────────────────
AUDIT_LOG_PATH: str = os.getenv("AUDIT_LOG_PATH", "/app/audit.log")

# ─── Socket ─────────────────────────────────────────────────────────────────
AGENT_SOCKET: str = os.getenv("AGENT_SOCKET", "/tmp/vps-agent.sock")

# TCP — nasłuchiwanie dla SSH tunnel z laptopa (TYLKO 127.0.0.1!)
TCP_HOST: str = os.getenv("TCP_HOST", "127.0.0.1")
TCP_PORT: int = int(os.getenv("TCP_PORT", "7379"))

# ─── Validation ─────────────────────────────────────────────────────────────
def validate() -> None:
    """Rzuca ValueError jeśli brakuje wymaganych ustawień."""
    if not LLM_API_KEY:
        raise ValueError(
            "LLM_API_KEY nie jest ustawiony. "
            "Uzupełnij plik .env (skopiuj z .env.example)."
        )
