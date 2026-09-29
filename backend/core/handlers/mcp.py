"""
Handlery MCP: wywolanie narzedzia zewnetrznego serwera (`mcp__<serwer>__<narzedzie>`)
i zarzadzanie lista serwerow (mcp_manage).

Narzedzie MCP to cudzy kod o dowolnych skutkach — domyslnie kazde wywolanie wymaga
potwierdzenia z widocznymi argumentami; bez pytania tylko to, co uzytkownik uznal za
bezpieczne (autoApprove / trustReadOnly w konfiguracji). Zatwierdzone wywolanie trafia
do dziennika zmian z adnotacja, ze skutkow Pipe nie cofnie.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

from backend.core import audit, safety
from backend.core.events import Attachment, Event
from backend.core.handlers.common import reply
from backend.core.mcp.client import McpError, result_text
from backend.core.mcp.registry import (
    McpConfigError,
    add_server,
    describe_server,
    get_manager,
    load_config,
    needs_confirmation,
    remove_server,
    validate_server,
)
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


async def handle_mcp_tool(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]
                          ) -> AsyncGenerator[Event, None]:
    manager = get_manager()
    resolved = manager.resolve(tool_call.function.name)
    if resolved is None:
        reply(session, tool_call, "Narzedzie MCP jest niedostepne (serwer odlaczony albo usuniety). "
                                  "Lista: mcp_manage operation=list.")
        return
    state, tool = resolved
    label = f"{state.name}.{tool['name']}"

    async def call() -> tuple[str, list[tuple[str, bytes]], bool]:
        result = await manager.call(state, tool, args)
        await audit.log_confirmed(session.interface, f"mcp {label}", 1 if result.get("isError") else 0)
        return result_text(result)

    if needs_confirmation(state.config, tool):
        async def confirmed() -> str:
            text, _images, is_error = await call()
            return ("BLAD NARZEDZIA MCP: " if is_error else "") + text

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="mcp", command=f"mcp {label} {json.dumps(args, ensure_ascii=False)}",
            classification="confirm", action=confirmed,
            plan=safety.Plan(notes=[f"narzedzie zewnetrznego serwera MCP {state.name} — skutkow Pipe nie cofnie"]),
        )
        shown = json.dumps(args, ensure_ascii=False, indent=2)
        yield (f"[POTWIERDZ] Narzedzie MCP {as_code(label)} wymaga potwierdzenia. Argumenty:\n{as_code(shown)}")
        return

    try:
        text, images, is_error = await call()
    except (McpError, OSError) as exc:
        reply(session, tool_call, f"Blad serwera MCP {state.name}: {exc}")
        return
    for index, (mime, data) in enumerate(images[:3]):
        if mime.startswith("image/"):
            yield Attachment(f"mcp-{state.name}-{index}.{mime.split('/')[-1]}", mime, data, caption=label)
    note = f"\n[{len(images)} obraz(y) wyslano uzytkownikowi]" if images else ""
    reply(session, tool_call, ("BLAD NARZEDZIA MCP: " if is_error else "") + text + note)


async def handle_mcp_manage(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]
                            ) -> AsyncGenerator[Event, None]:
    """list | add (z potwierdzeniem) | remove | reload."""
    operation = str(args.get("operation", "list") or "list").strip().lower()
    manager = get_manager()
    if operation == "list":
        lines = manager.status() or [f"{name}: (nie polaczony)" for name in load_config()]
        reply(session, tool_call, "Serwery MCP:\n" + ("\n".join(f"- {l}" for l in lines) if lines else "(brak — dodaj: operation=add)"))
        return
    if operation == "reload":
        await manager.reload()
        reply(session, tool_call, "Polaczono ponownie:\n" + "\n".join(f"- {l}" for l in manager.status()))
        return
    name = str(args.get("name", "") or "").strip().lower()
    if operation == "remove":
        if remove_server(name):
            await manager.reload()
            await audit.log_file_write(session.interface, f"mcp.json#{name} (usuniety)", 0)
            reply(session, tool_call, f"Usunieto serwer MCP {name}.")
        else:
            reply(session, tool_call, f"Nie ma serwera MCP {name!r}.")
        return
    if operation == "add":
        cfg = {k: args.get(k) for k in ("command", "args", "env", "url", "headers", "autoApprove", "trustReadOnly",
                                         "description") if args.get(k) not in (None, "", [], {})}
        try:
            clean = validate_server(name, cfg)
        except McpConfigError as exc:
            reply(session, tool_call, f"Blad: {exc}")
            return

        async def do_add() -> str:
            created = add_server(name, clean)
            await manager.reload()
            state = manager.servers.get(name)
            return (f"{'Dodano' if created else 'Zaktualizowano'} serwer MCP {name}: "
                    + (state.describe() if state else "brak stanu"))

        from backend.core.mcp.registry import config_path
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="mcp_manage", command=f"dodanie serwera MCP {name}",
            classification="confirm", action=do_add,
            plan=safety.Plan(local_files=[(str(config_path()), "pamiec Pipe: mcp.json")]),
        )
        yield (f"[POTWIERDZ] Nowy serwer MCP wymaga potwierdzenia — Pipe bedzie uruchamial/wywolywal: "
               f"{as_code(describe_server(name, clean))}")
        return
    reply(session, tool_call, f"Nieznana operacja {operation!r}. Dostepne: list, add, remove, reload.")
