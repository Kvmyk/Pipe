"""
Agent — pętla LLM z tool calling do zarządzania serwerem VPS.

Cykl jednej wiadomości:
  1. Użytkownik wysyła wiadomość
  2. Agent dodaje do historii i wysyła do LLM
  3. LLM odpowiada: tool_call lub text
  4. tool_call → walidacja security → execute/confirm/forbid
  5. Wynik wraca do LLM jako tool_result
  6. LLM formułuje odpowiedź po polsku
  7. Odpowiedź trafia do użytkownika
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
    """Stan rozmowy jednej sesji użytkownika."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    interface: str = "cli"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None

    @property
    def system_prompt(self) -> str:
        if "telegram" in self.interface.lower():
            return TELEGRAM_SYSTEM_PROMPT
        return BASE_SYSTEM_PROMPT


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
            error_msg = f"❌ Błąd wewnętrzny agenta: {exc}"
            yield error_msg

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
            yield "⚠️ Brak oczekującej operacji do potwierdzenia."
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
            yield f"❌ Błąd po potwierdzeniu: {exc}"

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
            "⚠️ Agent osiągnął limit iteracji. "
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
            yield f"❌ Błąd parsowania argumentów narzędzia: {exc}"
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

        else:
            # Nieznane narzędzie
            result = f"Nieznane narzędzie: {tool_name}"
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            return
            yield  # noqa: unreachable — wymagane żeby metoda była async generator

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
            yield f"🚫 Nie mogę wykonać tej operacji. Komenda `{command}` jest bezwzględnie zakazana ze względów bezpieczeństwa."
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
            yield f"⚠️ Operacja wymaga potwierdzenia: `{command}`"
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
            yield f"🚫 Nie mogę zapisać do `{path}`. Ta ścieżka jest chroniona."
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
        yield f"⚠️ Operacja zapisu wymaga potwierdzenia: `{path}` ({len(content)} znaków)"

    async def _execute_tool_confirmed(
        self,
        session: Session,
        pending: ConfirmationRequest,
    ) -> str:
        """Wykonuje potwierdzoną operację i dodaje wynik do historii."""
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
                result = f"Plik {path} został zapisany pomyślnie."
            except PermissionError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Błąd zapisu (brak uprawnień): {exc}"
            except OSError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Błąd zapisu pliku: {exc}"
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


# ─── Globalna instancja agenta ────────────────────────────────────────────────
_agent: VPSAgent | None = None


def get_agent() -> VPSAgent:
    """Zwraca globalną instancję agenta (singleton)."""
    global _agent
    if _agent is None:
        _agent = VPSAgent()
    return _agent


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _format_tool_result(stdout: str, stderr: str, exit_code: int) -> str:
    """Formatuje wynik komendy dla LLM."""
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    parts.append(f"EXIT CODE: {exit_code}")
    return "\n".join(parts)
