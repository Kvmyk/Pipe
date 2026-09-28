"""
Executor — wykonywanie komend lokalnie na serwerze przez subprocess.

Agent działa bezpośrednio NA serwerze, więc nie ma SSH.
Wszystkie komendy są wykonywane lokalnie przez asyncio.create_subprocess_shell().
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path


# Limit dla odczytow wykonywanych bez pytania. Operacje zatwierdzone przez
# uzytkownika (aktualizacje, buildy) dostaja dluzszy limit od wywolujacego.
TIMEOUT_SECONDS: int = 30
# Wiecej nie ma sensu wysylac modelowi — reszta i tak by nie zmiescila sie w kontekscie.
MAX_OUTPUT_BYTES: int = 2_000_000


async def execute(cmd: str, cwd: str | None = None, timeout: float | None = None) -> tuple[str, str, int]:
    """
    Wykonuje komendę shell lokalnie w określonym katalogu.

    Args:
        cmd: Komenda do wykonania.
        cwd: Katalog roboczy (opcjonalny).
        timeout: Limit czasu w sekundach (domyślnie TIMEOUT_SECONDS).

    Returns:
        Tuple (stdout, stderr, exit_code).
    """
    limit = timeout or TIMEOUT_SECONDS
    if cwd is not None and not os.path.isdir(cwd):
        return "", f"Katalog roboczy nie istnieje: {cwd}", 1
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.DEVNULL,  # nic nie czeka na klawiature
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,  # wlasna grupa procesow — timeout zabija tez dzieci powloki
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=limit,
            )
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)  # nie zostawiaj zombie
            except asyncio.TimeoutError:
                pass
            return (
                "",
                f"Timeout: komenda przekroczyła {int(limit)} sekund",
                124,
            )
        stdout_bytes = stdout_bytes[:MAX_OUTPUT_BYTES]
        stderr_bytes = stderr_bytes[:MAX_OUTPUT_BYTES]

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else 1

        # Jeśli brak outputu — zwróć informację
        if not stdout.strip() and not stderr.strip():
            stdout = "Komenda wykonana bez outputu"

        return stdout, stderr, exit_code

    except FileNotFoundError as exc:
        return "", f"Komenda nie znaleziona: {exc}", 127
    except PermissionError as exc:
        return "", f"Brak uprawnień: {exc}", 1
    except Exception as exc:
        return "", f"Błąd wykonania: {exc}", 1


async def read_file(path: str) -> str:
    """
    Odczytuje zawartość pliku.

    Args:
        path: Absolutna ścieżka do pliku.

    Returns:
        Zawartość pliku jako string.

    Raises:
        FileNotFoundError: Gdy plik nie istnieje.
        PermissionError: Gdy brak uprawnień.
        OSError: Przy innych błędach I/O.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Plik nie istnieje: {path}")
    if not file_path.is_file():
        raise ValueError(f"Ścieżka nie wskazuje na plik: {path}")

    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        raise PermissionError(f"Brak uprawnień do odczytu: {path}")
    except OSError as exc:
        raise OSError(f"Błąd odczytu pliku {path}: {exc}") from exc


async def write_file(path: str, content: str) -> None:
    """
    Zapisuje zawartość do pliku (tworzy lub nadpisuje).

    Args:
        path: Absolutna ścieżka do pliku.
        content: Zawartość do zapisania.

    Raises:
        PermissionError: Gdy brak uprawnień.
        OSError: Przy innych błędach I/O.
    """
    file_path = Path(path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except PermissionError:
        raise PermissionError(f"Brak uprawnień do zapisu: {path}")
    except OSError as exc:
        raise OSError(f"Błąd zapisu pliku {path}: {exc}") from exc
