"""
Handler dla operacji Docker.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core.security import classify_command
from backend.core.session import Session, ConfirmationRequest


async def handle_docker_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie docker_manage."""
    docker_cmd = args.get("docker_command", "").strip()

    if not docker_cmd:
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "Błąd: pusta komenda docker",
        })
        return

    full_cmd = f"docker {docker_cmd}"
    classification = classify_command(full_cmd)

    if classification == "forbidden":
        from backend.core import audit
        await audit.log_blocked(session.interface, f"docker_manage({full_cmd})")
        yield f"[ODMOWA] Komenda docker `{docker_cmd}` jest zabroniona."
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "ODMOWA SYSTEMOWA",
        })
        return

    if classification == "confirm":
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="docker_manage",
            command=full_cmd,
            classification="confirm",
        )
        yield f"[POTWIERDZ] Operacja docker wymaga potwierdzenia: `{docker_cmd}`"
        return

    # Safe — wykonaj
    try:
        stdout, stderr, exit_code = await agent._executor.execute(full_cmd)
        result = f"[STDOUT]\n{stdout}\n[EXIT CODE]\n{exit_code}"
        if stderr:
            result += f"\n[STDERR]\n{stderr}"

        from backend.core import audit
        await audit.log_safe(session.interface, full_cmd, exit_code)
        yield result
    except Exception as exc:
        yield f"[ERROR] Błąd Docker: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": "Docker komenda wykonana",
    })
