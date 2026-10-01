"""
Handler dla operacji wykonywania komend shell.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

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

    async for event in run_classified(session, tool_call, command, tool_name="execute_command",
                                      classification=classification):
        yield event
