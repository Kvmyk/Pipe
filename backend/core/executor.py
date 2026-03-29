"""
Executor — wykonywanie komend lokalnie na serwerze przez subprocess.

Agent działa bezpośrednio NA serwerze (Mikrus), więc nie ma SSH.
Wszystkie komendy są wykonywane lokalnie przez asyncio.create_subprocess_shell().
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalExecutor:
    """Wykonywacz komend lokalnych na serwerze."""

    TIMEOUT_SECONDS: int = 30

    async def execute(self, cmd: str, cwd: str | None = None) -> tuple[str, str, int]:
        """
        Wykonuje komendę shell lokalnie w określonym katalogu.

        Args:
            cmd: Komenda do wykonania.
            cwd: Katalog roboczy (opcjonalny).

        Returns:
            Tuple (stdout, stderr, exit_code).
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return (
                    "",
                    f"Timeout: komenda przekroczyła {self.TIMEOUT_SECONDS} sekund",
                    124,
                )

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

    async def read_file(self, path: str) -> str:
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

    async def write_file(self, path: str, content: str) -> None:
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
