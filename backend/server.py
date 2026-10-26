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
            + opcjonalnie "attachments": [{"name", "mime", "data" (base64)}] (core/attachments.py)
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
import contextvars
import hmac
import json
import os
import sys
import time
from pathlib import Path

from backend.config import settings
from backend.core import i18n
from backend.core.i18n import CONFIRM_PHRASES, lang, prompt, tr
from backend.core import audit, diagram, incidents, journal, memory, metrics, routines, runtime, targets, usage
from backend.core.agent import get_agent
from backend.core.events import Activity, Attachment, Progress
from backend.core.watch import get_watcher

# Wiadomosc moze niesc zalaczniki (do 20 MB razem, base64 +33%) — domyslne 64 KiB to za malo.
READ_LIMIT = 32 * 1024 * 1024


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
                await _remembered_command(writer, agent, request, session_id, interface, identity, role)
                continue

            # Obsłuż wiadomość (z zalacznikami: pliki, zdjecia, zrzuty ekranu)
            from backend.core import attachments
            message = str(request.get("message", "") or "").strip()
            try:
                uploads = attachments.parse(request.get("attachments"))
            except attachments.AttachmentError as exc:
                await _send(writer, _error(str(exc)))
                continue
            if not message and not uploads:
                await _send(writer, _error(tr("Pusta wiadomość.", "Empty message.")))
                continue

            await _stream_chat(writer, agent, session_id, message, interface, owner=identity, role=role,
                               attachments=uploads)

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
                       generated: bool = False, owner: str = "", role: str = "admin",
                       attachments: list | None = None) -> list[str]:
    """Przekazuje wiadomosc agentowi i streamuje odpowiedz (JSON lines, ostatnia z done=true). Zwraca teksty."""
    files = f" (+{len(attachments)} zal.)" if attachments else ""
    print(f"[server] Wiadomość od {interface}: {message[:80]}{files}", flush=True)
    kwargs: dict = {"owner": owner, "role": role}
    if generated:
        kwargs["generated"] = True
    if attachments:
        kwargs["attachments"] = attachments
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
        elif command in ("status", "overview"):
            # stan serwera bez modelu: od razu i za darmo; tekst w `response` dla starszych klientow
            data = await _overview(interface)
            if command == "status":
                await _send(writer, {"response": data["text"], "status": "ok", "done": False})
            await _send(writer, _data(data))
        elif command == "feed":
            await _feed(writer, request, interface)
        elif command == "feed_act":
            await _feed_act(writer, agent, request, session_id, interface, identity, role)
        elif command == "prefs":
            await _prefs(writer, request, interface, role)
        elif command == "cleanup":
            await _cleanup(writer, agent, session_id, interface, role)
        elif command == "web":
            await _web(writer, agent, request, session_id, interface, identity, role)
        elif command == "mcp_servers":
            await _mcp_servers(writer, request, role)
        elif command == "install":
            await _install(writer, request, interface, role)
        elif command == "projects":
            await _projects(writer, request, interface, role)
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
        elif command == "new_chat":
            # /nowa — rozmowa od czystej karty: historia, oczekujace potwierdzenie i workery znikaja z pamieci
            # i z dysku. Wiedza o serwerze zostaje: prompt systemowy co zapytanie sklada sie od nowa z SERVER.md,
            # DIRECTORY, skilli, VIBE i alertow. Tylko wlasna sesja (agent.owns() sprawdzone wyzej).
            await _send(writer, _data({"cleared": agent.delete_session(session_id)}))
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
            if runtime.observe() and request.get("execute"):
                await _send(writer, _error(tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe niczego nie zmienia na serwerze, "
                                              "takze nie cofa zmian.",
                                              "Observe mode (PIPE_OBSERVE=1): Pipe changes nothing on the server, "
                                              "undo included.")))
                return
            await _undo(writer, request, interface)
        elif command == "patch":
            await _patch(writer, request, interface, role, session_id)
        elif command == "vulns":
            text, view = await _vulns_report()
            await _send(writer, _data({"text": text, "view": view}))
        elif command == "transcribe":
            await _transcribe(writer, request, interface)
        elif command in ("providers", "provider_models", "provider_set", "provider_forget"):
            await _providers(writer, command, request, interface, role)
        elif command == "language":
            await _language(writer, args, interface, role)
        elif command == "view":
            await _view(writer, args, interface, role)
        elif command == "yolo":
            await _yolo(writer, agent, args, session_id, interface, identity, role)
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


async def _overview(interface: str) -> dict:
    """Stan serwera bez LLM (core/overview.py): pulpit pipe web, /status w CLI i na Telegramie."""
    from backend.core import feed, infra, overview, projects

    watcher = get_watcher()
    containers = await infra.docker_containers()
    found = projects.load()
    states = await projects.statuses(found) if found else {}
    listed = [{"name": p.name, "stack": p.stack, "url": p.run.get("url", ""), "strategy": p.run.get("strategy", ""),
               "status": states.get(p.name, {}).get("status", "none"), "source": p.url} for p in found]
    return overview.build(
        active=list(watcher.active.values()), checks_report=watcher.checks_report, containers=containers,
        projects=listed, unread=feed.unread(memory.vibe_key(interface)), usage_today=usage.day_summary(),
        hostname=infra.host_identity()[0], disk_pct=settings.WATCH_DISK_PCT, mem_pct=settings.WATCH_MEM_PCT,
        load_factor=settings.WATCH_LOAD_FACTOR)


async def _feed(writer, request: dict, interface: str) -> None:
    """
    Skrzynka (core/feed.py): wiadomosci, ktore Pipe wyslal sam, z liczba nowych dla tego odbiorcy. `args: seen` —
    przeczytane; `since` (znacznik czasu) — tylko nowsze (CLI sprawdza tak w tle, czy cos przyszlo).
    """
    from backend.core import feed

    user = memory.vibe_key(interface)
    if str(request.get("args", "")).strip().lower() in ("seen", "przeczytane", "read"):
        feed.mark_seen(user)
    try:
        since = float(request.get("since")) if request.get("since") not in (None, "") else None
    except (TypeError, ValueError):
        since = None
    data = feed.listing(user)
    if since is not None:
        data["items"] = [i for i in data["items"] if i["ts"] > since]
    data["text"] = feed.text_list(user)
    data["prefs"] = feed.prefs(get_watcher().feed_defaults()).to_data()
    await _send(writer, _data(data))


# Komendy, ktore moze uruchomic przycisk wiadomosci ze skrzynki — akcje buduje backend, ale i tak tylko z tej listy.
FEED_COMMANDS = frozenset({"cleanup", "patch", "investigate", "overview", "digest", "audit", "vulns", "projects", "web"})


async def _feed_act(writer, agent, request: dict, session_id: str, interface: str, identity: str, role: str) -> None:
    """Przycisk wiadomosci ze skrzynki: komenda backendu, wiadomosc do agenta, "pozniej", "mniej takich", "ukryj"."""
    from backend.core import feed

    item = feed.get(str(request.get("id", "") or ""))
    try:
        index = int(request.get("action"))
    except (TypeError, ValueError):
        index = -1
    if item is None or not 0 <= index < len(item.actions):
        await _send(writer, _error(tr("Nie znam tej wiadomosci albo przycisku.", "I do not know this message or button.")))
        return
    action = item.actions[index]
    kind, label = action.get("kind"), str(action.get("label", ""))
    if action.get("admin") and role != "admin":
        await _send(writer, _error(tr("To moze zrobic tylko administrator.", "Only an administrator can do this.")))
        return
    if item.done and kind in ("command", "message"):
        await _send(writer, _error(tr(f"Juz zrobione: {item.done}", f"Already done: {item.done}")))
        return
    if kind == "snooze":
        feed.snooze(item.key or item.id, 24)
        feed.mark_done(item.id, label)
        await _send(writer, _data({"id": item.id, "done": label, "text": tr("Odlozone o dobe.", "Snoozed for a day.")}))
    elif kind == "mute":
        muted = feed.mute(item.kind)
        feed.mark_done(item.id, label)
        await _send(writer, _data({"id": item.id, "done": label, "muted": muted,
                                   "text": tr("Takie podpowiedzi zostana w skrzynce, bez powiadomien. Przywrocisz je w "
                                              "ustawieniach powiadomien.", "Suggestions like this stay in the inbox, "
                                              "without notifications. Bring them back in the notification settings.")}))
    elif kind == "dismiss":
        feed.mark_done(item.id, label)
        await _send(writer, _data({"id": item.id, "done": label}))
    elif kind == "message":
        feed.mark_done(item.id, label)
        text = str((action.get("args") or {}).get("text", "")).strip()
        await _stream_chat(writer, agent, session_id, text, interface, generated=True, owner=identity, role=role)
    elif kind == "command" and action.get("command") in FEED_COMMANDS:
        feed.mark_done(item.id, label)
        await _handle_command(writer, agent, {**(action.get("args") or {}), "command": action["command"]},
                              session_id, interface, identity, role)
    else:
        await _send(writer, _error(tr("Ten przycisk nie jest juz obslugiwany.", "This button is no longer supported.")))


async def _prefs(writer, request: dict, interface: str, role: str) -> None:
    """Ustawienia powiadomien: poziom, cisza nocna, podpowiedzi dziennie, wyciszone rodzaje (zmiana — administrator)."""
    from backend.core import feed

    defaults = get_watcher().feed_defaults()
    changes = {k: request.get(k) for k in ("level", "quiet", "per_day") if request.get(k) not in (None, "")}
    args = str(request.get("args", "") or "").strip().lower()
    if changes or args.startswith("unmute"):
        if role != "admin":
            await _send(writer, _error(tr("Ustawienia powiadomien zmienia administrator.",
                                          "An administrator changes the notification settings.")))
            return
        try:
            if "per_day" in changes:
                changes["per_day"] = int(changes["per_day"])
            if changes:
                feed.set_prefs(defaults=defaults, **{k: (str(v) if k != "per_day" else v) for k, v in changes.items()})
            if args.startswith("unmute"):
                kind = args.split(None, 1)[1] if " " in args else "all"
                for muted in (feed.prefs(defaults).muted if kind == "all" else [kind]):
                    feed.mute(muted, on=False)
        except (feed.FeedError, ValueError) as exc:
            await _send(writer, _error(str(exc)))
            return
        await audit.log_file_write(interface, f"feed.json (prefs: {', '.join(changes) or args})", 0)
    current = feed.prefs(defaults)
    level = {"all": tr("wszystko", "everything"), "important": tr("wazne", "important"),
             "critical": tr("tylko krytyczne", "critical only")}[current.level]
    text = tr(f"Powiadomienia: {level}; cisza nocna: {current.quiet or 'wylaczona'}; podpowiedzi dziennie: "
              f"{current.per_day}" + (f"; wyciszone: {', '.join(current.muted)}" if current.muted else ""),
              f"Notifications: {level}; quiet hours: {current.quiet or 'off'}; suggestions per day: {current.per_day}"
              + (f"; muted: {', '.join(current.muted)}" if current.muted else ""))
    await _send(writer, _data({**current.to_data(), "text": text, "can_edit": role == "admin"}))


async def _install(writer, request: dict, interface: str, role: str) -> None:
    """
    /pobierz skill|plugin <zrodlo> [filtr] bez LLM (core/extensions.py): najpierw podglad z `token`, potem — po TAK
    w kliencie — {"command": "install", "token": ..., "execute": true}. Tylko administrator.
    """
    import shutil

    from backend.core import extensions

    if role != "admin":
        await _send(writer, _error(tr("Skille i pluginy instaluje tylko administrator.", "Only an administrator installs skills and plugins.")))
        return
    servers_allowed = not runtime.observe()
    if request.get("execute"):
        found = extensions.take(str(request.get("token", "")))
        if found is None:
            await _send(writer, _error(tr("Podglad wygasl albo juz zainstalowano — wpisz /pobierz jeszcze raz.",
                                          "The preview expired or was already installed — type /download again.")))
            return
        text, added = extensions.install(found, servers_allowed=servers_allowed)
        for skill in found.skills:
            await audit.log_file_write(interface, f"skills/{skill.name}/SKILL.md ({found.source})", 0)
        if added:
            from backend.core.mcp.registry import get_manager
            asyncio.get_running_loop().create_task(get_manager().reload())
        await _send(writer, _data({"text": text}))
        return
    words = str(request.get("args", "") or "").split()
    kind = "plugin"
    if words and words[0].lower() in ("skill", "skille", "skills"):
        kind, words = "skill", words[1:]
    elif words and words[0].lower() in ("plugin", "pluginy", "plugins"):
        words = words[1:]
    if not words:
        await _send(writer, _error(tr("Uzycie: /pobierz skill <wlasciciel/repo|URL> [filtr] albo /pobierz plugin <...>.",
                                      "Usage: /download skill <owner/repo|URL> [filter] or /download plugin <...>.")))
        return
    try:
        url, ref, sub = extensions.parse_source(words[0])
    except extensions.ExtensionError as exc:
        await _send(writer, _error(str(exc)))
        return
    await _send(writer, event_frame(Progress(tr(f"pobieram {url}...", f"fetching {url}..."), source="pobierz")))
    try:
        root = await extensions.fetch(url, ref)
    except extensions.ExtensionError as exc:
        await _send(writer, _error(str(exc)))
        return
    try:
        found = extensions.discover(root, sub, kind, url + (f" ({sub})" if sub else ""), " ".join(words[1:]))
    except extensions.ExtensionError as exc:
        await _send(writer, _error(str(exc)))
        return
    finally:
        shutil.rmtree(root, ignore_errors=True)
    text = extensions.preview(found, servers_allowed=servers_allowed)
    token = "" if found.empty else extensions.remember(found)
    await _send(writer, _data({"token": token, "text": text, "skills": len(found.skills), "servers": len(found.servers),
                               "empty": found.empty}))


async def _mcp_servers(writer, request: dict, role: str) -> None:
    """
    Serwery MCP, z ktorych korzysta Pipe: lista; `dodaj <nazwa> <npx ...|uvx ...|docker run ...|url> [ZMIENNA=${MCP_X}]`,
    `usun <nazwa>`, `odswiez`. Zmiany tylko administrator; wpisanie komendy jest zgoda (tresc widac w niej samej).
    Dodanie uruchamia kontener, wiec nie w trybie obserwacji.
    """
    from backend.core.mcp import registry

    manager = registry.get_manager()
    words = str(request.get("args", "") or "").strip()
    verb, _, rest = words.partition(" ")
    verb = {"add": "dodaj", "remove": "usun", "usuń": "usun", "reload": "odswiez", "odśwież": "odswiez"}.get(
        verb.lower(), verb.lower())
    text = ""
    if verb in ("dodaj", "usun", "odswiez"):
        if role != "admin":
            await _send(writer, _error(tr("Serwery MCP zmienia tylko administrator.", "Only an administrator changes MCP servers.")))
            return
        if verb == "dodaj":
            if runtime.observe():
                await _send(writer, _error(tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe nie uruchamia nowych serwerow MCP.",
                                              "Observe mode (PIPE_OBSERVE=1): Pipe starts no new MCP servers.")))
                return
            try:
                name, cfg = registry.parse_line(rest)
                created = registry.add_server(name, cfg)
            except registry.McpConfigError as exc:
                await _send(writer, _error(str(exc)))
                return
            await audit.log_file_write("mcp_servers", f"mcp_servers.json#{name}", 0)
            await manager.reload()
            state = manager.servers.get(name)
            text = (tr(f"{'Dodano' if created else 'Zaktualizowano'} serwer MCP ", f"{'Added' if created else 'Updated'} MCP server ")
                    + (state.describe() if state else name))
        elif verb == "usun":
            removed = registry.remove_server(rest.strip())
            await manager.reload()
            text = tr(f"Usunieto serwer MCP {rest.strip()}.", f"Removed MCP server {rest.strip()}.") if removed else                 tr(f"Nie ma serwera MCP {rest.strip()!r}.", f"No MCP server {rest.strip()!r}.")
        else:
            await manager.reload()
    elif verb:
        await _send(writer, _error(tr("Uzycie: /mcp, /mcp dodaj <nazwa> <npx ...|uvx ...|docker run ...|https://...> "
                                      "[ZMIENNA=${MCP_...}], /mcp usun <nazwa>, /mcp odswiez.",
                                      "Usage: /mcp, /mcp add <name> <npx ...|uvx ...|docker run ...|https://...> "
                                      "[VAR=${MCP_...}], /mcp remove <name>, /mcp reload.")))
        return
    config = registry.load_config()
    lines = [registry.describe_server(n, c) + "\n  " + (manager.servers[n].describe() if n in manager.servers
                                                        else tr("(nie polaczony)", "(not connected)"))
             for n, c in sorted(config.items())]
    listing = "\n".join(f"- {line}" for line in lines) if lines else tr(
        "Brak serwerow MCP. Dodaj: /mcp dodaj github npx -y @modelcontextprotocol/server-github "
        "GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN} (token w backend/.env jako MCP_GITHUB_TOKEN=...).",
        "No MCP servers. Add one: /mcp add github npx -y @modelcontextprotocol/server-github "
        "GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN} (the token in backend/.env as MCP_GITHUB_TOKEN=...).")
    text = (text + "\n\n" if text else "") + tr("Serwery MCP:\n", "MCP servers:\n") + listing
    await _send(writer, _data({"text": text, "servers": [{"name": n, "status": manager.servers[n].status
                                                            if n in manager.servers else "disconnected"}
                                                           for n in sorted(config)]}))


async def _cleanup(writer, agent, session_id: str, interface: str, role: str) -> None:
    """Zwolnij miejsce (core/cleanup.py) — bez LLM, z postepem na zywo; wynik trafia tez do rozmowy."""
    from backend.core import cleanup

    if role != "admin":
        await _send(writer, _error(tr("Sprzatanie moze uruchomic tylko administrator.", "Only an administrator can clean up.")))
        return
    if runtime.observe():
        await _send(writer, _error(tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe niczego nie usuwa.",
                                      "Observe mode (PIPE_OBSERVE=1): Pipe removes nothing.")))
        return

    async def emit(text: str) -> None:
        await _send(writer, event_frame(Progress(text, source=cleanup.SOURCE)))

    result = await cleanup.run(interface, emit)
    try:
        agent.note(session_id, tr(f"Uzytkownik zwolnil miejsce (sprzatanie Pipe):\n{result.text()}",
                                  f"The user freed disk space (Pipe cleanup):\n{result.text()}"))
    except Exception:
        pass
    await _send(writer, _data(result.to_data()))


def _publish_login(event: dict) -> None:
    """Prosba o wejscie do pipe web (core/webaccess.py) -> skrzynka i subskrybenci (Telegram, pipe web, CLI)."""
    get_watcher().notifier.publish(event)


async def _web(writer, agent, request: dict, session_id: str, interface: str, identity: str, role: str) -> None:
    """
    /web — Pipe w przegladarce pod publicznym adresem HTTPS (core/webpublic.py) i urzadzenia, ktore moga wejsc
    (core/webaccess.py). Stan widzi kazdy; wlaczenie, wylaczenie, wpuszczanie i odwolywanie — administrator.
    """
    from backend.core import webaccess, webpublic

    op, rest = webpublic.parse(str(request.get("args", "") or ""))
    if op == "help":
        await _send(writer, _error(webpublic.help_text()))
        return
    if op == "status":
        await _send(writer, _data(webpublic.status(role == "admin")))
        return
    if role != "admin":
        await _send(writer, _error(tr("Dostep z przegladarki zmienia tylko administrator.",
                                      "Only an administrator changes browser access.")))
        return

    async def emit(text: str) -> None:
        await _send(writer, event_frame(Progress(text, source="web")))

    if op == "on":
        try:
            port, domain = webpublic.parse_on(rest)
        except ValueError as exc:
            await _send(writer, _error(str(exc)))
            return
        ok, text = await webpublic.enable(port=port, domain=domain, backend_socket=settings.AGENT_SOCKET, emit=emit,
                                          publish=_publish_login)
        await audit.log_confirmed(interface, f"/web wlacz {' '.join(rest)}".strip(), 0 if ok else 1)
        _web_note(agent, session_id, text)
        await _send(writer, _data({**webpublic.status(True), "ok": ok, "text": text}))
    elif op == "off":
        text = await webpublic.disable(emit)
        await audit.log_confirmed(interface, "/web wylacz", 0)
        _web_note(agent, session_id, text)
        await _send(writer, _data({**webpublic.status(True), "text": text}))
    elif op in ("approve", "deny"):
        ref = str(request.get("id") or (rest[0] if rest else ""))
        words = {w.lower() for w in rest[1:]} | {str(request.get("role") or "").lower()}
        device_role = "viewer" if words & webpublic.VIEWER_WORDS else "admin"
        try:
            pairing = webaccess.decide(ref, op == "approve", role=device_role, by=identity)
        except webaccess.AccessError as exc:
            await _send(writer, _error(str(exc)))
            return
        if op == "approve":
            label = tr("Wpuszczone", "Let in") if device_role == "admin" else tr("Wpuszczone (podglad)", "Let in (view only)")
            text = tr(f"Wpuscilem {pairing.device} (kod {pairing.code}) — "
                      + ("pelny dostep." if device_role == "admin" else "tylko podglad."),
                      f"Let in {pairing.device} (code {pairing.code}) — "
                      + ("full access." if device_role == "admin" else "view only."))
        else:
            label = tr("Odrzucone", "Rejected")
            text = tr(f"Odrzucilem prosbe {pairing.code} ({pairing.device}).", f"Rejected request {pairing.code} ({pairing.device}).")
        webpublic.close_feed(pairing.id, label)
        await audit.log_confirmed(interface, f"/web {op} {pairing.code} {pairing.device} {pairing.ip} {device_role}", 0)
        await _send(writer, _data({"text": text, "status": pairing.status}))
    elif op == "revoke":
        try:
            gone = webaccess.revoke(" ".join(rest))
        except webaccess.AccessError as exc:
            await _send(writer, _error(str(exc)))
            return
        await audit.log_confirmed(interface, f"/web odwolaj {', '.join(gone)}", 0)
        await _send(writer, _data({**webpublic.status(True),
                                   "text": tr("Odwolane: ", "Revoked: ") + ", ".join(gone)}))


def _web_note(agent, session_id: str, text: str) -> None:
    try:
        agent.note(session_id, tr(f"Uzytkownik zmienil dostep do Pipe z przegladarki (/web):\n{text}",
                                  f"The user changed browser access to Pipe (/web):\n{text}"))
    except Exception:
        pass


async def _projects(writer, request: dict, interface: str, role: str) -> None:
    """
    Warsztat bez LLM (pulpit pipe web, /projekty): lista ze stanem; `args`: logs | stop | start | remove (z
    `execute: true`, inaczej podglad) dla projektu `name`. Zmiany — administrator, nie w trybie obserwacji.
    """
    from backend.core import hostexec, projects
    from backend.core.handlers.projects import config, project_list

    op = str(request.get("args", "") or "").strip().lower()
    if not op:
        found = projects.load()
        states = await projects.statuses(found) if found else {}
        await _send(writer, _data({"projects": [p.to_data(states.get(p.name)) for p in found],
                                   "workspace": settings.WORKSPACE_DIR, "text": await project_list(),
                                   "can_edit": role == "admin" and not runtime.observe()}))
        return
    project = projects.get(str(request.get("name", "") or "").strip().lower())
    if project is None:
        await _send(writer, _error(tr("Nie ma takiego projektu.", "No such project.")))
        return
    if op == "logs":
        await _send(writer, _data({"name": project.name, "text": await projects.logs(project)}))
        return
    if op not in ("stop", "start", "remove"):
        await _send(writer, _error(tr(f"Nieznana operacja {op!r}.", f"Unknown operation {op!r}.")))
        return
    reason = tr("Rola viewer: tylko odczyt.", "Viewer role: read-only.") if role != "admin" else projects.blocker()
    if reason:
        await _send(writer, _error(reason))
        return
    if op == "remove":
        if not request.get("execute"):
            await _send(writer, _data({"name": project.name, "preview": tr(
                f"Usune projekt {project.name}: katalog {project.path}, jego kontenery i zbudowany obraz.",
                f"I will remove project {project.name}: directory {project.path}, its containers and the built image.")}))
            return
        image_ref = await hostexec.image() if runtime.kind() == "docker" and project.run.get("compose") else ""
        try:
            text = await projects.remove(config(), project, image_ref=image_ref)
        except projects.ProjectError as exc:
            await _send(writer, _error(str(exc)))
            return
        await audit.log_confirmed(interface, f"project remove {project.name}", 0)
        await _send(writer, _data({"name": project.name, "removed": True, "text": text}))
        return
    text = await projects.set_running(project, op == "start")
    await audit.log_confirmed(interface, f"project {op} {project.name}", 0)
    await _send(writer, _data({"name": project.name, "text": text}))


async def _vulns_report() -> tuple[str, dict]:
    """
    /podatnosci bez narzedzi: czytelny raport z wyjasnieniami (model pisze brakujace, najwyzej EXPLAIN_WAIT s) —
    Markdown dla Telegrama i CLI, dane (vulns.report_view) dla pipe web.
    """
    from backend.core import infra, vulns
    from backend.core.watch import EXPLAIN_WAIT

    watcher = get_watcher()
    result, note = await watcher.vuln_for_audit()
    if result is not None:
        vulns.refresh_containers(result, await infra.docker_containers())
    explained = result is None or await watcher.explain_vulns(result, timeout=EXPLAIN_WAIT)
    if not explained and watcher.agent is not None and watcher.agent.llm_enabled():
        note = (note + " " if note else "") + tr("Czesc opisow jest jeszcze po angielsku (z OSV) — wyjasnienia sie generuja.",
                                                 "Some descriptions are still the original ones from OSV — explanations are being written.")
    explain = vulns.explanations(lang())
    return vulns.report_markdown(result, explain, note), vulns.report_view(result, explain, note)


async def _patch(writer, request: dict, interface: str, role: str, session_id: str = "") -> None:
    """
    /zalataj bez LLM i bez dodatkowych pytan: od razu lata to, co pokazuje skan (core/patching.py) — pakiety tego
    serwera, obrazy kontenerow z compose i zdalne serwery — z postepem na zywo (ramki `progress`), potem nowy skan
    i wynik: `text` dla Telegrama i CLI, `report` (stan po latce) dla pipe web. `scope`: '' / all, images, system,
    servers, nazwa zdalnego serwera, urgent. Latanie dziala w tle: rozlaczenie klienta go nie przerywa (wynik
    przychodzi wtedy jako wiadomosc), a drugie /zalataj w trakcie dostaje ten sam postep i wynik. Pola `execute`
    i `patch_token` (podglad sprzed v0.38) sa ignorowane.
    """
    from backend.core import hostexec, infra, patching, vulns

    blocker = await patching.blocker(role)
    if blocker:
        await _send(writer, _error(blocker))
        return
    run = patching.current()
    if run is None:
        scope = patching.parse_scope(request.get("scope") or request.get("args") or "")
        if not scope:
            await _send(writer, _error(patching.scope_help()))
            return
        watcher = get_watcher()
        await _send(writer, event_frame(Progress(tr("Sprawdzam, co jest do zalatania...", "Checking what there is to patch..."),
                                                 source=patching.SOURCE)))
        scan, note = await watcher.vuln_for_audit()
        if scan is None:
            await _send(writer, _error(tr(f"Brak wyniku skanu podatnosci ({note}).", f"No vulnerability scan result ({note}).")
                                       if note else tr("Brak wyniku skanu podatnosci.", "No vulnerability scan result.")))
            return
        # kontenery i compose z biezacego stanu Dockera — skan bywa sprzed odtworzenia kontenerow albo aktualizacji Pipe
        vulns.refresh_containers(scan, await infra.docker_containers())
        own = {p for p in [await hostexec.own_project()] if p}
        job = patching.plan(scan, scope, skip_projects=own, local_blocker=await patching.local_blocker())
        if job.empty:
            if job.notes:
                await _send(writer, _error(" ".join(job.notes)))
                return
            text = vulns.nothing_to_patch(scan, own) + (f" ({note})" if note else "")
            try:
                get_agent().note(session_id, tr(f"Uzytkownik uzyl /zalataj (bez Ciebie) i zobaczyl:\n{text}",
                                                f"The user ran /patch (without you) and saw:\n{text}"))
            except Exception:
                pass
            await _send(writer, _data({"scope": scope, "packages": [], "command": "", "token": "", "can_execute": False,
                                       "nothing": True, "text": text}))
            return
        run = patching.start(job, interface=interface, session_id=session_id, watcher=watcher, agent=get_agent())
        for line in job.notes:
            run.emit(line)
    else:
        await _send(writer, event_frame(Progress(tr("Latanie juz trwa — pokazuje jego postep.",
                                                    "Patching is already running — showing its progress."),
                                                 source=patching.SOURCE)))
    async for event in run.follow():
        try:
            if isinstance(event, patching.Done):
                await _send(writer, _data(event.data))
                run.delivered = True
            else:
                await _send(writer, event_frame(event))
        except (ConnectionError, OSError):
            return                                  # klient zniknal — latanie trwa, wynik przyjdzie jako wiadomosc


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


async def _language(writer, args: str, interface: str, role: str) -> None:
    """
    Jezyk Pipe w trakcie pracy (/jezyk w CLI, pipe web i na Telegramie), bez LLM. Bez argumentu — stan;
    z argumentem (pl / en) — przelaczenie: prompty, opisy narzedzi, raporty i komunikaty od nastepnej
    wiadomosci. Wybor jest wspolny dla calego agenta, wiec subskrybenci dostaja zdarzenie `language`.
    """
    def state(text: str) -> dict:
        return {"lang": lang(), "chosen": i18n.chosen(), "languages": list(i18n.LANGUAGES),
                "can_edit": role == "admin", "text": text}

    if not args:
        await _send(writer, _data(state(tr("Jezyk Pipe: polski. Zmiana: /jezyk en",
                                           "Pipe language: English. Change: /language pl"))))
        return
    if role != "admin":
        await _send(writer, _error(tr("Jezyk moze zmieniac tylko administrator.",
                                      "Only an administrator can change the language.")))
        return
    try:
        target = i18n.set_lang(args)
    except ValueError:
        await _send(writer, _error(tr(f"Nieznany jezyk: {args!r}. Dostepne: pl, en.",
                                      f"Unknown language: {args!r}. Available: pl, en.")))
        return
    try:
        memory.seed_builtin_skills()       # nietkniete skille wbudowane w nowym jezyku
    except OSError:
        pass
    await audit.log_file_write(interface, f"{i18n.STORE} (lang={target})", 0)
    print(f"[server] Jezyk: {target} (zmienil {interface})", flush=True)
    get_watcher().notifier.publish({"type": "language", "lang": target}, transient=True)
    await _send(writer, _data(state(tr("Od teraz rozmawiamy po polsku — instrukcje agenta, raporty i komunikaty.",
                                       "From now on we speak English — the agent's instructions, reports and messages."))))


async def _view(writer, args: str, interface: str, role: str) -> None:
    """
    Widok Pipe (/widok w CLI, pipe web i na Telegramie, core/views.py), bez LLM. Bez argumentu — stan; z argumentem
    (prosty / techniczny albo opis zwyklymi slowami: "chce ogarniac maile") — przelaczenie. Wybor jest wspolny dla
    calego agenta, wiec subskrybenci dostaja zdarzenie `view`.
    """
    from backend.core import views

    def state(text: str) -> dict:
        return {"view": views.current(), "chosen": views.chosen(), "views": list(views.VIEWS),
                "can_edit": role == "admin", "text": text}

    if not args:
        await _send(writer, _data(state(tr("Widok prosty: rozmowa, skille i dodatki. Zmiana: /widok techniczny",
                                           "Simple view: chat, skills and add-ons. Change: /view technical")
                                        if views.is_simple() else
                                        tr("Widok techniczny: serwer, schemat, podatnosci i zdalne cele. "
                                           "Zmiana: /widok prosty",
                                           "Technical view: the server, the diagram, vulnerabilities and remote "
                                           "targets. Change: /view simple"))))
        return
    if role != "admin":
        await _send(writer, _error(tr("Widok moze zmieniac tylko administrator.",
                                      "Only an administrator can change the view.")))
        return
    try:
        target = views.set_view(args)
    except ValueError:
        await _send(writer, _error(tr("Nie wiem, ktory widok wybrac. Napisz /widok prosty (rozmowa, skille, dodatki) "
                                      "albo /widok techniczny (serwer, schemat, podatnosci).",
                                      "I can't tell which view to pick. Type /view simple (chat, skills, add-ons) "
                                      "or /view technical (the server, the diagram, vulnerabilities).")))
        return
    await audit.log_file_write(interface, f"{views.STORE} (view={target})", 0)
    print(f"[server] Widok: {target} (zmienil {interface})", flush=True)
    get_watcher().notifier.publish({"type": "view", "view": target}, transient=True)
    await _send(writer, _data(state(
        tr("Ustawiono widok prosty: rozmowa, skille i dodatki do aplikacji. Pisze zwyklymi slowami. "
           "Wiecej o serwerze: /widok techniczny",
           "Simple view set: chat, skills and add-ons for apps. I write in plain words. "
           "More about the server: /view technical") if target == "simple" else
        tr("Ustawiono widok techniczny: serwer, schemat, podatnosci, zdalne cele i wszystko z widoku prostego. "
           "Prosciej: /widok prosty",
           "Technical view set: the server, the diagram, vulnerabilities, remote targets and everything from the "
           "simple view. Simpler: /view simple"))))


YOLO_ON = ("on", "wlacz", "włącz", "tak", "yes", "1", "true")
YOLO_OFF = ("off", "wylacz", "wyłącz", "nie", "no", "0", "false")


async def _yolo(writer, agent, args: str, session_id: str, interface: str, identity: str, role: str) -> None:
    """
    /yolo [on|off] — operacje wymagajace potwierdzenia wykonuja sie w tej sesji bez pytania (bezpiecznik,
    dziennik i /cofnij dzialaja dalej; zakazane — nadal odrzucane). Tylko admin; bez argumentu — stan.
    Domyslnie wylaczony, nie przetrwa restartu backendu; inne sesje (inne kanaly) go nie dziedzicza.
    """
    session = agent.get_or_create_session(session_id, interface, owner=identity, role=role)

    def state(text: str) -> dict:
        return {"yolo": session.runs_yolo, "can_edit": role == "admin", "text": text}

    choice = args.lower()
    if not choice:
        await _send(writer, _data(state(
            tr("YOLO jest wlaczone: zmiany wykonuja sie bez pytania. Wylacz: /yolo off", "YOLO is on: changes run "
               "without asking. Turn it off: /yolo off") if session.runs_yolo else
            tr("YOLO jest wylaczone: kazda zmiana czeka na Twoje TAK. Wlacz: /yolo on", "YOLO is off: every change "
               "waits for your YES. Turn it on: /yolo on"))))
        return
    if choice not in YOLO_ON + YOLO_OFF:
        await _send(writer, _error(tr(f"Nie rozumiem {args!r}. Uzyj: /yolo on albo /yolo off.",
                                      f"I do not understand {args!r}. Use: /yolo on or /yolo off.")))
        return
    if role != "admin":
        await _send(writer, _error(tr("Tryb YOLO moze wlaczyc tylko administrator.",
                                      "Only an administrator can turn on YOLO mode.")))
        return
    session.yolo = choice in YOLO_ON
    await audit.log_confirmed(interface, f"yolo {'on' if session.yolo else 'off'}", 0)
    print(f"[server] YOLO {'wlaczone' if session.yolo else 'wylaczone'} ({interface})", flush=True)
    await _send(writer, _data(state(
        tr("YOLO wlaczone. Od teraz zmiany w tej rozmowie wykonuje bez pytania — bezpiecznik robi kopie, a kazda "
           "zmiana trafia do dziennika (/cofnij). Operacje zakazane nadal odrzucam. Wylacz: /yolo off",
           "YOLO is on. From now on I make changes in this conversation without asking — the safety fuse takes "
           "backups and every change goes to the journal (/undo). Forbidden operations are still refused. "
           "Turn it off: /yolo off") if session.yolo else
        tr("YOLO wylaczone. Kazda zmiana znow czeka na Twoje TAK.",
           "YOLO is off. Every change waits for your YES again."))))


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


async def _transcribe(writer, request: dict, interface: str = "") -> None:
    """Wiadomosc glosowa -> tekst (klient wysyla potem tekst jak zwykla wiadomosc)."""
    import base64
    import binascii

    from backend.core import llm, voice
    from backend.core.agent import _record_usage

    try:
        data = base64.b64decode(str(request.get("audio", "")), validate=True)
    except (binascii.Error, ValueError):
        await _send(writer, _error(tr("Nieprawidlowe nagranie (oczekiwano base64).", "Invalid recording (expected base64).")))
        return
    try:
        usage.check_budget(settings.DAILY_TOKEN_LIMIT, settings.DAILY_COST_LIMIT)
        # Provider wybrany teraz (core/llm.py) — na Gemini transkrybuje sam model czatu (input_audio).
        current = llm.resolve(settings.LLM) if settings.LLM else None
        base = settings.LLM.provider_id if settings.LLM else ""

        def on_usage(model, response_usage):
            _record_usage(model, response_usage, interface,
                          priced=current is not None and current.provider_id == base and model == current.model)

        text = await voice.transcribe(data, str(request.get("filename", "") or "voice.ogg"),
                                      voice.resolve(dict(os.environ), current), on_usage=on_usage)
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
    from backend.core import views

    notifier = get_watcher().notifier
    queue = notifier.subscribe()
    await _send(writer, {"response": "", "status": "ok", "done": False, "event": {"type": "subscribed", "lang": lang(), "lang_chosen": i18n.chosen(),
                                                                     "view": views.current(), "view_chosen": views.chosen()}})
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


# Komendy bez modelu, ktorych wynik trafia do rozmowy: "zrob to sam", "napraw to" odnosza sie do tego, co
# uzytkownik przed chwila zobaczyl — bez tego model widzial tylko poprzednia wiadomosc (np. /aktualizuj) i ja powtarzal.
# /zalataj notuje sie samo (core/patching.py: wynik latania, _patch: "nie ma nic do zainstalowania").
REMEMBERED_COMMANDS = {
    "vulns": ("/podatnosci", "/vulns"), "audit": ("/audyt", "/audit"),
    "health": ("/zdrowie", "/health"), "changes": ("/zmiany", "/changes"), "alerts": ("/alerty", "/alerts"),
    "journal": ("/dziennik", "/journal"), "status": ("/status", "/status"), "feed": ("/nowosci", "/news"),
}
REMEMBERED_TEXT = 3500
_RECORDED: contextvars.ContextVar[list | None] = contextvars.ContextVar("pipe_recorded_frames", default=None)


def _shown_text(data: dict) -> str:
    """To, co klient pokazal z odpowiedzi komendy (tekst, raport podatnosci pod audytem, lista alertow)."""
    parts = [str(data.get("text") or "").strip(), str(data.get("vulns") or "").strip()]
    for alert in data.get("active") or []:
        parts.append(f"[{alert.get('severity')}] {alert.get('title')} — {alert.get('detail', '')}")
    if "active" in data and not data.get("active"):
        parts.append(tr("Brak aktywnych alertow.", "No active alerts."))
    return "\n\n".join(p for p in parts if p)


async def _remembered_command(writer, agent, request: dict, session_id: str, interface: str,
                              identity: str = "open", role: str = "admin") -> None:
    """`_handle_command` + wynik wybranych komend jako notatka w rozmowie (agent.note), zeby model wiedzial, co widac."""
    command = str(request.get("command", "")).strip()
    names = REMEMBERED_COMMANDS.get(command)
    if command == "feed" and str(request.get("args", "")).strip().lower() not in ("seen", "przeczytane", "read"):
        names = None            # odswiezanie pulpitu w tle to nie "uzytkownik zobaczyl"
    if names is None:
        await _handle_command(writer, agent, request, session_id, interface, identity, role)
        return
    frames: list = []
    token = _RECORDED.set(frames)
    try:
        await _handle_command(writer, agent, request, session_id, interface, identity, role)
    finally:
        _RECORDED.reset(token)
    shown = "\n\n".join(_shown_text(f["data"]) for f in frames if isinstance(f.get("data"), dict)).strip()
    if not shown:
        return
    args = str(request.get("scope") or request.get("args") or "").strip()
    if command == "feed":
        args = ""
    typed = tr(names[0], names[1]) + (f" {args}" if args else "")
    shown = memory.redact_secrets(shown)[0]
    if len(shown) > REMEMBERED_TEXT:
        shown = shown[:REMEMBERED_TEXT] + tr("\n[... skrocone]", "\n[... truncated]")
    try:
        agent.note(session_id, tr(f"Uzytkownik uzyl komendy {typed} (bez Ciebie) i zobaczyl:\n{shown}",
                                  f"The user ran the command {typed} (without you) and saw:\n{shown}"),
                   interface=interface, owner=identity, role=role)
    except Exception as exc:                             # rozmowa dziala dalej, tylko bez tej notatki
        print(f"[VPS Agent] note: {exc}", flush=True)


async def _send(writer: asyncio.StreamWriter, data: dict) -> None:
    """Wysyła odpowiedź JSON zakończoną newline."""
    recorded = _RECORDED.get()
    if recorded is not None:
        recorded.append(data)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()


async def _report_model_status() -> None:
    warning = await get_agent().verify_model()
    if warning:
        print(tr(f"[VPS Agent] [OSTRZEZENIE] {warning}", f"[VPS Agent] [WARNING] {warning}"), flush=True)


async def main() -> None:
    """Punkt wejścia serwera."""
    i18n.load_saved()          # jezyk wybrany w trakcie pracy (/jezyk) ma pierwszenstwo przed .env
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
    if settings.LLM.enabled:
        print(f"[VPS Agent] Provider    : {settings.LLM.provider_name} ({settings.LLM.base_url})", flush=True)
        print(f"[VPS Agent] Model       : {settings.LLM.model}", flush=True)
    else:
        print(tr("[VPS Agent] Provider    : bez modelu (LLM_PROVIDER=none) — rozmowa po dodaniu providera",
                 "[VPS Agent] Provider    : no model (LLM_PROVIDER=none) — chat once a provider is added"), flush=True)
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

    # Sesje sprzed restartu: przeterminowane (albo wszystkie przy SESSION_KEEP_DAYS=0) znikaja z dysku.
    try:
        from backend.core import sessions_store
        sessions_store.prune(settings.SESSION_KEEP_DAYS)
    except OSError as exc:
        print(f"[VPS Agent] [{tr('OSTRZEZENIE', 'WARNING')}] sessions: {exc}", flush=True)

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

    from backend.core.mcp.server import start_http as start_mcp_http
    mcp_server = await start_mcp_http(settings.MCP_HOST, settings.MCP_PORT,
                                      {o.strip() for o in settings.MCP_ALLOWED_ORIGINS.split(",") if o.strip()})
    if mcp_server is not None:
        print(f"[VPS Agent] MCP (HTTP)  : http://{settings.MCP_HOST}:{settings.MCP_PORT}/mcp", flush=True)

    from backend.core.mcp.registry import get_manager as get_mcp_manager, load_config as mcp_config
    if mcp_config():
        # serwery MCP, z ktorych korzysta Pipe — w tle (pierwsze `npx -y` w kontenerze potrafi trwac minute)
        mcp_task = asyncio.create_task(get_mcp_manager().reload())
        mcp_task.add_done_callback(lambda t: print(tr("[VPS Agent] MCP: ", "[VPS Agent] MCP: ")
                                                   + "; ".join(get_mcp_manager().status()), flush=True))

    # Pipe wystawiony w internecie (/web wlacz) — most strony wraca po restarcie, Caddy wstaje sam
    from backend.core import webpublic
    await webpublic.resume(settings.AGENT_SOCKET, _publish_login)

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
