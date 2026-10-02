"""
Server — backend VPS Agent.

Nasłuchuje równocześnie na:
  1. Unix socket (/tmp/vps-agent.sock) — dla klientów na tym samym serwerze (Telegram bot)
  2. TCP localhost:7379              — dla SSH tunnel z laptopa (CLI)

TCP port jest bindowany TYLKO na 127.0.0.1 — nie jest dostępny z zewnątrz.
Dostęp z laptopa odbywa się przez tunel SSH:
    ssh -L 7379:127.0.0.1:7379 user@serwer

Protokół JSON (linia po linii) — pełny opis w docs/protocol.md:
  Żądanie:  {"message": "tekst", "session_id": "uuid", "interface": "cli|telegram:123", "token": "..."}
  Żądanie:  {"confirm": true|false, "session_id": "uuid", "token": "..."}
  Żądanie:  {"command": "<nazwa>", "session_id": "uuid", ...}   (lista w _handle_command)
  (pole "token" wymagane tylko gdy AGENT_TOKEN jest ustawiony w .env)
  Odpowiedź: {"response": "tekst", "status": "ok|confirm|error", "done": true|false}
             + opcjonalnie "attachment" (plik, np. diagram PNG), "event" (postep, alert), "data"

Każda wiadomość może generować wiele odpowiedzi (streaming przez JSON lines).
Ostatnia odpowiedź ma "done": true. Wyjatek: {"command": "subscribe"} trzyma
polaczenie otwarte i przesyla zdarzenia czuwania ({"event": {...}}) az do rozlaczenia.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
from pathlib import Path

from backend.config import settings
from backend.core.i18n import CONFIRM_PHRASES, lang, prompt, tr
from backend.core import audit, diagram, incidents, journal, memory, metrics, routines, runtime, targets, usage
from backend.core.agent import get_agent
from backend.core.events import Activity, Attachment, Progress
from backend.core.watch import get_watcher

# Wiadomosc uzytkownika moze zawierac wklejone logi — domyslne 64 KiB to za malo.
READ_LIMIT = 4 * 1024 * 1024


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Obsługuje jedno połączenie klienta (Unix socket lub TCP)."""
    agent = get_agent()

    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break

            raw_str = raw.decode("utf-8", errors="replace").strip()
            if not raw_str:
                continue

            try:
                request = json.loads(raw_str)
            except json.JSONDecodeError as exc:
                await _send(writer, _error(tr(f"Błąd JSON: {exc}", f"JSON error: {exc}")))
                continue

            session_id = str(request.get("session_id", "default"))
            interface = str(request.get("interface", "cli"))

            # Token — PRZED jakakolwiek akcja, zeby nieuwierzytelniony klient nie mogl zatwierdzic
            # oczekujacej operacji. Token wyznacza tozsamosc i role (admin / viewer).
            auth = _authorize(request)
            if auth is None:
                await _send(writer, _error(tr("Blad: Nieprawidlowy token autoryzacji.", "Error: invalid authorization token.")))
                continue
            identity, role = auth
            # Sesja nalezy do klienta, ktory ja zalozyl — inny token nie przeczyta jej historii
            # ani nie zatwierdzi cudzej operacji, nawet znajac session_id.
            if not agent.owns(session_id, identity):
                await _send(writer, _error(tr("Ta sesja nalezy do innego klienta.", "This session belongs to another client.")))
                continue

            # Obsłuż potwierdzenie
            if "confirm" in request:
                if role == "viewer" and request["confirm"]:
                    await _send(writer, _error(tr("Rola viewer: tylko odczyt — nie mozesz zatwierdzac zmian.",
                                                  "Viewer role: read-only — you cannot approve changes.")))
                    continue
                await _stream(writer, agent.confirm(session_id, bool(request["confirm"])))
                continue

            # Subskrypcja zdarzen czuwania — polaczenie zostaje otwarte do rozlaczenia klienta
            if request.get("command") == "subscribe":
                await _subscribe(reader, writer)
                break

            # Komendy klientow: /server, /skille, /mapa... i skille jako komendy
            if "command" in request:
                await _handle_command(writer, agent, request, session_id, interface, identity, role)
                continue

            # Obsłuż wiadomość
            message = request.get("message", "").strip()
            if not message:
                await _send(writer, _error(tr("Pusta wiadomość.", "Empty message.")))
                continue

            await _stream_chat(writer, agent, session_id, message, interface, owner=identity, role=role)

    except ConnectionResetError:
        pass
    except Exception as exc:
        try:
            await _send(writer, _error(tr(f"Błąd serwera: {exc}", f"Server error: {exc}")))
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _authorize(request: dict) -> tuple[str, str] | None:
    """(tozsamosc, rola) dla tokenu z zadania albo None — patrz core/auth.py (hmac.compare_digest)."""
    from backend.core.auth import authorize

    return authorize(str(request.get("token", "") or ""))


def event_frame(event) -> dict:
    """Zdarzenie agenta -> ramka protokolu (done=false)."""
    if isinstance(event, Attachment):
        return {"response": "", "status": "ok", "done": False, "attachment": event.to_wire()}
    if isinstance(event, (Progress, Activity)):
        return {"response": "", "status": "ok", "done": False, "event": event.to_wire()}
    chunk = str(event)
    if "[POTWIERDZ]" in chunk and any(phrase in chunk for phrase in CONFIRM_PHRASES):
        status = "confirm"
    elif "[BLAD]" in chunk or "[ODMOWA]" in chunk:
        status = "error"
    else:
        status = "ok"
    return {"response": chunk, "status": status, "done": False}


async def _stream(writer, events, texts: list[str] | None = None) -> int:
    count = 0
    async for event in events:
        count += 1
        if texts is not None and isinstance(event, str):
            texts.append(event)
        await _send(writer, event_frame(event))
    await _send(writer, {"response": "", "status": "ok", "done": True})
    return count


async def _stream_chat(writer, agent, session_id: str, message: str, interface: str, *,
                       generated: bool = False, owner: str = "", role: str = "admin") -> list[str]:
    """Przekazuje wiadomosc agentowi i streamuje odpowiedz (JSON lines, ostatnia z done=true). Zwraca teksty."""
    print(f"[server] Wiadomość od {interface}: {message[:80]}", flush=True)
    kwargs: dict = {"owner": owner, "role": role}
    if generated:
        kwargs["generated"] = True
    texts: list[str] = []
    count = await _stream(writer, agent.chat(session_id, message, interface, **kwargs), texts)
    print(f"[server] Odpowiedź wysłana ({count} fragmentów)", flush=True)
    return texts


def _data(data: dict) -> dict:
    return {"response": "", "status": "ok", "done": True, "data": data}


def _error(text: str) -> dict:
    return {"response": f"[BLAD] {text}", "status": "error", "done": True}


async def _handle_command(writer, agent, request: dict, session_id: str, interface: str,
                          identity: str = "open", role: str = "admin") -> None:
    """
    Komendy klientow. Czesc zwraca dane w polu "data" (bez udzialu LLM), czesc
    wysyla agentowi wiadomosc zbudowana tutaj — klient nie sklada promptow.
    """
    command = str(request.get("command", "")).strip()
    args = str(request.get("args", "") or "").strip()
    user_key = memory.vibe_key(interface)
    chat_as = {"owner": identity, "role": role}
    try:
        if command == "list_skills":
            await _send(writer, _data({"skills": memory.skill_commands()}))
        elif command == "skill":
            # tresc jednego skilla (podglad w interfejsie webowym); nazwa albo komenda
            entry = memory.find_skill_command(str(request.get("name", "")))
            skill = memory.read_skill(entry["name"]) if entry else None
            if skill is None:
                await _send(writer, _error(tr(f"Nie ma skilla {request.get('name', '')!r}.",
                                              f"No skill {request.get('name', '')!r}.")))
                return
            await _send(writer, _data({"name": skill.name, "description": skill.description,
                                       "content": skill.content, "command": entry["command"]}))
        elif command == "server_md":
            await _send(writer, _data({"content": memory.read_server_md()}))
        elif command == "scan_server":
            message = prompt("SCAN_SERVER_UPDATE") if memory.read_server_md().strip() else prompt("SCAN_SERVER_CREATE")
            await _stream_chat(writer, agent, session_id, message, interface, generated=True, **chat_as)
        elif command == "status":
            await _stream_chat(writer, agent, session_id, prompt("STATUS_MESSAGE"), interface, generated=True)
        elif command == "update":
            # /aktualizuj [sprawdz] — aktualizacja samego Pipe narzedziem pipe_update (potwierdzenie jak zwykle)
            check = args.lower() in ("sprawdz", "sprawdź", "check", "wersja", "version")
            message = prompt("UPDATE_CHECK_MESSAGE" if check else "UPDATE_APPLY_MESSAGE")
            await _stream_chat(writer, agent, session_id, message, interface, generated=True, **chat_as)
        elif command == "run_skill":
            entry = memory.find_skill_command(str(request.get("name", "")))
            if entry is None:
                await _send(writer, _error(tr(f"Nie ma skilla {request.get('name', '')!r}. Lista: /skille",
                                              f"No skill {request.get('name', '')!r}. List: /skills")))
                return
            message = prompt("RUN_SKILL_MESSAGE").format(name=entry["name"], command=entry["command"] or entry["name"])
            if args:
                message += prompt("RUN_SKILL_EXTRA").format(args=args)
            await _stream_chat(writer, agent, session_id, message, interface, generated=True, **chat_as)
        elif command == "history":
            await _send(writer, _data({"entries": await audit.get_recent(15)}))
        elif command == "directory":
            entries = memory.load_directory()
            await _send(writer, _data({"entries": [e.__dict__ for e in entries], "text": memory.render_directory(entries)}))
        elif command == "vibe":
            if args.lower() in ("reset", "wyczysc", "wyczyść"):
                removed = memory.reset_vibe(user_key)
                await _send(writer, _data({"content": "", "reset": removed}))
            else:
                await _send(writer, _data({"content": memory.read_vibe(user_key)}))
        elif command == "alerts":
            watcher = get_watcher()
            await _send(writer, _data({
                "active": [a.to_event() for a in watcher.active.values()],
                "recent": watcher.notifier.history[-10:],
                "enabled": settings.WATCH_ENABLED,
                "subscribers": watcher.notifier.subscribers,
            }))
        elif command == "targets":
            await _send(writer, _data({"targets": [t.describe() for t in targets.load_targets()]}))
        elif command == "routines":
            await _send(writer, _data({"routines": [r.describe() for r in routines.load_routines()]}))
        elif command == "reminders":
            await _reminders(writer, request, interface, role)
        elif command == "graph":
            from backend.core import graph, infra
            found = await infra.discover()
            await _send(writer, _data(graph.build(found, [a.to_event() for a in get_watcher().active.values()])))
        elif command == "journal_changes":
            try:
                entry = journal.load(str(request.get("id", ""))) if request.get("id") else None
            except ValueError:
                entry = None
            if entry is None:
                await _send(writer, _error(tr("Nie ma takiego wpisu w dzienniku.", "No such journal entry.")))
                return
            await _send(writer, _data(journal.changes(entry)))
        elif command == "diagram":
            await _send_infra_diagram(writer, args)
        elif command == "investigate":
            alert = get_watcher().find_alert(str(request.get("id", "")))
            if alert is None:
                await _send(writer, _error(tr("Nie znam tego alertu (serwer mogl zostac zrestartowany).",
                                              "I do not know this alert (the server may have been restarted).")))
                return
            message = prompt("INVESTIGATE_ALERT_MESSAGE").format(title=alert["title"], detail=alert["detail"])
            message += incidents.context_for(alert.get("key", ""))
            message += await _recent_changes_context()
            texts = await _stream_chat(writer, agent, session_id, message, interface, generated=True, **chat_as)
            # Ustalenia agenta zostaja przy incydencie — nastepnym razem beda punktem wyjscia.
            answer = next((t for t in reversed(texts) if t.strip() and not t.lstrip().startswith("[")), "")
            incidents.note_investigation(alert.get("key", ""), answer)
        elif command == "changes":
            from backend.core.handlers.history import changes_text
            hours = metrics.parse_hours(args, default=24)
            await _send(writer, _data({"text": await changes_text(hours), "hours": hours}))
        elif command == "chart":
            await _send_chart(writer, args)
        elif command == "health":
            from backend.core.handlers.history import checks_text
            await _send(writer, _data({"text": await checks_text()}))
        elif command == "digest":
            result = await get_watcher().build_digest(fresh_checks=True)
            if result.attachment is not None:
                await _send(writer, {"response": "", "status": "ok", "done": False,
                                     "attachment": result.attachment.to_wire()})
            event = result.to_event()
            event.pop("attachment", None)
            await _send(writer, _data(event))
        elif command == "audit":
            from backend.core.handlers.audit import run_audit_text
            audit, _ = await run_audit_text()
            await _send(writer, _data(audit.to_data()))
        elif command == "welcome":
            from backend.core import welcome
            watcher = get_watcher()
            event = await welcome.build(checks_report=watcher.checks_report, digest_time=settings.DIGEST_TIME,
                                        cert_days=settings.WATCH_CERT_DAYS)
            attachment = event.pop("attachment", None)
            if attachment:
                await _send(writer, {"response": "", "status": "ok", "done": False, "attachment": attachment})
            await _send(writer, _data(event))
        elif command == "mcp":
            # Most MCP (stdio w CLI): jedna wiadomosc JSON-RPC -> odpowiedz (None dla powiadomien).
            from backend.core.mcp.server import Caller, handle as mcp_handle
            await _send(writer, _data({"rpc": await mcp_handle(request.get("rpc"), Caller(identity, role))}))
        elif command == "mcp_servers":
            from backend.core.mcp.registry import get_manager, load_config
            lines = get_manager().status() or [tr(f"{n}: (nie polaczony)", f"{n}: (not connected)") for n in load_config()]
            await _send(writer, _data({"servers": lines}))
        elif command == "approvals":
            from backend.core.approvals import get_approvals
            await _send(writer, _data({"pending": [a.to_event() for a in get_approvals().pending()]}))
        elif command == "approve":
            await _approve(writer, request, identity, role)
        elif command == "incidents":
            await _send(writer, _data({"text": incidents.render(),
                                       "incidents": [i.__dict__ for i in incidents.recent(15)]}))
        elif command == "journal":
            await _send(writer, _data({"entries": [journal.as_data(e) for e in journal.entries(15)],
                                       "text": journal.render_list()}))
        elif command == "undo":
            if role == "viewer" and request.get("execute"):
                await _send(writer, _error(tr("Rola viewer: tylko odczyt — cofniecie moze wykonac administrator.",
                                              "Viewer role: read-only — an administrator can run the undo.")))
                return
            await _undo(writer, request, interface)
        elif command == "transcribe":
            await _transcribe(writer, request)
        elif command in ("providers", "provider_models", "provider_set", "provider_forget"):
            await _providers(writer, command, request, interface, role)
        elif command == "usage":
            priced = bool(settings.LLM_PRICE_IN or settings.LLM_PRICE_OUT)
            await _send(writer, _data(usage.report(7, priced=priced, token_limit=settings.DAILY_TOKEN_LIMIT,
                                                   cost_limit=settings.DAILY_COST_LIMIT)))
        else:
            await _send(writer, _error(tr(f"Nieznana komenda: {command!r}", f"Unknown command: {command!r}")))
    except OSError as exc:
        await _send(writer, _error(tr(f"Blad odczytu pamieci agenta: {exc}", f"Error reading the agent's memory: {exc}")))


async def _undo(writer, request: dict, interface: str) -> None:
    """
    /cofnij bez LLM (dziala tez, gdy provider lezy albo skonczyl sie limit): najpierw podglad,
    potem — po TAK w kliencie — {"command": "undo", "id": ..., "execute": true}.
    """
    entry_id = str(request.get("id") or request.get("args") or "").strip().lstrip("#").lower()
    try:
        entry = journal.load(entry_id) if entry_id else journal.latest_undoable()
    except ValueError as exc:
        await _send(writer, _error(str(exc)))
        return
    if entry is None:
        await _send(writer, _error(tr("Nie ma takiego wpisu w dzienniku (/dziennik).", "No such journal entry (/journal).")
                                   if entry_id else tr("Nie ma zmiany, ktora da sie cofnac.",
                                                       "There is no change that can be undone.")))
        return
    if not request.get("execute"):
        await _send(writer, _data({"id": entry.id, "undoable": entry.undoable, "preview": journal.preview(entry)}))
        return
    if not entry_id:
        await _send(writer, _error(tr("Cofniecie wymaga identyfikatora wpisu z podgladu.",
                                      "Undo needs the entry id from the preview.")))
        return
    await _send(writer, _data({"id": entry.id, "text": await journal.rollback(entry, interface)}))


async def _providers(writer, command: str, request: dict, interface: str, role: str) -> None:
    """
    Wybor providera LLM w trakcie pracy (ekran wyboru i przelacznik w `pipe web`), bez LLM:
    `providers` — lista i aktywny; `provider_models` — modele providera (sprawdza tez podany klucz);
    `provider_set` — zapis klucza/modelu i przelaczenie; `provider_forget` — usuniecie klucza z interfejsu.
    Klucz przychodzi w polu `key` i nigdy nie wraca do klienta ani do logow.
    """
    from backend import configure
    from backend.core import llm

    base = settings.LLM
    state = lambda: {**llm.listing(base), "can_edit": role == "admin"}      # noqa: E731
    if command == "providers":
        await _send(writer, _data(state()))
        return
    if role != "admin":
        await _send(writer, _error(tr("Providera moze zmieniac tylko administrator.",
                                      "Only an administrator can change the provider.")))
        return
    provider_id = str(request.get("name", "") or "").strip().lower()
    try:
        new_key = llm.clean_key(str(request.get("key", "") or ""))
        name, base_url, stored_key, requires_key = llm.endpoint(provider_id, base)

        async def models(key: str) -> list[str]:
            if requires_key and not key:
                raise llm.LlmError(tr(f"Podaj klucz API providera {name}.", f"Enter the API key for {name}."))
            try:
                return await asyncio.to_thread(configure.fetch_models, base_url, key)
            except configure.ApiError as exc:
                if exc.status in (401, 403):
                    raise llm.LlmError(tr(f"{name} odrzucil ten klucz API ({exc}).",
                                          f"{name} rejected this API key ({exc}).")) from None
                raise llm.LlmError(tr(f"Nie udalo sie polaczyc z {name}: {exc}",
                                      f"Could not reach {name}: {exc}")) from None

        if command == "provider_models":
            found = await models(new_key or stored_key)
            await _send(writer, _data({"models": found[:400], "total": len(found)}))
        elif command == "provider_set":
            if new_key:
                await models(new_key)          # nowy klucz musi dzialac, zanim cokolwiek zapiszemy
            current = llm.select(provider_id, base, key=new_key, model=str(request.get("model", "") or ""))
            await audit.log_file_write(interface, f"{llm.STORE} (provider={current.provider_id}, model={current.model}"
                                                  f"{', new key' if new_key else ''})", 0)
            print(f"[server] Provider LLM: {current.provider_name} / {current.model} (zmienil {interface})", flush=True)
            await _send(writer, _data(state()))
        else:
            if llm.forget(provider_id, base):
                await audit.log_file_write(interface, f"{llm.STORE} (forget key: {provider_id})", 0)
            await _send(writer, _data(state()))
    except llm.LlmError as exc:
        await _send(writer, _error(str(exc)))


async def _reminders(writer, request: dict, interface: str, role: str) -> None:
    """
    Przypomnienia bez LLM: lista, anulowanie (`cancel`: id) i odbior zaleglych (`claim`: true) —
    klient bez subskrypcji (CLI) pobiera tak przypomnienia, ktore odpalily, gdy nikt nie sluchal.
    """
    from backend.core import reminders

    cancel_id = str(request.get("cancel", "") or "").strip()
    if cancel_id:
        found = reminders.get(cancel_id)
        if found is None:
            await _send(writer, _error(tr("Nie ma takiego przypomnienia.", "No such reminder.")))
            return
        if role == "viewer" and not reminders.owned_by(found, interface):
            await _send(writer, _error(tr("Rola viewer: mozesz anulowac tylko wlasne przypomnienia.",
                                          "Viewer role: you can cancel only your own reminders.")))
            return
        reminders.remove(found.id)
        get_watcher().reminders_changed()
        await audit.log_file_write(interface, f"reminders.json#{found.id} (cancel)", 0)
    claimed = reminders.claim(interface.split(":", 1)[0]) if request.get("claim") else []
    items = reminders.load_reminders()
    await _send(writer, _data({
        "text": reminders.render_list(items),
        "reminders": [{"id": r.id, "due": r.when, "kind": r.kind, "text": r.text, "fired": r.fired} for r in items],
        "claimed": [r.to_event() for r in claimed],
        "cancelled": cancel_id if cancel_id else "",
        "subscribers": get_watcher().notifier.subscribers,
    }))


async def _approve(writer, request: dict, identity: str, role: str) -> None:
    """Decyzja administratora o zgodzie dla zewnetrznego agenta (MCP)."""
    from backend.core.approvals import ApprovalError, get_approvals

    if role != "admin":
        await _send(writer, _error(tr("Zgody moze zatwierdzac tylko administrator.", "Only an administrator can approve.")))
        return
    try:
        approval = await get_approvals().decide(
            str(request.get("id", "")), bool(request.get("decision")), identity,
            cwd=runtime.to_local("/"), auto_restore=settings.SAFE_AUTO_ROLLBACK,
            sites_enabled=settings.WATCH_SITES)
    except ApprovalError as exc:
        await _send(writer, _error(str(exc)))
        return
    await _send(writer, _data({"id": approval.id, "status": approval.status, "text": approval.describe()}))


async def _transcribe(writer, request: dict) -> None:
    """Wiadomosc glosowa -> tekst (klient wysyla potem tekst jak zwykla wiadomosc)."""
    import base64
    import binascii

    from backend.core import voice

    try:
        data = base64.b64decode(str(request.get("audio", "")), validate=True)
    except (binascii.Error, ValueError):
        await _send(writer, _error(tr("Nieprawidlowe nagranie (oczekiwano base64).", "Invalid recording (expected base64).")))
        return
    try:
        usage.check_budget(settings.DAILY_TOKEN_LIMIT, settings.DAILY_COST_LIMIT)
        text = await voice.transcribe(data, str(request.get("filename", "") or "voice.ogg"),
                                      voice.resolve(dict(os.environ), settings.LLM))
    except (voice.TranscriptionError, usage.BudgetExceeded) as exc:
        await _send(writer, _error(str(exc)))
        return
    await _send(writer, _data({"text": text}))


async def _recent_changes_context(hours: float = 24) -> str:
    """Zmiany na serwerze z ostatniej doby — kontekst do badania alertu (czeste zrodlo awarii)."""
    from backend.core.handlers.history import changes_text
    try:
        text = await changes_text(hours)
    except Exception as exc:
        return tr(f"\n\n(Nie udalo sie odczytac historii zmian: {exc})",
                  f"\n\n(Could not read the change history: {exc})")
    return tr(f"\n\nZmiany na serwerze z ostatnich {int(hours)} h (migawki Pipe — sprawdz, czy ktoras "
              f"mogla wywolac problem):\n{text}",
              f"\n\nChanges on the server in the last {int(hours)} h (Pipe snapshots — check whether one of them "
              f"could have caused the problem):\n{text}")


async def _send_chart(writer, args: str) -> None:
    """/wykres [load|ram|dysk] [24h] — obraz z historii czuwania, bez LLM."""
    from backend.core.handlers.history import chart_attachment

    metric_name, hours_text = "load", ""
    for word in args.split():
        if metrics.normalize_metric(word):
            metric_name = word
        else:
            hours_text = word
    metric = metrics.normalize_metric(metric_name) or "load"
    attachment, text = await chart_attachment(metric, metrics.parse_hours(hours_text, default=24))
    if attachment is not None:
        await _send(writer, {"response": "", "status": "ok", "done": False, "attachment": attachment.to_wire()})
    await _send(writer, _data({"summary": text, "metric": metric, "image": attachment is not None}))


async def _send_infra_diagram(writer, title: str) -> None:
    """/mapa — mapa infrastruktury bez udzialu LLM (szybko i za darmo)."""
    from backend.core.handlers.diagram import attachment_for, build_infra_diagram

    title = title or tr("Mapa infrastruktury", "Infrastructure map")
    found, source = await build_infra_diagram(title)
    try:
        rendered = await diagram.render(source)
    except diagram.DiagramError as exc:
        await _send(writer, _error(tr(f"Nie udalo sie narysowac mapy: {exc}", f"Could not draw the map: {exc}")))
        return
    attachment = attachment_for(rendered, title, tr("mapa-infrastruktury", "infrastructure-map"))
    if attachment is None:
        await _send(writer, {"response": f"```mermaid\n{rendered.source}\n```", "status": "ok", "done": False})
    else:
        await _send(writer, {"response": "", "status": "ok", "done": False, "attachment": attachment.to_wire()})
    await _send(writer, {"response": "", "status": "ok", "done": True, "data": {"summary": found.summary()}})


async def _subscribe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Przesyla zdarzenia czuwania (alerty, raporty rutyn) do rozlaczenia klienta."""
    notifier = get_watcher().notifier
    queue = notifier.subscribe()
    await _send(writer, {"response": "", "status": "ok", "done": False, "event": {"type": "subscribed"}})
    get_watcher().reminders_changed()      # przypomnienia, ktore odpalily, gdy nikt nie sluchal
    closed = asyncio.ensure_future(reader.read(1))
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({closed, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await _send(writer, {"response": "", "status": "ok", "done": False, "event": getter.result()})
            else:
                getter.cancel()
            if closed in done:
                return
    finally:
        closed.cancel()
        notifier.unsubscribe(queue)


async def _send(writer: asyncio.StreamWriter, data: dict) -> None:
    """Wysyła odpowiedź JSON zakończoną newline."""
    line = json.dumps(data, ensure_ascii=False) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()


async def _report_model_status() -> None:
    warning = await get_agent().verify_model()
    if warning:
        print(tr(f"[VPS Agent] [OSTRZEZENIE] {warning}", f"[VPS Agent] [WARNING] {warning}"), flush=True)


async def main() -> None:
    """Punkt wejścia serwera."""
    try:
        settings.validate()
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    socket_path = settings.AGENT_SOCKET
    tcp_host = settings.TCP_HOST
    tcp_port = settings.TCP_PORT

    # ─── Unix socket ───────────────────────────────────────────────────────
    socket_file = Path(socket_path)
    if socket_file.exists():
        socket_file.unlink()

    unix_server = await asyncio.start_unix_server(
        handle_client,
        path=socket_path,
        limit=READ_LIMIT,
    )
    os.chmod(socket_path, 0o600)

    # ─── TCP (localhost only — dla SSH tunnel) ────────────────────────────
    tcp_server = await asyncio.start_server(
        handle_client,
        host=tcp_host,       # w kontenerze 0.0.0.0; na hoscie publikowany tylko na 127.0.0.1
        port=tcp_port,
        limit=READ_LIMIT,
    )

    print(f"[VPS Agent] Unix socket : {socket_path}", flush=True)
    print(f"[VPS Agent] TCP         : {tcp_host}:{tcp_port} ({tr('tylko localhost — użyj SSH tunnel', 'localhost only — use an SSH tunnel')})", flush=True)
    print(f"[VPS Agent] Provider    : {settings.LLM.provider_name} ({settings.LLM.base_url})", flush=True)
    print(f"[VPS Agent] Model       : {settings.LLM.model}", flush=True)
    print(f"[VPS Agent] Audit log   : {settings.AUDIT_LOG_PATH}", flush=True)
    print(f"[VPS Agent] Runtime     : {runtime.kind()} (host: {runtime.workspace_root()}, proc: {runtime.host_proc()})", flush=True)
    print(f"[VPS Agent] {tr('Diagramy', 'Diagrams')}    : "
          f"{'mermaidx' if diagram.available() else tr('brak renderera (tylko kod Mermaid)', 'no renderer (Mermaid source only)')}",
          flush=True)
    watch_state = tr(f"co {settings.WATCH_INTERVAL} s", f"every {settings.WATCH_INTERVAL} s") if settings.WATCH_ENABLED \
        else tr("wylaczone", "disabled")
    print(f"[VPS Agent] {tr('Czuwanie', 'Watcher ')}    : {watch_state}", flush=True)
    print(f"[VPS Agent] {tr('Jezyk   ', 'Language')}    : {lang()}", flush=True)
    print(tr("[VPS Agent] Serwer gotowy. Ctrl+C aby zatrzymać.", "[VPS Agent] Server ready. Ctrl+C to stop."), flush=True)

    # Skille wbudowane (backend/skills_builtin) — nowe i zaktualizowane, bez nadpisywania zmian uzytkownika
    try:
        seeded = memory.seed_builtin_skills()
        if seeded:
            print(tr("[VPS Agent] Skille wbudowane: ", "[VPS Agent] Built-in skills: ") + ", ".join(seeded), flush=True)
    except OSError as exc:
        print(tr(f"[VPS Agent] [OSTRZEZENIE] Nie zainstalowano skilli wbudowanych: {exc}",
                 f"[VPS Agent] [WARNING] Built-in skills were not installed: {exc}"), flush=True)

    # Weryfikacja modelu w tle — nie blokuje startu, a wycofany model
    # (np. po latach bez aktualizacji .env) od razu widac w logach.
    asyncio.create_task(_report_model_status())
    # Czuwanie i rutyny — proaktywne alerty dla subskrybentow (bot Telegram)
    get_watcher(get_agent()).start()
    # Po aktualizacji samego Pipe (pipe_update): raport dla tego, kto ja zlecil
    from backend.core import selfupdate
    asyncio.create_task(selfupdate.resume(get_watcher()))

    from backend.core.mcp.registry import get_manager, load_config
    from backend.core.mcp.server import start_http as start_mcp_http
    if load_config():
        asyncio.create_task(get_manager().reload())     # serwery MCP lacza sie w tle
    mcp_server = await start_mcp_http(settings.MCP_HOST, settings.MCP_PORT,
                                      {o.strip() for o in settings.MCP_ALLOWED_ORIGINS.split(",") if o.strip()})
    if mcp_server is not None:
        print(f"[VPS Agent] MCP (HTTP)  : http://{settings.MCP_HOST}:{settings.MCP_PORT}/mcp", flush=True)

    from backend.core import webhooks
    hook_server = await webhooks.start(get_watcher(), settings.WEBHOOK_HOST, settings.WEBHOOK_PORT,
                                       settings.WEBHOOK_TOKEN, settings.WEBHOOK_INVESTIGATE)
    if hook_server is not None:
        print(f"[VPS Agent] {tr('Webhooki', 'Webhooks')}    : http://{settings.WEBHOOK_HOST}:{settings.WEBHOOK_PORT}/hook/"
              f"<{tr('zrodlo', 'source')}>", flush=True)

    async with unix_server, tcp_server:
        await asyncio.gather(
            unix_server.serve_forever(),
            tcp_server.serve_forever(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(tr("\n[VPS Agent] Zatrzymano.", "\n[VPS Agent] Stopped."), flush=True)
