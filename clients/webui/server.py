"""
Pipe Web — lokalny serwer interfejsu przegladarkowego (`pipe web`).

Dziala NA LAPTOPIE uzytkownika, tak jak CLI. Nie otwiera zadnego portu na serwerze:

    przegladarka --HTTP--> 127.0.0.1:7400 (ten proces) --tunel SSH--> backend/server.py

Ten proces serwuje strone (clients/webui/static) i mostkuje jej zadania na protokol
JSON lines backendu (docs/protocol.md):

    POST /api/request   dowolne zadanie protokolu (message / confirm / command);
                        odpowiedz to strumien ramek, jedna na linie (NDJSON)
    GET  /api/events    zdarzenia czuwania na zywo (subscribe) jako Server-Sent Events
    GET  /api/app       stan pierwszego uruchomienia (czy jest backend, Docker, paramiko)
    POST /api/setup     postaw Pipe lokalnie albo na serwerze (clients/cli/appsetup.py); postep jako NDJSON

Bez backendu (pierwsze `pipe web`, nic nie zapisano w ~/.pipe/app.json) most dziala w trybie konfiguracji:
strona pyta, co uzytkownik chce robic i gdzie ma dzialac Pipe, a /api/setup stawia go i przelacza most na nowy
backend. Haslo SSH przychodzi tylko z tej strony (ten sam komputer) i zostaje w tym procesie.

Token backendu zostaje w tym procesie — przegladarka go nie widzi. Strone otwiera tylko
ktos, kto zna jednorazowy klucz z adresu (drukowany w terminalu); pozniej klucz jest w
ciasteczku HttpOnly. Zadania z obcym naglowkiem Host albo Origin sa odrzucane (ochrona
przed stronami internetowymi, ktore probowalyby siegnac do localhosta).

Tryb publiczny (`public`, `/web wlacz` — backend/core/webpublic.py): ten sam most dziala w procesie Pipe na serwerze,
za Caddy z HTTPS. Nie ma wtedy klucza w adresie ani tokenu backendu — strona bez ciasteczka urzadzenia pokazuje
tylko prosbe o dostep (pair.html):

    POST /api/pair/start   nowa prosba: kod do pokazania, administrator zatwierdza go w rozmowie
    POST /api/pair/poll    stan prosby; po zgodzie ciasteczko urzadzenia (HttpOnly, Secure)
    POST /api/logout       odwoluje to urzadzenie

Kazde zadanie do backendu idzie z tokenem urzadzenia (rola admin albo viewer), wiec backend sam pilnuje uprawnien.

Wylacznie biblioteka standardowa.
"""

from __future__ import annotations

import asyncio
import getpass
import hmac
import ipaddress
import json
import mimetypes
import secrets
import socket
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC_DIR = Path(__file__).resolve().parent / "static"
mimetypes.add_type("font/woff2", ".woff2")       # kroj IBM Plex (static/fonts) — nie kazdy system zna to rozszerzenie
READ_LIMIT = 32 * 1024 * 1024          # ramki z diagramami (base64) sa duze
MAX_BODY = 32 * 1024 * 1024           # zalaczniki: do 20 MB razem (core/attachments.py), base64 +33%
DEFAULT_PORT = 7400
COOKIE = "pipe_key"
DEVICE_COOKIE = "pipe_device"          # tryb publiczny: token urzadzenia (core/webaccess.py)
DEVICE_MAX_AGE = 30 * 86400
# Pola zadania, ktore przegladarka moze ustawic — reszte (token, interface) dodaje ten proces.
ALLOWED_FIELDS = {"message", "confirm", "command", "name", "args", "id", "execute", "decision", "cancel", "claim",
                  "key", "model", "attachments", "audio", "filename", "scope", "patch_token", "action", "level", "quiet",
                  "per_day", "since"}
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
    def __init__(self, backend_host: str | None, backend_port: int, token: str, *, lang: str = "pl",
                 server_label: str = "", port: int = DEFAULT_PORT, setup=None, public=None) -> None:
        # setup: obiekt z state(lang) -> dict i run(request, progress, lang) -> (host, port, token, etykieta);
        # dostarcza go CLI (`pipe web`). Bez niego strona nie stawia Pipe, tylko z nim rozmawia.
        # public: tryb publiczny (backend/core/webpublic.py) — obiekt z hosts, device(token), pair_start(ua, ip),
        # pair_poll(id, secret), logout(token). backend_host "unix:<sciezka>" — gniazdo backendu.
        self.setup = setup
        self.public = public
        self._setup_lock = asyncio.Lock()
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
    def setup_mode(self) -> bool:
        """Brak backendu: strona pokazuje tylko pierwsze uruchomienie."""
        return not self.backend_host

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.key}"

    def _adopt_language(self, value) -> None:
        """Jezyk zmieniony na serwerze (/jezyk z dowolnego kanalu) — strona po odswiezeniu dostaje nowy."""
        if value in ("pl", "en"):
            self.lang = value

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
        """Tylko nasza wlasna strona: Host musi byc localhostem na naszym porcie (w trybie publicznym — adresem
        strony), Origin (jesli jest) — tym samym."""
        if self.public is not None:
            allowed, scheme = set(self.public.hosts), "https"
        else:
            allowed, scheme = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}, "http"
        if headers.get("host", "") not in allowed:
            raise HttpError(403, "forbidden host")
        origin = headers.get("origin", "")
        if origin and origin not in {f"{scheme}://{host}" for host in allowed}:
            raise HttpError(403, "forbidden origin")

    @staticmethod
    def _cookies(headers: dict[str, str]) -> dict[str, str]:
        return dict(part.strip().split("=", 1) for part in headers.get("cookie", "").split(";") if "=" in part)

    def _authorized(self, headers: dict[str, str], query: dict[str, list[str]]) -> bool:
        cookies = self._cookies(headers)
        # Wystarczy jedno z dwojga: po ponownym uruchomieniu `pipe web` przegladarka ma jeszcze ciasteczko
        # z poprzedniego klucza — nie moze ono uniewazniac poprawnego klucza z adresu.
        candidates = [cookies.get(COOKIE, ""), (query.get("k") or [""])[0]]
        return any(c and hmac.compare_digest(c.encode(), self.key.encode()) for c in candidates)

    def _who(self, headers: dict[str, str], query: dict[str, list[str]]) -> dict | None:
        """Z czym zadanie idzie do backendu: token i interfejs (None — nieuprawnione)."""
        if self.public is None:
            return {"token": self.token, "interface": self.interface} if self._authorized(headers, query) else None
        token = self._cookies(headers).get(DEVICE_COOKIE, "")
        found = self.public.device(token) if token else None
        if not found:
            return None
        return {"token": token, "interface": f"web:{found[0]}", "role": found[1]}

    @staticmethod
    def _client_ip(headers: dict[str, str]) -> str:
        """Adres przegladarki od Caddy (naglowek ustawia proxy, nie klient); '?' gdy go brak."""
        try:
            return str(ipaddress.ip_address(headers.get("x-pipe-client-ip", "").strip()))
        except ValueError:
            return "?"

    def _page(self, name: str, extra_config: dict | None = None) -> bytes:
        page = (STATIC_DIR / name).read_text(encoding="utf-8")
        config = {"lang": self.lang, "server": self.server_label, "session": str(uuid.uuid4()),
                  "setup": self.setup_mode, "canSetup": self.setup is not None, "public": self.public is not None,
                  **(extra_config or {})}
        return page.replace("__PIPE_CONFIG__", json.dumps(config).replace("<", "\\u003c")).encode("utf-8")

    async def _route(self, writer, method: str, target: str, headers: dict[str, str], body: bytes) -> None:
        self._check_origin(headers)
        url = urlsplit(target)
        query = parse_qs(url.query)
        path = url.path
        who = self._who(headers, query)
        if who is None:
            if self.public is None:
                raise HttpError(401, "open the address printed by `pipe web` (it contains the access key)")
            await self._route_guest(writer, method, path, headers, body)
            return

        if method == "GET" and path in ("/", "/index.html"):
            extra = "Cache-Control: no-store\r\n"
            if self.public is None:
                extra = f"Set-Cookie: {COOKIE}={self.key}; HttpOnly; SameSite=Strict; Path=/\r\n" + extra
            page = self._page("index.html", {"role": who["role"]} if "role" in who else None)
            await self._send(writer, 200, page, "text/html; charset=utf-8", extra)
        elif method == "GET" and path.startswith("/static/"):
            await self._static(writer, path[len("/static/"):])
        elif method == "GET" and path == "/api/app" and self.public is None:
            await self._app_state(writer)
        elif method == "POST" and path == "/api/setup" and self.public is None:
            await self._run_setup(writer, body)
        elif method == "POST" and path == "/api/request":
            await self._proxy_request(writer, body, who)
        elif method == "GET" and path == "/api/events":
            await self._events(writer, who)
        elif method == "POST" and path == "/api/logout" and self.public is not None:
            self.public.logout(who["token"])
            await self._send(writer, 200, b'{"ok": true}', "application/json", self._device_cookie("", 0))
        else:
            raise HttpError(404, "not found")

    # --- tryb publiczny: urzadzenie bez dostepu ------------------------------

    @staticmethod
    def _device_cookie(token: str, max_age: int) -> str:
        # Lax, nie Strict: link do strony otwierany z Telegrama (inna witryna) ma od razu wejsc z ciasteczkiem.
        # Zadania API to POST z kontrola Origin, wiec Lax nie otwiera drogi do CSRF.
        return f"Set-Cookie: {DEVICE_COOKIE}={token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={max_age}\r\n"

    async def _route_guest(self, writer, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        """Bez urzadzenia: strona prosby o dostep, jej pliki i dwa wywolania parowania — nic wiecej."""
        if method == "GET" and path in ("/", "/index.html"):
            stale = self._cookies(headers).get(DEVICE_COOKIE)
            extra = "Cache-Control: no-store\r\n" + (self._device_cookie("", 0) if stale else "")
            await self._send(writer, 200, self._page("pair.html"), "text/html; charset=utf-8", extra)
        elif method == "GET" and path.startswith("/static/"):
            await self._static(writer, path[len("/static/"):])
        elif method == "POST" and path == "/api/pair/start":
            try:
                result = self.public.pair_start(headers.get("user-agent", "")[:400], self._client_ip(headers))
            except ValueError as exc:                 # limit prob, za duzo oczekujacych
                raise HttpError(429, str(exc)) from None
            await self._send(writer, 200, json.dumps(result).encode(), "application/json", "Cache-Control: no-store\r\n")
        elif method == "POST" and path == "/api/pair/poll":
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                raise HttpError(400, "invalid JSON") from None
            if not isinstance(payload, dict):
                raise HttpError(400, "expected a JSON object")
            result = dict(self.public.pair_poll(str(payload.get("id", ""))[:64], str(payload.get("secret", ""))[:128]))
            token = result.pop("token", "")
            extra = "Cache-Control: no-store\r\n" + (self._device_cookie(token, DEVICE_MAX_AGE) if token else "")
            await self._send(writer, 200, json.dumps(result).encode(), "application/json", extra)
        else:
            raise HttpError(401, "this device has no access yet — open the page and ask for access")

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
                   409: "Conflict", 413: "Payload Too Large", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
                   500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}
        writer.write((f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\nContent-Type: {mime}\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n{SECURITY_HEADERS}{extra}\r\n").encode())
        writer.write(body)
        await writer.drain()

    # --- pierwsze uruchomienie ---------------------------------------------

    async def _app_state(self, writer) -> None:
        state = {"setup": self.setup_mode, "canSetup": self.setup is not None, "server": self.server_label}
        if self.setup is not None:
            try:
                state.update(await asyncio.to_thread(self.setup.state, self.lang))
            except Exception as exc:
                state["error"] = str(exc)
        await self._send(writer, 200, json.dumps(state).encode(), "application/json", "Cache-Control: no-store\r\n")

    async def _run_setup(self, writer, body: bytes) -> None:
        """Stawia Pipe (lokalnie albo na serwerze) i przelacza most na nowy backend; postep linia po linii (NDJSON)."""
        if self.setup is None:
            raise HttpError(404, "not found")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(payload, dict) or payload.get("kind") not in ("local", "server"):
            raise HttpError(400, "expected kind: local | server")
        if self._setup_lock.locked():
            raise HttpError(409, "setup already running")
        request = {key: str(payload.get(key) or "")[:4096] for key in ("kind", "host", "port", "user", "password", "key")}
        view = str(payload.get("view") or "")
        async with self._setup_lock:
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson; charset=utf-8\r\n"
                          f"Cache-Control: no-store\r\nX-Accel-Buffering: no\r\nConnection: close\r\n"
                          f"{SECURITY_HEADERS}\r\n").encode())
            await writer.drain()
            loop = asyncio.get_running_loop()
            lines: asyncio.Queue = asyncio.Queue()

            def progress(text: str) -> None:          # wolane z watku instalacji
                loop.call_soon_threadsafe(lines.put_nowait, str(text)[:2000])

            task = asyncio.ensure_future(asyncio.to_thread(self.setup.run, request, progress, self.lang))
            while not task.done() or not lines.empty():
                getter = asyncio.ensure_future(lines.get())
                await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    await self._line(writer, {"progress": getter.result()})
                else:
                    getter.cancel()
            try:
                host, port, token, label = task.result()
            except Exception as exc:
                await self._line(writer, {"done": True, "ok": False, "error": str(exc)})
                return
            self.backend_host, self.backend_port, self.token, self.server_label = host, port, token, label
            if view:
                try:
                    await self._backend_once({"command": "view", "args": view, "session_id": "web-setup"})
                except Exception:
                    pass                 # widok wybierze sie przy pierwszym wejsciu
            await self._line(writer, {"done": True, "ok": True, "server": label})

    async def _line(self, writer, payload: dict) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _connect(self):
        """Polaczenie z backendem: TCP (tunel z laptopa) albo gniazdo Unix (most w procesie Pipe)."""
        if self.backend_host and self.backend_host.startswith("unix:"):
            return await asyncio.open_unix_connection(self.backend_host[len("unix:"):], limit=READ_LIMIT)
        return await asyncio.open_connection(self.backend_host, self.backend_port, limit=READ_LIMIT)

    async def _backend_once(self, request: dict) -> dict:
        """Jedno zadanie do backendu (bez przegladarki): dane z ostatniej ramki."""
        request = {**request, "interface": self.interface}
        if self.token:
            request["token"] = self.token
        reader, backend = await self._connect()
        try:
            backend.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            await backend.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), 30)
                if not line:
                    return {}
                frame = json.loads(line)
                if frame.get("done"):
                    return frame.get("data") or {}
        finally:
            backend.close()

    # --- most do backendu ---------------------------------------------------

    def _backend_request(self, payload: dict, who: dict | None = None) -> dict:
        who = who or {"token": self.token, "interface": self.interface}
        request = {key: value for key, value in payload.items() if key in ALLOWED_FIELDS}
        if not any(key in request for key in ("message", "confirm", "command")):
            raise HttpError(400, "expected message, confirm or command")
        if request.get("command") == "subscribe":
            raise HttpError(400, "use /api/events")
        session = str(payload.get("session", ""))[:64]
        request["session_id"] = "web-" + (session or "default")
        request["interface"] = who["interface"]
        if who["token"]:
            request["token"] = who["token"]
        return request

    async def _proxy_request(self, writer, body: bytes, who: dict | None = None) -> None:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(payload, dict):
            raise HttpError(400, "expected a JSON object")
        request = self._backend_request(payload, who)
        if self.setup_mode:
            raise HttpError(503, "Pipe is not set up yet")
        try:
            reader, backend = await self._connect()
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
                    frame = json.loads(line)
                    if frame.get("done"):
                        data = frame.get("data")
                        if request.get("command") == "language" and isinstance(data, dict) \
                                and (request.get("args") or data.get("chosen")):
                            self._adopt_language(data.get("lang"))
                        break
                except (json.JSONDecodeError, AttributeError):
                    continue
        finally:
            backend.close()

    async def _events(self, writer, who: dict | None = None) -> None:
        """Zdarzenia czuwania (alerty, przypomnienia, raporty) jako SSE; laczy sie ponownie po zerwaniu."""
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\n"
                      f"X-Accel-Buffering: no\r\nConnection: close\r\n{SECURITY_HEADERS}\r\n").encode())
        await writer.drain()
        if self.setup_mode:
            writer.write(b"event: down\ndata: \"setup\"\n\n")
            await writer.drain()
            return
        who = who or {"token": self.token, "interface": self.interface}
        request = {"command": "subscribe", "session_id": "web-events", "interface": who["interface"]}
        if who["token"]:
            request["token"] = who["token"]
        try:
            reader, backend = await self._connect()
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
                    if self.public is not None and not self.public.device(who["token"]):
                        writer.write(b'event: down\ndata: "revoked"\n\n')   # urzadzenie odwolane w trakcie
                        await writer.drain()
                        break
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
                if payload.get("type") == "language" or (payload.get("type") == "subscribed" and payload.get("lang_chosen")):
                    self._adopt_language(payload.get("lang"))
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


def run(backend_host: str | None, backend_port: int, token: str, *, lang: str = "pl", server_label: str = "",
        port: int = DEFAULT_PORT, open_browser: bool = True, ready=None, setup=None) -> None:
    bridge = WebBridge(backend_host, backend_port, token, lang=lang, server_label=server_label, port=free_port(port),
                       setup=setup)
    asyncio.run(bridge.serve(open_browser=open_browser, ready=ready))
