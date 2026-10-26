"""
Handlery MCP: wywolanie narzedzia zewnetrznego serwera (`mcp__<serwer>__<narzedzie>`) i lista serwerow (mcp_manage).

Narzedzie MCP to cudzy kod — dziala w izolowanym kontenerze (core/mcp/registry.py), wiec do serwera Pipe nie
siegnie, ale moze zmieniac swoja usluge (repozytorium na GitHubie, baze danych). Dlatego:
  - odczyt (readOnlyHint) bez pytania, kazde inne wywolanie po TAK z widocznymi argumentami,
  - argument z sekretem nie wychodzi do cudzego serwera,
  - wynik to tresc z zewnatrz: sesja jak po czytaniu internetu (`web_tainted`, bez YOLO do nastepnej wiadomosci),
  - tryb obserwacji i viewer: tylko odczyty; dodanie serwera — zawsze po TAK.
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

from backend.core import audit, memory, runtime, safety
from backend.core.events import Attachment, Event
from backend.core.handlers.common import reply
from backend.core.i18n import tr
from backend.core.mcp.client import McpError, result_text
from backend.core.mcp.registry import (
    McpConfigError,
    add_server,
    config_path,
    describe_server,
    get_manager,
    has_secret,
    load_config,
    remove_state,
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
        reply(session, tool_call, tr("Narzedzie MCP jest niedostepne (serwer odlaczony albo usuniety). "
                                     "Lista: mcp_manage operation=list.",
                                     "The MCP tool is unavailable (server disconnected or removed). "
                                     "List: mcp_manage operation=list."))
        return
    state, tool = resolved
    label = f"{state.name}.{tool['name']}"
    shown = json.dumps(args, ensure_ascii=False, indent=2)
    if memory.find_secret(shown):
        reply(session, tool_call, tr("ODMOWA: argumenty zawieraja sekret — nie wysylam go do zewnetrznego serwera MCP.",
                                     "REFUSED: the arguments contain a secret — I do not send it to an external MCP server."))
        return
    ask = needs_confirmation(state.config, tool)
    if ask and (runtime.observe() or session.role == "viewer"):
        reply(session, tool_call, tr(f"ODMOWA: {label} moze zmieniac dane uslugi, a w tym trybie (obserwacja / viewer) "
                                     "dzialaja tylko narzedzia MCP oznaczone jako odczyt.",
                                     f"REFUSED: {label} may change the service's data, and in this mode (observe / "
                                     "viewer) only MCP tools marked as reads work."))
        return

    async def call() -> tuple[str, list[tuple[str, bytes]], bool]:
        result = await manager.call(state, tool, args)
        await audit.log_confirmed(session.interface, f"mcp {label}", 1 if result.get("isError") else 0)
        session.web_tainted = True          # tresc z zewnatrz: bez YOLO do nastepnej wiadomosci uzytkownika
        return result_text(result)

    if ask:
        async def confirmed() -> str:
            text, _images, is_error = await call()
            return (tr("BLAD NARZEDZIA MCP: ", "MCP TOOL ERROR: ") if is_error else "") + text

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="mcp", command=f"mcp {label} {json.dumps(args, ensure_ascii=False)}",
            classification="confirm", action=confirmed,
            plan=safety.Plan(notes=[tr(f"narzedzie zewnetrznego serwera MCP {state.name} (izolowany kontener) — "
                                       "skutkow w jego usludze Pipe nie cofnie",
                                       f"a tool of the external MCP server {state.name} (isolated container) — Pipe "
                                       "cannot undo its effects in that service")]),
        )
        yield tr(f"[POTWIERDZ] Narzedzie MCP {as_code(label)} wymaga potwierdzenia. Argumenty:\n{as_code(shown)}",
                 f"[POTWIERDZ] The MCP tool {as_code(label)} requires confirmation. Arguments:\n{as_code(shown)}")
        return

    try:
        text, images, is_error = await call()
    except (McpError, OSError) as exc:
        reply(session, tool_call, tr(f"Blad serwera MCP {state.name}: {exc}", f"MCP server {state.name} error: {exc}"))
        return
    for index, (mime, data) in enumerate(images[:3]):
        if mime.startswith("image/"):
            yield Attachment(f"mcp-{state.name}-{index}.{mime.split('/')[-1]}", mime, data, caption=label)
    note = tr(f"\n[{len(images)} obraz(y) wyslano uzytkownikowi]", f"\n[{len(images)} image(s) sent to the user]") \
        if images else ""
    reply(session, tool_call, (tr("BLAD NARZEDZIA MCP: ", "MCP TOOL ERROR: ") if is_error else "") + text + note)


async def handle_mcp_manage(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]
                            ) -> AsyncGenerator[Event, None]:
    """list | catalog | add (zawsze z potwierdzeniem; sama nazwa = dodatek z katalogu) | remove | reload."""
    from backend.core import addons

    operation = str(args.get("operation", "list") or "list").strip().lower()
    manager = get_manager()
    if operation == "catalog":
        installed = load_config()
        lines = []
        for addon in addons.catalog():
            state = tr("dodany", "added") if addon.name in installed else (
                tr("gotowy", "ready") if all(has_secret(s.name) for s in addon.secrets)
                else tr("brak kluczy: ", "missing keys: ") + ", ".join(s.name for s in addon.secrets if not has_secret(s.name)))
            lines.append(f"- {addon.name} ({addon.text(addon.title)}): {addon.text(addon.description)} [{state}]"
                         + (f" {tr('Po dodaniu', 'After adding')}: {addon.text(addon.login)}" if addon.login else ""))
        reply(session, tool_call, tr("Katalog gotowych dodatkow (add z sama nazwa):\n",
                                     "Catalog of ready add-ons (add with just the name):\n") + "\n".join(lines)
              + tr("\nKlucze uzytkownik wpisuje w oknie /mcp (pipe web albo CLI) — nigdy w rozmowie.",
                   "\nThe user enters keys in the /mcp window (pipe web or the CLI) — never in the chat."))
        return
    if operation == "list":
        lines = manager.status() or [f"{name}: " + tr("(nie polaczony)", "(not connected)") for name in load_config()]
        reply(session, tool_call, tr("Serwery MCP:\n", "MCP servers:\n") + ("\n".join(f"- {line}" for line in lines) if lines
                                  else tr("(brak — dodaj: operation=add)", "(none — add one: operation=add)")))
        return
    if operation == "reload":
        await manager.reload()
        reply(session, tool_call, tr("Polaczono ponownie:\n", "Reconnected:\n") + "\n".join(f"- {l}" for l in manager.status()))
        return
    name = str(args.get("name", "") or "").strip().lower()
    if operation == "remove":
        stateful = bool((load_config().get(name) or {}).get("state"))
        if remove_server(name):
            if stateful:
                await remove_state(name)
            await manager.reload()
            await audit.log_file_write(session.interface, f"mcp_servers.json#{name} (usuniety)", 0)
            yield tr(f"[PAMIEC] usunieto serwer MCP {name}", f"[PAMIEC] removed MCP server {name}")
            reply(session, tool_call, tr(f"Usunieto serwer MCP {name}.", f"Removed MCP server {name}."))
        else:
            reply(session, tool_call, tr(f"Nie ma serwera MCP {name!r}.", f"No MCP server {name!r}."))
        return
    if operation == "add":
        cfg = {k: args.get(k) for k in ("command", "args", "env", "image", "url", "headers", "askAlways", "description")
               if args.get(k) not in (None, "", [], {})}
        addon = addons.get(name) if not any(k in cfg for k in ("command", "image", "url")) else None
        if addon is not None:
            missing = [s.name for s in addon.secrets if not has_secret(s.name)]
            if missing:
                reply(session, tool_call, tr(
                    f"Dodatek {name} potrzebuje kluczy: {', '.join(missing)}. Popros uzytkownika, zeby wpisal je w oknie "
                    "/mcp (pipe web albo CLI) — nie w rozmowie — i powiedz, skad je wziac: "
                    + " ".join(addon.text(s.help) for s in addon.secrets if s.name in missing),
                    f"Add-on {name} needs keys: {', '.join(missing)}. Ask the user to enter them in the /mcp window "
                    "(pipe web or the CLI) — not in the chat — and say where to get them: "
                    + " ".join(addon.text(s.help) for s in addon.secrets if s.name in missing)))
                return
            cfg = {**addon.server, "description": addon.text(addon.description), "source": tr("katalog Pipe", "Pipe catalog"),
                   "addon": addon.name}
        try:
            clean = validate_server(name, cfg)
        except McpConfigError as exc:
            reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
            return

        async def do_add() -> str:
            created = add_server(name, clean)
            await manager.reload()
            await audit.log_file_write(session.interface, f"mcp_servers.json#{name}", 0)
            state = manager.servers.get(name)
            return (tr(f"{'Dodano' if created else 'Zaktualizowano'} serwer MCP {name}: ",
                       f"{'Added' if created else 'Updated'} MCP server {name}: ")
                    + (state.describe() if state else tr("brak stanu", "no state"))
                    + (f"\n{addon.text(addon.login)}" if addon is not None and addon.login else ""))

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="mcp_manage", command=tr(f"dodanie serwera MCP {name}", f"adding MCP server {name}"),
            classification="confirm", action=do_add,
            plan=safety.Plan(local_files=[(str(config_path()), tr("pamiec Pipe: mcp_servers.json",
                                                                  "Pipe memory: mcp_servers.json"))]),
        )
        yield tr(f"[POTWIERDZ] Nowy serwer MCP wymaga potwierdzenia — Pipe bedzie uruchamial w izolowanym kontenerze "
                 f"albo wywolywal: {as_code(describe_server(name, clean))}",
                 f"[POTWIERDZ] A new MCP server requires confirmation — Pipe will run in an isolated container or "
                 f"call: {as_code(describe_server(name, clean))}")
        return
    reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: list, catalog, add, remove, reload.",
                                 f"Unknown operation {operation!r}. Available: list, catalog, add, remove, reload."))
