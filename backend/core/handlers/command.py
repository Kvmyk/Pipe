"""
Handler dla operacji wykonywania komend shell.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import executor
from backend.core.security import classify_command
from backend.core.session import Session, ConfirmationRequest


async def handle_execute_command(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """
    Obsługuje narzędzie execute_command.
    
    Klasyfikuje komendę (safe/confirm/forbidden) i wykonuje odpowiednią akcję.
    """
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
        from backend.core import audit
        await audit.log_blocked(session.interface, f"execute_command({command})")
        yield f"[ODMOWA] Komenda `{command}` jest zabroniona."
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "ODMOWA SYSTEMOWA: Komenda jest zakazana.",
            }
        )
        return

    if classification == "confirm":
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="execute_command",
            command=command,
            classification="confirm",
        )
        yield f"[POTWIERDZ] Operacja wymaga potwierdzenia: `{command}`"
        return

    # Safe — wykonaj natychmiast
    try:
        stdout, stderr, exit_code = await executor.execute(command, cwd=f"/hostfs{session.cwd}")
        result = f"[STDOUT]\n{stdout}\n[EXIT CODE]\n{exit_code}"
        if stderr:
            result += f"\n[STDERR]\n{stderr}"
        
        from backend.core import audit
        await audit.log_safe(session.interface, command, exit_code)
    except Exception as exc:
        result = f"[ERROR] Błąd wykonania: {exc}"

    session.messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        }
    )
