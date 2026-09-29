"""
Wspolne elementy handlerow narzedzi.

Kazdy handler musi dopisac do `session.messages` dokladnie jedna odpowiedz
narzedzia (`reply`) albo ustawic `session.pending_confirmation` — wtedy
odpowiedz dopisze `agent.confirm()` po decyzji uzytkownika.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import audit, executor, runtime, safety
from backend.core.events import Event
from backend.core.security import Classification, classify_command
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


def reply(session: Session, tool_call: Any, content: str) -> None:
    """Dopisuje wynik narzedzia dla modelu."""
    session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})


def format_result(stdout: str, stderr: str, exit_code: int) -> str:
    """Wynik komendy dla modelu (nigdy dla uzytkownika — model go interpretuje)."""
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    parts.append(f"EXIT CODE: {exit_code}")
    return "\n".join(parts)


def local_cwd(session: Session) -> str:
    """Katalog roboczy sesji tak, jak widzi go proces Pipe."""
    return runtime.to_local(session.cwd)


async def run_classified(
    session: Session,
    tool_call: Any,
    command: str,
    *,
    tool_name: str,
    cwd: str | None = None,
    classification: Classification | None = None,
    what: str = "Operacja",
    inner: str | None = None,
) -> AsyncGenerator[Event, None]:
    """
    Wspolny przebieg dla narzedzi uruchamiajacych komende powloki:
    forbidden -> odmowa, confirm -> pytanie o potwierdzenie, safe -> wykonanie.

    `command` to dokladnie to, co zostanie uruchomione (i pokazane w potwierdzeniu).
    `inner` — gdy komenda jest opakowaniem (ssh/kubectl exec), klasyfikujemy
    polecenie docelowe, a nie opakowanie.
    """
    classification = classification or classify_command(inner if inner is not None else command)

    if classification == "forbidden":
        await audit.log_blocked(session.interface, f"{tool_name}({command})")
        yield f"[ODMOWA] Komenda {as_code(inner or command)} jest zabroniona przez polityke bezpieczenstwa."
        reply(session, tool_call, "ODMOWA SYSTEMOWA: komenda jest na liscie zakazanych operacji. Nie probuj jej obejsc.")
        return

    if classification == "confirm":
        # Plan bezpiecznika tylko dla komend wykonywanych lokalnie — na zdalnym celu
        # (ssh/kubectl exec) pliki i uslugi sa po drugiej stronie.
        plan = await safety.plan_for_command(command, session.cwd) if inner is None or inner == command \
            else safety.Plan(notes=["zdalny cel — bez kopii i weryfikacji po stronie Pipe"])
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name=tool_name,
            command=command,
            classification="confirm",
            plan=plan,
        )
        described = plan.describe()
        yield f"[POTWIERDZ] {what} wymaga potwierdzenia: {as_code(command)}" + (f"\n{described}" if described else "")
        return

    stdout, stderr, exit_code = await executor.execute(command, cwd=cwd if cwd is not None else local_cwd(session))
    await audit.log_safe(session.interface, command, exit_code)
    reply(session, tool_call, format_result(stdout, stderr, exit_code))
