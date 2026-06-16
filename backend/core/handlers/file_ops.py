"""
Handler dla operacji na plikach (read/write).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core.security import classify_file_write, validate_workspace_access
from backend.core.session import Session, ConfirmationRequest


async def handle_read_file(
    agent: Any,
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

    # Walidacja dostępu do workspace'u
    is_allowed, reason = validate_workspace_access(path)
    if not is_allowed:
        from backend.core import audit
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
        content = await agent._executor.read_file(path)
        from backend.core import audit
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


async def handle_write_file(
    agent: Any,
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

    # Walidacja dostępu do workspace'u
    is_allowed, reason = validate_workspace_access(path)
    if not is_allowed:
        from backend.core import audit
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
        from backend.core import audit
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
