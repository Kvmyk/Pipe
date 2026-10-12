"""
Handler dla operacji wykonywania komend shell.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import hostexec, runtime, safety
from backend.core.events import Event
from backend.core.handlers.common import reply, run_classified
from backend.core.security import classify_command
from backend.core.session import Session
from backend.core.i18n import tr


async def handle_execute_command(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """
    Obsluguje narzedzie execute_command: klasyfikuje komende (safe/confirm/forbidden)
    i wykonuje ja w katalogu roboczym sesji albo prosi o potwierdzenie.
    `requires_confirmation=true` od modelu moze tylko zaostrzyc klasyfikacje.
    """
    command = str(args.get("command", "")).strip()
    if not command:
        reply(session, tool_call, tr("Blad: pusta komenda", "Error: empty command"))
        return

    classification = classify_command(command)
    if classification == "safe" and args.get("requires_confirmation") is True:
        classification = "confirm"
    if args.get("host") is True and runtime.kind() != "native":
        async for event in _on_host(session, tool_call, command, classification):
            yield event
        return

    async for event in run_classified(session, tool_call, command, tool_name="execute_command",
                                      classification=classification):
        yield event


async def _on_host(session: Session, tool_call: Any, command: str, classification: str) -> AsyncGenerator[Event, None]:
    """
    `host: true` w trybie docker: komenda na hoscie przez kontener pomocniczy (core/hostexec.py). Zawsze po TAK —
    nawet odczyt, bo uruchamia kontener uprzywilejowany (odczyty i tak robi sie z katalogu hosta). Klasyfikowana
    i pokazywana jest komenda docelowa; opakowanie sklada kod.
    """
    reason = await hostexec.blocker()
    if reason:
        reply(session, tool_call, tr(f"Nie wykonam na hoscie: {reason}", f"Cannot run on the host: {reason}"))
        return
    wrapped = hostexec.wrap(command, await hostexec.image(), session.cwd)
    async for event in run_classified(
            session, tool_call, wrapped, tool_name="execute_command", inner=command, shown=command,
            classification="forbidden" if classification == "forbidden" else "confirm",
            plan=safety.Plan(notes=[hostexec.note()]), what=tr("Komenda na hoscie", "Host command")):
        yield event
