"""
Handler dla operacji Git.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import executor
from backend.core.security import classify_command
from backend.core.session import Session, ConfirmationRequest
from backend.core.text import as_code


async def handle_git_command(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie git_command."""
    repo_path = args.get("repo_path", "").strip()
    subcommand = args.get("subcommand", "").strip()
    needs_confirm = args.get("requires_confirmation", False)

    if not repo_path or not subcommand:
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "Błąd: repo_path lub subcommand jest pusty",
        })
        return

    git_cmd = f"git -C {repo_path} {subcommand}"
    classification = classify_command(git_cmd)

    if classification == "forbidden":
        from backend.core import audit
        await audit.log_blocked(session.interface, f"git_command({git_cmd})")
        yield f"[ODMOWA] Komenda git {as_code(git_cmd)} jest zabroniona."
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "ODMOWA SYSTEMOWA",
        })
        return

    if classification == "confirm" or needs_confirm:
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="git_command",
            command=git_cmd,
            classification="confirm",
        )
        yield f"[POTWIERDZ] Git operacja wymaga potwierdzenia: {as_code(git_cmd)}"
        return

    # Safe — wykonaj
    try:
        stdout, stderr, exit_code = await executor.execute(git_cmd, cwd=f"/hostfs{session.cwd}")
        result = f"[STDOUT]\n{stdout}\n[EXIT CODE]\n{exit_code}"
        if stderr:
            result += f"\n[STDERR]\n{stderr}"

        from backend.core import audit
        await audit.log_safe(session.interface, git_cmd, exit_code)
    except Exception as exc:
        result = f"[ERROR] Błąd Git: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": result,
    })
