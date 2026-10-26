"""
`/web wlacz` — `pipe web` z serwera pod publicznym adresem HTTPS, jedna komenda z CLI, Telegrama albo `pipe web`.
Dwie drogi, wybierane same:

    bezposrednio: przegladarka --HTTPS--> Caddy (kontener pipe-web, siec hosta, certyfikat Let's Encrypt)
    tunel:        przegladarka --HTTPS--> Cloudflare <--polaczenie wychodzace-- cloudflared (kontener pipe-web)
                  --gniazdo /tmp/pipe-web/web.sock--> most pipe web (clients/webui/server.py) w procesie Pipe
                  --gniazdo backendu (AGENT_SOCKET)--> backend/server.py

- Bezposrednio, gdy serwer ma wlasny publiczny IPv4 (nie jest za NAT-em ani na wspoldzielonym adresie) i 80 albo 443
  jest wolny. Tunel, gdy nie (mikr.us, serwer w domu, dostawca blokujacy porty) albo gdy certyfikat nie powstal —
  wtedy Pipe przelacza sie sam i mowi o tym. Wymuszenie: `/web wlacz tunel` albo `/web wlacz bezposrednio`.
- Tunel: szybki tunel Cloudflare bez konta (`https://<losowe>.trycloudflare.com`). Nie potrzebuje zadnego otwartego
  portu, ale Cloudflare widzi ruch (konczy TLS), nie ma gwarancji dzialania, nie przepuszcza SSE (strona odbiera
  zdarzenia odpytywaniem, `/api/poll`), a adres zmienia sie po restarcie kontenera — `watch_tunnel()` zauwaza nowy
  i wysyla go w wiadomosci od Pipe.

- Adres: podana domena albo `<ip-z-myslnikami>.sslip.io` (publiczna nazwa wskazujaca na IP serwera), port 443,
  a gdy zajety (np. nginx) — 8443. Certyfikat wystawia Caddy sam: na 443 wyzwaniem TLS-ALPN, na innym porcie
  przez chwilowe zajecie portu 80 (HTTP-01) — dlatego jeden z nich musi byc wolny.
- Wejscie: parowanie urzadzen przez rozmowe (core/webaccess.py); most nie ma zadnego tokenu, kazde zadanie idzie
  z tokenem urzadzenia z ciasteczka.
- Zapora: przy wlaczonym ufw Pipe otwiera potrzebne porty (i zamyka je przy `/web wylacz`). Zapory dostawcy
  (panel chmury) Pipe nie widzi — mowi o niej, gdy strona nie odpowiada.

Tylko tryb docker i native (Caddy zawsze jako kontener, wiec potrzebny Docker); nie w trybie obserwacji ani
w Kubernetes (tam Ingress). Most zyje w procesie Pipe i wraca po restarcie (`resume()`), Caddy ma
`--restart unless-stopped`. Bez importu `settings`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from backend.core import memory, runtime, webaccess
from backend.core.i18n import tr

CONTAINER = "pipe-web"
VOLUME = "pipe-web-caddy"
CADDY_IMAGE = "caddy:2.11-alpine"
LABEL = "pipe.web"
SOCKET_DIR = "/tmp/pipe-web"
SOCKET_NAME = "web.sock"
TUNNEL_IMAGE = "cloudflare/cloudflared:2026.10.4"
DEFAULT_PORTS = (443, 8443)
READY_TIMEOUT = 120
DIRECT_TIMEOUT = 75                 # proba bezposrednio przed przejsciem na tunel (bledy ACME widac w logach szybciej)
TUNNEL_TIMEOUT = 60
TUNNEL_WATCH = 20
_TUNNEL_URL = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")
IP_SERVICES = ("https://api.ipify.org?format=json", "https://api64.ipify.org?format=json")
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CERT_OK = ("certificate obtained successfully",)
_CERT_FAIL = ("could not get certificate", "challenge failed", "obtaining certificate", "no solvers available")

OPS = {
    "": "status", "status": "status", "stan": "status", "urzadzenia": "status", "urządzenia": "status",
    "devices": "status",
    "wlacz": "on", "włącz": "on", "on": "on", "start": "on", "publiczny": "on", "public": "on",
    "wylacz": "off", "wyłącz": "off", "off": "off", "stop": "off",
    "tunel": "tunnel", "tunnel": "tunnel",
    "wpusc": "approve", "wpuść": "approve", "approve": "approve", "allow": "approve",
    "odrzuc": "deny", "odrzuć": "deny", "deny": "deny", "reject": "deny",
    "odwolaj": "revoke", "odwołaj": "revoke", "revoke": "revoke", "wyloguj": "revoke", "logout": "revoke",
}
VIEWER_WORDS = {"podglad", "podgląd", "viewer", "view", "tylko", "readonly", "read-only"}

Emit = Callable[[str], Awaitable[None]]


@dataclass
class State:
    on: bool = False
    host: str = ""
    port: int = 0
    since: str = ""
    firewall: list[int] = field(default_factory=list)    # porty otwarte przez Pipe w ufw (zamykane przy wylaczeniu)
    tls: str = ""                                        # "" = Let's Encrypt, "internal" = wlasne CA Caddy (testy)
    mode: str = "direct"                                 # direct (Caddy) | tunnel (cloudflared)

    @property
    def url(self) -> str:
        return f"https://{self.host}" + ("" if self.port == 443 else f":{self.port}")


def state_path() -> Path:
    return memory.data_dir() / "web_public.json"


def load_state() -> State:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        known = set(State.__dataclass_fields__)
        return State(**{k: v for k, v in raw.items() if k in known})
    except (OSError, ValueError, TypeError):
        return State()


def save_state(state: State) -> None:
    memory._write_atomic(state_path(), json.dumps(asdict(state), indent=1) + "\n")


def parse(args: str) -> tuple[str, list[str]]:
    """`/web <slowo> [reszta]` -> (operacja, reszta); nieznane slowo -> ("help", [...])."""
    words = (args or "").strip().split()
    if not words:
        return "status", []
    return OPS.get(words[0].lower(), "help"), words[1:]


DIRECT_WORDS = {"bezposrednio", "bezpośrednio", "direct", "caddy"}
TUNNEL_WORDS = {"tunel", "tunnel", "cloudflare"}


def parse_on(words: list[str]) -> tuple[int, str, str]:
    """`/web wlacz [tunel|bezposrednio] [port] [domena]` w dowolnej kolejnosci -> (port, domena, tryb). ValueError."""
    port, domain, mode = 0, "", ""
    for word in words:
        if word.lower() in TUNNEL_WORDS | DIRECT_WORDS:
            mode = "tunnel" if word.lower() in TUNNEL_WORDS else "direct"
        elif word.isdigit():
            port = int(word)
            if not 1 <= port <= 65535:
                raise ValueError(tr(f"Port {word} jest poza zakresem 1-65535.", f"Port {word} is outside 1-65535."))
        else:
            candidate = word.lower().rstrip(".")
            if candidate.startswith(("http://", "https://")):
                candidate = candidate.split("://", 1)[1].split("/", 1)[0]
            if not _DOMAIN.match(candidate):
                raise ValueError(tr(f"{word!r} nie wyglada na domene ani port.", f"{word!r} does not look like a domain or a port."))
            domain = candidate
    if mode == "tunnel" and (port or domain):
        raise ValueError(tr("Tunel sam dostaje adres — bez portu i domeny.", "The tunnel gets its own address — no port or domain."))
    return port, domain, mode


def sslip_host(ip: str) -> str:
    return ip.replace(".", "-") + ".sslip.io"


def caddyfile(host: str, port: int, *, internal: bool = False) -> str:
    """Konfiguracja Caddy: tylko ta jedna strona, bez API administracyjnego i bez przekierowan z portu 80."""
    tls = "\n\ttls internal" if internal else ""
    return (
        "{\n\tadmin off\n\tauto_https disable_redirects\n}\n"
        f"{host}:{port} {{{tls}\n"
        f"\treverse_proxy unix//run/pipe-web/{SOCKET_NAME} {{\n"
        "\t\tflush_interval -1\n"
        "\t\theader_up X-Pipe-Client-Ip {remote_host}\n"
        "\t}\n"
        "\theader {\n\t\tStrict-Transport-Security \"max-age=31536000\"\n\t\t-Server\n\t}\n"
        "}\n"
    )


def tunnel_command() -> list[str]:
    """`docker run` cloudflared: szybki tunel do gniazda mostu. Tylko polaczenia wychodzace (siec bridge), bez portow;
    root w kontenerze tylko po to, by otworzyc gniazdo 0600 nalezace do roota — bez zadnych uprawnien."""
    return ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped", "--label", f"{LABEL}=tunnel",
            "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "256m",
            "--pids-limit", "200", "-v", f"{SOCKET_DIR}:/run/pipe-web", TUNNEL_IMAGE,
            "tunnel", "--no-autoupdate", "--unix-socket", f"/run/pipe-web/{SOCKET_NAME}"]


def tunnel_host(logs: str) -> str:
    """Ostatni adres szybkiego tunelu z logow cloudflared ('' — jeszcze go nie ma)."""
    found = _TUNNEL_URL.findall(logs or "")
    return found[-1] if found else ""


def caddy_command(host: str, port: int, *, internal: bool = False) -> list[str]:
    """`docker run` kontenera z Caddy: siec hosta (port publiczny i wyzwania ACME), tylko gniazdo mostu i wolumen
    certyfikatow, bez uprawnien poza otwarciem portu < 1024."""
    script = 'printf "%s" "$PIPE_CADDYFILE" > /tmp/Caddyfile && exec caddy run --config /tmp/Caddyfile --adapter caddyfile'
    return ["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped", "--network", "host",
            "--label", f"{LABEL}=1", "--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE",
            "--security-opt", "no-new-privileges", "--memory", "256m", "--pids-limit", "200",
            "-v", f"{VOLUME}:/data", "-v", f"{SOCKET_DIR}:/run/pipe-web",
            "-e", "PIPE_CADDYFILE=" + caddyfile(host, port, internal=internal),
            "--entrypoint", "sh", CADDY_IMAGE, "-c", script]


# ─── Host: porty, zapora, adres ─────────────────────────────────────────────

def busy_ports() -> set[int]:
    from backend.core import hostinfo
    return {s.port for s in hostinfo.sockets() if s.proto.startswith("tcp")}


def local_ipv4(proc: str | None = None) -> set[str]:
    """Adresy IPv4 interfejsow hosta (z /proc/1/net/fib_trie — siec hosta, takze z kontenera z pid: host)."""
    from backend.core import hostinfo
    text = hostinfo._read(f"{hostinfo._proc(proc)}/1/net/fib_trie")
    found, last = set(), ""
    for line in text.splitlines():
        match = re.search(r"[|+]-- (\d+\.\d+\.\d+\.\d+)$", line.rstrip())
        if match:
            last = match.group(1)
        elif "/32 host LOCAL" in line and last:
            found.add(last)
    return found


def choose_port(wanted: int, busy: set[int]) -> int:
    """Port strony: podany albo pierwszy wolny z DEFAULT_PORTS. Rzuca ValueError z wyjasnieniem."""
    if wanted:
        if wanted in busy:
            raise ValueError(tr(f"Port {wanted} jest zajety na serwerze — wybierz inny, np. /web wlacz 8443.",
                                f"Port {wanted} is in use on the server — pick another one, e.g. /web on 8443."))
        port = wanted
    else:
        port = next((p for p in DEFAULT_PORTS if p not in busy), 0)
        if not port:
            raise ValueError(tr("Porty 443 i 8443 sa zajete — podaj inny: /web wlacz <port>.",
                                "Ports 443 and 8443 are in use — give another one: /web on <port>."))
    if port != 443 and {80, 443} <= busy:
        raise ValueError(tr(
            "Porty 80 i 443 sa zajete (pewnie przez nginx albo inny serwer WWW), a certyfikat HTTPS potrzebuje "
            "jednego z nich na chwile wystawiania. Najprosciej: dodaj w swoim serwerze WWW subdomene kierujaca "
            "na Pipe albo zwolnij port 80.",
            "Ports 80 and 443 are both in use (probably nginx or another web server), and the HTTPS certificate needs "
            "one of them while it is issued. Simplest fix: add a subdomain for Pipe in your web server, or free port 80."))
    return port


def ufw_rules(root: str = "") -> tuple[bool, set[int]]:
    """(ufw wlaczony, porty TCP z regula allow) z plikow ufw hosta — bez uruchamiania ufw."""
    base = root if root else runtime.to_local("/")
    try:
        conf = Path(base, "etc/ufw/ufw.conf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, set()
    enabled = re.search(r"^\s*ENABLED\s*=\s*yes\s*$", conf, re.M | re.I) is not None
    allowed: set[int] = set()
    for name in ("user.rules", "user6.rules"):
        try:
            text = Path(base, "etc/ufw", name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for proto, ports in re.findall(r"^### tuple ### allow (tcp|any) (\S+) ", text, re.M):
            for part in ports.split(","):
                low, _, high = part.partition(":")
                if low.isdigit() and (high or low).isdigit():
                    allowed.update(range(int(low), int(high or low) + 1))
    return enabled, allowed


async def public_ip() -> str:
    """Publiczny IPv4 serwera (z serwisu ipify). '' = nie udalo sie."""
    from backend.core import websearch

    for url in IP_SERVICES:
        try:
            data = await asyncio.to_thread(websearch.get_json, url, None, 8)
            address = ipaddress.ip_address(str((data or {}).get("ip", "")))
        except Exception:
            continue
        if address.version == 4 and address.is_global:
            return str(address)
    return ""


async def resolves_to(domain: str) -> set[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, domain, None, socket.AF_INET)
    except OSError:
        return set()
    return {info[4][0] for info in infos}


# ─── Docker i kontener Caddy ────────────────────────────────────────────────

async def _run(*args: str, timeout: float = 120) -> tuple[str, int]:
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return str(exc), 127
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return "timeout", 124
    return output.decode("utf-8", errors="replace").strip(), process.returncode or 0


async def _host_shell(command: str) -> tuple[str, int]:
    """Komenda na hoscie: w trybie docker przez kontener pomocniczy (core/hostexec.py), w native wprost."""
    if runtime.kind() == "docker":
        from backend.core import hostexec
        image = await hostexec.image()
        if not image:
            return "no helper image", 1
        return await _run("sh", "-c", hostexec.wrap(command, image))
    return await _run("sh", "-c", command)


async def blocker() -> str:
    """Powod, dla ktorego Pipe nie wystawi strony ('' = moze)."""
    if runtime.observe():
        return tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe niczego na serwerze nie uruchamia.",
                  "Observe mode (PIPE_OBSERVE=1): Pipe starts nothing on the server.")
    if runtime.kind() == "kubernetes":
        return tr("W Kubernetes wystaw Pipe przez Ingress z TLS — /web wlacz dziala w trybie docker i native.",
                  "In Kubernetes expose Pipe through an Ingress with TLS — /web on works in docker and native mode.")
    output, code = await _run("docker", "version", "--format", "{{.Server.Version}}", timeout=20)
    if code != 0:
        return tr("Do HTTPS potrzebny jest Docker (Caddy dziala jako kontener), a Pipe go nie widzi: "
                  f"{output[:200]}", "HTTPS needs Docker (Caddy runs as a container) and Pipe cannot reach it: "
                  f"{output[:200]}")
    return ""


def _log_verdict(logs: str) -> tuple[str, str]:
    """('ok' | 'fail' | '', szczegol) z logow Caddy (JSON w liniach)."""
    verdict, detail = "", ""
    for line in logs.splitlines():
        low = line.lower()
        try:
            entry = json.loads(line)
        except ValueError:
            entry = {}
        message = str(entry.get("msg") or "")
        if any(mark in low for mark in _CERT_OK) and str(entry.get("level", "info")) != "error":
            verdict, detail = "ok", message
        elif entry.get("level") == "error" and any(mark in low for mark in _CERT_FAIL):
            verdict, detail = "fail", str(entry.get("error") or message)[:400]
    return verdict, detail


def _tls_check(connect: str, port: int, host: str, verify: bool) -> str:
    """'' gdy strona odpowiada przez HTTPS z poprawnym certyfikatem dla `host`, inaczej opis bledu."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    try:
        with socket.create_connection((connect, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return ""
    except (OSError, ssl.SSLError) as exc:
        return str(exc)[:200]


async def wait_ready(state: State, emit: Emit, since: float, timeout: float = READY_TIMEOUT) -> tuple[bool, str]:
    """Czeka, az Caddy dostanie certyfikat i strona odpowie. (gotowe, szczegol)."""
    connect = "host.docker.internal" if runtime.kind() == "docker" else "127.0.0.1"
    deadline = time.monotonic() + timeout
    told = False
    detail = ""
    while time.monotonic() < deadline:
        logs, _ = await _run("docker", "logs", "--since", str(int(since)), CONTAINER, timeout=20)
        verdict, detail = _log_verdict(logs)
        if verdict == "fail":
            return False, detail
        if verdict == "ok" or not await asyncio.to_thread(_tls_check, connect, state.port, state.host, not state.tls):
            return True, ""
        if not told:
            await emit(tr("Czekam na certyfikat HTTPS (zwykle kilkanascie sekund)…",
                          "Waiting for the HTTPS certificate (usually a few seconds)…"))
            told = True
        await asyncio.sleep(3)
    return False, detail or tr("brak certyfikatu po 2 minutach", "no certificate after 2 minutes")


# ─── Most (clients/webui/server.py) w procesie Pipe ─────────────────────────

class _Access:
    """To, czego most potrzebuje w trybie publicznym — bez importu backendu po stronie klienta."""

    def __init__(self, state: State, publish: Callable[[dict], None] | None) -> None:
        self.state = state
        self.publish = publish

    @property
    def hosts(self) -> set[str]:
        hosts = {f"{self.state.host}:{self.state.port}"}
        if self.state.port == 443:
            hosts.add(self.state.host)
        return hosts

    @property
    def ip_header(self) -> str:
        # adres przegladarki: Caddy ustawia X-Pipe-Client-Ip, Cloudflare — Cf-Connecting-Ip (obu klient nie podrobi)
        return "cf-connecting-ip" if self.state.mode == "tunnel" else "x-pipe-client-ip"

    @property
    def events(self) -> str:
        # szybki tunel Cloudflare nie przepuszcza SSE — strona odbiera zdarzenia odpytywaniem
        return "poll" if self.state.mode == "tunnel" else "sse"

    def device(self, token: str) -> tuple[str, str] | None:
        return webaccess.device(token)

    def pair_start(self, user_agent: str, ip: str) -> dict[str, Any]:
        pairing, secret = webaccess.start(user_agent, ip)
        if self.publish is not None:
            self.publish({**pairing.to_event(), **login_texts(pairing)})
        return {"id": pairing.id, "code": pairing.code, "secret": secret, "device": pairing.device,
                "expires_in": webaccess.PAIR_TTL}

    def pair_poll(self, pairing_id: str, secret: str) -> dict[str, Any]:
        return webaccess.collect(pairing_id, secret)

    def logout(self, token: str) -> None:
        found = webaccess.device(token)
        if found:
            from backend import tokens
            tokens.revoke(found[0])


def login_texts(pairing: webaccess.Pairing) -> dict[str, str]:
    """Tytul i tresc wiadomosci "ktos chce wejsc" (skrzynka, Telegram, pipe web)."""
    return {
        "title": tr("Ktos chce wejsc do Pipe przez przegladarke", "Someone wants to open Pipe in a browser"),
        "text": tr(f"Kod na ekranie: **{pairing.code}**\nUrzadzenie: {pairing.device}\nAdres IP: {pairing.ip}\n\n"
                   "Wpusc tylko wtedy, gdy to Ty i ten sam kod widzisz na swoim ekranie.",
                   f"Code on the screen: **{pairing.code}**\nDevice: {pairing.device}\nIP address: {pairing.ip}\n\n"
                   "Let it in only if it is you and you see the same code on your screen."),
    }


_bridge: Any = None
_server: asyncio.AbstractServer | None = None
_watcher: asyncio.Task | None = None
_state: State | None = None               # stan, ktory widzi most (adres tunelu zmienia sie w miejscu)


def socket_path() -> str:
    return os.path.join(SOCKET_DIR, SOCKET_NAME)


async def start_bridge(state: State, backend_socket: str, publish: Callable[[dict], None] | None = None) -> None:
    """Uruchamia most pipe web na gniezdzie dla Caddy albo cloudflared (ponownie — z nowym adresem)."""
    global _bridge, _server, _state
    from backend.core.i18n import lang
    from clients.webui.server import READ_LIMIT, WebBridge

    await stop_bridge()
    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    os.chmod(SOCKET_DIR, 0o700)
    path = socket_path()
    if os.path.exists(path):
        os.unlink(path)
    _state = state
    _bridge = WebBridge("unix:" + backend_socket, 0, "", lang=lang(), server_label=state.host,
                        public=_Access(state, publish))
    _server = await asyncio.start_unix_server(_bridge.handle, path=path, limit=READ_LIMIT)
    os.chmod(path, 0o600)


async def stop_bridge() -> None:
    global _bridge, _server, _watcher
    if _watcher is not None:
        _watcher.cancel()
        _watcher = None
    if _server is not None:
        _server.close()
        _server = None
    _bridge = None
    try:
        os.unlink(socket_path())
    except OSError:
        pass


def bridge_running() -> bool:
    return _server is not None


async def refresh_tunnel(publish: Callable[[dict], None] | None = None) -> bool:
    """Adres tunelu z logow cloudflared; nowy (restart kontenera) — zapis, most i wiadomosc. Zwraca, czy sie zmienil."""
    if _state is None or _state.mode != "tunnel":
        return False
    logs, code = await _run("docker", "logs", "--tail", "300", CONTAINER, timeout=20)
    host = tunnel_host(logs) if code == 0 else ""
    if not host or host == _state.host:
        return False
    _state.host = host
    save_state(_state)
    if _bridge is not None:
        _bridge.server_label = host
    if publish is not None:
        publish({"type": "notice", "title": tr("Pipe w przegladarce ma nowy adres", "Pipe in the browser has a new address"),
                 "text": tr(f"Tunel uruchomil sie ponownie i dostal nowy adres: {_state.url}\nZapamietane urzadzenia "
                            "wchodza pod nowy adres po zgodzie jeszcze raz.",
                            f"The tunnel restarted and got a new address: {_state.url}\nRemembered devices need to be "
                            "approved once more at the new address.")})
    return True


async def watch_tunnel(publish: Callable[[dict], None] | None = None, every: float = TUNNEL_WATCH) -> None:
    """Pilnuje adresu szybkiego tunelu (zmienia sie po restarcie serwera albo kontenera)."""
    while True:
        try:
            await refresh_tunnel(publish)
        except Exception as exc:          # nie zatrzymuje strony
            print(f"[VPS Agent] pipe web tunel: {exc}", flush=True)
        await asyncio.sleep(every)


def _watch(publish: Callable[[dict], None] | None) -> None:
    global _watcher
    if _state is not None and _state.mode == "tunnel":
        _watcher = asyncio.create_task(watch_tunnel(publish))


async def resume(backend_socket: str, publish: Callable[[dict], None] | None = None) -> None:
    """Start serwera: wlaczona strona wraca (Caddy i cloudflared wstaja same dzieki --restart)."""
    state = load_state()
    if not state.on:
        return
    try:
        await start_bridge(state, backend_socket, publish)
        await refresh_tunnel(publish)
        _watch(publish)
        print(f"[VPS Agent] pipe web    : {state.url}", flush=True)
    except Exception as exc:
        print(f"[VPS Agent] pipe web: {exc}", flush=True)


# ─── Wlacz / wylacz ─────────────────────────────────────────────────────────

async def enable(*, port: int, domain: str, backend_socket: str, emit: Emit, mode: str = "",
                 publish: Callable[[dict], None] | None = None, internal_tls: bool = False) -> tuple[bool, str]:
    """Wystawia strone bezposrednio albo przez tunel (`mode` puste — wybor sam). (udalo sie, tekst dla uzytkownika)."""
    reason = await blocker()
    if reason:
        return False, reason
    previous = load_state()
    await stop_bridge()
    await _run("docker", "rm", "-f", CONTAINER, timeout=60)        # nasz poprzedni Caddy/tunel nie blokuje portu
    if previous.on:
        await asyncio.sleep(1)
    if mode == "tunnel":
        return await _enable_tunnel(previous, backend_socket, emit, publish, [])

    await emit(tr("Sprawdzam adres serwera i wolne porty…", "Checking the server address and free ports…"))
    busy = busy_ports() - ({previous.port} if previous.on and previous.mode == "direct" else set())
    ip = await public_ip()
    why = ""
    if not domain and mode != "direct":
        if not ip:
            why = tr("nie ustalilem publicznego adresu IP serwera", "I could not find the server's public IP")
        elif ip not in local_ipv4():
            why = tr(f"serwer jest za NAT-em albo na wspoldzielonym adresie ({ip} nie jest jego wlasnym adresem), "
                     "wiec porty 80/443 z internetu do niego nie dochodza",
                     f"the server is behind NAT or on a shared address ({ip} is not its own address), so ports "
                     "80/443 from the internet do not reach it")
        elif {80, 443} <= busy and not port:
            why = tr("porty 80 i 443 sa zajete, a certyfikat potrzebuje jednego z nich",
                     "ports 80 and 443 are taken and the certificate needs one of them")
    if why:
        return await _enable_tunnel(previous, backend_socket, emit, publish,
                                    [tr(f"Wybralem tunel, bo {why}.", f"I chose the tunnel because {why}.")])
    try:
        port = choose_port(port, busy)
    except ValueError as exc:
        return False, str(exc)
    if not domain and not ip:
        return False, tr("Nie ustalilem publicznego adresu IP serwera. Podaj domene, ktora na niego wskazuje: "
                         "/web wlacz pipe.twojadomena.pl, albo uzyj tunelu: /web wlacz tunel",
                         "I could not find the server's public IP. Give a domain that points to it: "
                         "/web on pipe.yourdomain.com, or use the tunnel: /web on tunnel")
    notes: list[str] = []
    if domain:
        host = domain
        if ip and ip not in await resolves_to(domain):
            notes.append(tr(f"Uwaga: {domain} nie wskazuje (jeszcze) na {ip} — certyfikat powstanie dopiero, gdy "
                            "rekord DNS bedzie gotowy.",
                            f"Note: {domain} does not point to {ip} (yet) — the certificate will be issued once "
                            "the DNS record is in place."))
    else:
        host = sslip_host(ip)
    state = State(on=True, host=host, port=port, since=time.strftime("%Y-%m-%d %H:%M"),
                  firewall=[p for p in previous.firewall], tls="internal" if internal_tls else "", mode="direct")

    enabled, allowed = ufw_rules()
    if enabled:
        needed = [p for p in ([port] + ([] if port == 443 else [80])) if p not in allowed]
        for wanted in needed:
            output, code = await _host_shell(f"ufw allow {wanted}/tcp comment 'pipe web'")
            if code == 0:
                state.firewall = sorted(set(state.firewall) | {wanted})
                await emit(tr(f"Otworzylem port {wanted} w zaporze ufw.", f"Opened port {wanted} in the ufw firewall."))
            else:
                notes.append(tr(f"Nie otworzylem portu {wanted} w ufw: {output[:200]}",
                                f"Could not open port {wanted} in ufw: {output[:200]}"))

    await emit(tr("Pobieram i uruchamiam Caddy (serwer HTTPS)…", "Pulling and starting Caddy (the HTTPS server)…"))
    await start_bridge(state, backend_socket, publish)
    await _run("docker", "pull", "-q", CADDY_IMAGE, timeout=300)
    started = time.time()
    output, code = await _run(*caddy_command(host, port, internal=internal_tls), timeout=120)
    if code != 0:
        await stop_bridge()
        return False, tr(f"Nie uruchomilem Caddy: {output[:400]}", f"Could not start Caddy: {output[:400]}")
    save_state(state)

    ready, detail = await wait_ready(state, emit, started, timeout=READY_TIMEOUT if mode == "direct" else DIRECT_TIMEOUT)
    if ready:
        return True, "\n\n".join([tr(f"Pipe jest pod adresem {state.url}", f"Pipe is at {state.url}"), how_to_enter()]
                                  + notes)
    if mode == "direct":
        return False, "\n\n".join([
            tr(f"Strona {state.url} jeszcze nie dziala: {detail}", f"The page {state.url} is not working yet: {detail}"),
            tr(f"Zapora u dostawcy (panel chmury) blokuje pewnie port {port}" + ("" if port == 443 else " albo 80")
               + ". Otworz go i sprawdz /web za chwile albo uzyj tunelu: /web wlacz tunel.",
               f"The provider's firewall (cloud panel) probably blocks port {port}" + ("" if port == 443 else " or 80")
               + ". Open it and check /web in a moment, or use the tunnel: /web on tunnel."), how_to_enter()] + notes)
    # bezposrednio sie nie da (dostawca blokuje porty) — tunel, bez zostawiania zepsutej strony
    await emit(tr("Certyfikat nie powstal — dostawca serwera blokuje pewnie porty 80/443. Przechodze na tunel…",
                  "No certificate — the hosting provider probably blocks ports 80/443. Switching to the tunnel…"))
    await stop_bridge()
    await _run("docker", "rm", "-f", CONTAINER, timeout=60)
    for opened in state.firewall:
        if opened not in previous.firewall:
            await _host_shell(f"ufw delete allow {opened}/tcp")
    return await _enable_tunnel(State(firewall=previous.firewall), backend_socket, emit, publish, [tr(
        f"Wybralem tunel, bo certyfikat dla {state.url} nie powstal ({detail[:160]}) — z internetu nie da sie "
        "dojsc do portow 80/443 tego serwera.",
        f"I chose the tunnel because the certificate for {state.url} was not issued ({detail[:160]}) — ports 80/443 "
        "of this server cannot be reached from the internet.")])


async def _enable_tunnel(previous: State, backend_socket: str, emit: Emit, publish: Callable[[dict], None] | None,
                         notes: list[str]) -> tuple[bool, str]:
    """Szybki tunel Cloudflare: kontener cloudflared do gniazda mostu, adres z jego logow."""
    await emit(tr("Uruchamiam tunel (Cloudflare) — nie potrzebuje zadnego otwartego portu…",
                  "Starting the tunnel (Cloudflare) — it needs no open port…"))
    state = State(on=True, host="", port=443, since=time.strftime("%Y-%m-%d %H:%M"),
                  firewall=list(previous.firewall), mode="tunnel")
    await start_bridge(state, backend_socket, publish)
    await _run("docker", "pull", "-q", TUNNEL_IMAGE, timeout=300)
    output, code = await _run(*tunnel_command(), timeout=120)
    if code != 0:
        await stop_bridge()
        return False, tr(f"Nie uruchomilem tunelu: {output[:400]}", f"Could not start the tunnel: {output[:400]}")
    deadline = time.monotonic() + TUNNEL_TIMEOUT
    while time.monotonic() < deadline and not state.host:
        logs, _ = await _run("docker", "logs", CONTAINER, timeout=20)
        state.host = tunnel_host(logs)
        if not state.host:
            await asyncio.sleep(2)
    if not state.host:
        await stop_bridge()
        await _run("docker", "rm", "-f", CONTAINER, timeout=60)
        return False, tr("Tunel nie dostal adresu od Cloudflare w ciagu minuty (serwer bez dostepu do internetu albo "
                         "Cloudflare chwilowo odmawia). Sprobuj /web wlacz tunel za chwile.",
                         "The tunnel got no address from Cloudflare within a minute (no internet on the server, or "
                         "Cloudflare refuses for now). Try /web on tunnel in a moment.")
    if _bridge is not None:
        _bridge.server_label = state.host
    save_state(state)
    _watch(publish)
    lines = [tr(f"Pipe jest pod adresem {state.url}", f"Pipe is at {state.url}")] + notes + [
        tr("To szybki tunel Cloudflare: dziala bez otwierania portow, ale ruch przechodzi przez Cloudflare (to on "
           "szyfruje polaczenie z przegladarka), a adres zmienia sie po restarcie serwera — wtedy Pipe sam przysle "
           "nowy. Powiadomienia na stronie przychodza co kilka sekund zamiast od razu.",
           "This is a Cloudflare quick tunnel: it works without opening ports, but traffic passes through Cloudflare "
           "(it encrypts the connection to the browser), and the address changes after a server restart — Pipe then "
           "sends the new one. Notifications on the page arrive every few seconds instead of instantly."),
        how_to_enter()]
    return True, "\n\n".join(lines)


def how_to_enter() -> str:
    return tr("Jak wejsc: otworz adres na telefonie albo komputerze i nacisnij \"Popros o dostep\". Strona pokaze kod, "
              "a ja zapytam tutaj, czy wpuscic to urzadzenie — kazde urzadzenie raz, potem pamieta logowanie przez "
              f"{webaccess.DEVICE_IDLE_DAYS} dni od ostatniego uzycia.",
              "How to get in: open the address on your phone or computer and press \"Ask for access\". The page shows "
              "a code and I ask here whether to let that device in — once per device, then it stays signed in for "
              f"{webaccess.DEVICE_IDLE_DAYS} days after its last use.")


async def disable(emit: Emit) -> str:
    state = load_state()
    await stop_bridge()          # takze pilnowanie adresu tunelu
    output, code = await _run("docker", "rm", "-f", CONTAINER, timeout=60)
    lines = [tr("Strona nie jest juz dostepna z internetu.", "The page is no longer reachable from the internet.")]
    if code != 0 and "No such container" not in output:
        lines.append(tr(f"Nie usunalem kontenera {CONTAINER}: {output[:200]}", f"Could not remove the {CONTAINER} container: {output[:200]}"))
    for port in state.firewall:
        out, rc = await _host_shell(f"ufw delete allow {port}/tcp")
        if rc == 0:
            await emit(tr(f"Zamknalem port {port} w ufw.", f"Closed port {port} in ufw."))
        else:
            lines.append(tr(f"Nie zamknalem portu {port} w ufw: {out[:200]}", f"Could not close port {port} in ufw: {out[:200]}"))
    known = webaccess.devices()
    if known:
        lines.append(tr(f"Zapamietane urzadzenia ({len(known)}) zostaja na wypadek ponownego wlaczenia — usuniesz je "
                        "przez /web odwolaj wszystkie.",
                        f"Remembered devices ({len(known)}) stay in case you turn it on again — remove them with "
                        "/web revoke all."))
    save_state(State())
    return "\n".join(lines)


# ─── Stan ───────────────────────────────────────────────────────────────────

def status(can_edit: bool) -> dict[str, Any]:
    state = load_state()
    devices = webaccess.devices() if can_edit else []
    pending = [{"code": p.code, "device": p.device, "ip": p.ip} for p in webaccess.pending()] if can_edit else []
    lines = []
    if state.on:
        lines.append(tr(f"Pipe w przegladarce: {state.url} (od {state.since})", f"Pipe in the browser: {state.url} (since {state.since})")
                     + (tr(" — przez tunel Cloudflare", " — through a Cloudflare tunnel") if state.mode == "tunnel" else ""))
        if not bridge_running():
            lines.append(tr("Most strony nie dziala — /web wlacz uruchomi go ponownie.",
                            "The page bridge is not running — /web on starts it again."))
    else:
        lines.append(tr("Pipe nie jest wystawiony w internecie. /web wlacz — jedna komenda, adres HTTPS i logowanie "
                        "przez zgode w rozmowie.",
                        "Pipe is not exposed to the internet. /web on — one command, an HTTPS address and sign-in "
                        "approved in the chat."))
    if devices:
        lines.append(tr("Urzadzenia:", "Devices:"))
        for number, entry in enumerate(devices, 1):
            role = tr("pelny dostep", "full access") if entry["role"] == "admin" else tr("podglad", "view only")
            seen = entry["seen"] or entry["created"]
            gone = tr(" (wygaslo)", " (expired)") if entry["expired"] else ""
            lines.append(f"  {number}. {entry['label']} — {role}, " + tr(f"ostatnio {seen}", f"last used {seen}") + gone)
        lines.append(tr("Odwolanie: /web odwolaj <numer> albo /web odwolaj wszystkie.",
                        "Revoke: /web revoke <number> or /web revoke all."))
    for entry in pending:
        lines.append(tr(f"Czeka na zgode: {entry['device']}, IP {entry['ip']}, kod {entry['code']} — /web wpusc {entry['code']}",
                        f"Waiting for approval: {entry['device']}, IP {entry['ip']}, code {entry['code']} — /web approve {entry['code']}"))
    return {"on": state.on, "url": state.url if state.on else "", "mode": state.mode if state.on else "",
            "devices": devices, "pending": pending,
            "text": "\n".join(lines), "can_edit": can_edit}


def help_text() -> str:
    return tr("/web — stan i urzadzenia; /web wlacz — wystaw Pipe pod adresem HTTPS (sam wybierze: bezposrednio albo "
              "tunel); /web wlacz tunel | bezposrednio [port] [domena]; /web wylacz; /web wpusc <kod> [podglad]; "
              "/web odrzuc <kod>; /web odwolaj <numer|wszystkie>.",
              "/web — state and devices; /web on — expose Pipe at an HTTPS address (picks direct or tunnel itself); "
              "/web on tunnel | direct [port] [domain]; /web off; /web approve <code> [viewer]; /web deny <code>; "
              "/web revoke <number|all>.")


def close_feed(pairing_id: str, label: str) -> None:
    """Zdecydowana prosba: przyciski przy jej wiadomosci w skrzynce znikaja."""
    from backend.core import feed

    for item in feed.recent(100):
        if item.type == "login" and item.ref == pairing_id and not item.done:
            feed.mark_done(item.id, label)

