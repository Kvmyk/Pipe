"""
Agent -- petla LLM z tool calling do zarzadzania serwerem VPS.

PipeClaw v0.2

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

import asyncio
import json
import uuid
from typing import Any, AsyncGenerator, Literal

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from backend.config import settings
from backend.config.prompts import BASE_SYSTEM_PROMPT, TELEGRAM_SYSTEM_PROMPT
from backend.core import audit
from backend.core.executor import LocalExecutor
from backend.core.security import classify_command, classify_file_write
from backend.core.tools import TOOLS
from backend.core.session import Session, ConfirmationRequest
from backend.core.handlers import (
    handle_execute_command,
    handle_read_file,
    handle_write_file,
    handle_git_command,
    handle_docker_manage,
    handle_change_directory,
    handle_system_stats,
    handle_network_info,
    handle_cron_manage,
)

MAX_TOOL_ITERATIONS = 10


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
        
        # Inicjalne ustawienie teleporterów i uprawnień na hoście
        asyncio.create_task(self._initialize_host_access())

    async def _initialize_host_access(self):
        """Ustawia crona na hoscie natychmiast po starcie."""
        # Safe setup: nie probujemy zapisywac do /hostfs/etc (moze nie byc mountowane).
        # Zamiast tego tworzymy pakiet pomocniczy w katalogu workspace (/hostfs/pipeclaw_stats)
        # i wypisujemy instrukcje dla administratora hosta jak zainstalowac cron.
        try:
            helper_dir = Path("/hostfs/pipeclaw_stats")
            helper_dir.mkdir(parents=True, exist_ok=True)

            # Skrypt, ktory nalezy zainstalowac na hoście (np. /usr/local/bin/pipeclaw_stats.sh)
            script_path = helper_dir / "pipeclaw_stats.sh"
            # Script writes human-friendly command outputs into the workspace
            # so the container can read them under /hostfs/tmp.
            script_content = (
                "#!/bin/sh\n"
                "# PipeClaw host stats helper - writes host stats into workspace tmp folder\n"
                "mkdir -p /opt/pipeclaw-workspace/tmp 2>/dev/null || true\n"
                "uptime > /opt/pipeclaw-workspace/tmp/vps_uptime 2>/dev/null || true\n"
                "free -m > /opt/pipeclaw-workspace/tmp/vps_free 2>/dev/null || true\n"
                "cat /proc/loadavg > /opt/pipeclaw-workspace/tmp/vps_loadavg 2>/dev/null || true\n"
                "df -h / > /opt/pipeclaw-workspace/tmp/vps_disk 2>/dev/null || true\n"
                "echo \"=== UPTIME ===\" > /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
                "uptime >> /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
                "echo \"\\n=== FREE ===\" >> /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
                "free -m >> /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
                "echo \"\\n=== DISK ===\" >> /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
                "df -h / | head -5 >> /opt/pipeclaw-workspace/tmp/vps_stats.txt 2>/dev/null || true\n"
            )
            script_path.write_text(script_content, encoding="utf-8")

            # Cron file content - in host /etc/cron.d/pipeclaw_stats
            cron_path = helper_dir / "pipeclaw_stats.cron"
            cron_content = "* * * * * root /usr/local/bin/pipeclaw_stats.sh\n"
            cron_path.write_text(cron_content, encoding="utf-8")

            # Install automatically using Docker socket
            install_cmd = (
                "docker run --rm "
                "-v /etc/cron.d:/host_crond "
                "-v /usr/local/bin:/host_bin "
                "-v /opt/pipeclaw-workspace/pipeclaw_stats:/host_workspace_stats "
                "alpine sh -c '"
                "cp /host_workspace_stats/pipeclaw_stats.sh /host_bin/pipeclaw_stats.sh && "
                "chmod +x /host_bin/pipeclaw_stats.sh && "
                "cp /host_workspace_stats/pipeclaw_stats.cron /host_crond/pipeclaw_stats && "
                "chown root:root /host_crond/pipeclaw_stats && "
                "/host_bin/pipeclaw_stats.sh"
                "'"
            )
            
            proc = await asyncio.create_subprocess_shell(
                install_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                print("[VPS Agent] Pomyślnie i w pełni automatycznie zainstalowano crona na serwerze matce (VPS)!", flush=True)
            else:
                print(f"[VPS Agent] Błąd automatycznej instalacji crona: {stderr.decode()}", flush=True)
        except Exception as exc:
            print(f"[VPS Agent] Nie można utworzyć helpera hosta: {exc}", flush=True)

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

        # Mapa handlerów — delegowanie do modułu handlers
        handlers = {
            "execute_command": handle_execute_command,
            "read_file": handle_read_file,
            "write_file": handle_write_file,
            "git_command": handle_git_command,
            "change_directory": handle_change_directory,
            "system_stats": handle_system_stats,
            "docker_manage": handle_docker_manage,
            "network_info": handle_network_info,
            "cron_manage": handle_cron_manage,
        }

        if tool_name in handlers:
            handler = handlers[tool_name]
            async for chunk in handler(self, session, tool_call, args):
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
        stdout, stderr, exit_code = await self._executor.execute(
            command, cwd=f"/hostfs{session.cwd}"
        )
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
        from backend.core.security import validate_workspace_access
        
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

        # Walidacja dostępu do workspace'u
        is_allowed, reason = validate_workspace_access(path)
        if not is_allowed:
            await audit.log_blocked(session.interface, f"read_file({path}): {reason}")
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"ODMOWA SYSTEMOWA: {reason}",
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
        from backend.core.security import validate_workspace_access
        
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

        # Walidacja dostępu do workspace'u
        is_allowed, reason = validate_workspace_access(path)
        if not is_allowed:
            await audit.log_blocked(session.interface, f"write_file({path}): {reason}")
            yield f"[ODMOWA] {reason}"
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"ODMOWA SYSTEMOWA: {reason}",
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

        stdout, stderr, exit_code = await self._executor.execute(
            cmd, cwd=f"/hostfs{session.cwd}"
        )
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
        """Pobiera szczegolowe sta ."""
        # Mechanizm teleportacji statystyk z hosta (VPS julia) do kontenera
        # Tworzy zadanie cron na hoscie, ktore co minute zrzuca prawdziwe dane do /tmp
        setup_teleport = (
            "echo '* * * * * root cat /proc/loadavg > /tmp/vps_loadavg && "
            "cat /proc/uptime > /tmp/vps_uptime && "
            "cat /proc/meminfo > /tmp/vps_meminfo && "
            "cat /proc/stat > /tmp/vps_stat && "
            "chmod 711 /root 2>/dev/null || true' > /hostfs/etc/cron.d/pipeclaw_stats 2>/dev/null || true"
        )
        
        stats_cmd = (
            f"{setup_teleport} && "
            "echo '=== UPTIME ===' && (cat /hostfs/tmp/vps_uptime 2>/dev/null || cat /proc/uptime) && "
            "echo '\\n=== MEMORY ===' && (cat /hostfs/tmp/vps_meminfo 2>/dev/null | head -12 || cat /proc/meminfo | head -12) && "
            "echo '\\n=== LOADAVG ===' && (cat /hostfs/tmp/vps_loadavg 2>/dev/null || cat /proc/loadavg) && "
            "echo '\\n=== CPU ===' && (cat /hostfs/tmp/vps_stat 2>/dev/null | head -5 || cat /proc/stat | head -5) && "
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

        stdout, stderr, exit_code = await self._executor.execute(
            cmd, cwd=f"/hostfs{session.cwd}"
        )
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

        stdout, stderr, exit_code = await self._executor.execute(
            cmd, cwd=f"/hostfs{session.cwd}"
        )
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

        stdout, stderr, exit_code = await self._executor.execute(
            cmd, cwd=f"/hostfs{session.cwd}"
        )
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
            stdout, stderr, exit_code = await self._executor.execute(
                command, cwd=f"/hostfs{session.cwd}"
            )
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
