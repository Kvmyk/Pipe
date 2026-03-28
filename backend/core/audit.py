"""
Audit log — append-only zapis każdej operacji agenta.

Format wpisu:
[2025-03-28 14:23:11] [CLI] [SAFE] systemctl restart nginx → exit_code=0
[2025-03-28 14:25:03] [TELEGRAM:123456789] [CONFIRMED] nano /etc/nginx/nginx.conf → exit_code=0
[2025-03-28 14:26:44] [TELEGRAM:123456789] [BLOCKED] rm -rf / → FORBIDDEN
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Literal

from backend.config.settings import AUDIT_LOG_PATH

# Lock zapobiega równoczesnym zapisom z wielu sesji
_write_lock = asyncio.Lock()


def _format_entry(
    interface: str,
    classification: Literal["SAFE", "CONFIRMED", "BLOCKED", "READ", "WRITE"],
    command: str,
    exit_code: int | None = None,
    extra: str = "",
) -> str:
    """Formatuje pojedynczy wpis do logu."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if exit_code is not None:
        result = f"→ exit_code={exit_code}"
    elif extra:
        result = f"→ {extra}"
    else:
        result = ""
    return f"[{timestamp}] [{interface}] [{classification}] {command} {result}".strip()


async def log_safe(
    interface: str,
    command: str,
    exit_code: int,
) -> None:
    """Loguje bezpieczną komendę wykonaną bez potwierdzenia."""
    await _append(_format_entry(interface, "SAFE", command, exit_code=exit_code))


async def log_confirmed(
    interface: str,
    command: str,
    exit_code: int,
) -> None:
    """Loguje komendę wykonaną po potwierdzeniu użytkownika."""
    await _append(_format_entry(interface, "CONFIRMED", command, exit_code=exit_code))


async def log_blocked(
    interface: str,
    command: str,
) -> None:
    """Loguje próbę wykonania zakazanej komendy."""
    await _append(_format_entry(interface, "BLOCKED", command, extra="FORBIDDEN"))


async def log_file_read(
    interface: str,
    path: str,
) -> None:
    """Loguje odczyt pliku (bez zawartości)."""
    await _append(_format_entry(interface, "READ", f"read_file({path})"))


async def log_file_write(
    interface: str,
    path: str,
    exit_code: int,
) -> None:
    """Loguje zapis pliku — TYLKO ścieżkę, nie zawartość (może zawierać sekrety)."""
    await _append(
        _format_entry(interface, "WRITE", f"write_file({path})", exit_code=exit_code)
    )


async def get_recent(n: int = 10) -> list[str]:
    """Zwraca ostatnie n wpisów z audit logu."""
    path = Path(AUDIT_LOG_PATH)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []


async def _append(entry: str) -> None:
    """Zapisuje wpis do pliku logu (thread-safe przez asyncio lock)."""
    async with _write_lock:
        path = Path(AUDIT_LOG_PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as exc:
            # Nie crashuj agenta z powodu błędu logowania
            print(f"[AUDIT ERROR] Nie można zapisać do {AUDIT_LOG_PATH}: {exc}")
