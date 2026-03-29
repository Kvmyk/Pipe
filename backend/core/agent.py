"""
Agent -- petla LLM z tool calling do zarzadzania serwerem VPS.

Pipe v0.2

Cykl jednej wiadomosci:
  1. Uzytkownik wysyla wiadomosc
  2. Agent dodaje do historii i wysyla do LLM
  3. LLM odpowiada: tool_call lub text
  4. tool_call -> walidacja security -> execute/confirm/forbid
  5. Wynik wraca do LLM jako tool_result
  6. LLM formuluje odpowiedz po polsku
  7. Odpowiedz trafia do uzytkownika
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Literal

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from backend.config import settings
from backend.config.prompts import BASE_SYSTEM_PROMPT, TELEGRAM_SYSTEM_PROMPT
from backend.core import audit
from backend.core.executor import LocalExecutor
from backend.core.security import classify_command, classify_file_write
from backend.core.tools import TOOLS

MAX_TOOL_ITERATIONS = 10


@dataclass
class ConfirmationRequest:
    """Zdefiniowany w locie, gdy agent potrzebuje potwierdzenia od użytkownika."""

    tool_call_id: str
    tool_name: str
    command: str
    classification: Literal["confirm"]
    # Dla write_file — przechowuje ścieżkę i zawartość
    file_path: str | None = None
    file_content: str | None = None


@dataclass
class Session:
    """Stan rozmowy jednej sesji uzytkownika."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    interface: str = "cli"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None
    cwd: str = "/"

    @property
    def system_prompt(self) -> str:
        dir_context = (
            f"\n\n--- NAWIGACJA ---\n"
            f"Twoj wirtualny katalog roboczy na serwerze to obecnie: {self.cwd}\n"
            f"(Pamietaj, ze w kontenerze ten katalog znajduje sie pod sciezka /hostfs{self.cwd}).\n"
            f"1. Jesli uruchamiasz komendy plikowe lokalnie dla tego katalogu, uzyj sciezki /hostfs{self.cwd}.\n"
            f"2. ZA KAZDYM RAZEM gdy odpisujesz uzytkownikowi, ZAWSZE rozpoczynaj pierwsza linie od "
            f"tagu reprezentujacego aktualna sciezke, np.: [Katalog: {self.cwd}]\n"
            f"3. Uzyj narzedzia change_directory, jesli uzytkownik prosi o wejscie/przejscie do innego folderu.\n"
        )
        if "telegram" in self.interface.lower():
            # W telegramie system_prompt uzywa HTML
            dir_context = dir_context.replace("[Katalog: ", "<b>[Katalog: ")
            dir_context = dir_context.replace("]\n", "]</b>\n")
            return TELEGRAM_SYSTEM_PROMPT + dir_context
        return BASE_SYSTEM_PROMPT + dir_context


class VPSAgent:
    """
    Autonomiczny agent AI do zarządzania serwerem VPS.

    Utrzymuje sesje rozmów i obsługuje pętlę LLM z tool calling.
    Jeden egzemplarz może obsługiwać wiele sesji równocześnie.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            timeout=60.0,  # max 60s na odpowiedź LLM
        )
        self._executor = LocalExecutor()
        self._sessions: dict[str, Session] = {}

    def get_or_create_session(
        self,
        session_id: str,
        interface: str = "cli",
    ) -> Session:
        """Zwraca istniejącą sesję lub tworzy nową."""
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(
                session_id=session_id,
                interface=interface,
            )
        return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """Usuwa sesję (np. po rozłączeniu klienta)."""
        self._sessions.pop(session_id, None)

    async def chat(
        self,
        session_id: str,
        user_message: str,
        interface: str = "cli",
    ) -> AsyncGenerator[str, None]:
        """
        Przetwarza wiadomość użytkownika i zwraca odpowiedź jako generator.

        Yields:
            Fragmenty odpowiedzi agenta (tekst w języku polskim).

        Raises:
            Nie rzuca wyjątków — błędy są logowane i zwracane jako odpowiedzi.
        """
        session = self.get_or_create_session(session_id, interface)

        # Dodaj wiadomość użytkownika do historii
        session.messages.append({"role": "user", "content": user_message})

        try:
            async for chunk in self._run_agent_loop(session):
                yield chunk
        except Exception as exc:
            yield f"[BLAD] Błąd wykonania: {exc}"

    async def confirm(
        self,
        session_id: str,
        confirmed: bool,
    ) -> AsyncGenerator[str, None]:
        """
        Obsługuje potwierdzenie lub odmowę użytkownika dla oczekującej operacji.

        Args:
            session_id: ID sesji.
            confirmed: True = TAK, False = NIE.

        Yields:
            Fragmenty odpowiedzi agenta.
        """
        session = self._sessions.get(session_id)
        if not session or not session.pending_confirmation:
            yield "[OSTRZEZENIE] Brak oczekującej operacji do potwierdzenia."
            return

        pending = session.pending_confirmation
        session.pending_confirmation = None

        if confirmed:
            # Wykonaj operację
            result_content = await self._execute_tool_confirmed(
                session, pending
            )
        else:
            # Użytkownik odmówił — poinformuj LLM
            result_content = "Użytkownik odmówił wykonania tej operacji."
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending.tool_call_id,
                    "content": result_content,
                }
            )

        # Kontynuuj pętlę LLM
        try:
            async for chunk in self._run_agent_loop(session):
                yield chunk
        except Exception as exc:
            yield f"[BLAD] Błąd po potwierdzeniu: {exc}"

    # ─── Wewnętrzna pętla agenta ──────────────────────────────────────────────

    async def _run_agent_loop(
        self,
        session: Session,
    ) -> AsyncGenerator[str, None]:
        """Pętla LLM z tool calling. Maksymalnie MAX_TOOL_ITERATIONS iteracji."""

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self._call_llm(session)
            message = response.choices[0].message

            # Dodaj odpowiedź asystenta do historii
            session.messages.append(message.model_dump(exclude_unset=True, exclude_none=True))

            # Sprawdź czy LLM chce wywołać narzędzie
            if not message.tool_calls:
                # LLM odpowiedział bezpośrednio — koniec pętli
                yield message.content or ""
                return

            # Obsłuż każde wywołanie narzędzia
            for tool_call in message.tool_calls:
                async for chunk in self._handle_tool_call(session, tool_call):
                    yield chunk

                # Jeśli oczekujemy na potwierdzenie — przerwij pętlę
                if session.pending_confirmation:
                    return

        # Przekroczono limit iteracji
        yield (
            "[OSTRZEZENIE] Agent osiągnął limit iteracji. "
            "Spróbuj przeformułować zapytanie lub podziel je na mniejsze kroki."
        )

    async def _call_llm(self, session: Session) -> ChatCompletion:
        """Wysyła historię sesji do LLM i zwraca odpowiedź."""
        messages = [
            {"role": "system", "content": session.system_prompt},
            *session.messages,
        ]
        return await self._client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

    async def _handle_tool_call(
        self,
        session: Session,
        tool_call: Any,
    ) -> AsyncGenerator[str, None]:
        """Obsługuje pojedyncze wywołanie narzędzia przez LLM."""
        tool_name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as exc:
            yield f"[BLAD] Błąd parsowania argumentów narzędzia: {exc}"
            return

        if tool_name == "execute_command":
            async for chunk in self._handle_execute_command(session, tool_call, args):
                yield chunk

        elif tool_name == "read_file":
            async for chunk in self._handle_read_file(session, tool_call, args):
                yield chunk

        elif tool_name == "write_file":
            async for chunk in self._handle_write_file(session, tool_call, args):
                yield chunk

        elif tool_name == "git_command":
            async for chunk in self._handle_git_command(session, tool_call, args):
                yield chunk

        elif tool_name == "change_directory":
            async for chunk in self._handle_change_directory(session, tool_call, args):
                yield chunk

        elif tool_name == "system_stats":
            async for chunk in self._handle_system_stats(session, tool_call, args):
                yield chunk

        elif tool_name == "docker_manage":
            async for chunk in self._handle_docker_manage(session, tool_call, args):
                yield chunk

        elif tool_name == "network_info":
            async for chunk in self._handle_network_info(session, tool_call, args):
                yield chunk

        elif tool_name == "cron_manage":
            async for chunk in self._handle_cron_manage(session, tool_call, args):
                yield chunk

        else:
            result = f"Nieznane narzedzie: {tool_name}"
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            return
            yield  # noqa: unreachable

    async def _handle_execute_command(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Obsługuje narzędzie execute_command."""
        command = args.get("command", "").strip()
        if not command:
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "Błąd: pusta komenda",
                }
            )
            return

        classification = classify_command(command)

        if classification == "forbidden":
            # FORBIDDEN — nie informuj LLM, sam odmów
            await audit.log_blocked(session.interface, command)
            yield f"[ODMOWA] Wykonanie polecenia `{command}` jest zabronione przez politykę bezpieczeństwa."
            # Dodaj informację do historii jako odmowę systemową
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "ODMOWA SYSTEMOWA: Komenda jest na liście zakazanych operacji.",
                }
            )
            return

        if classification == "confirm":
            # CONFIRM — poproś użytkownika o potwierdzenie
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id,
                tool_name="execute_command",
                command=command,
                classification="confirm",
            )
            yield f"[POTWIERDZ] Polecenie `{command}` wymaga potwierdzenia. Wpisz TAK aby wykonać."
            return

        # SAFE — wykonaj od razu
        stdout, stderr, exit_code = await self._executor.execute(command)
        await audit.log_safe(session.interface, command, exit_code)

        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
        )

    async def _handle_read_file(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Obsługuje narzędzie read_file."""
        path = args.get("path", "").strip()
        if not path:
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "Błąd: pusta ścieżka",
                }
            )
            return

        try:
            content = await self._executor.read_file(path)
            await audit.log_file_read(session.interface, path)
            result = content if content else "(plik jest pusty)"
        except FileNotFoundError:
            result = f"Błąd: plik nie istnieje: {path}"
        except PermissionError:
            result = f"Błąd: brak uprawnień do odczytu: {path}"
        except Exception as exc:
            result = f"Błąd odczytu pliku: {exc}"

        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
        )
        # read_file nie emituje chunków — LLM dostanie wynik i sformułuje odpowiedź
        return
        yield  # noqa: unreachable — wymagane żeby metoda była async generator

    async def _handle_write_file(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Obsługuje narzędzie write_file — zawsze wymaga potwierdzenia."""
        path = args.get("path", "").strip()
        content = args.get("content", "")

        if not path:
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "Błąd: pusta ścieżka",
                }
            )
            return

        classification = classify_file_write(path)

        if classification == "forbidden":
            await audit.log_blocked(session.interface, f"write_file({path})")
            yield f"[ODMOWA] Nie mogę zapisać do `{path}`. Ta ścieżka jest chroniona."
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "ODMOWA SYSTEMOWA: Zapis do tej ścieżki jest zakazany.",
                }
            )
            return

        # Write file zawsze wymaga potwierdzenia
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="write_file",
            command=f"write_file(path={path}, content=<{len(content)} znaków>)",
            classification="confirm",
            file_path=path,
            file_content=content,
        )
        yield f"[POTWIERDZ] Operacja zapisu wymaga potwierdzenia: `{path}` ({len(content)} znakow)"

    # --- Git ---

    async def _handle_git_command(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Obsluguje narzedzie git_command."""
        repo_path = args.get("repo_path", "").strip()
        subcommand = args.get("subcommand", "").strip()
        needs_confirm = args.get("requires_confirmation", False)

        if not repo_path or not subcommand:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "Blad: repo_path i subcommand sa wymagane.",
            })
            return

        cmd = f"git -C {repo_path} {subcommand}"

        # Operacje modyfikujace wymagaja potwierdzenia
        modify_keywords = ["commit", "push", "merge", "rebase", "reset", "checkout", "stash pop", "stash drop"]
        if needs_confirm or any(kw in subcommand.lower() for kw in modify_keywords):
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id,
                tool_name="execute_command",
                command=cmd,
                classification="confirm",
            )
            yield f"[POTWIERDZ] Operacja Git wymaga potwierdzenia: `{cmd}`"
            return

        stdout, stderr, exit_code = await self._executor.execute(cmd)
        await audit.log_safe(session.interface, cmd, exit_code)
        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    # --- Change directory ---

    async def _handle_change_directory(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Zmienia wirtualny katalog roboczy agenta."""
        path = args.get("path", "").strip()
        if not path:
            result = "Blad: parameter 'path' jest wymagany."
        else:
            # Upewnienie sie ze path jest absolutny w stosunku do hosta (zaczyna sie od /)
            import os
            # Zamiana ewentualnego ./ itp.
            new_cwd = os.path.normpath(os.path.join(session.cwd, path))
            
            # Weryfikacja czy istnieje na hostfs
            hostfs_path = os.path.join("/hostfs", new_cwd.lstrip("/"))
            if os.path.isdir(hostfs_path):
                session.cwd = new_cwd
                result = f"Katalog zmieniony na: {new_cwd}"
            else:
                result = f"Blad: Katalog {new_cwd} nie istnieje na serwerze."

        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    # --- System Stats ---

    async def _handle_system_stats(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Pobiera szczegolowe statystyki systemowe z /proc i narzedzi."""
        stats_cmd = (
            "echo '=== UPTIME ===' && cat /host_proc/uptime && "
            "echo '\\n=== MEMORY ===' && free -m && "
            "echo '\\n=== LOADAVG ===' && cat /host_proc/loadavg && "
            "echo '\\n=== MEMINFO ===' && cat /host_proc/meminfo | head -20 && "
            "echo '\\n=== CPU ===' && cat /host_proc/stat | head -5 && "
            "echo '\\n=== CPU_INFO ===' && nproc && "
            "echo '\\n=== DISK ===' && df -h / /hostfs 2>/dev/null && "
            "echo '\\n=== TOP_PROCS ===' && ps aux --sort=-%cpu | head -12"
        )

        stdout, stderr, exit_code = await self._executor.execute(stats_cmd)
        await audit.log_safe(session.interface, "system_stats", exit_code)
        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    # --- Docker Management ---

    async def _handle_docker_manage(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Obsluguje operacje Docker."""
        operation = args.get("operation", "").strip()
        target = args.get("target", "").strip()
        options = args.get("options", "").strip()
        needs_confirm = args.get("requires_confirmation", False)

        op_map = {
            "ps": "docker ps",
            "logs": f"docker logs {target}",
            "inspect": f"docker inspect {target}",
            "stats": "docker stats --no-stream",
            "top": f"docker top {target}",
            "restart": f"docker restart {target}",
            "stop": f"docker stop {target}",
            "start": f"docker start {target}",
            "rm": f"docker rm {target}",
            "rmi": f"docker rmi {target}",
            "images": "docker images",
            "prune": "docker system prune -f",
            "compose-ps": f"docker compose -f {target} ps" if target else "docker compose ps",
            "compose-logs": f"docker compose -f {target} logs --tail 50" if target else "docker compose logs --tail 50",
        }

        cmd = op_map.get(operation)
        if not cmd:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"Nieznana operacja Docker: {operation}",
            })
            return

        if options:
            cmd = f"{cmd} {options}"

        modify_ops = {"restart", "stop", "start", "rm", "rmi", "prune"}
        if needs_confirm or operation in modify_ops:
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id,
                tool_name="execute_command",
                command=cmd,
                classification="confirm",
            )
            yield f"[POTWIERDZ] Operacja Docker wymaga potwierdzenia: `{cmd}`"
            return

        stdout, stderr, exit_code = await self._executor.execute(cmd)
        await audit.log_safe(session.interface, cmd, exit_code)
        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    # --- Network Info ---

    async def _handle_network_info(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Diagnostyka sieciowa."""
        check_type = args.get("check_type", "").strip()
        target = args.get("target", "").strip()

        cmd_map = {
            "ports": "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null",
            "connections": "ss -tnp 2>/dev/null || netstat -tnp 2>/dev/null",
            "listeners": "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null",
            "ping": f"ping -c 4 {target}" if target else "echo 'Blad: target wymagany dla ping'",
            "curl": f"curl -sS -o /dev/null -w '%{{http_code}} %{{time_total}}s' {target}" if target else "echo 'Blad: target wymagany dla curl'",
            "dns": f"nslookup {target} 2>/dev/null || dig {target} +short 2>/dev/null" if target else "echo 'Blad: target wymagany dla dns'",
        }

        cmd = cmd_map.get(check_type)
        if not cmd:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"Nieznany typ diagnozy: {check_type}",
            })
            return

        stdout, stderr, exit_code = await self._executor.execute(cmd)
        await audit.log_safe(session.interface, f"network_info:{check_type}", exit_code)
        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    # --- Cron Management ---

    async def _handle_cron_manage(
        self,
        session: Session,
        tool_call: Any,
        args: dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        """Zarzadzanie zadaniami cron."""
        operation = args.get("operation", "").strip()
        schedule = args.get("schedule", "").strip()
        command = args.get("command", "").strip()
        needs_confirm = args.get("requires_confirmation", False)

        if operation == "list":
            cmd = "crontab -l 2>/dev/null || echo 'Brak zadan cron'"
        elif operation == "check-logs":
            cmd = "grep -i cron /var/log/syslog 2>/dev/null | tail -20 || journalctl -u cron --no-pager -n 20 2>/dev/null || echo 'Brak logow cron'"
        elif operation == "add":
            if not schedule or not command:
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "Blad: schedule i command sa wymagane dla operacji 'add'.",
                })
                return
            cmd = f"(crontab -l 2>/dev/null; echo '{schedule} {command}') | crontab -"
        elif operation == "remove":
            if not command:
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": "Blad: command (wzorzec do usuniecia) jest wymagany dla operacji 'remove'.",
                })
                return
            cmd = f"crontab -l 2>/dev/null | grep -v '{command}' | crontab -"
        else:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"Nieznana operacja cron: {operation}",
            })
            return

        modify_ops = {"add", "remove"}
        if needs_confirm or operation in modify_ops:
            session.pending_confirmation = ConfirmationRequest(
                tool_call_id=tool_call.id,
                tool_name="execute_command",
                command=cmd,
                classification="confirm",
            )
            yield f"[POTWIERDZ] Operacja cron wymaga potwierdzenia: `{cmd}`"
            return

        stdout, stderr, exit_code = await self._executor.execute(cmd)
        await audit.log_safe(session.interface, f"cron:{operation}", exit_code)
        result = _format_tool_result(stdout, stderr, exit_code)
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return
        yield  # noqa: unreachable

    async def _execute_tool_confirmed(
        self,
        session: Session,
        pending: ConfirmationRequest,
    ) -> str:
        """Wykonuje potwierdzona operacje i dodaje wynik do historii."""
        if pending.tool_name == "execute_command":
            command = pending.command
            stdout, stderr, exit_code = await self._executor.execute(command)
            await audit.log_confirmed(session.interface, command, exit_code)
            result = _format_tool_result(stdout, stderr, exit_code)

        elif pending.tool_name == "write_file":
            path = pending.file_path or ""
            content = pending.file_content or ""
            try:
                await self._executor.write_file(path, content)
                await audit.log_file_write(session.interface, path, 0)
                result = f"Plik {path} zostal zapisany pomyslnie."
            except PermissionError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Blad zapisu (brak uprawnien): {exc}"
            except OSError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Blad zapisu pliku: {exc}"
        else:
            result = "Nieznana operacja."

        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": pending.tool_call_id,
                "content": result,
            }
        )
        return result


# --- Globalna instancja agenta ---
_agent: VPSAgent | None = None


def get_agent() -> VPSAgent:
    """Zwraca globalna instancje agenta (singleton)."""
    global _agent
    if _agent is None:
        _agent = VPSAgent()
    return _agent


# --- Helpers ---

def _format_tool_result(stdout: str, stderr: str, exit_code: int) -> str:
    """Formatuje wynik komendy dla LLM."""
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    parts.append(f"EXIT CODE: {exit_code}")
    return "\n".join(parts)
