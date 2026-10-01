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

from backend.core.i18n import is_en, tr

# Tekst zwracany, gdy komenda nic nie wypisala (obie wersje jezykowe — do porownan).
NO_OUTPUT = ("Komenda wykonana bez outputu", "Command produced no output")


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
        return "", tr(f"Katalog roboczy nie istnieje: {cwd}", f"Working directory does not exist: {cwd}"), 1
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
                tr(f"Timeout: komenda przekroczyła {int(limit)} sekund", f"Timeout: the command exceeded {int(limit)} seconds"),
                124,
            )
        stdout_bytes = stdout_bytes[:MAX_OUTPUT_BYTES]
        stderr_bytes = stderr_bytes[:MAX_OUTPUT_BYTES]

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode if proc.returncode is not None else 1

        # Jeśli brak outputu — zwróć informację
        if not stdout.strip() and not stderr.strip():
            stdout = NO_OUTPUT[1] if is_en() else NO_OUTPUT[0]

        return stdout, stderr, exit_code

    except FileNotFoundError as exc:
        return "", tr(f"Komenda nie znaleziona: {exc}", f"Command not found: {exc}"), 127
    except PermissionError as exc:
        return "", tr(f"Brak uprawnień: {exc}", f"Permission denied: {exc}"), 1
    except Exception as exc:
        return "", tr(f"Błąd wykonania: {exc}", f"Execution error: {exc}"), 1


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
        raise FileNotFoundError(tr(f"Plik nie istnieje: {path}", f"File does not exist: {path}"))
    if not file_path.is_file():
        raise ValueError(tr(f"Ścieżka nie wskazuje na plik: {path}", f"Path is not a file: {path}"))

    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        raise PermissionError(tr(f"Brak uprawnień do odczytu: {path}", f"No permission to read: {path}"))
    except OSError as exc:
        raise OSError(tr(f"Błąd odczytu pliku {path}: {exc}", f"Error reading file {path}: {exc}")) from exc


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
        raise PermissionError(tr(f"Brak uprawnień do zapisu: {path}", f"No permission to write: {path}"))
    except OSError as exc:
        raise OSError(tr(f"Błąd zapisu pliku {path}: {exc}", f"Error writing file {path}: {exc}")) from exc
