"""
Server — backend VPS Agent.

Nasłuchuje równocześnie na:
  1. Unix socket (/tmp/vps-agent.sock) — dla klientów na tym samym serwerze (Telegram bot)
  2. TCP localhost:7379              — dla SSH tunnel z laptopa (CLI)

TCP port jest bindowany TYLKO na 127.0.0.1 — nie jest dostępny z zewnątrz.
Dostęp z laptopa odbywa się przez tunel SSH:
    ssh -L 7379:127.0.0.1:7379 user@serwer

Protokół JSON (linia po linii):
  Żądanie:  {"message": "tekst", "session_id": "uuid", "interface": "cli|telegram:123"}
  Żądanie:  {"confirm": true|false, "session_id": "uuid"}
  Odpowiedź: {"response": "tekst", "status": "ok|confirm|error", "done": true|false}

Każda wiadomość może generować wiele odpowiedzi (streaming przez JSON lines).
Ostatnia odpowiedź ma "done": true.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from backend.config import settings
from backend.core.agent import get_agent


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

            # Obsłuż potwierdzenie
            if "confirm" in request:
                confirmed: bool = bool(request["confirm"])
                async for chunk in agent.confirm(session_id, confirmed):
                    status = "confirm" if "⚠️" in chunk else "ok"
                    await _send(writer, {"response": chunk, "status": status, "done": False})
                await _send(writer, {"response": "", "status": "ok", "done": True})
                continue

            # Obsłuż wiadomość
            message = request.get("message", "").strip()
            if not message:
                await _send(writer, {"response": "Pusta wiadomość.", "status": "error", "done": True})
                continue

            print(f"[server] Wiadomość od {interface}: {message[:80]}", flush=True)
            chunk_count = 0
            async for chunk in agent.chat(session_id, message, interface):
                chunk_count += 1
                if "⚠️" in chunk and "wymaga potwierdzenia" in chunk:
                    status = "confirm"
                elif "❌" in chunk or "🚫" in chunk:
                    status = "error"
                else:
                    status = "ok"
                await _send(writer, {"response": chunk, "status": status, "done": False})

            print(f"[server] Odpowiedź wysłana ({chunk_count} fragmentów)", flush=True)
            await _send(writer, {"response": "", "status": "ok", "done": True})

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


async def _send(writer: asyncio.StreamWriter, data: dict) -> None:
    """Wysyła odpowiedź JSON zakończoną newline."""
    line = json.dumps(data, ensure_ascii=False) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()


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
    )
    os.chmod(socket_path, 0o600)

    # ─── TCP (localhost only — dla SSH tunnel) ────────────────────────────
    tcp_server = await asyncio.start_server(
        handle_client,
        host=tcp_host,       # TYLKO 127.0.0.1 — nie eksponuj na zewnątrz!
        port=tcp_port,
    )

    print(f"[VPS Agent] Unix socket : {socket_path}", flush=True)
    print(f"[VPS Agent] TCP         : {tcp_host}:{tcp_port} (tylko localhost — użyj SSH tunnel)", flush=True)
    print(f"[VPS Agent] Model       : {settings.LLM_MODEL}", flush=True)
    print(f"[VPS Agent] Audit log   : {settings.AUDIT_LOG_PATH}", flush=True)
    print(f"[VPS Agent] Serwer gotowy. Ctrl+C aby zatrzymać.", flush=True)

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
