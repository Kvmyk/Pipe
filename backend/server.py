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
from backend.config.prompts import (
    INVESTIGATE_ALERT_MESSAGE,
    RUN_SKILL_EXTRA,
    RUN_SKILL_MESSAGE,
    SCAN_SERVER_CREATE,
    SCAN_SERVER_UPDATE,
    STATUS_MESSAGE,
)
from backend.core import audit, diagram, memory, metrics, routines, runtime, targets, usage
from backend.core.agent import get_agent
from backend.core.events import Attachment, Progress
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
                await _send(writer, {"response": f"Błąd JSON: {exc}", "status": "error", "done": True})
                continue

            session_id = request.get("session_id", "default")
            interface = request.get("interface", "cli")

            # Sprawdź token (jeśli ustawiony) — PRZED jakąkolwiek akcją,
            # zeby nieuwierzytelniony klient nie mogl zatwierdzic oczekujacej operacji.
            # compare_digest — porownanie w stalym czasie, bez wycieku dlugosci/prefiksu.
            expected_token = settings.AGENT_TOKEN
            if expected_token and not hmac.compare_digest(str(request.get("token", "")), expected_token):
                await _send(writer, {"response": "Blad: Nieprawidlowy token autoryzacji.", "status": "error", "done": True})
                continue

            # Obsłuż potwierdzenie
            if "confirm" in request:
                await _stream(writer, agent.confirm(session_id, bool(request["confirm"])))
                continue

            # Subskrypcja zdarzen czuwania — polaczenie zostaje otwarte do rozlaczenia klienta
            if request.get("command") == "subscribe":
                await _subscribe(reader, writer)
                break

            # Komendy klientow: /server, /skille, /mapa... i skille jako komendy
            if "command" in request:
                await _handle_command(writer, agent, request, session_id, interface)
                continue

            # Obsłuż wiadomość
            message = request.get("message", "").strip()
            if not message:
                await _send(writer, {"response": "Pusta wiadomość.", "status": "error", "done": True})
                continue

            await _stream_chat(writer, agent, session_id, message, interface)

    except ConnectionResetError:
        pass
    except Exception as exc:
        try:
            await _send(writer, {"response": f"Błąd serwera: {exc}", "status": "error", "done": True})
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def event_frame(event) -> dict:
    """Zdarzenie agenta -> ramka protokolu (done=false)."""
    if isinstance(event, Attachment):
        return {"response": "", "status": "ok", "done": False, "attachment": event.to_wire()}
    if isinstance(event, Progress):
        return {"response": "", "status": "ok", "done": False, "event": event.to_wire()}
    chunk = str(event)
    if "[POTWIERDZ]" in chunk and "wymaga potwierdzenia" in chunk:
        status = "confirm"
    elif "[BLAD]" in chunk or "[ODMOWA]" in chunk:
        status = "error"
    else:
        status = "ok"
    return {"response": chunk, "status": status, "done": False}


async def _stream(writer, events) -> int:
    count = 0
    async for event in events:
        count += 1
        await _send(writer, event_frame(event))
    await _send(writer, {"response": "", "status": "ok", "done": True})
    return count


async def _stream_chat(writer, agent, session_id: str, message: str, interface: str, *, generated: bool = False) -> None:
    """Przekazuje wiadomosc agentowi i streamuje odpowiedz (JSON lines, ostatnia z done=true)."""
    print(f"[server] Wiadomość od {interface}: {message[:80]}", flush=True)
    kwargs = {"generated": True} if generated else {}
    count = await _stream(writer, agent.chat(session_id, message, interface, **kwargs))
    print(f"[server] Odpowiedź wysłana ({count} fragmentów)", flush=True)


def _data(data: dict) -> dict:
    return {"response": "", "status": "ok", "done": True, "data": data}


def _error(text: str) -> dict:
    return {"response": f"[BLAD] {text}", "status": "error", "done": True}


async def _handle_command(writer, agent, request: dict, session_id: str, interface: str) -> None:
    """
    Komendy klientow. Czesc zwraca dane w polu "data" (bez udzialu LLM), czesc
    wysyla agentowi wiadomosc zbudowana tutaj — klient nie sklada promptow.
    """
    command = str(request.get("command", "")).strip()
    args = str(request.get("args", "") or "").strip()
    user_key = memory.vibe_key(interface)
    try:
        if command == "list_skills":
            await _send(writer, _data({"skills": memory.skill_commands()}))
        elif command == "server_md":
            await _send(writer, _data({"content": memory.read_server_md()}))
        elif command == "scan_server":
            message = SCAN_SERVER_UPDATE if memory.read_server_md().strip() else SCAN_SERVER_CREATE
            await _stream_chat(writer, agent, session_id, message, interface, generated=True)
        elif command == "status":
            await _stream_chat(writer, agent, session_id, STATUS_MESSAGE, interface, generated=True)
        elif command == "run_skill":
            entry = memory.find_skill_command(str(request.get("name", "")))
            if entry is None:
                await _send(writer, _error(f"Nie ma skilla {request.get('name', '')!r}. Lista: /skille"))
                return
            message = RUN_SKILL_MESSAGE.format(name=entry["name"], command=entry["command"] or entry["name"])
            if args:
                message += RUN_SKILL_EXTRA.format(args=args)
            await _stream_chat(writer, agent, session_id, message, interface, generated=True)
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
            }))
        elif command == "targets":
            await _send(writer, _data({"targets": [t.describe() for t in targets.load_targets()]}))
        elif command == "routines":
            await _send(writer, _data({"routines": [r.describe() for r in routines.load_routines()]}))
        elif command == "diagram":
            await _send_infra_diagram(writer, args)
        elif command == "investigate":
            alert = get_watcher().find_alert(str(request.get("id", "")))
            if alert is None:
                await _send(writer, _error("Nie znam tego alertu (serwer mogl zostac zrestartowany)."))
                return
            message = INVESTIGATE_ALERT_MESSAGE.format(title=alert["title"], detail=alert["detail"])
            message += await _recent_changes_context()
            await _stream_chat(writer, agent, session_id, message, interface, generated=True)
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
        elif command == "usage":
            priced = bool(settings.LLM_PRICE_IN or settings.LLM_PRICE_OUT)
            await _send(writer, _data(usage.report(7, priced=priced, token_limit=settings.DAILY_TOKEN_LIMIT,
                                                   cost_limit=settings.DAILY_COST_LIMIT)))
        else:
            await _send(writer, _error(f"Nieznana komenda: {command!r}"))
    except OSError as exc:
        await _send(writer, _error(f"Blad odczytu pamieci agenta: {exc}"))


async def _recent_changes_context(hours: float = 24) -> str:
    """Zmiany na serwerze z ostatniej doby — kontekst do badania alertu (czeste zrodlo awarii)."""
    from backend.core.handlers.history import changes_text
    try:
        text = await changes_text(hours)
    except Exception as exc:
        return f"\n\n(Nie udalo sie odczytac historii zmian: {exc})"
    return (f"\n\nZmiany na serwerze z ostatnich {int(hours)} h (migawki Pipe — sprawdz, czy ktoras "
            f"mogla wywolac problem):\n{text}")


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

    title = title or "Mapa infrastruktury"
    found, source = await build_infra_diagram(title)
    try:
        rendered = await diagram.render(source)
    except diagram.DiagramError as exc:
        await _send(writer, _error(f"Nie udalo sie narysowac mapy: {exc}"))
        return
    attachment = attachment_for(rendered, title, "mapa-infrastruktury")
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
        print(f"[VPS Agent] [OSTRZEZENIE] {warning}", flush=True)


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
    print(f"[VPS Agent] TCP         : {tcp_host}:{tcp_port} (tylko localhost — użyj SSH tunnel)", flush=True)
    print(f"[VPS Agent] Provider    : {settings.LLM.provider_name} ({settings.LLM.base_url})", flush=True)
    print(f"[VPS Agent] Model       : {settings.LLM.model}", flush=True)
    print(f"[VPS Agent] Audit log   : {settings.AUDIT_LOG_PATH}", flush=True)
    print(f"[VPS Agent] Runtime     : {runtime.kind()} (host: {runtime.workspace_root()}, proc: {runtime.host_proc()})", flush=True)
    print(f"[VPS Agent] Diagramy    : {'mermaidx' if diagram.available() else 'brak renderera (tylko kod Mermaid)'}", flush=True)
    print(f"[VPS Agent] Czuwanie    : {'co ' + str(settings.WATCH_INTERVAL) + ' s' if settings.WATCH_ENABLED else 'wylaczone'}", flush=True)
    print(f"[VPS Agent] Serwer gotowy. Ctrl+C aby zatrzymać.", flush=True)

    # Weryfikacja modelu w tle — nie blokuje startu, a wycofany model
    # (np. po latach bez aktualizacji .env) od razu widac w logach.
    asyncio.create_task(_report_model_status())
    # Czuwanie i rutyny — proaktywne alerty dla subskrybentow (bot Telegram)
    get_watcher(get_agent()).start()

    async with unix_server, tcp_server:
        await asyncio.gather(
            unix_server.serve_forever(),
            tcp_server.serve_forever(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[VPS Agent] Zatrzymano.", flush=True)
