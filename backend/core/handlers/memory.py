"""
Handlery pamieci agenta: SERVER.md i skille.

Zapis nie wymaga potwierdzenia — nie zmienia serwera, tylko notatki agenta
w DATA_DIR. Kazdy zapis trafia do audit logu (sama sciezka, bez tresci).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import audit, memory
from backend.core.session import Session


def _reply(session: Session, tool_call: Any, content: str) -> None:
    session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})


async def handle_server_md(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie server_md: read, update_section, write."""
    operation = (args.get("operation") or "read").strip()
    try:
        if operation == "read":
            result = memory.read_server_md() or "(SERVER.md jeszcze nie istnieje)"
        elif operation == "update_section":
            section = args.get("section", "")
            document = memory.update_section(memory.read_server_md(), section, args.get("content", ""))
            memory.write_server_md(document)
            await audit.log_file_write(session.interface, f"SERVER.md#{section.strip()}", 0)
            result = f"Zaktualizowano sekcje '{section.strip()}' w SERVER.md."
        elif operation == "write":
            memory.write_server_md(args.get("content", ""))
            await audit.log_file_write(session.interface, "SERVER.md", 0)
            result = "Zapisano SERVER.md."
        else:
            result = f"Nieznana operacja {operation!r}. Dostepne: read, update_section, write."
    except memory.MemoryWriteError as exc:
        result = f"Blad: {exc}"
    except OSError as exc:
        result = f"Blad zapisu SERVER.md: {exc}"

    _reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


async def handle_skill_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie skill_manage: list, read, save, delete."""
    operation = (args.get("operation") or "list").strip()
    name = args.get("name", "")
    try:
        if operation == "list":
            skills = memory.list_skills()
            result = "\n".join(f"- {s.name}: {s.description}" for s in skills) or "(brak zapisanych skilli)"
        elif operation == "read":
            skill = memory.read_skill(name)
            result = (f"# {skill.name}\n{skill.description}\n\n{skill.content}" if skill
                      else f"Brak skilla {name!r}. Dostepne: skill_manage, operation=list.")
        elif operation == "save":
            created = memory.save_skill(name, args.get("description", ""), args.get("content", ""))
            skill_name = memory.validate_skill_name(name)
            await audit.log_file_write(session.interface, f"skills/{skill_name}/SKILL.md", 0)
            result = f"{'Utworzono' if created else 'Zaktualizowano'} skill '{skill_name}'."
        elif operation == "delete":
            if memory.delete_skill(name):
                await audit.log_file_write(session.interface, f"skills/{memory.validate_skill_name(name)}/SKILL.md (usuniety)", 0)
                result = f"Usunieto skill '{name}'."
            else:
                result = f"Brak skilla {name!r} — nic nie usunieto."
        else:
            result = f"Nieznana operacja {operation!r}. Dostepne: list, read, save, delete."
    except memory.MemoryWriteError as exc:
        result = f"Blad: {exc}"
    except OSError as exc:
        result = f"Blad zapisu skilla: {exc}"

    _reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem
