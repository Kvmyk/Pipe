"""
CLI — interaktywny REPL do zarządzania zdalnym serwerem VPS przez agenta AI.

Działa NA LAPTOPIE użytkownika.
Łączy się z backendem uruchomionym NA SERWERZE przez SSH tunnel.

CLI automatycznie zestawia tunel SSH który forwarduje Unix socket z serwera
do lokalnego portu TCP. Dzięki temu użytkownik nie musi ręcznie konfigurować
żadnych tuneli.

Schemat połączenia:
    Laptop → SSH tunnel → Serwer
      CLI --------------------→ backend/server.py
      :7379 (local TCP)       /tmp/vps-agent.sock (remote Unix)

Użycie:
    python cli.py --host user@serwer.example.com
    python cli.py --host root@1.2.3.4 --port 22
    python cli.py --host root@1.2.3.4 --key ~/.ssh/id_rsa
    python cli.py --host root@1.2.3.4 --no-tunnel  # jeśli tunnel jest już aktywny
    python cli.py --kube pipe                        # Pipe w Kubernetesie (kubectl port-forward)

Diagramy (np. /mapa) CLI zapisuje w ~/.pipe/diagrams/ i pokazuje ich podgląd
ASCII w terminalu; --open otwiera PNG w przeglądarce obrazów.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


def _initial_lang() -> str:
    """Jezyk CLI: --lang pl|en (ma pierwszenstwo) albo zmienna PIPE_LANG; domyslnie polski."""
    value = os.getenv("PIPE_LANG", "pl")
    for index, arg in enumerate(sys.argv):
        if arg == "--lang" and index + 1 < len(sys.argv):
            value = sys.argv[index + 1]
        elif arg.startswith("--lang="):
            value = arg.split("=", 1)[1]
    return "en" if value.strip().lower().startswith("en") else "pl"


LANG: str = _initial_lang()
# --lang podany wprost wygrywa z jezykiem wybranym na serwerze (/jezyk)
LANG_FORCED: bool = any(arg == "--lang" or arg.startswith("--lang=") for arg in sys.argv)


def tr(pl, en):
    """Tekst w jezyku CLI."""
    return en if LANG == "en" else pl


try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.rule import Rule
    from rich.text import Text
    from rich.spinner import Spinner
    from rich.live import Live
    from rich.markup import escape
except ImportError:
    print(tr("Błąd: zainstaluj zależności: pip install -r requirements.txt",
             "Error: install the dependencies: pip install -r requirements.txt"))
    sys.exit(1)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
except ImportError:      # starsza instalacja bez prompt_toolkit — zwykly prompt, bez podpowiedzi komend
    PromptSession = None

console = Console()
# `pipe --mcp`: stdout nalezy do protokolu MCP — wszystko inne idzie na stderr.
BRIDGE_MODE = False

# ─── Stałe ───────────────────────────────────────────────────────────────────
DEFAULT_SSH_PORT: int = 22
DEFAULT_LOCAL_PORT: int = 7379           # lokalny port TCP dla tunelu SSH
REMOTE_SOCKET: str = "/tmp/vps-agent.sock"  # socket na serwerze
READ_LIMIT: int = 32 * 1024 * 1024       # ramki z diagramami (base64) sa duze
DIAGRAMS_DIR: Path = Path(os.getenv("PIPE_DIAGRAMS_DIR", str(Path.home() / ".pipe" / "diagrams")))
OPEN_IMAGES: bool = False                # ustawiane flaga --open
RECONNECT_TIMEOUT: float = 180.0         # tyle CLI czeka na powrot backendu (restart po /aktualizuj)
RECONNECT_DELAY: float = 2.0
WEB_PORT: int = 7400                     # `pipe web`: port lokalnej strony (--web-port)
WEB_OPEN_BROWSER: bool = True            # `pipe web --no-browser` wylacza otwieranie przegladarki


# ─── SSH Tunel ────────────────────────────────────────────────────────────────

class SSHTunnel:
    """
    Zarządza tunelem SSH który forwarduje TCP port z serwera na lokalny port.

    Komenda SSH którą uruchamia:
        ssh -N -L 127.0.0.1:7379:127.0.0.1:7379 user@host -p PORT

    To znaczy: lokalny port 7379 → przez SSH → 127.0.0.1:7379 na serwerze
    (backend nasłuchuje na 127.0.0.1:7379 — dostępny tylko lokalnie na serwerze)
    """

    def __init__(
        self,
        host: str,
        ssh_port: int = DEFAULT_SSH_PORT,
        local_port: int = DEFAULT_LOCAL_PORT,
        remote_port: int = DEFAULT_LOCAL_PORT,  # port TCP na serwerze
        identity_file: str | None = None,
    ) -> None:
        self.host = host
        self.ssh_port = ssh_port
        self.local_port = local_port
        self.remote_port = remote_port
        self.identity_file = identity_file
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        """Uruchamia tunel SSH w tle."""
        if not shutil.which("ssh"):
            raise RuntimeError(tr(
                "Brak komendy 'ssh'. "
                "Zainstaluj OpenSSH client lub użyj --no-tunnel z ręcznym tunelem.",
                "The 'ssh' command is missing. "
                "Install the OpenSSH client or use --no-tunnel with a manual tunnel."
            ))

        cmd = [
            "ssh",
            "-N",                           # nie uruchamiaj powłoki zdalnej
            "-o", "StrictHostKeyChecking=accept-new",  # auto-accept nowych hostów
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            "-p", str(self.ssh_port),
        ]

        if self.identity_file:
            cmd.extend(["-i", self.identity_file])
        if BRIDGE_MODE:
            # Most MCP: stdin niesie JSON-RPC klienta — ssh nie moze go czytac ani pytac o haslo
            cmd[1:1] = ["-n", "-o", "BatchMode=yes"]

        cmd.append(self.host)

        # NIE przekierowujemy stdin — SSH może zapytać o hasło w terminalu
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL if BRIDGE_MODE else None,   # dziedzicz stdin (haslo), chyba ze most MCP
            stdout=subprocess.DEVNULL,
            stderr=None,         # dziedzicz stderr (pokazuje prompt hasła)
        )

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """
        Czeka aż tunel będzie gotowy (port TCP dostępny).

        Returns:
            True jeśli tunel jest gotowy, False po timeout.
        """
        import socket as _socket

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Sprawdź czy proces żyje
            if self._process and self._process.poll() is not None:
                raise RuntimeError(tr(
                    f"Tunel SSH zakończył się z kodem {self._process.returncode}.\n"
                    "Sprawdź adres serwera, port SSH i dane logowania.",
                    f"The SSH tunnel exited with code {self._process.returncode}.\n"
                    "Check the server address, SSH port and credentials."
                ))
            # Sprawdź czy port TCP jest już dostępny
            try:
                with _socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                    return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)

        return False

    def stop(self) -> None:
        """Zatrzymuje tunel SSH."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def __enter__(self) -> "SSHTunnel":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class KubePortForward(SSHTunnel):
    """
    Polaczenie z Pipe dzialajacym w Kubernetesie: `kubectl port-forward svc/pipe`.
    Ten sam interfejs co SSHTunnel (start / wait_ready / stop).
    """

    def __init__(self, namespace: str, local_port: int, context: str | None = None, service: str = "pipe") -> None:
        super().__init__(host=f"svc/{service}", local_port=local_port)
        self.namespace = namespace
        self.context = context
        self.service = service

    def start(self) -> None:
        if not shutil.which("kubectl"):
            raise RuntimeError(tr("Brak komendy 'kubectl' — zainstaluj ja albo uzyj polaczenia SSH (--host).",
                                  "The 'kubectl' command is missing — install it or connect over SSH (--host)."))
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", self.namespace, "port-forward", f"svc/{self.service}",
                f"{self.local_port}:{DEFAULT_LOCAL_PORT}", "--address", "127.0.0.1"]
        self._process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None)


# ─── Klient TCP (przez tunel SSH) ────────────────────────────────────────────

class RemoteClient:
    """
    Klient TCP łączący się z backendem przez tunel SSH.

    Tunel forwarduje lokalny port TCP → Unix socket na serwerze.
    """

    def __init__(
        self,
        session_id: str,
        host: str = "127.0.0.1",
        port: int = DEFAULT_LOCAL_PORT,
        token: str = "",
    ) -> None:
        self.session_id = session_id
        self.host = host
        self.port = port
        self.token = token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Nawiązuje połączenie TCP z lokalnym portem tunelu."""
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port, limit=READ_LIMIT)
        except ConnectionRefusedError:
            raise ConnectionRefusedError(tr(
                f"Nie można połączyć się z {self.host}:{self.port}.\n"
                "Sprawdź czy tunel SSH jest aktywny i backend działa na serwerze.",
                f"Cannot connect to {self.host}:{self.port}.\n"
                "Check that the SSH tunnel is up and the backend is running on the server."
            ))

    async def disconnect(self) -> None:
        """Rozłącza się."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    @property
    def interface(self) -> str:
        # Uzytkownik systemu rozroznia osoby w audit logu i w notatkach VIBE
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
        return f"cli:{user}" if user else "cli"

    async def send_message(self, message: str) -> list[dict]:
        """Wysyła wiadomość i zwraca listę odpowiedzi."""
        return await self._send(
            {"message": message, "session_id": self.session_id, "interface": self.interface}
        )

    async def send_confirm(self, confirmed: bool) -> list[dict]:
        """Wysyła potwierdzenie/odmowę."""
        return await self._send({"confirm": confirmed, "session_id": self.session_id})

    async def send_command(self, command: str, **fields) -> list[dict]:
        """Zadanie {"command": ...} — lista w docs/protocol.md."""
        return await self._send({"command": command, "session_id": self.session_id,
                                 "interface": self.interface, **fields})

    async def _send(self, data: dict) -> list[dict]:
        """
        Wysyła żądanie JSON i zbiera odpowiedzi do `done: true`. Gdy backend zniknal (restart po
        aktualizacji, zerwane polaczenie), laczy ponownie i ponawia zadanie — rozmowa trwa bez
        restartu CLI. Zadanie, na ktore przyszla juz czesc odpowiedzi, nie jest ponawiane.
        """
        if self.token:
            data = {**data, "token": self.token}

        deadline = 0.0
        while True:
            responses: list[dict] = []
            try:
                if not self._writer or not self._reader:
                    await self.connect()
                await self._exchange(data, responses)
                if deadline:
                    console.print(tr("[dim]Połączono ponownie.[/dim]", "[dim]Reconnected.[/dim]"))
                return responses
            except (OSError, EOFError) as exc:
                await self.disconnect()
                if responses:
                    console.print(tr("[yellow]Połączenie z backendem przerwane w trakcie odpowiedzi.[/yellow]",
                                     "[yellow]The connection to the backend dropped mid-answer.[/yellow]"))
                    return responses
                if not deadline:
                    deadline = time.monotonic() + RECONNECT_TIMEOUT
                    console.print(tr("[dim]Backend nie odpowiada (restart po aktualizacji?) — łączę ponownie...[/dim]",
                                     "[dim]The backend is not responding (restarting after an update?) — reconnecting...[/dim]"))
                elif time.monotonic() > deadline:
                    raise ConnectionError(tr(
                        f"Backend nie wrócił w ciągu {int(RECONNECT_TIMEOUT)} s. Sprawdź tunel SSH i kontener na serwerze.",
                        f"The backend did not come back within {int(RECONNECT_TIMEOUT)} s. Check the SSH tunnel and the container on the server."
                    )) from exc
                await asyncio.sleep(RECONNECT_DELAY)

    async def _exchange(self, data: dict, responses: list[dict]) -> None:
        """Jedna wymiana na otwartym polaczeniu; koniec strumienia przed `done` to blad polaczenia."""
        line = json.dumps(data, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

        # Postep i zalaczniki sa pokazywane od razu; reszta wraca jako lista.
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise EOFError("backend closed the connection")
            try:
                response = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if response.get("attachment"):
                _show_attachment(response["attachment"])
            elif (response.get("event") or {}).get("type") == "progress":
                console.print(f"[dim]  › {escape(response['event'].get('text', ''))}[/dim]")
            else:
                responses.append(response)
            if response.get("done"):
                return


# ─── Wyświetlanie ─────────────────────────────────────────────────────────────

def _open_file(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _show_attachment(attachment: dict) -> None:
    """Diagram: podgląd ASCII (jeśli mieści się w terminalu) + PNG zapisany na dysk."""
    caption = attachment.get("caption") or attachment.get("name") or tr("Załącznik", "Attachment")
    console.print(Rule(f"[bold cyan]{escape(caption)}[/bold cyan]", style="cyan"))
    preview = (attachment.get("text") or "").rstrip()
    if preview:
        width = max(len(line) for line in preview.splitlines())
        if width <= console.width:
            console.print(Text(preview), highlight=False)
        else:
            console.print(tr(f"[dim](podgląd ma {width} kolumn — terminal ma {console.width}; otwórz PNG)[/dim]",
                             f"[dim](the preview is {width} columns wide — the terminal has {console.width}; open the PNG)[/dim]"))
    try:
        data = base64.b64decode(attachment.get("data", ""))
        if data:
            DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
            name = Path(attachment.get("name") or "diagram.png").name
            path = DIAGRAMS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
            path.write_bytes(data)
            console.print(tr(f"[dim]Zapisano: {path}[/dim]", f"[dim]Saved: {path}[/dim]"))
            if OPEN_IMAGES:
                _open_file(path)
    except (OSError, ValueError) as exc:
        console.print(tr(f"[red]Nie zapisano pliku: {escape(str(exc))}[/red]",
                         f"[red]File not saved: {escape(str(exc))}[/red]"))
    source = attachment.get("source")
    if source:
        console.print(tr("[dim]Kod Mermaid: /mermaid — pokaż ostatni[/dim]",
                         "[dim]Mermaid source: /mermaid — show the last one[/dim]"))
        global _last_mermaid
        _last_mermaid = source


_last_mermaid: str = ""


def _print_banner(host: str) -> None:
    """Wyświetla baner startowy z informacją o serwerze."""
    ascii_art = """[bold cyan]
  ____  _            
 |  _ \\(_)_ __   ___ 
 | |_) | | '_ \\ / _ \\
 |  __/| | |_) |  __/
 |_|   |_| .__/ \\___|
         |_|         
[/bold cyan]"""
    console.print(
        Panel.fit(
            f"{ascii_art}\n"
            + tr("[dim]Autonomiczny agent AI do zarządzania serwerem Linux | v0.21.4[/dim]\n\n"
                 f"[dim]Połączono z: [bold white]{host}[/bold white][/dim]\n"
                 "[dim]Komendy: [bold cyan]/status[/bold cyan]  [bold cyan]/raport[/bold cyan]  [bold cyan]/zmiany[/bold cyan]  "
                 "[bold cyan]/mapa[/bold cyan]  [bold cyan]/server[/bold cyan]  "
                 "[bold cyan]/skille[/bold cyan]  [bold cyan]/pomoc[/bold cyan]  "
                 "[bold cyan]/exit[/bold cyan][/dim]",
                 "[dim]Autonomous AI agent for managing a Linux server | v0.21.4[/dim]\n\n"
                 f"[dim]Connected to: [bold white]{host}[/bold white][/dim]\n"
                 "[dim]Commands: [bold cyan]/status[/bold cyan]  [bold cyan]/report[/bold cyan]  [bold cyan]/changes[/bold cyan]  "
                 "[bold cyan]/map[/bold cyan]  [bold cyan]/server[/bold cyan]  "
                 "[bold cyan]/skills[/bold cyan]  [bold cyan]/help[/bold cyan]  "
                 "[bold cyan]/exit[/bold cyan][/dim]"),
            border_style="cyan",
        )
    )


def _print_response(text: str, status: str) -> None:
    """Wyświetla odpowiedź agenta w estetycznym formacie terminalowym."""
    text = text.strip()
    if not text:
        return

    # Zamiana tagow statusowych na kolorowe oznaczenia Rich
    # [SUKCES] usuniety -- nie wyswietlamy go
    text = text.replace("[SUKCES]", "")
    text = text.replace("[BLAD]", tr("**BŁĄD:**", "**ERROR:**"))
    text = text.replace("[POTWIERDZ]", tr("**WYMAGA POTWIERDZENIA:**", "**NEEDS CONFIRMATION:**"))
    text = text.replace("[ODMOWA]", tr("**ODMOWA:**", "**REFUSED:**"))
    if LANG == "en":
        text = text.replace("[OSTRZEZENIE]", "**WARNING:**").replace("[PAMIEC]", "**MEMORY:**")

    console.print(Markdown(text))


async def _handle_responses(
    responses: list[dict],
    client: RemoteClient,
) -> bool:
    needs_confirm = False

    for resp in responses:
        text = resp.get("response", "")
        status = resp.get("status", "ok")
        if not text:
            continue
        _print_response(text, status)
        if status == "confirm":
            needs_confirm = True

    if needs_confirm:
        console.print()
        try:
            confirmed = Confirm.ask(
                tr("[yellow]Czy chcesz wykonać tę operację?[/yellow]", "[yellow]Do you want to run this operation?[/yellow]"),
                default=False,
            )
        except (KeyboardInterrupt, EOFError):
            confirmed = False
            console.print()

        if confirmed:
            console.print(tr("[dim]✔ Operacja zatwierdzona — wykonuję...[/dim]", "[dim]✔ Approved — running...[/dim]"))
        else:
            console.print(tr("[dim]✖ Operacja anulowana.[/dim]", "[dim]✖ Operation cancelled.[/dim]"))

        confirm_responses = await client.send_confirm(confirmed)
        await _handle_responses(confirm_responses, client)
        return True

    return False


# ─── Komendy "/" ──────────────────────────────────────────────────────────────

SCAN_WORDS = ("aktualizuj", "odswiez", "odśwież", "skanuj", "update", "refresh", "scan")

# Angielskie nazwy komend — dzialaja w obu jezykach obok polskich.
COMMAND_ALIASES = {
    "report": "raport", "digest": "raport", "changes": "zmiany", "chart": "wykres", "health": "zdrowie",
    "audit": "audyt", "map": "mapa", "directory": "katalogi", "dirs": "katalogi", "skills": "skille",
    "alerts": "alerty", "routines": "rutyny", "targets": "cele", "journal": "dziennik", "undo": "cofnij",
    "approvals": "zgody", "cost": "koszt", "usage": "koszt", "history": "historia", "incidents": "incydenty",
    "reminders": "przypomnienia", "update": "aktualizuj", "language": "jezyk", "lang": "jezyk", "język": "jezyk",
}

# Podpowiedzi po wpisaniu "/": (nazwa polska, nazwa angielska, opis polski, opis angielski).
COMMANDS = [
    ("status", "status", "stan serwera", "server status"),
    ("raport", "report", "raport: stan, zmiany, certyfikaty, backupy", "report: health, changes, certificates, backups"),
    ("zmiany", "changes", "co się zmieniło na serwerze (np. /zmiany 3d)", "what changed on the server (e.g. /changes 3d)"),
    ("wykres", "chart", "wykres load / ram / dysk (np. /wykres ram 7d)", "chart of load / ram / disk (e.g. /chart ram 7d)"),
    ("zdrowie", "health", "certyfikaty, strony, DNS, backupy", "certificates, sites, DNS, backups"),
    ("audyt", "audit", "audyt bezpieczeństwa z poprawkami", "security audit with fixes"),
    ("mapa", "map", "diagram infrastruktury", "infrastructure diagram"),
    ("mermaid", "mermaid", "kod ostatniego diagramu", "source of the last diagram"),
    ("server", "server", "notatki o serwerze (SERVER.md); /server aktualizuj", "server notes (SERVER.md); /server update"),
    ("katalogi", "directory", "mapa repozytoriów i katalogów", "map of repositories and directories"),
    ("skille", "skills", "lista skilli", "list of skills"),
    ("alerty", "alerts", "aktywne alerty czuwania", "active watcher alerts"),
    ("incydenty", "incidents", "pamięć incydentów", "incident memory"),
    ("rutyny", "routines", "zadania według harmonogramu", "scheduled tasks"),
    ("przypomnienia", "reminders", "przypomnienia; /przypomnienia anuluj <id>", "reminders; /reminders cancel <id>"),
    ("cele", "targets", "zdalne serwery, kontenery i klastry", "remote servers, containers and clusters"),
    ("vibe", "vibe", "styl rozmowy; /vibe reset", "conversation style; /vibe reset"),
    ("dziennik", "journal", "zatwierdzone zmiany z kopiami", "approved changes with backups"),
    ("cofnij", "undo", "cofnij ostatnią (albo wybraną) zmianę", "undo the last (or the chosen) change"),
    ("zgody", "approvals", "operacje agentów MCP czekające na zgodę", "MCP agent operations waiting for approval"),
    ("mcp", "mcp", "serwery MCP", "MCP servers"),
    ("koszt", "cost", "zużycie tokenów i koszt LLM", "token usage and LLM cost"),
    ("historia", "history", "ostatnie wpisy audit logu", "latest audit-log entries"),
    ("aktualizuj", "update", "zaktualizuj Pipe na serwerze; /aktualizuj sprawdz", "update Pipe on the server; /update check"),
    ("jezyk", "language", "język Pipe: /jezyk en albo /jezyk pl", "Pipe's language: /language pl or /language en"),
    ("pomoc", "help", "lista komend", "list of commands"),
    ("exit", "exit", "wyjście", "quit"),
]

HELP_TEXT = """**Komendy**

- `/status` — stan serwera
- `/aktualizuj` — zaktualizuj Pipe na serwerze (po potwierdzeniu); `/aktualizuj sprawdz` — tylko sprawdź wersję
- `/raport` — poranny raport: stan, zmiany od wczoraj, certyfikaty, backupy, aktualizacje
- `/zmiany [24h|3d]` — co się zmieniło na serwerze (pakiety, kontenery, porty, cron, konta, konfiguracje)
- `/wykres [load|ram|dysk] [24h|7d]` — wykres z historii czuwania (PNG)
- `/zdrowie` — certyfikaty TLS, odpowiedź stron, DNS i świeżość backupów
- `/audyt` — ocena bezpieczeństwa hosta z gotowymi poprawkami („napraw 1”)
- `/mapa` — diagram infrastruktury (PNG + podgląd w terminalu); `/mermaid` — kod ostatniego diagramu
- `/server` — pokaż SERVER.md (`/server aktualizuj` — zbadaj serwer ponownie)
- `/katalogi` — mapa repozytoriów i katalogów (DIRECTORY)
- `/skille` — zapisane procedury; każda ma własną komendę, np. `/odnow_certyfikat`
- `/alerty` — aktywne alerty czuwania
- `/incydenty` — pamięć incydentów: co się działo, jaka była przyczyna i co pomogło
- `/rutyny` — zadania wykonywane według harmonogramu
- `/przypomnienia` — jednorazowe przypomnienia („przypomnij mi za 2 godziny…”); `/przypomnienia anuluj <id>`
- `/cele` — zdalne serwery, kontenery i klastry
- `/vibe` — co agent wie o Twoim stylu rozmowy (`/vibe reset` — wyczyść)
- `/dziennik` — zatwierdzone zmiany z kopiami; `/cofnij [id]` — cofnij ostatnią (albo wybraną) zmianę
- `/zgody` — operacje zewnętrznych agentów (MCP) czekające na Twoją zgodę
- `/mcp` — serwery MCP, z których korzysta Pipe
- `/koszt` — zużycie tokenów i koszt LLM
- `/historia` — ostatnie wpisy audit logu
- `/jezyk [pl|en]` — język Pipe: instrukcje agenta, raporty i komunikaty (wspólny dla CLI, weba i Telegrama)
- `/pomoc` — ta lista
- `/exit` — wyjście

Do komendy skilla możesz dopisać wskazówki: `/odnow_certyfikat tylko dla example.com`.
Diagramy możesz też zamawiać zwykłym tekstem: „narysuj, jak zapytanie trafia do sklepu”."""

HELP_TEXT_EN = """**Commands**

- `/status` — server status
- `/update` — update Pipe on the server (after confirmation); `/update check` — only check the version
- `/report` — morning report: health, changes since yesterday, certificates, backups, updates
- `/changes [24h|3d]` — what changed on the server (packages, containers, ports, cron, accounts, configs)
- `/chart [load|ram|disk] [24h|7d]` — chart from the watcher's history (PNG)
- `/health` — TLS certificates, site responses, DNS and backup freshness
- `/audit` — host security score with ready-made fixes ("fix 1")
- `/map` — infrastructure diagram (PNG + terminal preview); `/mermaid` — source of the last diagram
- `/server` — show SERVER.md (`/server update` — explore the server again)
- `/directory` — map of repositories and directories (DIRECTORY)
- `/skills` — saved procedures; each has its own command, e.g. `/renew_certificate`
- `/alerts` — active watcher alerts
- `/incidents` — incident memory: what happened, what the cause was and what helped
- `/routines` — tasks run on a schedule
- `/reminders` — one-off reminders ("remind me in 2 hours…"); `/reminders cancel <id>`
- `/targets` — remote servers, containers and clusters
- `/vibe` — what the agent knows about your conversation style (`/vibe reset` — clear it)
- `/journal` — approved changes with backups; `/undo [id]` — undo the last (or the chosen) change
- `/approvals` — operations of external agents (MCP) waiting for your approval
- `/mcp` — MCP servers Pipe uses
- `/cost` — token usage and LLM cost
- `/history` — latest audit-log entries
- `/language [pl|en]` — Pipe's language: the agent's instructions, reports and messages (shared by the CLI, web and Telegram)
- `/help` — this list
- `/exit` — quit

You can add hints to a skill command: `/renew_certificate only for example.com`.
You can also ask for diagrams in plain text: "draw how a request reaches the shop"."""


def _print_list(title: str, items: list[str], empty: str) -> None:
    console.print(f"[bold]{escape(title)}[/bold]")
    if not items:
        console.print(f"[dim]{escape(empty)}[/dim]")
    for item in items:
        console.print(f"  • {escape(item)}")


def _response_data(responses: list[dict]) -> dict:
    """Pole 'data' z odpowiedzi na zadanie {"command": ...}; błąd backendu -> RuntimeError."""
    for resp in reversed(responses):
        if "data" in resp:
            return resp["data"]
    error = next((r.get("response") for r in responses if r.get("status") == "error"), "") or tr("brak danych", "no data")
    raise RuntimeError(error)


# Skille z komendami ([{name, description, command}]) — do podpowiedzi; odswiezane po kazdej turze.
_skills: list[dict] = []


async def _refresh_skills(client: "RemoteClient") -> None:
    global _skills
    try:
        _skills = _response_data(await client.send_command("list_skills")).get("skills", [])
    except Exception:
        pass            # podpowiedzi to wygoda — brak listy skilli niczego nie blokuje


def _slash_options(text: str, skills: list[dict] | None = None) -> list[tuple[str, str]]:
    """
    Podpowiedzi dla wpisywanej komendy: [(nazwa bez "/", opis)]. Pusta lista, gdy tekst
    nie zaczyna sie od "/" albo ma juz spacje (wtedy to argumenty albo zwykla wiadomosc).
    Pasuje poczatek nazwy w dowolnym jezyku (i alias), od 2 znakow takze srodek.
    """
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    query = text[1:].lower()

    def match(*names: str) -> bool:
        names = [n.lower() for n in names if n]
        return not query or any(n.startswith(query) for n in names) or (len(query) > 1 and any(query in n for n in names))

    options = []
    for name_pl, name_en, desc_pl, desc_en in COMMANDS:
        aliases = [alias for alias, target in COMMAND_ALIASES.items() if target == name_pl]
        if match(name_pl, name_en, *aliases):
            options.append((tr(name_pl, name_en), tr(desc_pl, desc_en)))
    for skill in _skills if skills is None else skills:
        if skill.get("command") and match(skill["command"], skill.get("name", "")):
            options.append((skill["command"], skill.get("description", "")))
    # dokladne trafienie na gorze, potem pasujace poczatkiem
    return sorted(options, key=lambda o: 0 if o[0].lower() == query else 1 if o[0].lower().startswith(query) else 2)


if PromptSession is not None:
    class SlashCompleter(Completer):
        """Lista komend i skilli pod promptem — pojawia sie po "/", Tab wstawia wybrana."""

        def get_completions(self, document, complete_event):
            for name, description in _slash_options(document.text_before_cursor):
                yield Completion(name, start_position=1 - len(document.text_before_cursor),
                                 display="/" + name, display_meta=description)


def _prompt_reader(**session_options):
    """
    Zwraca korutyne czytajaca jedna linie od uzytkownika. Z prompt_toolkit: podpowiedzi
    komend na zywo, Tab uzupelnia, strzalki przywoluja historie. Bez niego (albo gdy
    wejscie nie jest terminalem) — zwykly prompt.
    """
    if PromptSession is None or not (session_options or sys.stdin.isatty()):
        async def plain() -> str:
            return Prompt.ask("[bold cyan]>[/bold cyan]")
        return plain

    session = PromptSession(completer=SlashCompleter(), complete_while_typing=True, **session_options)

    async def ask() -> str:
        return await session.prompt_async([("bold ansicyan", ">"), ("", ": ")])
    return ask


def _set_lang(value: str) -> None:
    """Przelacza jezyk CLI w trakcie pracy — `tr()` czyta LANG przy kazdym wywolaniu."""
    global LANG
    LANG = "en" if str(value).strip().lower().startswith("en") else "pl"


async def _sync_language(client: "RemoteClient") -> None:
    """Po polaczeniu: jezyk wybrany na serwerze komenda /jezyk obowiazuje tez tutaj (chyba ze podano --lang)."""
    if LANG_FORCED:
        return
    try:
        data = _response_data(await client.send_command("language"))
    except Exception:
        return          # starszy backend nie zna tej komendy
    if data.get("chosen") and data.get("lang"):
        _set_lang(data["lang"])


async def _handle_slash(user_input: str, client: "RemoteClient") -> bool:
    """
    Komendy /server, /skille, /pomoc i skille. Zwraca False, gdy to nie jest
    komenda — wtedy tekst idzie do agenta jak zwykła wiadomość (np. '/var/log jest pełny?').
    """
    head, _, args = user_input.partition(" ")
    name, args = head[1:].lower(), args.strip()
    name = COMMAND_ALIASES.get(name, name)

    if name == "server":
        content = "" if args.lower() in SCAN_WORDS else _response_data(await client.send_command("server_md")).get("content", "")
        if content.strip():
            console.print(Markdown(content))
            console.print(tr("[dim]/server aktualizuj — zbadaj serwer ponownie[/dim]",
                             "[dim]/server update — explore the server again[/dim]"))
        else:
            console.print("[dim]" + (tr("Badam serwer i aktualizuję SERVER.md...",
                                        "Exploring the server and updating SERVER.md...") if args.lower() in SCAN_WORDS
                                     else tr("SERVER.md jeszcze nie istnieje. Badam serwer i tworzę go...",
                                             "SERVER.md does not exist yet. Exploring the server to create it...")) + "[/dim]")
            await _handle_responses(await client.send_command("scan_server"), client)
        return True

    if name in ("pomoc", "help"):
        console.print(Markdown(tr(HELP_TEXT, HELP_TEXT_EN)))
        return True

    if name == "jezyk":
        data = _response_data(await client.send_command("language", args=args))
        if args and data.get("lang"):
            _set_lang(data["lang"])
        console.print(escape(data.get("text", "")))
        return True

    if name == "aktualizuj":
        console.print(tr("[dim]Sprawdzam aktualizację Pipe...[/dim]", "[dim]Checking the Pipe update...[/dim]"))
        await _handle_responses(await client.send_command("update", args=args), client)
        return True

    if name == "status":
        console.print(tr("[dim]Analizuję stan serwera...[/dim]", "[dim]Analysing the server status...[/dim]"))
        await _handle_responses(await client.send_command("status"), client)
        return True

    if name == "mapa":
        console.print(tr("[dim]Odkrywam infrastrukturę i rysuję mapę...[/dim]",
                         "[dim]Discovering the infrastructure and drawing the map...[/dim]"))
        responses = await client.send_command("diagram", args=args)
        for resp in responses:
            if resp.get("status") == "error":
                console.print(f"[red]{escape(resp.get('response', ''))}[/red]")
        return True

    if name == "zmiany":
        data = _response_data(await client.send_command("changes", args=args))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "wykres":
        data = _response_data(await client.send_command("chart", args=args))
        console.print(f"[dim]{escape(data.get('summary', ''))}[/dim]")
        return True

    if name == "zdrowie":
        console.print(tr("[dim]Sprawdzam certyfikaty, strony i backupy...[/dim]",
                         "[dim]Checking certificates, sites and backups...[/dim]"))
        data = _response_data(await client.send_command("health"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "raport":
        console.print(tr("[dim]Składam raport...[/dim]", "[dim]Putting the report together...[/dim]"))
        data = _response_data(await client.send_command("digest"))
        lines = [f"**{data.get('title', tr('Raport', 'Report'))}**"]
        for section in data.get("sections", []):
            lines.append(f"\n**{section.get('title', '')}**\n")
            lines += [f"- {line}" for line in section.get("lines", [])]
        console.print(Markdown("\n".join(lines)))
        return True

    if name == "audyt":
        console.print(tr("[dim]Sprawdzam konfigurację bezpieczeństwa...[/dim]",
                         "[dim]Checking the security configuration...[/dim]"))
        data = _response_data(await client.send_command("audit"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        if data.get("findings"):
            console.print(tr('[dim]Napisz „napraw 1”, a przygotuję poprawkę do zatwierdzenia.[/dim]',
                             '[dim]Write "fix 1" and I will prepare the fix for your approval.[/dim]'))
        return True

    if name == "dziennik":
        data = _response_data(await client.send_command("journal"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        console.print(tr("[dim]/cofnij <id> — cofnij wybraną zmianę[/dim]", "[dim]/undo <id> — undo the chosen change[/dim]"))
        return True

    if name == "cofnij":
        data = _response_data(await client.send_command("undo", id=args))
        console.print(Markdown(data.get("preview", "")))
        if not data.get("undoable"):
            return True
        try:
            confirmed = Confirm.ask(tr("[yellow]Cofnąć tę zmianę?[/yellow]", "[yellow]Undo this change?[/yellow]"), default=False)
        except (KeyboardInterrupt, EOFError):
            confirmed = False
        if confirmed:
            result = _response_data(await client.send_command("undo", id=data["id"], execute=True))
            console.print(Text(result.get("text", ""), overflow="fold"), highlight=False)
        else:
            console.print(tr("[dim]✖ Anulowano.[/dim]", "[dim]✖ Cancelled.[/dim]"))
        return True

    if name == "mcp":
        _print_list(tr("Serwery MCP", "MCP servers"),
                    _response_data(await client.send_command("mcp_servers")).get("servers", []),
                    tr("Brak. Napisz np.: dodaj serwer MCP github (npx -y @modelcontextprotocol/server-github)",
                       "None. Write e.g.: add the MCP server github (npx -y @modelcontextprotocol/server-github)"))
        return True

    if name == "zgody":
        pending = _response_data(await client.send_command("approvals")).get("pending", [])
        if not pending:
            console.print(tr("[dim]Nic nie czeka na zgodę.[/dim]", "[dim]Nothing is waiting for approval.[/dim]"))
        for item in pending:
            console.print(Rule(f"[bold]{tr('Zgoda', 'Approval')} {escape(item['id'])}[/bold] — agent {escape(item['requested_by'])}"))
            console.print(Markdown(f"{tr('Cel', 'Target')} `{item['target']}`: `{item['command']}`\n\n"
                                   + (f"{tr('Powód', 'Reason')}: {item['reason']}\n\n" if item.get("reason") else "")
                                   + (item.get("plan") or "")))
            try:
                decision = Confirm.ask(tr("[yellow]Zatwierdzić?[/yellow]", "[yellow]Approve?[/yellow]"), default=False)
            except (KeyboardInterrupt, EOFError):
                decision = False
            result = _response_data(await client.send_command("approve", id=item["id"], decision=decision))
            console.print(Text(result.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "incydenty":
        data = _response_data(await client.send_command("incidents"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "koszt":
        data = _response_data(await client.send_command("usage"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "mermaid":
        console.print(Markdown(f"```mermaid\n{_last_mermaid}\n```") if _last_mermaid else tr("[dim]Brak diagramu w tej sesji.[/dim]", "[dim]No diagram in this session.[/dim]"))
        return True

    if name == "katalogi":
        data = _response_data(await client.send_command("directory"))
        console.print(Markdown("**DIRECTORY**\n\n" + (data.get("text") or tr("_(pusto — napisz: znajdź repozytoria na serwerze)_",
                                                                              "_(empty — write: find the repositories on the server)_"))))
        return True

    if name == "alerty":
        data = _response_data(await client.send_command("alerts"))
        if not data.get("enabled", True):
            console.print(tr("[dim]Czuwanie jest wyłączone (WATCH_ENABLED=0).[/dim]",
                             "[dim]The watcher is disabled (WATCH_ENABLED=0).[/dim]"))
        _print_list(tr("Aktywne alerty", "Active alerts"),
                    [f"[{a.get('severity')}] {a.get('title')} — {a.get('detail')}" for a in data.get("active", [])],
                    tr("Brak — wszystko w normie.", "None — everything is normal."))
        return True

    if name == "cele":
        _print_list(tr("Zdalne cele", "Remote targets"),
                    _response_data(await client.send_command("targets")).get("targets", []),
                    tr("Brak. Napisz np.: dodaj serwer 10.0.0.5 jako web-2 (ssh, root)",
                       "None. Write e.g.: add the server 10.0.0.5 as web-2 (ssh, root)"))
        return True

    if name == "rutyny":
        _print_list(tr("Rutyny", "Routines"),
                    _response_data(await client.send_command("routines")).get("routines", []),
                    tr("Brak. Napisz np.: codziennie o 7 sprawdzaj backupy",
                       "None. Write e.g.: check the backups every day at 7"))
        return True

    if name == "przypomnienia":
        parts = args.split()
        cancel = parts[1].lstrip("#") if len(parts) >= 2 and parts[0].lower() in ("anuluj", "cancel", "usun", "remove") else ""
        data = _response_data(await client.send_command("reminders", cancel=cancel))
        if data.get("cancelled"):
            console.print(tr(f"Anulowano #{data['cancelled']}.", f"Cancelled #{data['cancelled']}."))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        console.print(tr("[dim]W CLI przypomnienie pokaże się przy najbliższej wiadomości albo po połączeniu; "
                         "na Telegram przychodzi samo, o czasie.[/dim]",
                         "[dim]In the CLI a reminder shows up with your next message or on connect; "
                         "on Telegram it arrives by itself, on time.[/dim]"))
        return True

    if name == "vibe":
        data = _response_data(await client.send_command("vibe", args=args))
        if "reset" in data:
            console.print(tr("Wyczyściłem notatkę o Twoim stylu.", "I cleared the note about your style.") if data["reset"]
                          else tr("Nie było notatki.", "There was no note."))
        elif data.get("content", "").strip():
            console.print(Markdown(data["content"]))
            console.print(tr("[dim]/vibe reset — wyczyść[/dim]", "[dim]/vibe reset — clear it[/dim]"))
        else:
            console.print(tr("[dim]Jeszcze nie znam Twojego stylu — uczę się z rozmów.[/dim]",
                             "[dim]I do not know your style yet — I learn from our conversations.[/dim]"))
        return True

    if name == "historia":
        entries = _response_data(await client.send_command("history")).get("entries", [])
        console.print(Text("\n".join(entries) or tr("(pusto)", "(empty)")), highlight=False)
        return True

    if not name:
        return False
    skills = _response_data(await client.send_command("list_skills")).get("skills", [])

    if name == "skille":
        if not skills:
            console.print(tr('Brak zapisanych skilli. Po wykonaniu wieloetapowej procedury poproś: "zapisz to jako skill".',
                             'No saved skills. After a multi-step procedure ask: "save this as a skill".'))
        for skill in skills:
            label = f"/{skill['command']}" if skill["command"] else f"{skill['name']} {tr('(bez komendy)', '(no command)')}"
            console.print(f"  [bold cyan]{escape(label)}[/bold cyan] — {escape(skill['description'])}")
        return True

    entry = next((s for s in skills if name == s["name"] or (s["command"] and name == s["command"])), None)
    if entry is None:
        return False
    console.print(tr(f"[dim]Uruchamiam skill {escape(entry['name'])}...[/dim]",
                     f"[dim]Running the skill {escape(entry['name'])}...[/dim]"))
    await _handle_responses(await client.send_command("run_skill", name=entry["name"], args=args), client)
    return True


async def run_mcp_bridge(client: "RemoteClient", host: str) -> None:
    """
    Most MCP (stdio): Claude Code / Cursor uruchamia `pipe --mcp --host root@serwer`, a kazda wiadomosc
    JSON-RPC idzie do backendu Pipe (komenda "mcp") przez tunel SSH i token Pipe. Na stdout — tylko MCP.
    """
    await client.connect()
    console.print(tr(f"[dim]Pipe MCP: polaczono z {escape(host)}[/dim]", f"[dim]Pipe MCP: connected to {escape(host)}[/dim]"))
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=READ_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    out = sys.stdout

    def emit(message: dict) -> None:
        out.write(json.dumps(message, ensure_ascii=False) + "\n")
        out.flush()

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": tr("Nieprawidlowy JSON.", "Invalid JSON.")}})
                continue
            try:
                responses = await client.send_command("mcp", rpc=message)
            except (OSError, RuntimeError):
                await client.connect()                       # jedna proba ponownego polaczenia
                responses = await client.send_command("mcp", rpc=message)
            try:
                reply = _response_data(responses).get("rpc")
            except RuntimeError as exc:
                reply = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                         "error": {"code": -32603, "message": f"Pipe: {exc}"}}
            if reply is not None:
                emit(reply)
    finally:
        await client.disconnect()


async def _show_due_reminders(client: "RemoteClient") -> None:
    """
    CLI nie odbiera zdarzen na zywo (REPL czeka na klawiature), wiec przypomnienia, ktore odpalily bez
    odbiorcy, pobiera przy polaczeniu i po kazdej wymianie. Cicho, gdy backend jest starszy albo nic nie czeka.
    """
    try:
        claimed = _response_data(await client.send_command("reminders", claim=True)).get("claimed", [])
    except Exception:
        return
    for event in claimed:
        title = tr("Przypomnienie", "Reminder") if event.get("kind") != "task" else tr("Zadanie zaplanowane", "Scheduled task")
        body = escape(str(event.get("text", "")))
        if event.get("report"):
            body += "\n\n" + escape(str(event["report"]))
        console.print(Panel(body, title=f"{title} · {escape(str(event.get('due', '')))}", border_style="yellow"))


async def run_web(client: "RemoteClient", host: str) -> None:
    """
    `pipe web`: interfejs w przegladarce. Ten proces serwuje strone na 127.0.0.1 i mostkuje ja na
    backend przez ten sam tunel co CLI — token zostaje tutaj, na serwerze nie otwiera sie zaden port.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    try:
        import server as web_server
    except ImportError as exc:
        console.print(tr(f"[red]Nie znaleziono interfejsu webowego (clients/webui): {exc}[/red]",
                         f"[red]Web interface not found (clients/webui): {exc}[/red]"))
        return
    try:
        await client.connect()
        await _sync_language(client)
        await client.disconnect()
    except Exception as exc:
        console.print(tr(f"[red]Błąd połączenia: {exc}[/red]", f"[red]Connection error: {exc}[/red]"))
        return
    bridge = web_server.WebBridge(client.host, client.port, client.token, lang=LANG, server_label=host,
                                  port=web_server.free_port(WEB_PORT))
    console.print(Panel.fit(
        tr(f"[bold cyan]Pipe Web[/bold cyan] — połączono z [bold white]{escape(host)}[/bold white]\n\n"
           "[dim]Adres poniżej zawiera jednorazowy klucz dostępu i działa tylko na tym komputerze.\n"
           "Ctrl+C kończy.[/dim]",
           f"[bold cyan]Pipe Web[/bold cyan] — connected to [bold white]{escape(host)}[/bold white]\n\n"
           "[dim]The address below contains a one-time access key and works only on this computer.\n"
           "Ctrl+C to quit.[/dim]"),
        border_style="cyan"))
    # Adres poza ramka, w jednej linii i jako hiperlacze terminala (OSC 8): ramka lamalaby go na dwie linie,
    # a wtedy klikniecie otwiera uciety adres bez klucza.
    console.print(tr("Otwórz w przeglądarce (kliknij):", "Open in your browser (click):"))
    console.print(f"[bold underline cyan][link={bridge.url}]{bridge.url}[/link][/bold underline cyan]",
                  soft_wrap=True, highlight=False)
    console.print()
    await bridge.serve(open_browser=WEB_OPEN_BROWSER)


def _free_port() -> int:
    import socket as _socket
    with _socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _own_local_port(port: int) -> int:
    """
    Port dla wlasnego tunelu. Gdy `port` juz ktos trzyma (drugie otwarte CLI, osierocony tunel),
    wait_ready() uznaloby cudzy tunel za gotowy, zanim nasz ssh skonczy logowanie — rozmowa
    ruszylaby, a haslo wpisywane dla ssh trafiloby do agenta. Wtedy bierzemy wolny port.
    """
    import socket
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            pass
    free = _free_port()
    console.print(tr(f"[dim]Port {port} jest zajęty (inne otwarte CLI albo stary tunel) — używam {free}.[/dim]",
                     f"[dim]Port {port} is taken (another open CLI or a stale tunnel) — using {free}.[/dim]"))
    return free


def _welcome_marker(host: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host)[:80] or "default"
    return Path.home() / ".pipe" / f"welcomed-{safe}"


async def _first_run_welcome(client: "RemoteClient", host: str) -> None:
    """Pierwsze polaczenie z serwerem: mapa, ocena bezpieczenstwa i co Pipe pilnuje (raz na serwer)."""
    marker = _welcome_marker(host)
    if marker.exists():
        return
    console.print(tr("[dim]Pierwsze połączenie z tym serwerem — rozglądam się (mapa i audyt)...[/dim]",
                     "[dim]First connection to this server — looking around (map and audit)...[/dim]"))
    try:
        data = _response_data(await client.send_command("welcome"))
    except Exception as exc:
        console.print(tr(f"[dim]Powitanie niedostępne: {escape(str(exc))}[/dim]",
                         f"[dim]Welcome unavailable: {escape(str(exc))}[/dim]"))
        return
    lines = [f"**{data.get('title', '')}**"]
    for section in data.get("sections", []):
        lines.append(f"\n**{section.get('title', '')}**\n")
        lines += [f"- {line}" for line in section.get("lines", [])]
    console.print(Markdown("\n".join(lines)))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%d %H:%M"))
    except OSError:
        pass


# ─── Główna pętla REPL ────────────────────────────────────────────────────────

async def run_cli(client: RemoteClient, host: str) -> None:
    """Główna pętla REPL."""
    # Połącz z backendem
    try:
        await client.connect()
    except Exception as exc:
        _print_banner(host)
        console.print(tr(f"[red]Błąd połączenia: {exc}[/red]", f"[red]Connection error: {exc}[/red]"))
        return

    # baner dopiero po poznaniu jezyka agenta (/jezyk) — inaczej powitanie byloby zawsze w jezyku startowym
    await _sync_language(client)
    _print_banner(host)
    console.print(tr(f"[dim]Połączono z agentem na {host}[/dim]", f"[dim]Connected to the agent on {host}[/dim]"))
    await _first_run_welcome(client, host)
    await _show_due_reminders(client)
    await _refresh_skills(client)
    ask = _prompt_reader()
    if PromptSession is None:
        install = escape(f"{sys.executable} -m pip install prompt_toolkit")
        console.print(tr(f"[yellow]Podpowiedzi komend po \"/\" są wyłączone — brakuje prompt_toolkit. Zainstaluj:[/yellow]\n  {install}",
                         f"[yellow]Command suggestions after \"/\" are off — prompt_toolkit is missing. Install:[/yellow]\n  {install}"))

    # Status startowy ukryty na zyczenie
    console.print(Rule(style="dim"))
    console.print()

    # Pętla REPL
    try:
        while True:
            try:
                user_input = await ask()
            except (KeyboardInterrupt, EOFError):
                console.print()
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("/exit", "exit", "quit", "wyjðź", "koniec"):
                break

            # /server, /skille, /pomoc i skille jako komendy
            if user_input.startswith("/"):
                console.print()
                try:
                    handled = await _handle_slash(user_input, client)
                except Exception as exc:
                    console.print(tr(f"[red]Błąd: {escape(str(exc))}[/red]", f"[red]Error: {escape(str(exc))}[/red]"))
                    handled = True
                if handled:
                    await _show_due_reminders(client)
                    await _refresh_skills(client)
                    console.print()
                    continue

            console.print()
            try:
                responses = await client.send_message(user_input)
                await _handle_responses(responses, client)
                await _show_due_reminders(client)
                await _refresh_skills(client)
            except Exception as exc:
                # _send samo laczy ponownie; blad tutaj nie konczy rozmowy — nastepna wiadomosc sprobuje znowu
                console.print(tr(f"[red]Błąd komunikacji: {escape(str(exc))}[/red]", f"[red]Communication error: {escape(str(exc))}[/red]"))

            console.print()

    finally:
        await client.disconnect()
        console.print(tr("[dim]Do widzenia![/dim]", "[dim]Goodbye![/dim]"))


# ─── Punkt wejścia ────────────────────────────────────────────────────────────

def main() -> None:
    """Punkt wejścia CLI."""
    parser = argparse.ArgumentParser(
        description=tr("VPS Management Agent — Interfejs CLI (działa na laptopie)",
                       "VPS Management Agent — CLI (runs on your laptop)"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=tr("""
Przykłady:
  # Połącz przez SSH (automatyczny tunel)
  python cli.py --host user@serwer.example.com
  python cli.py --host root@1.2.3.4
  python cli.py --host root@1.2.3.4 --ssh-port 2222
  python cli.py --host root@1.2.3.4 --key ~/.ssh/id_rsa

  # Jesli masz juz wlasny tunel SSH (np. ssh -L 7379:127.0.0.1:7379 ...)
  python cli.py --no-tunnel --local-port 7379
        """, """
Examples:
  # Connect over SSH (automatic tunnel)
  python cli.py --host user@server.example.com
  python cli.py --host root@1.2.3.4
  python cli.py --host root@1.2.3.4 --ssh-port 2222
  python cli.py --host root@1.2.3.4 --key ~/.ssh/id_rsa

  # If you already have your own SSH tunnel (e.g. ssh -L 7379:127.0.0.1:7379 ...)
  python cli.py --no-tunnel --local-port 7379
        """),
    )
    parser.add_argument("mode", nargs="?", choices=("web",), default=None,
                        help=tr("web — interfejs w przeglądarce zamiast terminala (pipe web)",
                                "web — browser interface instead of the terminal (pipe web)"))
    parser.add_argument("--web-port", type=int, default=int(os.getenv("PIPE_WEB_PORT", "7400")), metavar="PORT",
                        help=tr("pipe web: port strony na 127.0.0.1 (domyślnie 7400)",
                                "pipe web: port of the page on 127.0.0.1 (default 7400)"))
    parser.add_argument("--no-browser", action="store_true",
                        help=tr("pipe web: nie otwieraj przeglądarki, tylko wypisz adres",
                                "pipe web: do not open the browser, just print the address"))
    parser.add_argument("--lang", choices=("pl", "en"), default=LANG,
                        help=tr("Język interfejsu CLI (domyślnie z PIPE_LANG, inaczej pl).",
                                "CLI language (defaults to PIPE_LANG, otherwise pl)."))

    # ─── SSH ───────────────────────────────────────────────────────────────
    ssh_group = parser.add_argument_group(tr("Połączenie SSH (domyślne)", "SSH connection (default)"))
    ssh_group.add_argument(
        "--host",
        default=os.getenv("VPS_HOST"),
        help=tr("Adres serwera: user@host lub host (np. root@serwer.example.com). "
                "Można też ustawić przez zmienną środowiskową VPS_HOST.",
                "Server address: user@host or host (e.g. root@server.example.com). "
                "Can also be set with the VPS_HOST environment variable."),
        metavar="USER@HOST",
    )
    ssh_group.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("VPS_SSH_PORT", str(DEFAULT_SSH_PORT))),
        help=tr(f"Port SSH serwera (domyślnie: {DEFAULT_SSH_PORT})", f"Server SSH port (default: {DEFAULT_SSH_PORT})"),
        metavar="PORT",
    )
    ssh_group.add_argument(
        "--key",
        default=os.getenv("VPS_SSH_KEY"),
        help=tr("Ścieżka do klucza prywatnego SSH (domyślnie: domyślny klucz z ~/.ssh/)",
                "Path to the SSH private key (default: the default key from ~/.ssh/)"),
        metavar="PATH",
    )
    ssh_group.add_argument(
        "--remote-port",
        type=int,
        default=int(os.getenv("VPS_REMOTE_PORT", str(DEFAULT_LOCAL_PORT))),
        help=tr(f"Port TCP backendu na serwerze (domyslnie: {DEFAULT_LOCAL_PORT})",
                f"Backend TCP port on the server (default: {DEFAULT_LOCAL_PORT})"),
        metavar="PORT",
    )

    # ─── Tryb bez tunelu ───────────────────────────────────────────────────
    manual_group = parser.add_argument_group(tr("Tryb bez automatycznego tunelu", "Mode without the automatic tunnel"))
    manual_group.add_argument(
        "--no-tunnel",
        action="store_true",
        help=tr("Nie zestawiaj tunelu SSH — połącz się bezpośrednio z lokalnym portem "
                "(użyj gdy masz własny tunel lub testujesz lokalnie)",
                "Do not set up an SSH tunnel — connect straight to the local port "
                "(use it when you have your own tunnel or test locally)"),
    )
    manual_group.add_argument(
        "--local-port",
        type=int,
        default=int(os.getenv("VPS_LOCAL_PORT", str(DEFAULT_LOCAL_PORT))),
        help=tr(f"Lokalny port TCP do połączenia (domyślnie: {DEFAULT_LOCAL_PORT})",
                f"Local TCP port to connect to (default: {DEFAULT_LOCAL_PORT})"),
        metavar="PORT",
    )

    # ─── Kubernetes ────────────────────────────────────────────────────────
    kube_group = parser.add_argument_group(tr("Pipe w Kubernetesie (kubectl port-forward)",
                                              "Pipe in Kubernetes (kubectl port-forward)"))
    kube_group.add_argument(
        "--kube",
        metavar="NAMESPACE",
        default=os.getenv("PIPE_KUBE_NAMESPACE"),
        help=tr("Połącz z Pipe w klastrze: kubectl port-forward svc/pipe w podanym namespace (np. pipe).",
                "Connect to Pipe in a cluster: kubectl port-forward svc/pipe in the given namespace (e.g. pipe)."),
    )
    kube_group.add_argument("--kube-context", default=os.getenv("PIPE_KUBE_CONTEXT"), metavar="CONTEXT",
                            help=tr("Kontekst kubeconfig (domyślnie bieżący).", "kubeconfig context (default: current)."))

    parser.add_argument("--open", action="store_true",
                        help=tr("Otwieraj zapisane diagramy PNG w domyślnej przeglądarce obrazów.",
                                "Open saved PNG diagrams in the default image viewer."))
    parser.add_argument("--mcp", action="store_true",
                        help=tr("Most MCP (stdio) dla Claude Code, Cursora i innych agentow: narzedzia Pipe "
                                "przez tunel SSH i token Pipe. Stdout = protokol MCP.",
                                "MCP bridge (stdio) for Claude Code, Cursor and other agents: Pipe tools "
                                "over the SSH tunnel and the Pipe token. Stdout = the MCP protocol."))

    # ─── Sesja ─────────────────────────────────────────────────────────────
    parser.add_argument(
        "--token",
        default=os.getenv("AGENT_TOKEN", ""),
        help=tr("Token autoryzacji backendu (jesli AGENT_TOKEN jest ustawiony w .env serwera). "
                "Mozna tez ustawic przez zmienna srodowiskowa AGENT_TOKEN.",
                "Backend authorization token (if AGENT_TOKEN is set in the server's .env). "
                "Can also be set with the AGENT_TOKEN environment variable."),
        metavar="TOKEN",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=tr("ID sesji (domyślnie: losowy UUID)", "Session id (default: a random UUID)"),
        metavar="ID",
    )

    args = parser.parse_args()

    session_id = args.session or str(uuid.uuid4())
    global OPEN_IMAGES, BRIDGE_MODE, console, WEB_PORT, WEB_OPEN_BROWSER
    OPEN_IMAGES = args.open
    WEB_PORT, WEB_OPEN_BROWSER = args.web_port, not args.no_browser
    runner = run_web if args.mode == "web" else run_cli
    if args.mcp:
        BRIDGE_MODE = True
        console = Console(stderr=True)
        runner = run_mcp_bridge
        if not args.no_tunnel and args.local_port == DEFAULT_LOCAL_PORT:
            args.local_port = _free_port()           # nie koliduj z otwartym CLI na 7379

    # ─── Kubernetes: port-forward zamiast tunelu SSH ──────────────────────
    if args.kube:
        args.local_port = _own_local_port(args.local_port)
        forward = KubePortForward(args.kube, args.local_port, args.kube_context)
        client = RemoteClient(session_id=session_id, host="127.0.0.1", port=args.local_port, token=args.token)
        console.print(f"[dim]kubectl port-forward svc/pipe (namespace {args.kube})...[/dim]")
        try:
            forward.start()
            if not forward.wait_ready(timeout=20.0):
                console.print(tr("[red]Port-forward nie odpowiada. Sprawdź: kubectl -n ",
                                 "[red]Port-forward does not respond. Check: kubectl -n ")
                              + f"{args.kube} get pods,svc[/red]")
                sys.exit(1)
            asyncio.run(runner(client, f"kubernetes/{args.kube}"))
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)
        except KeyboardInterrupt:
            pass
        finally:
            forward.stop()
        return

    # ─── Tryb bez tunelu ───────────────────────────────────────────────────
    if args.no_tunnel:
        client = RemoteClient(
            session_id=session_id,
            host="127.0.0.1",
            port=args.local_port,
            token=args.token,
        )
        display_host = f"127.0.0.1:{args.local_port} ({tr('lokalny tunel', 'local tunnel')})"
        try:
            asyncio.run(runner(client, display_host))
        except KeyboardInterrupt:
            pass
        return

    # ─── Tryb SSH tunel ────────────────────────────────────────────────────
    if not args.host:
        console.print(tr(
            "[red]Brak adresu serwera.[/red]\n\n"
            "Podaj --host user@twoj-serwer lub ustaw zmienną VPS_HOST.\n\n"
            "[dim]Przykład: python cli.py --host root@serwer.example.com[/dim]",
            "[red]No server address.[/red]\n\n"
            "Pass --host user@your-server or set the VPS_HOST variable.\n\n"
            "[dim]Example: python cli.py --host root@server.example.com[/dim]"
        ))
        sys.exit(1)

    args.local_port = _own_local_port(args.local_port)
    tunnel = SSHTunnel(
        host=args.host,
        ssh_port=args.ssh_port,
        local_port=args.local_port,
        remote_port=args.remote_port,
        identity_file=args.key,
    )

    client = RemoteClient(
        session_id=session_id,
        host="127.0.0.1",
        port=args.local_port,
        token=args.token,
    )

    console.print(tr(f"[dim]Laczę z {args.host} przez SSH...[/dim]", f"[dim]Connecting to {args.host} over SSH...[/dim]"))
    console.print(tr("[dim](Jesli pojawi sie monit o haslo SSH, wpisz je ponizej)[/dim]",
                     "[dim](If an SSH password prompt appears, type it below)[/dim]"))

    try:
        tunnel.start()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)

    try:
        # Czekaj az tunel bedzie gotowy — BEZ spinnera zeby SSH mogl pytac o haslo
        console.print(tr("[dim]Zestawiam tunel SSH...[/dim]", "[dim]Setting up the SSH tunnel...[/dim]"))
        ready = tunnel.wait_ready(timeout=20.0)

        if not ready:
            console.print(tr(
                "[red]Tunel SSH nie odpowiada (timeout 20s).\n"
                "Sprawdz:\n"
                "  * czy wpisales haslo SSH (jesli bylo wymagane)\n"
                "  * czy backend dziala na serwerze: docker logs backend_vps-agent_1\n"
                "  * czy port 7379 jest widoczny: docker ps"
                "[/red]",
                "[red]The SSH tunnel does not respond (20 s timeout).\n"
                "Check:\n"
                "  * that you typed the SSH password (if it was required)\n"
                "  * that the backend runs on the server: docker logs backend_vps-agent_1\n"
                "  * that port 7379 is visible: docker ps"
                "[/red]"
            ))
            sys.exit(1)

        asyncio.run(runner(client, args.host))

    except RuntimeError as exc:
        console.print(tr(f"[red]Błąd tunelu SSH: {exc}[/red]", f"[red]SSH tunnel error: {exc}[/red]"))
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        console.print(tr("[dim]Zamykam tunel SSH...[/dim]", "[dim]Closing the SSH tunnel...[/dim]"))
        tunnel.stop()


if __name__ == "__main__":
    main()
