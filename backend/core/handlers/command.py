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
    asked = args.get("requires_confirmation") is True
    if classification == "safe" and asked:
        classification = "confirm"
    if args.get("host") is True and runtime.kind() != "native":
        async for event in _on_host(session, tool_call, command, classification, asked):
            yield event
        return

    async for event in run_classified(session, tool_call, command, tool_name="execute_command",
                                      classification=classification, always_ask=asked):
        yield event


async def _on_host(session: Session, tool_call: Any, command: str, classification: str,
                   asked: bool = False) -> AsyncGenerator[Event, None]:
    """
    `host: true` w trybie docker: komenda na hoscie przez kontener pomocniczy (core/hostexec.py). Zawsze przez
    potwierdzenie (kontener uprzywilejowany) — rutyne core/autonomy.py wykonuje bez pytania. Klasyfikowana
    i pokazywana jest komenda docelowa; opakowanie sklada kod. Plan bezpiecznika jak dla komendy lokalnej:
    kopie plikow czyta z katalogu hosta, sprawdzenia i komendy odwrotne ida tym samym kontenerem pomocniczym.
    """
    reason = await hostexec.blocker()
    if reason:
        reply(session, tool_call, tr(f"Nie wykonam na hoscie: {reason}", f"Cannot run on the host: {reason}"))
        return
    image_ref = await hostexec.image()
    wrapped = hostexec.wrap(command, image_ref, session.cwd)
    plan = hostexec.host_plan(await safety.plan_for_command(command, session.cwd), image_ref, session.cwd) \
        if classification != "forbidden" else safety.Plan()
    async for event in run_classified(
            session, tool_call, wrapped, tool_name="execute_command", inner=command, shown=command,
            classification="forbidden" if classification == "forbidden" else "confirm",
            plan=plan, what=tr("Komenda na hoscie", "Host command"), always_ask=asked):
        yield event
