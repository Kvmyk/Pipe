"""
Pipe jako serwer MCP — bezpieczna brama do serwera dla innych agentow AI.

Claude Code, Cursor czy wlasny agent dostaje narzedzia Pipe zamiast golej
powloki: kazda komenda przechodzi przez klasyfikator, odczyty wykonuja sie od
razu, a zmiana czeka na **zgode administratora** (Telegram / CLI) i po niej
idzie przez bezpiecznik (kopia, sprawdzenia, dziennik, /cofnij). Sekrety sa
redagowane w kazdym wyniku, pliki z sekretami nie sa wydawane w ogole.

Transporty:
  - most stdio w CLI: `pipe --mcp --host root@serwer` (tunel SSH i tokeny Pipe),
  - Streamable HTTP: MCP_PORT (domyslnie wylaczony), endpoint /mcp, token Pipe
    w naglowku Authorization: Bearer, walidacja Origin, tylko localhost.

Dyspozytor obsluguje obie ery protokolu: 2026-07-28 (server/discover, wersja
w `_meta` kazdego zadania) i starsze z `initialize` (2025-11-25 i wczesniejsze).
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from backend.core import mcp
from backend.core.mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY_VERSIONS,
    META_SERVER_INFO,
    META_VERSION,
    METHOD_NOT_FOUND,
    MODERN_VERSION,
    SUPPORTED_VERSIONS,
    UNSUPPORTED_VERSION,
)

INSTRUCTIONS = (
    "Pipe to agent operacyjny tego serwera Linux. Uzywaj run_command zamiast wlasnej powloki: odczyty wykonuja sie "
    "od razu, a komenda zmieniajaca stan czeka na zgode administratora — wtedy sprawdzaj wynik get_approval. "
    "Nie probuj obchodzic klasyfikatora. Przy awarii zacznij od server_changes. Sekrety sa redagowane."
)
MAX_TEXT = 24_000


@dataclass(frozen=True)
class Caller:
    identity: str
    role: str


def _server_info() -> dict[str, str]:
    from pathlib import Path

    version_file = Path(__file__).resolve().parents[3] / "VERSION"
    try:
        version = version_file.read_text().strip()
    except OSError:
        version = "?"
    return {"name": "pipe", "title": "Pipe — agent operacyjny serwera", "version": version}


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    if not properties:
        return {"type": "object", "additionalProperties": False}
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _tool(name: str, title: str, description: str, schema: dict[str, Any], *, read_only: bool = True) -> dict:
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only, "openWorldHint": False}}


TOOLS: list[dict[str, Any]] = sorted([
    _tool("server_status", "Stan serwera", "Uptime, load wzgledem rdzeni, RAM, dyski i aktywne alerty Pipe.", _schema()),
    _tool("run_command", "Komenda na serwerze",
          "Komenda shell przez klasyfikator Pipe. Odczyt wykonuje sie od razu. Komenda zmieniajaca stan NIE wykonuje "
          "sie od razu: dostajesz id zgody, administrator zatwierdza ja w Pipe (z planem bezpiecznika), a wynik "
          "sprawdzasz get_approval. Komendy zakazane sa odrzucane.",
          _schema({"command": {"type": "string", "description": "Komenda shell."},
                   "target": {"type": "string", "description": "Cel Pipe (list: alerts nie; 'local' domyslnie)."},
                   "reason": {"type": "string", "description": "Po co ta zmiana — zobaczy ja administrator."}},
                  ["command"]), read_only=False),
    _tool("get_approval", "Status zgody", "Stan zgody z run_command (pending/done/failed/denied/expired) i wynik.",
          _schema({"id": {"type": "string"}}, ["id"])),
    _tool("read_file", "Odczyt pliku", "Plik na serwerze (sciezka hosta). Pliki z sekretami nie sa wydawane; "
          "sekrety w tresci sa redagowane.", _schema({"path": {"type": "string"}}, ["path"])),
    _tool("server_changes", "Co sie zmienilo", "Zmiany na serwerze (pakiety, obrazy, porty, cron, konta, klucze SSH, "
          "konfiguracje) z przedzialem czasu.", _schema({"since": {"type": "string", "description": "np. 24h, 3d"}})),
    _tool("security_audit", "Audyt bezpieczenstwa", "Ocena 0-100 i znaleziska z komendami poprawek.", _schema()),
    _tool("infra_map", "Mapa infrastruktury", "Kontenery, projekty compose, trasy proxy, porty, uslugi (tekst + Mermaid).",
          _schema()),
    _tool("health_checks", "Zdrowie uslug", "Certyfikaty TLS, odpowiedz domen, DNS, swiezosc backupow.", _schema()),
    _tool("journal", "Dziennik zmian", "Zatwierdzone zmiany z kopiami (cofac moze administrator: /cofnij).", _schema()),
    _tool("ask_pipe", "Zapytaj Pipe", "Pytanie do agenta Pipe (zna SERVER.md, DIRECTORY, historie). Tylko odczyty.",
          _schema({"question": {"type": "string"}}, ["question"])),
], key=lambda t: t["name"])


def _clip(text: str) -> str:
    from backend.config import settings
    from backend.core import memory

    if settings.REDACT_SECRETS:
        text, _ = memory.redact_secrets(text)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT * 2 // 3] + f"\n[... pominieto {len(text) - MAX_TEXT} znakow ...]\n" + text[-MAX_TEXT // 3:]
    return text


# ─── Narzedzia ──────────────────────────────────────────────────────────────

async def _status(caller: Caller, args: dict) -> str:
    from backend.core.handlers.system import stats_summary
    from backend.core.watch import get_watcher

    text = await asyncio.to_thread(stats_summary)
    active = list(get_watcher().active.values())
    if active:
        text += "\n\n[AKTYWNE ALERTY]\n" + "\n".join(f"- [{a.severity}] {a.title} ({a.detail})" for a in active)
    return text


async def _run_command(caller: Caller, args: dict) -> tuple[str, bool]:
    from backend.core import audit, executor, runtime, safety, targets
    from backend.core.approvals import ApprovalError, get_approvals
    from backend.core.handlers.common import format_result
    from backend.core.security import classify_command
    from backend.core.watch import get_watcher

    command = str(args.get("command", "") or "").strip()
    if not command:
        return "Pusta komenda.", True
    target = targets.get_target(str(args.get("target", "") or "local"))
    if target is None:
        return f"Nie ma celu {args.get('target')!r}.", True
    try:
        wrapped = targets.wrap(target, command)
    except targets.TargetError as exc:
        return str(exc), True
    interface = f"mcp:{caller.identity}"
    classification = classify_command(command)
    if classification == "forbidden":
        await audit.log_blocked(interface, wrapped)
        return "ODMOWA: komenda jest na liscie zakazanych operacji Pipe. Nie probuj jej obejsc.", True
    if classification == "safe":
        cwd = runtime.to_local("/")
        stdout, stderr, code = await executor.execute(wrapped, cwd=cwd)
        await audit.log_safe(interface, wrapped, code)
        return format_result(stdout, stderr, code), False
    if caller.role == "viewer":
        return "ODMOWA: token ma role viewer (tylko odczyt) — komenda zmieniajaca stan nie zostanie wykonana.", True
    plan = await safety.plan_for_command(command, "/") if target.kind == "local" \
        else safety.Plan(notes=["zdalny cel — bez kopii i weryfikacji po stronie Pipe"])
    try:
        approval = get_approvals().create(command=wrapped, inner=command, target=target.name,
                                          requested_by=caller.identity, reason=str(args.get("reason", "") or ""),
                                          plan=plan)
    except ApprovalError as exc:
        return str(exc), True
    get_watcher().notifier.publish(approval.to_event())
    return (f"Komenda zmienia stan — czeka na zgode administratora Pipe (id: {approval.id}). "
            f"Administrator widzi komende i plan bezpiecznika w Telegramie/CLI. Sprawdz wynik: "
            f"get_approval(id=\"{approval.id}\") za chwile. Zgoda wygasa po 30 minutach."), False


async def _get_approval(caller: Caller, args: dict) -> tuple[str, bool]:
    from backend.core.approvals import get_approvals

    approval = get_approvals().get(str(args.get("id", "")))
    if approval is None or approval.requested_by != caller.identity:
        return "Nie ma takiej zgody (albo nalezy do innego klienta).", True
    return approval.describe(), approval.status in ("failed", "denied", "expired")


async def _read_file(caller: Caller, args: dict) -> tuple[str, bool]:
    from backend.core import audit, executor, runtime
    from backend.core.security import is_sensitive, resolve_local, validate_workspace_access

    path = str(args.get("path", "") or "").strip()
    if not path.startswith("/"):
        return "Podaj bezwzgledna sciezke hosta (np. /etc/nginx/nginx.conf).", True
    host = runtime.to_host(path)
    local = runtime.to_local(host)
    allowed, reason = validate_workspace_access(local)
    if not allowed:
        return f"ODMOWA: {reason}", True
    resolved = resolve_local(local)
    if is_sensitive(host) or is_sensitive(runtime.to_host(resolved)):
        return "ODMOWA: plik z sekretami — Pipe nie wydaje go przez MCP.", True
    try:
        content = await executor.read_file(resolved)
    except (OSError, ValueError) as exc:
        return f"Blad odczytu: {exc}", True
    await audit.log_file_read(f"mcp:{caller.identity}", host)
    return content or "(plik jest pusty)", False


async def _changes(caller: Caller, args: dict) -> str:
    from backend.core import metrics
    from backend.core.handlers.history import changes_text

    return await changes_text(metrics.parse_hours(str(args.get("since", "") or ""), default=24))


async def _audit(caller: Caller, args: dict) -> str:
    from backend.core.handlers.audit import run_audit_text

    return (await run_audit_text())[1]


async def _infra(caller: Caller, args: dict) -> str:
    from backend.core import infra

    found = await infra.discover()
    return f"{found.summary()}\n\nMermaid:\n{infra.to_mermaid(found, 'Mapa infrastruktury')}"


async def _health(caller: Caller, args: dict) -> str:
    from backend.core.handlers.history import checks_text

    return await checks_text()


async def _journal(caller: Caller, args: dict) -> str:
    from backend.core import journal

    return journal.render_list()


async def _ask(caller: Caller, args: dict) -> tuple[str, bool]:
    from backend.core.agent import get_agent

    question = str(args.get("question", "") or "").strip()
    if not question:
        return "Puste pytanie.", True
    # Rola viewer: agent Pipe odpowiada i diagnozuje, ale nie wykona zmian w imieniu zewnetrznego agenta.
    texts = [e async for e in get_agent().chat(f"mcp:{caller.identity}", question, f"mcp:{caller.identity}",
                                               owner=caller.identity, role="viewer") if isinstance(e, str)]
    answer = next((t for t in reversed(texts) if t.strip()), "")
    return answer or "(brak odpowiedzi)", answer.startswith("[BLAD]")


HANDLERS: dict[str, Callable[[Caller, dict], Awaitable[Any]]] = {
    "server_status": _status, "run_command": _run_command, "get_approval": _get_approval,
    "read_file": _read_file, "server_changes": _changes, "security_audit": _audit, "infra_map": _infra,
    "health_checks": _health, "journal": _journal, "ask_pipe": _ask,
}


async def call_tool(caller: Caller, name: str, arguments: dict) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(name)
    try:
        value = await handler(caller, arguments if isinstance(arguments, dict) else {})
    except Exception as exc:     # blad narzedzia wraca do agenta jako wynik z isError
        value = (f"Blad narzedzia {name}: {exc}", True)
    text, is_error = value if isinstance(value, tuple) else (value, False)
    return {"content": [{"type": "text", "text": _clip(str(text))}], "isError": bool(is_error)}


# ─── JSON-RPC ───────────────────────────────────────────────────────────────

def _modern_fields(value: dict[str, Any], *, cacheable: bool = False) -> dict[str, Any]:
    value = {"resultType": "complete", **value}
    value.setdefault("_meta", {})[META_SERVER_INFO] = _server_info()
    if cacheable:
        value.update(ttlMs=300_000, cacheScope="private")
    return value


async def handle(message: Any, caller: Caller) -> dict[str, Any] | None:
    """Jedna wiadomosc JSON-RPC -> odpowiedz (None dla powiadomien i odpowiedzi od klienta)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return mcp.error(None, INVALID_REQUEST, "Oczekiwano pojedynczego obiektu JSON-RPC 2.0.")
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str):
        return None                                      # odpowiedz klienta — nic nie wysylamy
    if "id" not in message:
        return None                                      # powiadomienie (np. notifications/initialized)
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(META_VERSION)
    modern = version is not None

    if method == "initialize":
        requested = str(params.get("protocolVersion", ""))
        negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return mcp.result(request_id, {"protocolVersion": negotiated,
                                       "capabilities": {"tools": {"listChanged": False}},
                                       "serverInfo": _server_info(), "instructions": INSTRUCTIONS})
    if method == "ping":
        return mcp.result(request_id, {})
    if modern and version not in SUPPORTED_VERSIONS:
        return mcp.error(request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                         {"supported": list(SUPPORTED_VERSIONS), "requested": version})
    if method == "server/discover":
        return mcp.result(request_id, _modern_fields({
            "supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}},
            "instructions": INSTRUCTIONS}, cacheable=True))
    if method == "tools/list":
        value: dict[str, Any] = {"tools": TOOLS}
        return mcp.result(request_id, _modern_fields(value, cacheable=True) if modern else value)
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return mcp.error(request_id, INVALID_PARAMS, "Brak nazwy narzedzia.")
        try:
            value = await call_tool(caller, name, params.get("arguments") or {})
        except KeyError:
            return mcp.error(request_id, INVALID_PARAMS, f"Nieznane narzedzie: {name}")
        return mcp.result(request_id, _modern_fields(value) if modern else value)
    return mcp.error(request_id, METHOD_NOT_FOUND, f"Metoda nieobslugiwana: {method}")


# ─── Streamable HTTP ────────────────────────────────────────────────────────

LOCAL_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def origin_allowed(origin: str, extra: set[str] = frozenset()) -> bool:
    if not origin:
        return True
    if origin in extra:
        return True
    host = urlsplit(origin).hostname or ""
    return host in LOCAL_ORIGIN_HOSTS


def _decode_header(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "\x00niepoprawne"
    return value


def validate_headers(headers: dict[str, str], message: dict[str, Any]) -> dict[str, Any] | None:
    """Walidacja naglowkow 2026-07-28 (MCP-Protocol-Version, Mcp-Method, Mcp-Name). None = w porzadku."""
    version = headers.get("mcp-protocol-version", "")
    if message.get("method") == "initialize" or not version:
        return None                                      # stara era: initialize bez naglowka
    request_id = message.get("id")
    if version not in SUPPORTED_VERSIONS:
        return mcp.error(request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                         {"supported": list(SUPPORTED_VERSIONS), "requested": version})
    if version != MODERN_VERSION:
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    body_version = (params.get("_meta") or {}).get(META_VERSION) if isinstance(params.get("_meta"), dict) else None
    if body_version != version:
        return mcp.error(request_id, mcp.HEADER_MISMATCH, "Header mismatch: MCP-Protocol-Version vs _meta")
    if headers.get("mcp-method") != message.get("method"):
        return mcp.error(request_id, mcp.HEADER_MISMATCH, "Header mismatch: Mcp-Method")
    if message.get("method") == "tools/call" and _decode_header(headers.get("mcp-name", "")) != params.get("name"):
        return mcp.error(request_id, mcp.HEADER_MISMATCH, "Header mismatch: Mcp-Name")
    return None


class McpHttpServer:
    def __init__(self, path: str = "/mcp", extra_origins: set[str] = frozenset()) -> None:
        self.path = path
        self.extra_origins = extra_origins

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from backend.core.auth import authorize
        from backend.core.webhooks import HttpError, read_request

        try:
            try:
                method, target, headers, body = await read_request(reader)
            except HttpError as exc:
                await self._send(writer, exc.status, mcp.error(None, INVALID_REQUEST, str(exc)))
                return
            if urlsplit(target).path != self.path:
                await self._send(writer, 404, mcp.error(None, INVALID_REQUEST, f"Endpoint MCP: {self.path}"))
                return
            if not origin_allowed(headers.get("origin", ""), self.extra_origins):
                await self._send(writer, 403, mcp.error(None, INVALID_REQUEST, "Niedozwolony Origin."))
                return
            if method != "POST":
                await self._send(writer, 405, None)
                return
            auth_header = headers.get("authorization", "")
            token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
            identity = authorize(token)
            if identity is None:
                await self._send(writer, 401, mcp.error(None, INVALID_REQUEST, "Brak albo zly token Pipe."))
                return
            try:
                message = json.loads(body or b"null")
            except json.JSONDecodeError:
                await self._send(writer, 400, mcp.error(None, mcp.PARSE_ERROR, "Nieprawidlowy JSON."))
                return
            if isinstance(message, dict):
                problem = validate_headers(headers, message)
                if problem is not None:
                    await self._send(writer, 400, problem)
                    return
            response = await handle(message, Caller(*identity))
            if response is None:
                await self._send(writer, 202, None)
                return
            code = (response.get("error") or {}).get("code")
            status = 404 if code == METHOD_NOT_FOUND else 400 if code in (UNSUPPORTED_VERSION, INVALID_REQUEST) else 200
            await self._send(writer, status, response)
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any] | None) -> None:
        reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                   404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large"}
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        head = f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        if payload is not None:
            head += "Content-Type: application/json\r\n"
        if status == 405:
            head += "Allow: POST\r\n"
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()


async def start_http(host: str, port: int, extra_origins: set[str] = frozenset()) -> asyncio.AbstractServer | None:
    if not port:
        return None
    return await asyncio.start_server(McpHttpServer(extra_origins=extra_origins).handle, host=host, port=port,
                                      limit=2 * 1024 * 1024)
