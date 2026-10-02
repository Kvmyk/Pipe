"""
Pipe Web — lokalny serwer interfejsu przegladarkowego (`pipe web`).

Dziala NA LAPTOPIE uzytkownika, tak jak CLI. Nie otwiera zadnego portu na serwerze:

    przegladarka --HTTP--> 127.0.0.1:7400 (ten proces) --tunel SSH--> backend/server.py

Ten proces serwuje strone (clients/webui/static) i mostkuje jej zadania na protokol
JSON lines backendu (docs/protocol.md):

    POST /api/request   dowolne zadanie protokolu (message / confirm / command);
                        odpowiedz to strumien ramek, jedna na linie (NDJSON)
    GET  /api/events    zdarzenia czuwania na zywo (subscribe) jako Server-Sent Events

Token backendu zostaje w tym procesie — przegladarka go nie widzi. Strone otwiera tylko
ktos, kto zna jednorazowy klucz z adresu (drukowany w terminalu); pozniej klucz jest w
ciasteczku HttpOnly. Zadania z obcym naglowkiem Host albo Origin sa odrzucane (ochrona
przed stronami internetowymi, ktore probowalyby siegnac do localhosta).

Wylacznie biblioteka standardowa.
"""

from __future__ import annotations

import asyncio
import getpass
import hmac
import json
import mimetypes
import secrets
import socket
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC_DIR = Path(__file__).resolve().parent / "static"
READ_LIMIT = 32 * 1024 * 1024          # ramki z diagramami (base64) sa duze
MAX_BODY = 4 * 1024 * 1024
DEFAULT_PORT = 7400
COOKIE = "pipe_key"
# Pola zadania, ktore przegladarka moze ustawic — reszte (token, interface) dodaje ten proces.
ALLOWED_FIELDS = {"message", "confirm", "command", "name", "args", "id", "execute", "decision", "cancel", "claim",
                  "key", "model"}
SECURITY_HEADERS = (
    "X-Content-Type-Options: nosniff\r\n"
    "X-Frame-Options: DENY\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\n"
)


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def free_port(preferred: int) -> int:
    """Preferowany port, a gdy zajety — dowolny wolny."""
    for candidate in (preferred, 0):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


class WebBridge:
    def __init__(self, backend_host: str, backend_port: int, token: str, *, lang: str = "pl",
                 server_label: str = "", port: int = DEFAULT_PORT) -> None:
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.token = token
        self.lang = "en" if lang.startswith("en") else "pl"
        self.server_label = server_label
        self.port = port
        self.key = secrets.token_urlsafe(24)
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        # "web" na poczatku interfejsu wlacza w backendzie zdarzenia aktywnosci (schemat na zywo)
        self.interface = f"web:{user}" if user else "web"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.key}"

    # --- HTTP ---------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target, headers, body = await asyncio.wait_for(self._read_request(reader), 30)
            await self._route(writer, method, target, headers, body)
        except HttpError as exc:
            await self._send(writer, exc.status, json.dumps({"error": str(exc)}).encode(), "application/json")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except Exception as exc:
            try:
                await self._send(writer, 500, json.dumps({"error": str(exc)}).encode(), "application/json")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        line = (await reader.readline()).decode("latin-1").strip()
        parts = line.split()
        if len(parts) != 3:
            raise HttpError(400, "bad request line")
        method, target, _version = parts
        headers: dict[str, str] = {}
        for _ in range(100):
            raw = (await reader.readline()).decode("latin-1")
            if raw in ("\r\n", "\n", ""):
                break
            name, _, value = raw.partition(":")
            headers[name.strip().lower()] = value.strip()
        else:
            raise HttpError(431, "too many headers")
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "request too large")
        body = await reader.readexactly(length) if length else b""
        return method.upper(), target, headers, body

    def _check_origin(self, headers: dict[str, str]) -> None:
        """Tylko nasza wlasna strona: Host musi byc localhostem na naszym porcie, Origin (jesli jest) — tym samym."""
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if headers.get("host", "") not in allowed:
            raise HttpError(403, "forbidden host")
        origin = headers.get("origin", "")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise HttpError(403, "forbidden origin")

    def _authorized(self, headers: dict[str, str], query: dict[str, list[str]]) -> bool:
        cookies = dict(part.strip().split("=", 1) for part in headers.get("cookie", "").split(";") if "=" in part)
        # Wystarczy jedno z dwojga: po ponownym uruchomieniu `pipe web` przegladarka ma jeszcze ciasteczko
        # z poprzedniego klucza — nie moze ono uniewazniac poprawnego klucza z adresu.
        candidates = [cookies.get(COOKIE, ""), (query.get("k") or [""])[0]]
        return any(c and hmac.compare_digest(c.encode(), self.key.encode()) for c in candidates)

    async def _route(self, writer, method: str, target: str, headers: dict[str, str], body: bytes) -> None:
        self._check_origin(headers)
        url = urlsplit(target)
        query = parse_qs(url.query)
        path = url.path
        if not self._authorized(headers, query):
            raise HttpError(401, "open the address printed by `pipe web` (it contains the access key)")

        if method == "GET" and path in ("/", "/index.html"):
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            config = {"lang": self.lang, "server": self.server_label, "session": str(uuid.uuid4())}
            page = page.replace("__PIPE_CONFIG__", json.dumps(config).replace("<", "\\u003c"))
            extra = f"Set-Cookie: {COOKIE}={self.key}; HttpOnly; SameSite=Strict; Path=/\r\nCache-Control: no-store\r\n"
            await self._send(writer, 200, page.encode("utf-8"), "text/html; charset=utf-8", extra)
        elif method == "GET" and path.startswith("/static/"):
            await self._static(writer, path[len("/static/"):])
        elif method == "POST" and path == "/api/request":
            await self._proxy_request(writer, body)
        elif method == "GET" and path == "/api/events":
            await self._events(writer)
        else:
            raise HttpError(404, "not found")

    async def _static(self, writer, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            raise HttpError(404, "not found")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "image/svg+xml"):
            mime += "; charset=utf-8"
        await self._send(writer, 200, target.read_bytes(), mime, "Cache-Control: no-cache\r\n")

    async def _send(self, writer, status: int, body: bytes, mime: str, extra: str = "") -> None:
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                   413: "Payload Too Large", 431: "Request Header Fields Too Large", 500: "Internal Server Error",
                   502: "Bad Gateway"}
        writer.write((f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\nContent-Type: {mime}\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n{SECURITY_HEADERS}{extra}\r\n").encode())
        writer.write(body)
        await writer.drain()

    # --- most do backendu ---------------------------------------------------

    def _backend_request(self, payload: dict) -> dict:
        request = {key: value for key, value in payload.items() if key in ALLOWED_FIELDS}
        if not any(key in request for key in ("message", "confirm", "command")):
            raise HttpError(400, "expected message, confirm or command")
        if request.get("command") == "subscribe":
            raise HttpError(400, "use /api/events")
        session = str(payload.get("session", ""))[:64]
        request["session_id"] = "web-" + (session or "default")
        request["interface"] = self.interface
        if self.token:
            request["token"] = self.token
        return request

    async def _proxy_request(self, writer, body: bytes) -> None:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(payload, dict):
            raise HttpError(400, "expected a JSON object")
        request = self._backend_request(payload)
        try:
            reader, backend = await asyncio.open_connection(self.backend_host, self.backend_port, limit=READ_LIMIT)
        except OSError as exc:
            raise HttpError(502, f"backend unreachable: {exc}") from None
        try:
            backend.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            await backend.drain()
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson; charset=utf-8\r\n"
                          f"Cache-Control: no-store\r\nX-Accel-Buffering: no\r\nConnection: close\r\n"
                          f"{SECURITY_HEADERS}\r\n").encode())
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                writer.write(line if line.endswith(b"\n") else line + b"\n")
                await writer.drain()
                try:
                    if json.loads(line).get("done"):
                        break
                except (json.JSONDecodeError, AttributeError):
                    continue
        finally:
            backend.close()

    async def _events(self, writer) -> None:
        """Zdarzenia czuwania (alerty, przypomnienia, raporty) jako SSE; laczy sie ponownie po zerwaniu."""
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\n"
                      f"X-Accel-Buffering: no\r\nConnection: close\r\n{SECURITY_HEADERS}\r\n").encode())
        await writer.drain()
        request = {"command": "subscribe", "session_id": "web-events", "interface": self.interface}
        if self.token:
            request["token"] = self.token
        try:
            reader, backend = await asyncio.open_connection(self.backend_host, self.backend_port, limit=READ_LIMIT)
        except OSError as exc:
            writer.write(f"event: down\ndata: {json.dumps(str(exc))}\n\n".encode())
            await writer.drain()
            return
        try:
            backend.write((json.dumps(request) + "\n").encode("utf-8"))
            await backend.drain()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), 25)
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n\n")      # podtrzymanie polaczenia i wykrycie zamknietej karty
                    await writer.drain()
                    continue
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = frame.get("event") or {"type": "error", "text": frame.get("response", "")}
                writer.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                await writer.drain()
        finally:
            backend.close()

    # --- start --------------------------------------------------------------

    async def serve(self, open_browser: bool = True, ready=None) -> None:
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, limit=READ_LIMIT)
        if ready is not None:
            ready(self)
        if open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass
        async with server:
            await server.serve_forever()


def run(backend_host: str, backend_port: int, token: str, *, lang: str = "pl", server_label: str = "",
        port: int = DEFAULT_PORT, open_browser: bool = True, ready=None) -> None:
    bridge = WebBridge(backend_host, backend_port, token, lang=lang, server_label=server_label, port=free_port(port))
    asyncio.run(bridge.serve(open_browser=open_browser, ready=ready))
