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
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import appsetup          # clients/cli/appsetup.py — pierwsze uruchomienie `pipe web` (lokalnie albo na serwerze)


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
    from rich import box
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.prompt import Confirm as _RichConfirm, InvalidResponse, Prompt
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    from rich.spinner import Spinner
    from rich.live import Live
    from rich.markup import escape
except ImportError:
    print(tr("Błąd: zainstaluj zależności: pip install -r requirements.txt",
             "Error: install the dependencies: pip install -r requirements.txt"))
    sys.exit(1)

YES_WORDS = {"t", "tak", "y", "yes", "ok"}
NO_WORDS = {"n", "nie", "no"}


class Confirm(_RichConfirm):
    """
    Pytanie tak/nie w jezyku CLI: po polsku [t/n] — i przyjmuje "tak", bo tak uczy README, web i Telegram
    (rich przyjmowal tylko y/n i odpowiadal po angielsku "Please enter Y or N").
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.choices = [tr("t", "y"), "n"]
        self.validate_error_message = tr("[prompt.invalid]Wpisz t (tak) albo n (nie)", "[prompt.invalid]Type y (yes) or n (no)")

    def process_response(self, value: str) -> bool:
        word = value.strip().lower()
        if word in YES_WORDS:
            return True
        if word in NO_WORDS:
            return False
        raise InvalidResponse(self.validate_error_message)

    def render_default(self, default) -> Text:
        return Text(f"({tr('t', 'y') if default else 'n'})", "prompt.default")


try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:      # starsza instalacja bez prompt_toolkit — zwykly prompt, bez podpowiedzi komend
    PromptSession = None

# Kolor tylko z funkcja: bursztyn = potwierdzenie, zielony / czerwony = wynik; reszta w tonach terminala.
PIPE_THEME = Theme({"markdown.code": "bold", "prompt.choices": "bold", "prompt.default": "dim",
                    "markdown.item.bullet": "dim", "markdown.link_url": "underline"})
console = Console(theme=PIPE_THEME)
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
REMINDER_POLL: float = 10.0              # co ile CLI czekajace na wpis sprawdza, czy przyszla wiadomosc od Pipe
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
        batch: bool = False,
    ) -> None:
        self.host = host
        self.ssh_port = ssh_port
        self.local_port = local_port
        self.remote_port = remote_port
        self.identity_file = identity_file
        self.batch = batch          # bez pytania o haslo (polaczenie zapisane w `pipe web` — wlasny klucz Pipe)
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
        if BRIDGE_MODE or self.batch:
            # Most MCP: stdin niesie JSON-RPC klienta — ssh nie moze go czytac ani pytac o haslo
            cmd[1:1] = ["-n", "-o", "BatchMode=yes"]

        cmd.append(self.host)

        # NIE przekierowujemy stdin — SSH może zapytać o hasło w terminalu
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL if BRIDGE_MODE or self.batch else None,   # dziedzicz stdin (haslo), chyba ze most MCP
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
        # polaczono ponownie po zerwaniu (np. restart backendu) — stan sesji (YOLO) trzeba odczytac na nowo
        self.reconnected = False
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

    async def send_message(self, message: str, attachments: list[dict] | None = None) -> list[dict]:
        """Wysyła wiadomość (opcjonalnie z plikami: [{name, mime, data}]) i zwraca listę odpowiedzi."""
        request = {"message": message, "session_id": self.session_id, "interface": self.interface}
        if attachments:
            request["attachments"] = attachments
        return await self._send(request)

    async def send_confirm(self, confirmed: bool) -> list[dict]:
        """Wysyła potwierdzenie/odmowę."""
        return await self._send({"confirm": confirmed, "session_id": self.session_id})

    async def send_command(self, command: str, **fields) -> list[dict]:
        """Zadanie {"command": ...} — lista w docs/protocol.md."""
        return await self._send({"command": command, "session_id": self.session_id,
                                 "interface": self.interface, **fields})

    async def probe(self, command: str, **fields) -> list[dict]:
        """Jak send_command, ale jedna cicha proba z limitem czasu — do odpytywania w tle, gdy backend moze nie zyc."""
        data = {"command": command, "session_id": self.session_id, "interface": self.interface, **fields}
        if self.token:
            data["token"] = self.token
        responses: list[dict] = []
        try:
            if not self._writer or not self._reader:
                await asyncio.wait_for(self.connect(), 5)
            await asyncio.wait_for(self._exchange(data, responses), 10)
        except (OSError, EOFError):
            await self.disconnect()          # polowiczna wymiana zostawilaby smieci w strumieniu
            raise
        return responses

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
                    self.reconnected = True
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
                console.print(f"[dim]  · {escape(response['event'].get('text', ''))}[/dim]")
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
    console.print(Rule(f"[bold]{escape(caption)}[/bold]", style="dim", align="left"))
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
    """Naglowek startowy: znak, wersja, serwer i najczestsze komendy — bez ramki i ASCII-artu."""
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    grid.add_row(tr("serwer", "server"), f"[green]●[/green] [bold]{escape(host)}[/bold]")
    grid.add_row(tr("na start", "try"), tr("/status  /nowosci  /projekty  /pomoc  [dim]albo po prostu napisz, co mam zrobić[/dim]",
                                           "/status  /news  /projects  /help  [dim]or just write what I should do[/dim]"))
    console.print()
    console.print("[bold reverse] pipe [/bold reverse]  "
                  + tr("[dim]Autonomiczny agent AI do zarządzania serwerem Linux | v0.44.0[/dim]",
                       "[dim]Autonomous AI agent for managing a Linux server | v0.44.0[/dim]"), highlight=False)
    console.print()
    console.print(Padding(grid, (0, 0, 0, 1)), highlight=False)
    console.print()


# Potwierdzenie: komenda z handlerow (core/handlers/common.py) i plan bezpiecznika (core/safety.py Plan.describe).
FUSE_HEAD = re.compile(r"^(Bezpiecznik|Safety fuse):\s*$")
_CODE_TAIL = re.compile(r"^(?P<lead>.*?:)\s*(?P<fence>`+)(?P<code>.+)(?P=fence)\s*$")
_CONFIRM_LEAD = re.compile(r"^(?P<subject>.*?)\s+(?:wymaga potwierdzenia|requires confirmation)\s*[:.]?\s*(?P<rest>.*)$", re.IGNORECASE)
_GENERIC_SUBJECT = re.compile(r"^(operacja|operation|komenda|command)$", re.IGNORECASE)


def _confirm_subject(lead: str) -> tuple[str, str]:
    """("Zapis pliku wymaga potwierdzenia: ...") -> ("Zapis pliku", reszta); temat "Operacja" nic nie mowi -> ""."""
    match = _CONFIRM_LEAD.match(lead.strip())
    if not match:
        return "", lead
    subject = "" if _GENERIC_SUBJECT.match(match["subject"]) else match["subject"]
    return subject, match["rest"].lstrip("—–- ")


def _code_span(raw: str) -> str:
    """Tresc kodu inline wg CommonMark: po jednej spacji z brzegow znika tylko wtedy, gdy sa po obu stronach."""
    if len(raw) > 1 and raw[0] == " " and raw[-1] == " " and raw.strip():
        return raw[1:-1]
    return raw


def _fuse_mark(key: str) -> tuple[str, str]:
    low = key.lower()
    if low.startswith(("jesli", "if ")):
        return "↺", "green"
    if low.startswith(("cofniecie", "undo")):
        return "↩", "dim"
    if low.startswith(("uwaga", "note")):
        return "!", "yellow"
    return "✓", "green"


def _confirmation(text: str) -> Panel:
    """
    [POTWIERDZ] jako panel: wstep, komenda jednoliniowa jako zwykly tekst (bez Markdowna — to, co widac,
    zostanie wykonane znak w znak), reszta jako Markdown (komenda wieloliniowa jest tam blokiem kodu)
    i plan bezpiecznika jako tabela.
    """
    lines = text.replace("[POTWIERDZ]", "", 1).strip().split("\n")
    head, rows = "", []
    at = next((i for i, line in enumerate(lines) if FUSE_HEAD.match(line.strip())), -1)
    if at >= 0:
        head = lines[at].strip().rstrip(":")
        end = at + 1
        while end < len(lines) and lines[end].startswith("- "):
            rows.append(lines[end][2:])
            end += 1
        del lines[at:end]
    parts: list = []
    match = _CODE_TAIL.match(lines[0]) if lines else None
    if match:
        subject, _ = _confirm_subject(match["lead"].rstrip(":"))
        if subject:
            parts.append(Text(subject, style="dim"))
        parts.append(Padding(Text.assemble(("$ ", "dim"), (_code_span(match["code"]), "bold")), (0, 0, 1, 2)))
        rest = "\n".join(lines[1:]).strip()
    else:
        subject, first = _confirm_subject(lines[0] if lines else "")
        if subject:
            parts.append(Text(subject, style="dim"))
        rest = "\n".join([first] + lines[1:]).strip()
    if rest:
        parts.append(Markdown(rest))
    if rows:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(style="dim", max_width=30)
        table.add_column()
        for row in rows:
            key, sep, value = row.partition(": ")
            if not sep:
                key, value = "", row
            mark, style = _fuse_mark(key)
            table.add_row(Text(mark, style=style), Text(key), Text(value.replace("; ", "\n")))
        parts.append(Text(head, style="bold"))
        parts.append(Padding(table, (0, 0, 0, 1)))
    return Panel(Group(*parts), title=tr(" Potrzebuję Twojej zgody ", " I need your approval "), title_align="left",
                 border_style="yellow", box=box.ROUNDED, padding=(1, 2, 0, 2))


def _print_response(text: str, status: str) -> None:
    """Wyświetla odpowiedź agenta w estetycznym formacie terminalowym."""
    text = text.strip()
    if not text:
        return

    # Zamiana tagow statusowych na kolorowe oznaczenia Rich
    # [SUKCES] usuniety -- nie wyswietlamy go
    text = text.replace("[SUKCES]", "")
    if status == "confirm":
        console.print(_confirmation(text))
        return
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
                tr("[yellow]Wykonać?[/yellow]", "[yellow]Run it?[/yellow]"),
                default=False,
            )
        except (KeyboardInterrupt, EOFError):
            confirmed = False
            console.print()

        if confirmed:
            console.print(tr("[green]✓[/green] [dim]Zatwierdzone — wykonuję...[/dim]", "[green]✓[/green] [dim]Approved — running...[/dim]"))
        else:
            console.print(tr("[dim]✕ Odrzucone — nic nie zmieniam.[/dim]", "[dim]✕ Rejected — I change nothing.[/dim]"))

        confirm_responses = await client.send_confirm(confirmed)
        await _handle_responses(confirm_responses, client)
        return True

    return False


# ─── Stan serwera i skrzynka (Pipe pisze pierwszy) ───────────────────────────

_LEVEL_STYLE = {"ok": "white", "warn": "yellow", "crit": "red"}
_STATUS_DOT = {"ok": "[green]●[/green]", "warning": "[yellow]▲[/yellow]", "critical": "[red]■[/red]"}


def _bar(pct: float, width: int = 16) -> str:
    filled = max(0, min(width, round(float(pct) * width / 100)))
    return "━" * filled + "[dim]" + "━" * (width - filled) + "[/dim]"


def _print_overview(data: dict, compact: bool = False) -> None:
    """Stan serwera bez LLM (komenda overview): naglowek zwyklymi slowami i paski procesora, pamieci, dysku."""
    dot = _STATUS_DOT.get(str(data.get("status")), "[green]●[/green]")
    console.print(f"{dot} [bold]{escape(str(data.get('headline', '')))}[/bold]", highlight=False)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(style="dim")
    for gauge in data.get("gauges") or []:
        style = _LEVEL_STYLE.get(str(gauge.get("level")), "white")
        table.add_row(escape(str(gauge.get("label", ""))), f"[{style}]{_bar(gauge.get('pct', 0))}[/{style}]",
                      f"[{style}]{int(gauge.get('pct', 0))}%[/{style}]", escape(str(gauge.get("value", ""))))
    console.print(Padding(table, (0, 0, 0, 2)), highlight=False)
    if data.get("facts") and not compact:
        console.print(Padding(f"[dim]{escape(' · '.join(data['facts']))}[/dim]", (0, 0, 0, 2)), highlight=False)
    if data.get("unread"):
        console.print(Padding(tr(f"📬 Nowe wiadomości od Pipe: [bold]{int(data['unread'])}[/bold] — /nowosci",
                                 f"📬 New messages from Pipe: [bold]{int(data['unread'])}[/bold] — /news"), (0, 0, 0, 2)))


def _feed_item(index: int, item: dict, full: bool = True) -> None:
    """Jedna wiadomosc ze skrzynki: naglowek, tresc (Markdown), przyciski jako 1a, 1b..."""
    mark = {"critical": "[red]■[/red]", "warning": "[yellow]▲[/yellow]", "ok": "[green]✓[/green]"}.get(
        str(item.get("severity")), "[cyan]●[/cyan]")
    new = tr(" [bold blue]nowe[/bold blue]", " [bold blue]new[/bold blue]") if item.get("unread") else ""
    console.print(f"{mark} [bold]{index}. {escape(str(item.get('title', '')))}[/bold]{new} "
                  f"[dim]{escape(str(item.get('at', ''))[5:])}[/dim]", highlight=False)
    text = str(item.get("text") or "").strip()
    if full and text:
        console.print(Padding(Markdown(text if len(text) < 1200 else text[:1200] + " …"), (0, 0, 0, 3)))
    if item.get("done"):
        console.print(Padding(f"[green]✓ {escape(str(item['done']))}[/green]", (0, 0, 0, 3)))
    elif item.get("actions"):
        labels = "   ".join(f"[bold]{index}{chr(97 + n)}[/bold] {escape(str(a.get('label', '')))}"
                           for n, a in enumerate(item["actions"]))
        console.print(Padding(labels, (0, 0, 0, 3)), highlight=False)


async def _inbox(client: "RemoteClient") -> None:
    """/nowosci: co Pipe napisal sam; wybor akcji (np. 1a) wykonuje ja w backendzie — jak przycisk w Telegramie."""
    data = _response_data(await client.send_command("feed", args="seen"))
    items = (data.get("items") or [])[:8]
    if not items:
        console.print(tr("[dim]Skrzynka jest pusta — nic nie wymaga Twojej uwagi. Napiszę sam, gdy coś zauważę.[/dim]",
                         "[dim]The inbox is empty — nothing needs your attention. I will write when I notice something.[/dim]"))
        return
    for index, item in enumerate(items, start=1):
        _feed_item(index, item, full=index <= 3 or bool(item.get("unread")))
        console.print()
    if not any(i.get("actions") and not i.get("done") for i in items):
        return
    try:
        choice = Prompt.ask(tr("[yellow]Akcja (np. 1a), Enter — nic[/yellow]", "[yellow]Action (e.g. 1a), Enter — none[/yellow]"),
                            default="", show_default=False).strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    match = re.fullmatch(r"(\d+)\s*([a-z])", choice)
    if not match:
        return
    position, letter = int(match.group(1)) - 1, ord(match.group(2)) - 97
    if not 0 <= position < len(items) or not 0 <= letter < len(items[position].get("actions") or []):
        console.print(tr("[dim]Nie ma takiej akcji.[/dim]", "[dim]No such action.[/dim]"))
        return
    item = items[position]
    responses = await client.send_command("feed_act", id=item["id"], action=item["actions"][letter].get("index", letter))
    await _handle_responses(responses, client)
    result = next((r.get("data") for r in reversed(responses) if isinstance(r.get("data"), dict)), None)
    if result and result.get("text"):
        console.print(Text(str(result["text"]), overflow="fold"), highlight=False)
    elif result and result.get("done"):
        console.print(f"[green]✓[/green] {escape(str(result['done']))}")


def _prefs_fields(args: str) -> dict:
    """'krytyczne cisza 22-7 dziennie 1' -> {"level": "critical", "quiet": "22:00-07:00", "per_day": 1}."""
    fields: dict = {}
    words = args.lower().split()
    levels = {"wszystko": "all", "all": "all", "wazne": "important", "ważne": "important", "important": "important",
              "krytyczne": "critical", "critical": "critical"}
    i = 0
    while i < len(words):
        word = words[i]
        if word in levels:
            fields["level"] = levels[word]
        elif word in ("cisza", "quiet") and i + 1 < len(words):
            i += 1
            value = words[i]
            hours = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?", value)
            if value in ("off", "wyl", "wył", "nie", "no"):
                fields["quiet"] = "off"
            elif hours:
                h1, m1, h2, m2 = hours.groups()
                fields["quiet"] = f"{int(h1):02d}:{m1 or '00'}-{int(h2):02d}:{m2 or '00'}"
            else:
                return {}
        elif word in ("dziennie", "daily") and i + 1 < len(words) and words[i + 1].isdigit():
            i += 1
            fields["per_day"] = int(words[i])
        else:
            return {}
        i += 1
    return fields


# ─── Komendy "/" ──────────────────────────────────────────────────────────────

SCAN_WORDS = ("aktualizuj", "odswiez", "odśwież", "skanuj", "update", "refresh", "scan")

# Angielskie nazwy komend — dzialaja w obu jezykach obok polskich.
COMMAND_ALIASES = {
    "report": "raport", "digest": "raport", "changes": "zmiany", "chart": "wykres", "health": "zdrowie",
    "audit": "audyt", "map": "mapa", "directory": "katalogi", "dirs": "katalogi", "skills": "skille",
    "alerts": "alerty", "routines": "rutyny", "targets": "cele", "journal": "dziennik", "undo": "cofnij",
    "approvals": "zgody", "cost": "koszt", "usage": "koszt", "history": "historia", "incidents": "incydenty",
    "reminders": "przypomnienia", "update": "aktualizuj", "language": "jezyk", "lang": "jezyk", "język": "jezyk",
    "providers": "providerzy", "provider": "providerzy", "file": "plik", "attach": "plik", "patch": "zalataj", "vulns": "podatnosci",
    "new": "nowa", "news": "nowosci", "inbox": "nowosci", "projects": "projekty", "notifications": "powiadomienia",
    "cleanup": "sprzataj", "download": "pobierz", "view": "widok",
}

# Podpowiedzi po wpisaniu "/": (nazwa polska, nazwa angielska, opis polski, opis angielski).
COMMANDS = [
    ("status", "status", "stan serwera — od razu, bez AI", "server status — instant, no AI"),
    ("nowosci", "news", "co Pipe napisał sam (skrzynka) i akcje", "what Pipe wrote on its own (inbox) and actions"),
    ("projekty", "projects", "projekty w warsztacie", "workshop projects"),
    ("sprzataj", "cleanup", "zwolnij miejsce na dysku (bez AI)", "free up disk space (no AI)"),
    ("web", "web", "Pipe w przeglądarce z telefonu i komputera: /web wlacz, /web wylacz, /web wpusc <kod>",
     "Pipe in the browser on your phone and computer: /web on, /web off, /web approve <code>"),
    ("pobierz", "download", "skille i pluginy z repozytorium: /pobierz skill <wlasciciel/repo|URL>, /pobierz plugin <...>",
     "skills and plugins from a repository: /download skill <owner/repo|URL>, /download plugin <...>"),
    ("mcp", "mcp", "serwery MCP: /mcp dodaj <nazwa> <npx ...|url>, /mcp usun <nazwa>",
     "MCP servers: /mcp add <name> <npx ...|url>, /mcp remove <name>"),
    ("powiadomienia", "notifications", "kiedy Pipe ma powiadamiać: /powiadomienia wazne cisza 22-7",
     "when Pipe should notify you: /notifications important quiet 22-7"),
    ("raport", "report", "raport: stan, zmiany, certyfikaty, backupy", "report: health, changes, certificates, backups"),
    ("zmiany", "changes", "co się zmieniło na serwerze (np. /zmiany 3d)", "what changed on the server (e.g. /changes 3d)"),
    ("wykres", "chart", "wykres load / ram / dysk (np. /wykres ram 7d)", "chart of load / ram / disk (e.g. /chart ram 7d)"),
    ("zdrowie", "health", "certyfikaty, strony, DNS, backupy", "certificates, sites, DNS, backups"),
    ("audyt", "audit", "audyt bezpieczeństwa z poprawkami", "security audit with fixes"),
    ("podatnosci", "vulns", "znane podatności z opisami: pakiety, obrazy, serwery, aplikacje",
     "known vulnerabilities with descriptions: packages, images, servers, apps"),
    ("zalataj", "patch", "załataj od razu wszystko; /zalataj obrazy, /zalataj serwer, /zalataj aplikacje, /zalataj serwery",
     "patch everything right away; /patch images, /patch server, /patch apps, /patch servers"),
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
    ("nowa", "new", "nowa rozmowa od czystej karty (wiedza o serwerze zostaje)",
     "new conversation from a clean slate (server knowledge stays)"),
    ("vibe", "vibe", "styl rozmowy; /vibe reset", "conversation style; /vibe reset"),
    ("dziennik", "journal", "zatwierdzone zmiany z kopiami", "approved changes with backups"),
    ("cofnij", "undo", "cofnij ostatnią (albo wybraną) zmianę", "undo the last (or the chosen) change"),
    ("zgody", "approvals", "operacje agentów MCP czekające na zgodę", "MCP agent operations waiting for approval"),
    ("koszt", "cost", "zużycie tokenów i koszt LLM", "token usage and LLM cost"),
    ("historia", "history", "ostatnie wpisy audit logu", "latest audit-log entries"),
    ("aktualizuj", "update", "zaktualizuj Pipe na serwerze; /aktualizuj sprawdz", "update Pipe on the server; /update check"),
    ("providerzy", "providers", "providerzy LLM: klucz, model, przełączenie (/providerzy groq)",
     "LLM providers: key, model, switching (/providers groq)"),
    ("plik", "file", "wyślij plik albo zrzut ekranu: /plik <ścieżka> [pytanie], w wiadomości @ścieżka",
     "send a file or screenshot: /file <path> [question], in a message @path"),
    ("yolo", "yolo", "zmiany bez pytania o TAK: /yolo on, /yolo off", "changes without asking for YES: /yolo on, /yolo off"),
    ("jezyk", "language", "język Pipe: /jezyk en albo /jezyk pl", "Pipe's language: /language pl or /language en"),
    ("widok", "view", "widok: /widok prosty (rozmowa, skille, dodatki) albo /widok techniczny (serwer, schemat)",
     "view: /view simple (chat, skills, add-ons) or /view technical (the server, the diagram)"),
    ("pomoc", "help", "lista komend", "list of commands"),
    ("exit", "exit", "wyjście", "quit"),
]

# Komendy, ktore dzialaja, ale nie ma ich w podpowiedziach ani w /pomoc — to samo daja /status i /nowosci
# (alerty, strony i certyfikaty) oraz /dziennik (zmiany). Podpowiedz pojawia sie tylko po wpisaniu pelnej nazwy.
HIDDEN_COMMANDS = frozenset({"alerty", "zdrowie", "historia"})
# Widok prosty (/widok prosty, wspolny dla CLI, pipe web i Telegrama): tych komend nie ma w podpowiedziach ani
# w /pomoc, ale dzialaja po wpisaniu — tak samo w clients/webui/static/app.js i clients/telegram/tg_format.py.
TECH_COMMANDS = frozenset({"projekty", "raport", "zmiany", "wykres", "audyt", "podatnosci", "zalataj", "sprzataj",
                           "mapa", "mermaid", "server", "katalogi", "incydenty", "rutyny", "cele", "dziennik", "zgody",
                           "yolo", "aktualizuj"})
_view: str = "tech"      # widok z backendu (komenda `view`), odczytany po polaczeniu

HELP_TEXT = """Nie musisz znać komend — napisz, co mam zrobić, np. „czemu sklep działa wolno?”, „postaw mi tę aplikację
z GitHuba” albo „napisz skrypt, który co noc robi kopię bazy”. Pipe sam pisze, gdy coś wymaga uwagi.

**Na co dzień**

- `/status` — stan serwera, od razu i bez AI
- `/nowosci` — co Pipe napisał sam: alerty, podpowiedzi, ustalenia (z akcjami: wpisz np. `1a`)
- `/projekty` — projekty w warsztacie (sklonowane repozytoria, Twoje skrypty)
- `/raport` — stan, zmiany od wczoraj, certyfikaty, backupy (przychodzi też sam co rano)
- `/zmiany [24h|3d]` — co się zmieniło na serwerze
- `/mapa` — schemat serwera (`/mermaid` — jego kod)
- `/wykres [load|ram|dysk] [24h|7d]` — wykres z historii

**Bezpieczeństwo i zmiany**

- `/audyt` — ocena bezpieczeństwa z gotowymi poprawkami („napraw 1”)
- `/podatnosci` — znane podatności, każda z krótkim opisem
- `/zalataj [obrazy|serwer|serwery]` — łata od razu: pakiety, obrazy kontenerów, zdalne serwery; sprząta po sobie
- `/sprzataj` — zwolnij miejsce: obrazy bez tagu, cache budowania, pobrane paczki
- `/dziennik` — zatwierdzone zmiany; `/cofnij [id]` — cofnij ostatnią albo wybraną
- `/zgody` — operacje innych agentów (MCP) czekające na Twoją zgodę

**Pamięć i automatyzacja**

- `/server` — co wiem o serwerze (`/server aktualizuj` — zbadaj go ponownie)
- `/skille` — zapisane procedury, każda z własną komendą
- `/rutyny`, `/przypomnienia` — zadania według harmonogramu i jednorazowe przypomnienia
- `/incydenty` — co się działo, jaka była przyczyna i co pomogło
- `/katalogi`, `/cele`, `/vibe` — repozytoria, zdalne serwery, Twój styl rozmowy

**Ustawienia**

- `/powiadomienia [wszystko|wazne|krytyczne] [cisza 22-7|cisza off] [dziennie 3]` — kiedy Pipe ma pisać sam
- `/providerzy` — model AI: klucz, model, przełączenie (`/providerzy groq`, `/providerzy model`)
- `/jezyk [pl|en]` — język Pipe
- `/web [wlacz|wylacz]` — Pipe w przeglądarce z telefonu i komputera pod adresem HTTPS; `/web wpusc <kod>` — wpuść urządzenie
- `/widok [prosty|techniczny]` — widok Pipe we wszystkich aplikacjach (prosty: rozmowa, skille, dodatki)
- `/yolo [on|off]` — zmiany bez pytania o TAK w tej rozmowie (kopie i `/cofnij` działają dalej)
- `/koszt`, `/aktualizuj` — koszt modelu, aktualizacja Pipe

**W tej rozmowie**

- `/nowa` — nowa rozmowa od czystej karty (wiedza o serwerze zostaje)
- `/plik <ścieżka> [pytanie]` albo `@ścieżka` w wiadomości — wyślij plik lub zrzut ekranu
- `/pomoc` — ta lista, `/exit` — wyjście

Tab podpowiada komendy i ścieżki. Do komendy skilla możesz dopisać wskazówki: `/odnow_certyfikat tylko dla example.com`."""

HELP_TEXT_EN = """You don't need to know the commands — write what I should do, e.g. "why is the shop slow?", "set up this app
from GitHub for me" or "write a script that backs up the database every night". Pipe writes on its own when
something needs attention.

**Every day**

- `/status` — server status, instant and without AI
- `/news` — what Pipe wrote on its own: alerts, suggestions, findings (with actions: type e.g. `1a`)
- `/projects` — workshop projects (cloned repositories, your scripts)
- `/report` — health, changes since yesterday, certificates, backups (also arrives every morning)
- `/changes [24h|3d]` — what changed on the server
- `/map` — diagram of the server (`/mermaid` — its source)
- `/chart [load|ram|disk] [24h|7d]` — chart from the history

**Security and changes**

- `/audit` — security score with ready-made fixes ("fix 1")
- `/vulns` — known vulnerabilities, each briefly explained
- `/patch [images|server|servers]` — patches right away: packages, container images, remote servers; cleans up after itself
- `/cleanup` — free up space: untagged images, build cache, downloaded packages
- `/journal` — approved changes; `/undo [id]` — undo the last or the chosen one
- `/approvals` — operations of other agents (MCP) waiting for your approval

**Memory and automation**

- `/server` — what I know about the server (`/server update` — explore it again)
- `/skills` — saved procedures, each with its own command
- `/routines`, `/reminders` — scheduled tasks and one-off reminders
- `/incidents` — what happened, what the cause was and what helped
- `/directory`, `/targets`, `/vibe` — repositories, remote servers, your conversation style

**Settings**

- `/notifications [all|important|critical] [quiet 22-7|quiet off] [daily 3]` — when Pipe writes on its own
- `/providers` — the AI model: key, model, switching (`/providers groq`, `/providers model`)
- `/language [pl|en]` — Pipe's language
- `/web [on|off]` — Pipe in the browser on your phone and computer at an HTTPS address; `/web approve <code>` — let a device in
- `/view [simple|technical]` — Pipe's view in every app (simple: chat, skills, add-ons)
- `/yolo [on|off]` — changes without asking for YES in this conversation (backups and `/undo` still work)
- `/cost`, `/update` — model cost, updating Pipe

**In this conversation**

- `/new` — new conversation from a clean slate (server knowledge stays)
- `/file <path> [question]` or `@path` in a message — send a file or a screenshot
- `/help` — this list, `/exit` — quit

Tab completes commands and paths. You can add hints to a skill command: `/renew_certificate only for example.com`."""

HELP_SIMPLE = """Nie musisz znać komend — napisz zwykłymi słowami, w czym pomóc, np. „przypomnij mi jutro o 9 o fakturze”,
„streść ten plik” albo „podłącz mi kalendarz”. Możesz też wysłać plik: `/plik <ścieżka>` albo `@ścieżka`.

- `/status` — czy wszystko działa, od razu i bez AI
- `/nowosci` — co Pipe napisał sam (z akcjami: wpisz np. `1a`)
- `/skille` — gotowe umiejętności, każda z własną komendą
- `/pobierz skill <właściciel/repo>` — nowe umiejętności z GitHuba; `/mcp` — dodatki do aplikacji zewnętrznych
- `/przypomnienia` — Twoje przypomnienia
- `/cofnij` — cofnij ostatnią zmianę
- `/powiadomienia`, `/providerzy`, `/jezyk`, `/koszt` — ustawienia
- `/widok techniczny` — więcej o serwerze: schemat, podatności, zdalne serwery
- `/nowa` — nowa rozmowa, `/exit` — wyjście"""

HELP_SIMPLE_EN = """You don't need the commands — write in plain words what to help with, e.g. "remind me tomorrow at 9 about
the invoice", "summarise this file" or "connect my calendar". You can also send a file: `/file <path>` or `@path`.

- `/status` — is everything working, instant and without AI
- `/news` — what Pipe wrote on its own (with actions: type e.g. `1a`)
- `/skills` — ready-made skills, each with its own command
- `/download skill <owner/repo>` — new skills from GitHub; `/mcp` — add-ons for external apps
- `/reminders` — your reminders
- `/undo` — undo the last change
- `/notifications`, `/providers`, `/language`, `/cost` — settings
- `/view technical` — more about the server: the diagram, vulnerabilities, remote servers
- `/new` — new conversation, `/exit` — quit"""


def _severity(value) -> str:
    """Waga alertu slowem i kolorem (backend podaje critical / warning / info)."""
    words = {"critical": (tr("krytyczny", "critical"), "bold red"), "warning": (tr("ostrzeżenie", "warning"), "yellow"),
             "info": (tr("informacja", "info"), "cyan")}
    word, style = words.get(str(value), (str(value or "alert"), "dim"))
    return f"[{style}]{escape(word)}[/{style}]"


def _print_list(title: str, items: list[str], empty: str) -> None:
    console.print(f"[bold]{escape(title)}[/bold]")
    if not items and empty:
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
        hidden = name_pl in HIDDEN_COMMANDS or (_view == "simple" and name_pl in TECH_COMMANDS)
        if hidden and query not in (name_pl, name_en, *aliases):
            continue
        if match(name_pl, name_en, *aliases):
            options.append((tr(name_pl, name_en), tr(desc_pl, desc_en)))
    for skill in _skills if skills is None else skills:
        if skill.get("command") and match(skill["command"], skill.get("name", "")):
            options.append((skill["command"], skill.get("description", "")))
    # dokladne trafienie na gorze, potem pasujace poczatkiem
    return sorted(options, key=lambda o: 0 if o[0].lower() == query else 1 if o[0].lower().startswith(query) else 2)


# ─── Załączniki: /plik <ścieżka> i @ścieżka ─────────────────────────────────────
# Limity jak w backendzie (backend/core/attachments.py) — za duzy plik odrzucamy, zanim pojdzie przez tunel.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
# @sciezka na poczatku albo po spacji (adres e-mail user@host sie nie liczy); w cudzyslowie moze miec spacje
AT_PATH = re.compile(r"""(?:(?<=\s)|^)@(?:"([^"]+)"|'([^']+)'|(\S+))""")
PATH_OPTIONS_SHOWN = 60


class AttachmentError(ValueError):
    """Pliku nie da sie wyslac (komunikat dla uzytkownika)."""


def _extract_files(text: str) -> tuple[str, list[Path]]:
    """
    (wiadomosc, pliki) z wpisu uzytkownika. `/plik <sciezka> [pytanie]` — plik musi istniec;
    `@sciezka` w zwyklej wiadomosci — tylko istniejace pliki (inaczej zostaje tekstem, np. @admin).
    """
    head, _, rest = text.partition(" ")
    name = COMMAND_ALIASES.get(head[1:].lower(), head[1:].lower()) if head.startswith("/") else ""
    if name == "plik":
        rest = rest.strip()
        if not rest:
            raise AttachmentError(tr("Podaj ścieżkę: /plik <ścieżka> [pytanie]", "Give a path: /file <path> [question]"))
        match = re.match(r"""^(?:"([^"]+)"|'([^']+)'|(\S+))\s*(.*)$""", rest, re.DOTALL)
        raw = next(group for group in match.groups()[:3] if group)
        path = Path(raw.lstrip("@")).expanduser()
        if not path.is_file():
            raise AttachmentError(tr(f"Nie ma takiego pliku: {raw}", f"No such file: {raw}"))
        message, files = _extract_files(match.group(4)) if match.group(4) else ("", [])
        return message, [path, *files]

    files: list[Path] = []

    def take(found: re.Match) -> str:
        raw = next(group for group in found.groups() if group)
        path = Path(raw).expanduser()
        if not path.is_file():
            return found.group(0)
        files.append(path)
        return path.name              # w tresci zostaje nazwa pliku — model wie, o ktory chodzi

    message = AT_PATH.sub(take, text).strip()
    return message, files


def _load_files(paths: list[Path]) -> list[dict]:
    """Pliki -> [{name, mime, data(base64)}] (pole `attachments` w docs/protocol.md)."""
    import mimetypes

    if len(paths) > MAX_FILES:
        raise AttachmentError(tr(f"Najwyżej {MAX_FILES} plików w jednej wiadomości.",
                                 f"At most {MAX_FILES} files in one message."))
    attachments, total = [], 0
    for path in paths:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise AttachmentError(tr(f"Plik {path.name} jest za duży ({size // 1048576} MB, limit 10 MB).",
                                     f"{path.name} is too large ({size // 1048576} MB, limit 10 MB)."))
        total += size
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(tr("Pliki razem przekraczają 20 MB.", "The files together exceed 20 MB."))
        attachments.append({"name": path.name, "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            "data": base64.b64encode(path.read_bytes()).decode("ascii")})
    return attachments


def _size_label(size: int) -> str:
    return f"{max(1, round(size / 1024))} kB" if size < 1048576 else f"{size / 1048576:.1f} MB"


def _path_options(partial: str) -> list[str]:
    """Podpowiedzi sciezek dla `/plik ...` i `@...`: pelne uzupelnienia `partial` (katalogi z '/' na koncu)."""
    expanded = os.path.expanduser(partial) if partial.startswith("~") else partial
    folder, prefix = os.path.split(expanded)
    base = Path(folder or ".")
    try:
        entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    shown_folder = partial[: len(partial) - len(prefix)]
    options = []
    for entry in entries:
        if not entry.name.lower().startswith(prefix.lower()) or (entry.name.startswith(".") and not prefix.startswith(".")):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        options.append(shown_folder + entry.name + ("/" if is_dir else ""))
        if len(options) >= PATH_OPTIONS_SHOWN:
            break
    return options


def _path_fragment(text: str) -> str | None:
    """Fragment sciezki pod kursorem, gdy uzytkownik wpisuje `/plik ...` albo `@...` — inaczej None."""
    head = text.split(" ", 1)[0]
    if head.startswith("/") and COMMAND_ALIASES.get(head[1:].lower(), head[1:].lower()) == "plik" and " " in text:
        rest = text.split(" ", 1)[1]
        if " " not in rest:
            return rest.lstrip("@")
    found = re.search(r"(?:^|\s)@(\S*)$", text)
    return found.group(1) if found else None


if PromptSession is not None:
    class SlashCompleter(Completer):
        """Lista komend i skilli pod promptem — pojawia sie po "/", Tab wstawia wybrana; po @ i /plik — sciezki."""

        def get_completions(self, document, complete_event):
            before = document.text_before_cursor
            fragment = _path_fragment(before)
            if fragment is not None:
                for option in _path_options(fragment):
                    yield Completion(option, start_position=-len(fragment), display=option.rstrip("/").rsplit("/", 1)[-1]
                                     + ("/" if option.endswith("/") else ""))
                return
            for name, description in _slash_options(before):
                yield Completion(name, start_position=1 - len(before),
                                 display="/" + name, display_meta=description)


def _prompt_reader(client: "RemoteClient | None" = None, **session_options):
    """
    Zwraca korutyne czytajaca jedna linie od uzytkownika. Z prompt_toolkit: podpowiedzi
    komend na zywo, Tab uzupelnia, strzalki przywoluja historie. Bez niego (albo gdy
    wejscie nie jest terminalem) — zwykly prompt.
    """
    if PromptSession is None or not (session_options or sys.stdin.isatty()):
        async def plain() -> str:
            return Prompt.ask(("[bold red]YOLO[/bold red] " if _yolo else "") + "[bold]›[/bold]")
        return plain

    session = PromptSession(completer=SlashCompleter(), complete_while_typing=True, **session_options)

    async def ask() -> str:
        message = ([("bold ansired", "YOLO "), ] if _yolo else []) + [("bold", "›"), ("", " ")]
        if client is None:
            return await session.prompt_async(message)
        # czekajac na wpis, CLI odbiera wiadomosci od Pipe; patch_stdout rysuje je nad promptem
        stop = asyncio.Event()
        watcher = asyncio.ensure_future(_watch_reminders(client, stop))
        try:
            with patch_stdout(raw=True):
                return await session.prompt_async(message)
        finally:
            stop.set()
            await watcher                    # nie przerywamy wymiany w polowie — polaczenie jest wspolne
    return ask


# Tryb YOLO tej sesji (stan zyje w backendzie; tu tylko znacznik w prompcie).
_yolo = False


async def _sync_yolo(client: "RemoteClient") -> None:
    """Stan YOLO z backendu — po starcie i po ponownym polaczeniu (restart backendu wylacza YOLO)."""
    global _yolo
    was = _yolo
    try:
        _yolo = bool(_response_data(await client.send_command("yolo")).get("yolo"))
    except Exception:
        _yolo = False          # starszy backend nie zna tej komendy
    if was and not _yolo:
        console.print(tr("[yellow]YOLO jest wyłączone (backend uruchomił się ponownie) — zmiany znów czekają na TAK.[/yellow]",
                         "[yellow]YOLO is off (the backend restarted) — changes wait for YES again.[/yellow]"))


async def _after_reconnect(client: "RemoteClient") -> None:
    if client.reconnected:
        client.reconnected = False
        if _yolo:
            await _sync_yolo(client)


PROVIDER_FORGET = ("zapomnij", "forget", "usun", "usuń", "remove")
PROVIDER_MODEL = ("model", "modele", "models")         # /providerzy model — tylko zmiana modelu
MODELS_SHOWN = 20


def _provider_line(index: int, provider: dict) -> str:
    """Wiersz listy providerow (bez kluczy — backend ich nie wysyla)."""
    if provider.get("active"):
        mark = "[green]✓[/green]"
    elif provider.get("ready"):
        mark = "[cyan]●[/cyan]"
    else:
        mark = "[dim]○[/dim]"
    model = provider.get("model") or provider.get("default_model") or ""
    if provider.get("ready"):
        state = tr("klucz z .env", "key from .env") if provider.get("key_source") == "env" else \
            tr("klucz dodany", "key added") if provider.get("key_source") == "web" else tr("bez klucza", "no key needed")
    else:
        state = tr("brak klucza", "no key") if provider.get("requires_key") else tr("wybierz model", "pick a model")
    active = tr(" — aktywny", " — active") if provider.get("active") else ""
    return (f"  {index:>2}. {mark} [bold]{escape(provider.get('name', ''))}[/bold] [dim]({escape(provider.get('id', ''))})[/dim]"
            f"  {escape(model)}  [dim]{escape(state)}{active}[/dim]")


def _print_providers(data: dict) -> None:
    active = data.get("active") or {}
    console.print(tr(f"[bold]Providerzy LLM[/bold] — agent korzysta z [bold]{escape(active.get('name', '?'))}[/bold] · {escape(active.get('model', ''))}",
                     f"[bold]LLM providers[/bold] — the agent is using [bold]{escape(active.get('name', '?'))}[/bold] · {escape(active.get('model', ''))}"))
    for index, provider in enumerate(data.get("providers", []), 1):
        console.print(_provider_line(index, provider))


def _find_provider(data: dict, wanted: str) -> dict | None:
    wanted = wanted.strip().lower()
    providers = data.get("providers", [])
    if wanted.isdigit() and 1 <= int(wanted) <= len(providers):
        return providers[int(wanted) - 1]
    return next((p for p in providers if p.get("id") == wanted or str(p.get("name", "")).lower() == wanted), None)


def _pick_model(models: list[str], answer: str) -> str:
    """Odpowiedz uzytkownika -> nazwa modelu: numer z listy, dokladna nazwa albo jedyne pasujace."""
    answer = answer.strip()
    if answer.isdigit() and 1 <= int(answer) <= min(len(models), MODELS_SHOWN):
        return models[int(answer) - 1]
    if answer in models or not answer:
        return answer
    matching = _matching_models(models, answer)
    return matching[0] if len(matching) == 1 else answer


def _matching_models(models: list[str], query: str) -> list[str]:
    query = query.strip().lower()
    return [m for m in models if query in m.lower()] if query else list(models)


def _print_models(models: list[str], current: str) -> None:
    for index, name in enumerate(models[:MODELS_SHOWN], 1):
        mark = tr(" [green]✓ obecny[/green]", " [green]✓ current[/green]") if name == current else ""
        console.print(f"  {index:>2}. {escape(name)}{mark}")
    if len(models) > MODELS_SHOWN:
        console.print(tr(f"[dim]  … i {len(models) - MODELS_SHOWN} więcej — wpisz fragment nazwy, żeby zawęzić listę[/dim]",
                         f"[dim]  … and {len(models) - MODELS_SHOWN} more — type part of a name to narrow the list[/dim]"))


async def _choose_model(client: "RemoteClient", provider: dict, key: str = "", query: str = "") -> str:
    """
    Model z listy, ktora zwraca provider (obecny zaznaczony i domyslny — Enter zostawia go bez zmian).
    `query` — fragment nazwy podany w komendzie: jedno trafienie wybiera od razu, kilka zaweza liste.
    Gdy listy nie da sie pobrac (np. lokalna Ollama nie odpowiada), nazwe mozna wpisac recznie.
    """
    console.print(tr(f"[dim]Pobieram modele {escape(provider.get('name', ''))}...[/dim]",
                     f"[dim]Fetching {escape(provider.get('name', ''))} models...[/dim]"))
    try:
        models = _response_data(await client.send_command("provider_models", name=provider["id"], key=key)).get("models", [])
    except RuntimeError as exc:
        if key:
            raise                              # nowy klucz musi dzialac — blad idzie do uzytkownika
        console.print(tr(f"[yellow]{escape(str(exc))}[/yellow] [dim]— wpisz nazwę modelu ręcznie.[/dim]",
                         f"[yellow]{escape(str(exc))}[/yellow] [dim]— type the model name yourself.[/dim]"))
        models = []
    current = provider.get("model") or provider.get("default_model") or (models[0] if models else "")
    if query:
        if not models or query in models:
            return query
        shown = _matching_models(models, query)
        if len(shown) == 1:
            return shown[0]
        if not shown:
            console.print(tr(f"[yellow]Żaden model nie pasuje do {escape(query)!r} — oto pełna lista.[/yellow]",
                             f"[yellow]No model matches {escape(query)!r} — here is the full list.[/yellow]"))
            shown = models
    else:
        shown = models
    while True:
        if shown:
            console.print(tr(f"[bold]Modele {escape(provider.get('name', ''))}[/bold] ({len(shown)})",
                             f"[bold]{escape(provider.get('name', ''))} models[/bold] ({len(shown)})"))
            _print_models(shown, current)
        answer = Prompt.ask(tr("Model (numer, nazwa albo fragment; Enter — obecny)", "Model (number, name or part of it; Enter — current)"),
                            default=current).strip()
        picked = _pick_model(shown, answer)
        if not models or picked in models:
            return picked
        narrowed = _matching_models(models, answer)
        if narrowed:
            shown = narrowed
            continue
        if Confirm.ask(tr(f"Modelu {escape(picked)!r} nie ma na liście providera. Użyć mimo to?",
                          f"Model {escape(picked)!r} is not on the provider's list. Use it anyway?"), default=False):
            return picked
        shown = models


def _announce_provider(data: dict) -> None:
    active = data.get("active") or {}
    console.print(tr(f"[green]✓[/green] Od teraz odpowiada [bold]{escape(active.get('name', ''))}[/bold] · {escape(active.get('model', ''))} "
                     "[dim](dla całego agenta: CLI, web, Telegram, rutyny)[/dim]",
                     f"[green]✓[/green] From now on you are talking to [bold]{escape(active.get('name', ''))}[/bold] · {escape(active.get('model', ''))} "
                     "[dim](for the whole agent: CLI, web, Telegram, routines)[/dim]"))


async def _setup_provider(client: "RemoteClient", provider: dict, query: str = "", *, ask_key: bool = True) -> None:
    """Klucz (gdy trzeba; wpisywany bez echa) -> model z listy providera -> przelaczenie. Klucz idzie tylko do backendu."""
    key = ""
    if provider.get("privacy"):
        console.print(f"[yellow]{escape(provider['privacy'])}[/yellow]")
    if provider.get("requires_key"):
        change = False
        if provider.get("has_key") and ask_key:
            change = Confirm.ask(tr("Zmienić klucz API?", "Change the API key?"), default=False)
        if not provider.get("has_key") or change:
            if provider.get("key_url"):
                console.print(tr(f"[dim]Klucz utworzysz tutaj: {escape(provider['key_url'])}[/dim]",
                                 f"[dim]Create a key here: {escape(provider['key_url'])}[/dim]"))
            key = getpass.getpass(tr("Klucz API (nie będzie widoczny): ", "API key (hidden): ")).strip()
            if not key and not provider.get("has_key"):
                console.print(tr("[dim]✖ Anulowano — bez klucza nie da się przełączyć.[/dim]",
                                 "[dim]✖ Cancelled — cannot switch without a key.[/dim]"))
                return
    model = await _choose_model(client, provider, key, query)
    if not model:
        console.print(tr("[dim]✖ Anulowano — nie wybrano modelu.[/dim]", "[dim]✖ Cancelled — no model chosen.[/dim]"))
        return
    _announce_provider(_response_data(await client.send_command("provider_set", name=provider["id"], key=key, model=model)))


async def _providers_command(args: str, client: "RemoteClient") -> None:
    """
    /providerzy — lista i (dla admina) wybor z klawiatury: provider, potem model z jego listy;
    /providerzy <id|nr> [model albo fragment] — od razu do wyboru modelu (provider bez klucza: najpierw klucz);
    /providerzy model — zmiana modelu obecnego providera; /providerzy zapomnij <id> — usuniecie klucza z interfejsu.
    """
    parts = args.split()
    if parts and parts[0].lower() in PROVIDER_FORGET:
        if len(parts) < 2:
            console.print(tr("[dim]Użycie: /providerzy zapomnij <id>[/dim]", "[dim]Usage: /providers forget <id>[/dim]"))
            return
        data = _response_data(await client.send_command("provider_forget", name=parts[1].lower()))
        console.print(tr("Klucz usunięty (o ile był dodany z interfejsu).", "Key removed (if it was added from the interface)."))
        _print_providers(data)
        return
    data = _response_data(await client.send_command("providers"))
    if parts:
        if not data.get("can_edit"):
            console.print(tr("[yellow]Providera może zmieniać tylko administrator.[/yellow]",
                             "[yellow]Only an administrator can change the provider.[/yellow]"))
            return
        if parts[0].lower() in PROVIDER_MODEL:
            provider = next((p for p in data.get("providers", []) if p.get("active")), None)
            rest = parts[1:]
        else:
            provider = _find_provider(data, parts[0])
            rest = parts[1:]
        if provider is None:
            console.print(tr(f"[red]Nie znam providera {escape(parts[0])!r}.[/red]", f"[red]Unknown provider {escape(parts[0])!r}.[/red]"))
            _print_providers(data)
            return
        try:
            await _setup_provider(client, provider, " ".join(rest), ask_key=not provider.get("ready"))
        except (KeyboardInterrupt, EOFError):
            console.print(tr("[dim]✖ Anulowano.[/dim]", "[dim]✖ Cancelled.[/dim]"))
        return
    _print_providers(data)
    if not data.get("can_edit"):
        console.print(tr("[dim]Providera może zmieniać tylko administrator.[/dim]", "[dim]Only an administrator can change the provider.[/dim]"))
        return
    try:
        choice = Prompt.ask(tr("Numer albo id providera — także obecnego, żeby zmienić model (Enter — bez zmian)",
                               "Provider number or id — the current one too, to change the model (Enter — no change)"),
                            default="", show_default=False)
    except (KeyboardInterrupt, EOFError):
        return
    if not choice.strip():
        return
    provider = _find_provider(data, choice)
    if provider is None:
        console.print(tr(f"[red]Nie znam providera {escape(choice)!r}.[/red]", f"[red]Unknown provider {escape(choice)!r}.[/red]"))
        return
    try:
        await _setup_provider(client, provider)
    except (KeyboardInterrupt, EOFError):
        console.print(tr("[dim]✖ Anulowano.[/dim]", "[dim]✖ Cancelled.[/dim]"))


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


VIEW_QUESTION = ("Co chcesz robić z Pipe? Opisz w paru słowach albo wybierz:\n"
                 "  [bold]1[/bold] — codzienne sprawy: rozmowa, przypomnienia, skille, dodatki do aplikacji (widok prosty)\n"
                 "  [bold]2[/bold] — serwer: schemat, podatności, kontenery, zdalne serwery (widok techniczny)",
                 "What do you want to do with Pipe? Describe it in a few words or pick:\n"
                 "  [bold]1[/bold] — everyday things: chat, reminders, skills, add-ons for apps (simple view)\n"
                 "  [bold]2[/bold] — the server: diagram, vulnerabilities, containers, remote servers (technical view)")


async def _view_command(client: "RemoteClient", args: str) -> None:
    """/widok [prosty|techniczny|opis] — widok wspolny dla CLI, pipe web i Telegrama."""
    global _view
    try:
        data = _response_data(await client.send_command("view", args=args))
    except RuntimeError as exc:
        console.print(f"[yellow]{escape(str(exc))}[/yellow]")
        return
    _view = data.get("view") or _view
    console.print(escape(data.get("text", "")))


async def _sync_view(client: "RemoteClient") -> None:
    """
    Po polaczeniu: widok z backendu. Nikt go jeszcze nie wybral (pierwsze uruchomienie) — pytamy, co uzytkownik chce
    robic; odpowiedz zwyklymi slowami backend zamienia na widok (core/views.py). Pusty Enter zostawia techniczny.
    """
    global _view
    try:
        data = _response_data(await client.send_command("view"))
    except Exception:
        return          # starszy backend nie zna tej komendy
    _view = data.get("view") or "tech"
    if data.get("chosen") or not data.get("can_edit") or BRIDGE_MODE or not sys.stdin.isatty():
        return
    console.print(tr(*VIEW_QUESTION))
    for _ in range(2):
        try:
            answer = console.input("[bold]> [/bold]").strip() or "2"
        except (EOFError, KeyboardInterrupt):
            return
        try:
            data = _response_data(await client.send_command("view", args=answer))
        except RuntimeError as exc:
            console.print(f"[yellow]{escape(str(exc))}[/yellow]")
            continue
        _view = data.get("view") or _view
        console.print(f"[dim]{escape(data.get('text', ''))}[/dim]\n")
        return


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
        console.print(Markdown(tr(HELP_SIMPLE, HELP_SIMPLE_EN) if _view == "simple" else tr(HELP_TEXT, HELP_TEXT_EN)))
        return True

    if name == "widok":
        await _view_command(client, args)
        return True

    if name == "jezyk":
        data = _response_data(await client.send_command("language", args=args))
        if args and data.get("lang"):
            _set_lang(data["lang"])
        console.print(escape(data.get("text", "")))
        return True

    if name == "providerzy":
        await _providers_command(args, client)
        return True

    if name == "yolo":
        global _yolo
        data = _response_data(await client.send_command("yolo", args=args))
        _yolo = bool(data.get("yolo"))
        console.print(f"[bold red]{escape(data.get('text', ''))}[/bold red]" if _yolo and args else escape(data.get("text", "")))
        return True

    if name == "aktualizuj":
        console.print(tr("[dim]Sprawdzam aktualizację Pipe...[/dim]", "[dim]Checking the Pipe update...[/dim]"))
        await _handle_responses(await client.send_command("update", args=args), client)
        return True

    if name == "status":
        _print_overview(_response_data(await client.send_command("overview")))
        return True

    if name == "nowosci":
        await _inbox(client)
        return True

    if name == "projekty":
        data = _response_data(await client.send_command("projects"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        console.print(tr("[dim]Nowy projekt: napisz np. „sklonuj https://github.com/… i uruchom”.[/dim]",
                         '[dim]New project: write e.g. "clone https://github.com/… and run it".[/dim]'))
        return True

    if name == "powiadomienia":
        fields = _prefs_fields(args)
        if args and not fields:
            console.print(tr("[yellow]Użyj: /powiadomienia [wszystko|wazne|krytyczne] [cisza 22-7|cisza off] [dziennie 3][/yellow]",
                             "[yellow]Use: /notifications [all|important|critical] [quiet 22-7|quiet off] [daily 3][/yellow]"))
            return True
        data = _response_data(await client.send_command("prefs", **fields))
        console.print(escape(data.get("text", "")))
        return True

    if name == "pobierz":
        console.print(tr("[dim]Pobieram i czytam repozytorium...[/dim]", "[dim]Fetching and reading the repository...[/dim]"))
        frames = await client.send_command("install", args=args)
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        if error:
            console.print(Text(error, overflow="fold"), style="red", highlight=False)
            return True
        data = _response_data(frames)
        console.print(Markdown(data.get("text", "")))
        if not data.get("token"):
            return True
        try:
            confirmed = Confirm.ask(tr("[yellow]Zainstalować?[/yellow]", "[yellow]Install?[/yellow]"), default=False)
        except (KeyboardInterrupt, EOFError):
            confirmed = False
        if confirmed:
            result = _response_data(await client.send_command("install", token=data["token"], execute=True))
            console.print(Text(result.get("text", ""), overflow="fold"), highlight=False)
            await _refresh_skills(client)
        else:
            console.print(tr("[dim]✖ Anulowano.[/dim]", "[dim]✖ Cancelled.[/dim]"))
        return True

    if name == "mcp":
        frames = await client.send_command("mcp_servers", args=args)
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        console.print(Text(error or _response_data(frames).get("text", ""), overflow="fold"),
                      style="red" if error else None, highlight=False)
        return True

    if name == "web":
        if args.split()[:1] and args.split()[0].lower() in ("wlacz", "włącz", "on", "start", "publiczny", "public"):
            console.print(tr("[dim]Wystawiam Pipe pod adresem HTTPS (Caddy, certyfikat Let's Encrypt)...[/dim]",
                             "[dim]Exposing Pipe at an HTTPS address (Caddy, Let's Encrypt certificate)...[/dim]"))
        frames = await client.send_command("web", args=args)
        error = next((f.get("response", "") for f in frames if f.get("status") == "error"), "")
        console.print(Text(error or _response_data(frames).get("text", ""), overflow="fold"),
                      style="red" if error else None, highlight=False)
        return True

    if name == "sprzataj":
        console.print(tr("[dim]Sprzątam to, co da się odtworzyć: obrazy bez tagu, cache budowania, pobrane paczki...[/dim]",
                         "[dim]Cleaning what is reproducible: untagged images, build cache, downloaded packages...[/dim]"))
        data = _response_data(await client.send_command("cleanup"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
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
        if data.get("vulns"):
            console.print(Rule(tr("Podatności", "Vulnerabilities"), style="dim"))
            console.print(Markdown(str(data["vulns"])))
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

    if name == "podatnosci":
        console.print(tr("[dim]Sprawdzam podatności i przygotowuję opisy...[/dim]",
                         "[dim]Checking vulnerabilities and preparing descriptions...[/dim]"))
        console.print(Markdown(_response_data(await client.send_command("vulns")).get("text", "")))
        return True

    if name == "zalataj":
        # backend lata od razu i sam rozumie zakres (obrazy, serwer, serwery, nazwa serwera, pilne); kroki idą na żywo
        console.print(tr("[dim]Łatanie: kroki poniżej, całość może potrwać kilka minut.[/dim]",
                         "[dim]Patching: the steps follow, the whole thing can take a few minutes.[/dim]"))
        data = _response_data(await client.send_command("patch", scope=args.strip()))
        console.print(Markdown(data.get("text", "")))
        if not data.get("can_execute") or not data.get("token"):
            return True
        # backend sprzed v0.38 odpowiada podglądem z tokenem — wtedy jak dawniej: zainstaluj / anuluj
        scope = data.get("scope", "all")
        install, cancel = tr("zainstaluj", "install"), tr("anuluj", "cancel")
        try:
            choice = Prompt.ask(tr("[yellow]Co robimy?[/yellow]", "[yellow]What now?[/yellow]"),
                                choices=[install, cancel], default=cancel)
        except (KeyboardInterrupt, EOFError):
            choice = cancel
        confirmed = choice == install
        if confirmed:
            console.print(tr("[dim]Instaluję łatki — to może potrwać kilka minut...[/dim]",
                             "[dim]Installing patches — this can take a few minutes...[/dim]"))
            result = _response_data(await client.send_command("patch", scope=data.get("scope", scope),
                                                              patch_token=data["token"], execute=True))
            console.print(Text(result.get("text", ""), overflow="fold"), highlight=False)
        else:
            console.print(tr("[dim]✖ Anulowano.[/dim]", "[dim]✖ Cancelled.[/dim]"))
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
        active = data.get("active", [])
        console.print(tr("[bold]Aktywne alerty[/bold]", "[bold]Active alerts[/bold]"))
        if not active:
            console.print(tr("[dim]Brak — wszystko w normie.[/dim]", "[dim]None — everything is normal.[/dim]"))
        for alert in active:
            console.print(f"  {_severity(alert.get('severity'))}  [bold]{escape(str(alert.get('title')))}[/bold]")
            # alert z trescia w Markdownie (podatnosci: kazda z opisem) — w calosci, pod tytulem
            if alert.get("report"):
                console.print(Padding(Markdown(str(alert["report"])), (0, 0, 1, 4)))
            elif alert.get("detail"):
                console.print(f"    [dim]{escape(str(alert['detail']))}[/dim]")
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

    if name == "nowa":
        data = _response_data(await client.send_command("new_chat"))
        console.print((tr("Zaczynamy nową rozmowę.", "Starting a new conversation.") if data.get("cleared")
                       else tr("Rozmowa już była pusta.", "The conversation was already empty."))
                      + tr(" [dim]Pamiętam to, co trwałe: notatki o serwerze, katalogi, skille, Twój styl i alerty.[/dim]",
                           " [dim]I keep what lasts: the server notes, directories, skills, your style and the alerts.[/dim]"))
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
            console.print(f"  [bold]{escape(label)}[/bold] [dim]—[/dim] {escape(skill['description'])}")
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


async def _watch_reminders(client: "RemoteClient", stop: asyncio.Event) -> None:
    """
    W tle, gdy CLI czeka na wpis: pokazuje wiadomosci od Pipe, gdy tylko przyjda — przypomnienia i wiadomosci ze
    skrzynki, o ktorych backend chce powiadomic (`push`: poziom, cisza nocna, limit podpowiedzi).
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), REMINDER_POLL)
            return
        except asyncio.TimeoutError:
            await _show_due_reminders(client, quiet=True)
            await _show_new_feed(client)


_feed_since: float = 0.0     # znacznik czasu ostatniej pokazanej wiadomosci ze skrzynki (0 = jeszcze nie pobrano)


async def _show_new_feed(client: "RemoteClient", baseline: bool = False) -> None:
    """Nowe wiadomosci ze skrzynki od ostatniego sprawdzenia. `baseline` — tylko zapamietaj, od kiedy liczyc."""
    global _feed_since
    try:
        data = _response_data(await client.probe("feed", **({"since": _feed_since} if _feed_since else {})))
    except Exception:
        return
    items = data.get("items") or []
    newest = max([float(i.get("ts", 0)) for i in items] + [_feed_since])
    if baseline or not _feed_since:
        _feed_since = newest or time.time()
        return
    _feed_since = newest
    for item in reversed([i for i in items if i.get("push", True)]):
        console.print(Panel(Group(Markdown(str(item.get("text") or "")[:1500])) if item.get("text") else "",
                            title=f" {escape(str(item.get('title', '')))} ", title_align="left",
                            subtitle=tr(" /nowosci — akcje ", " /news — actions ") if item.get("actions") else None,
                            subtitle_align="right", border_style={"critical": "red", "warning": "yellow"}.get(
                                str(item.get("severity")), "dim"), box=box.ROUNDED))


async def _show_due_reminders(client: "RemoteClient", quiet: bool = False) -> None:
    """
    CLI nie odbiera zdarzen na zywo (REPL czeka na klawiature), wiec przypomnienia, ktore odpalily bez
    odbiorcy, pobiera przy polaczeniu i po kazdej wymianie. Cicho, gdy backend jest starszy albo nic nie czeka.
    """
    try:
        send = client.probe if quiet else client.send_command
        claimed = _response_data(await send("reminders", claim=True)).get("claimed", [])
    except Exception:
        return
    for event in claimed:
        title = tr("Przypomnienie", "Reminder") if event.get("kind") != "task" else tr("Zadanie zaplanowane", "Scheduled task")
        body = escape(str(event.get("text", "")))
        if event.get("report"):
            body += "\n\n" + escape(str(event["report"]))
        console.print(Panel(body, title=f" {title} · {escape(str(event.get('due', '')))} ", title_align="left",
                            border_style="dim", box=box.SQUARE))


class AppSetup:
    """
    `pipe web` bez --host: pierwsze uruchomienie (lokalnie albo na serwerze, appsetup.py) i zapisane polaczenie.
    Most strony (clients/webui/server.py) wola state() i run() z osobnego watku.
    """

    def __init__(self) -> None:
        self.tunnel: SSHTunnel | None = None

    def state(self, lang: str = "pl") -> dict:
        saved = appsetup.saved_connection()
        return {"local": appsetup.local_problem(lang), "paramiko": appsetup.has_paramiko(),
                "saved": appsetup.label(saved, lang) if saved else ""}

    def run(self, request: dict, progress, lang: str = "pl") -> tuple[str, int, str, str]:
        if request.get("kind") == "local":
            conn = appsetup.start_local(progress, lang)
        else:
            conn = appsetup.deploy_server(request, progress, lang)
        target = self.open(conn, progress, lang)
        appsetup.remember(conn)
        return target

    def open(self, conn: dict, progress=None, lang: str = "pl") -> tuple[str, int, str, str]:
        """(host, port, token, etykieta) dzialajacego backendu; lokalny w razie potrzeby uruchamia, do serwera tunel."""
        progress = progress or (lambda line: None)
        if conn.get("kind") == "local":
            if not appsetup.port_open():
                appsetup.start_local(progress, lang)
            return "127.0.0.1", appsetup.BACKEND_PORT, appsetup.local_token(), appsetup.label(conn, lang)
        self.close()
        tunnel = SSHTunnel(host=appsetup.ssh_target(conn), ssh_port=int(conn.get("ssh_port") or DEFAULT_SSH_PORT),
                           local_port=_free_port(), identity_file=conn.get("key") or None, batch=True)
        progress(tr(f"Zestawiam tunel SSH do {appsetup.ssh_target(conn)}…",
                    f"Setting up the SSH tunnel to {appsetup.ssh_target(conn)}…"))
        tunnel.start()
        try:
            ready = tunnel.wait_ready(timeout=30.0)
        except RuntimeError as exc:
            tunnel.stop()
            raise appsetup.SetupError(str(exc)) from None
        if not ready:
            tunnel.stop()
            raise appsetup.SetupError(tr("Tunel SSH nie odpowiada (30 s).", "The SSH tunnel does not respond (30 s)."))
        self.tunnel = tunnel
        return "127.0.0.1", tunnel.local_port, str(conn.get("token") or ""), appsetup.label(conn, lang)

    def close(self) -> None:
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None


async def run_web(client: "RemoteClient | None", host: str, setup: AppSetup | None = None) -> None:
    """
    `pipe web`: interfejs w przegladarce. Ten proces serwuje strone na 127.0.0.1 i mostkuje ja na
    backend przez ten sam tunel co CLI — token zostaje tutaj, na serwerze nie otwiera sie zaden port.
    Bez klienta (pierwsze uruchomienie) strona pyta, co uzytkownik chce robic, i stawia Pipe (AppSetup).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
    try:
        import server as web_server
    except ImportError as exc:
        console.print(tr(f"[red]Nie znaleziono interfejsu webowego (clients/webui): {exc}[/red]",
                         f"[red]Web interface not found (clients/webui): {exc}[/red]"))
        return
    if client is None:
        bridge = web_server.WebBridge(None, 0, "", lang=LANG, port=web_server.free_port(WEB_PORT), setup=setup)
        console.print(Panel.fit(
            tr("[bold]Pipe Web[/bold] — pierwsze uruchomienie\n\n"
               "[dim]Na stronie wybierzesz, co chcesz robić i gdzie ma działać Pipe: na tym komputerze albo na\n"
               "Twoim serwerze. Dane logowania do serwera zostają na tym komputerze. Ctrl+C kończy.[/dim]",
               "[bold]Pipe Web[/bold] — first run\n\n"
               "[dim]On the page you pick what you want to do and where Pipe should run: on this computer or on\n"
               "your server. Server credentials stay on this computer. Ctrl+C to quit.[/dim]"),
            border_style="dim", box=box.SQUARE, padding=(1, 2)))
        console.print(tr("Otwórz w przeglądarce (kliknij):", "Open in your browser (click):"))
        console.print(f"[bold underline][link={bridge.url}]{bridge.url}[/link][/bold underline]",
                      soft_wrap=True, highlight=False)
        console.print()
        await bridge.serve(open_browser=WEB_OPEN_BROWSER)
        return
    try:
        await client.connect()
        await _sync_language(client)
        await client.disconnect()
    except Exception as exc:
        console.print(tr(f"[red]Błąd połączenia: {exc}[/red]", f"[red]Connection error: {exc}[/red]"))
        return
    bridge = web_server.WebBridge(client.host, client.port, client.token, lang=LANG, server_label=host,
                                  port=web_server.free_port(WEB_PORT), setup=setup)
    console.print(Panel.fit(
        tr(f"[bold]Pipe Web[/bold] — połączono z [bold]{escape(host)}[/bold]\n\n"
           "[dim]Adres poniżej zawiera jednorazowy klucz dostępu i działa tylko na tym komputerze.\n"
           "Ctrl+C kończy.[/dim]",
           f"[bold]Pipe Web[/bold] — connected to [bold]{escape(host)}[/bold]\n\n"
           "[dim]The address below contains a one-time access key and works only on this computer.\n"
           "Ctrl+C to quit.[/dim]"),
        border_style="dim", box=box.SQUARE, padding=(1, 2)))
    # Adres poza ramka, w jednej linii i jako hiperlacze terminala (OSC 8): ramka lamalaby go na dwie linie,
    # a wtedy klikniecie otwiera uciety adres bez klucza.
    console.print(tr("Otwórz w przeglądarce (kliknij):", "Open in your browser (click):"))
    console.print(f"[bold underline][link={bridge.url}]{bridge.url}[/link][/bold underline]",
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
    await _sync_yolo(client)
    _print_banner(host)
    await _sync_view(client)
    await _first_run_welcome(client, host)
    # na dzien dobry: stan serwera i ile nowych wiadomosci od Pipe (bez modelu, od razu)
    try:
        _print_overview(_response_data(await client.probe("overview")), compact=True)
        console.print()
    except Exception:
        pass                    # starszy backend bez `overview` — po prostu bez paska stanu
    await _show_new_feed(client, baseline=True)
    await _show_due_reminders(client)
    await _refresh_skills(client)
    ask = _prompt_reader(client)
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

            # /plik <sciezka> i @sciezka — pliki z tego komputera ida razem z wiadomoscia
            try:
                message, paths = _extract_files(user_input)
                attachments = _load_files(paths)
            except (AttachmentError, OSError) as exc:
                console.print(f"[red]{escape(str(exc))}[/red]\n")
                continue
            if attachments:
                console.print()
                for path in paths:
                    console.print(f"[dim]  ↑ {escape(path.name)} ({_size_label(path.stat().st_size)})[/dim]")
                try:
                    await _handle_responses(await client.send_message(message, attachments), client)
                    await _show_due_reminders(client)
                    await _after_reconnect(client)
                except Exception as exc:
                    console.print(tr(f"[red]Błąd komunikacji: {escape(str(exc))}[/red]",
                                     f"[red]Communication error: {escape(str(exc))}[/red]"))
                console.print()
                continue

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
                    await _after_reconnect(client)
                    console.print()
                    continue

            console.print()
            try:
                responses = await client.send_message(user_input)
                await _handle_responses(responses, client)
                await _show_due_reminders(client)
                await _refresh_skills(client)
                await _after_reconnect(client)
            except Exception as exc:
                # _send samo laczy ponownie; blad tutaj nie konczy rozmowy — nastepna wiadomosc sprobuje znowu
                console.print(tr(f"[red]Błąd komunikacji: {escape(str(exc))}[/red]", f"[red]Communication error: {escape(str(exc))}[/red]"))

            console.print()

    finally:
        await client.disconnect()
        console.print(tr("[dim]Do widzenia![/dim]", "[dim]Goodbye![/dim]"))


# ─── Punkt wejścia ────────────────────────────────────────────────────────────

# ─── pipe connect: Pipe jako serwer MCP dla innych aplikacji ─────────────────
# Gotowa konfiguracja do wklejenia. Nic nie laczy sie z serwerem — skladamy most `pipe --mcp` z tych samych opcji
# polaczenia co zwykle CLI. Token idzie do zmiennej srodowiskowej AGENT_TOKEN (nigdy do argumentow procesu).
CONNECT_APPS = ("claude", "claude-desktop", "cursor", "codex", "hermes", "openclaw")


def _bridge_args(host: str | None, ssh_port: int = DEFAULT_SSH_PORT, key: str | None = None,
                 kube: str | None = None, kube_context: str | None = None) -> list[str]:
    """Argumenty mostu MCP: `--mcp` + to, czym CLI laczy sie z serwerem."""
    if kube:
        return ["--mcp", "--kube", kube] + (["--kube-context", kube_context] if kube_context else [])
    if not host:
        raise ValueError(tr("Podaj serwer: pipe connect <aplikacja> --host root@serwer (albo --kube NAMESPACE).",
                            "Give the server: pipe connect <app> --host root@server (or --kube NAMESPACE)."))
    args = ["--mcp", "--host", host]
    if ssh_port and ssh_port != DEFAULT_SSH_PORT:
        args += ["--ssh-port", str(ssh_port)]
    if key:
        args += ["--key", key]
    return args


def _q(value: str) -> str:
    """Argument do wklejenia w powloce (POSIX)."""
    return value if re.fullmatch(r"[A-Za-z0-9@%+=:,./_-]+", value) else "'" + value.replace("'", "'\\''") + "'"


def connect_config(app: str, bridge: list[str], token: str = "") -> str:
    """Konfiguracja Pipe jako serwera MCP dla danej aplikacji (tekst do wklejenia, z instrukcja)."""
    secret = token or "<TOKEN>"
    hint = "" if token else tr(
        "\nToken: na serwerze, w katalogu Pipe: python3 -m backend.tokens add {app} --role admin\n"
        "(osobny token na aplikację — można go odwołać bez ruszania reszty).\n",
        "\nToken: on the server, in the Pipe directory: python3 -m backend.tokens add {app} --role admin\n"
        "(one token per app — it can be revoked without touching the rest).\n").format(app=app.split("-")[0])
    as_json = {"command": "pipe", "args": bridge, "env": {"AGENT_TOKEN": secret}}
    if app == "claude":
        body = tr("Claude Code — wpisz w terminalu:\n\n", "Claude Code — run in a terminal:\n\n") + \
            f"claude mcp add pipe -e AGENT_TOKEN={_q(secret)} -- pipe " + " ".join(_q(a) for a in bridge)
    elif app in ("claude-desktop", "cursor"):
        where = tr("Claude Desktop — dopisz do claude_desktop_config.json (Ustawienia → Developer → Edit Config):",
                   "Claude Desktop — add to claude_desktop_config.json (Settings → Developer → Edit Config):") \
            if app == "claude-desktop" else tr("Cursor — dopisz do ~/.cursor/mcp.json (albo .cursor/mcp.json w projekcie):",
                                                "Cursor — add to ~/.cursor/mcp.json (or .cursor/mcp.json in the project):")
        body = where + "\n\n" + json.dumps({"mcpServers": {"pipe": as_json}}, indent=2, ensure_ascii=False)
    elif app == "codex":
        body = tr("Codex — dopisz do ~/.codex/config.toml:\n\n", "Codex — add to ~/.codex/config.toml:\n\n") + \
            "[mcp_servers.pipe]\ncommand = \"pipe\"\nargs = " + json.dumps(bridge) + \
            f"\nenv = {{ AGENT_TOKEN = {json.dumps(secret)} }}"
    elif app == "hermes":
        body = tr("Hermes Agent — dopisz do config.yaml (~/.hermes/config.yaml):\n\n",
                  "Hermes Agent — add to config.yaml (~/.hermes/config.yaml):\n\n") + \
            "mcp_servers:\n  pipe:\n    command: \"pipe\"\n    args: " + json.dumps(bridge) + \
            "\n    env:\n      AGENT_TOKEN: \"${env:PIPE_TOKEN}\"\n\n" + \
            tr(f"a do ~/.hermes/.env: PIPE_TOKEN={secret}\nPotem w rozmowie z Hermesem: /reload-mcp",
               f"and to ~/.hermes/.env: PIPE_TOKEN={secret}\nThen in a Hermes conversation: /reload-mcp")
    elif app == "openclaw":
        body = tr("OpenClaw — wpisz w terminalu:\n\n", "OpenClaw — run in a terminal:\n\n") + \
            "openclaw mcp add pipe --command pipe " + " ".join(f"--arg {_q(a)}" for a in bridge) + \
            "\nopenclaw mcp doctor pipe --probe\n\n" + \
            tr(f"Token: ustaw AGENT_TOKEN={secret} w środowisku Gatewaya OpenClaw (np. w pliku usługi), żeby proces "
               "`pipe` go odziedziczył.",
               f"Token: set AGENT_TOKEN={secret} in the OpenClaw Gateway's environment (e.g. in its service file) so "
               "the `pipe` process inherits it.")
    else:
        raise ValueError(tr(f"Nie znam aplikacji {app!r}. Dostępne: {', '.join(CONNECT_APPS)}.",
                            f"Unknown app {app!r}. Available: {', '.join(CONNECT_APPS)}."))
    footer = tr("\n\nAgent dostaje narzedzia Pipe: odczyty działają od razu, zmiany czekają na Twoją zgodę "
                "(Telegram, /zgody w CLI), zakazane są odrzucane, a każda zmiana trafia do dziennika (/cofnij).",
                "\n\nThe agent gets Pipe's tools: reads run right away, changes wait for your approval "
                "(Telegram, /approvals in the CLI), forbidden ones are refused, and every change goes to the journal (/undo).")
    return body + "\n" + hint + footer


def _run_saved(args, runner, saved: dict | None, session_id: str) -> None:
    """Polaczenie zapisane w `pipe web` (~/.pipe/app.json) — dla CLI i strony; bez niego strona zaczyna od konfiguracji."""
    setup = AppSetup()
    target = None
    if saved:
        console.print(tr(f"[dim]Łączę z Pipe: {escape(appsetup.label(saved, LANG))}…[/dim]",
                         f"[dim]Connecting to Pipe: {escape(appsetup.label(saved, LANG))}…[/dim]"))
        try:
            target = setup.open(saved, lambda line: console.print(f"[dim]{escape(line)}[/dim]"), LANG)
        except (appsetup.SetupError, RuntimeError, OSError) as exc:
            console.print(f"[yellow]{escape(str(exc))}[/yellow]")
            if runner is not run_web:
                setup.close()
                sys.exit(1)
    try:
        if target is None:
            asyncio.run(run_web(None, "", setup=setup))
            return
        host, port, token, label = target
        client = RemoteClient(session_id=session_id, host=host, port=port, token=args.token or token)
        asyncio.run(run_web(client, label, setup=setup) if runner is run_web else runner(client, label))
    except KeyboardInterrupt:
        pass
    finally:
        setup.close()


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
    parser.add_argument("mode", nargs="?", choices=("web", "connect"), default=None,
                        help=tr("web — interfejs w przeglądarce zamiast terminala (pipe web); connect <aplikacja> — "
                                "konfiguracja Pipe jako serwera MCP dla " + ", ".join(CONNECT_APPS),
                                "web — browser interface instead of the terminal (pipe web); connect <app> — "
                                "configuration of Pipe as an MCP server for " + ", ".join(CONNECT_APPS)))
    parser.add_argument("app", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--web-port", type=int, default=int(os.getenv("PIPE_WEB_PORT", "7400")), metavar="PORT",
                        help=tr("pipe web: port strony na 127.0.0.1 (domyślnie 7400)",
                                "pipe web: port of the page on 127.0.0.1 (default 7400)"))
    parser.add_argument("--setup", action="store_true",
                        help=tr("pipe web: zacznij od pierwszego uruchomienia (gdzie ma działać Pipe), "
                                "nawet gdy połączenie jest zapisane",
                                "pipe web: start with the first run (where Pipe should run) even when a connection "
                                "is saved"))
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

    if args.mode == "connect":
        if not args.app:
            print(tr("Użyj: pipe connect <aplikacja> --host root@serwer. Aplikacje: ",
                     "Use: pipe connect <app> --host root@server. Apps: ") + ", ".join(CONNECT_APPS), file=sys.stderr)
            sys.exit(2)
        try:
            text = connect_config(args.app.lower(), _bridge_args(args.host, args.ssh_port, args.key, args.kube,
                                                                 args.kube_context), args.token)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
        print(text)
        return

    session_id = args.session or str(uuid.uuid4())
    global OPEN_IMAGES, BRIDGE_MODE, console, WEB_PORT, WEB_OPEN_BROWSER
    OPEN_IMAGES = args.open
    WEB_PORT, WEB_OPEN_BROWSER = args.web_port, not args.no_browser
    runner = run_web if args.mode == "web" else run_cli
    if args.mcp:
        BRIDGE_MODE = True
        console = Console(stderr=True, theme=PIPE_THEME)
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

    # ─── Bez --host: zapisane polaczenie albo pierwsze uruchomienie w pipe web ──
    if not args.host:
        saved = None if args.setup else appsetup.saved_connection()
        if args.mode == "web" or saved:
            _run_saved(args, runner, saved, session_id)
            return

    # ─── Tryb SSH tunel ────────────────────────────────────────────────────
    if not args.host:
        console.print(tr(
            "[red]Brak adresu serwera.[/red]\n\n"
            "Podaj --host user@twoj-serwer lub ustaw zmienną VPS_HOST — albo uruchom [bold]pipe web[/bold], "
            "a aplikacja sama postawi Pipe na tym komputerze albo na Twoim serwerze.\n\n"
            "[dim]Przykład: python cli.py --host root@serwer.example.com[/dim]",
            "[red]No server address.[/red]\n\n"
            "Pass --host user@your-server or set the VPS_HOST variable — or run [bold]pipe web[/bold] and the app "
            "will set Pipe up on this computer or on your server.\n\n"
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
