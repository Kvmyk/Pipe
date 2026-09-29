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
    print("Błąd: zainstaluj zależności: pip install -r requirements.txt")
    sys.exit(1)

console = Console()

# ─── Stałe ───────────────────────────────────────────────────────────────────
DEFAULT_SSH_PORT: int = 22
DEFAULT_LOCAL_PORT: int = 7379           # lokalny port TCP dla tunelu SSH
REMOTE_SOCKET: str = "/tmp/vps-agent.sock"  # socket na serwerze
READ_LIMIT: int = 32 * 1024 * 1024       # ramki z diagramami (base64) sa duze
DIAGRAMS_DIR: Path = Path(os.getenv("PIPE_DIAGRAMS_DIR", str(Path.home() / ".pipe" / "diagrams")))
OPEN_IMAGES: bool = False                # ustawiane flaga --open


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
            raise RuntimeError(
                "Brak komendy 'ssh'. "
                "Zainstaluj OpenSSH client lub użyj --no-tunnel z ręcznym tunelem."
            )

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

        cmd.append(self.host)

        # NIE przekierowujemy stdin — SSH może zapytać o hasło w terminalu
        self._process = subprocess.Popen(
            cmd,
            stdin=None,          # dziedzicz stdin z procesu rodzica (dla hasła)
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
                raise RuntimeError(
                    f"Tunel SSH zakończył się z kodem {self._process.returncode}.\n"
                    "Sprawdź adres serwera, port SSH i dane logowania."
                )
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
            raise RuntimeError("Brak komendy 'kubectl' — zainstaluj ja albo uzyj polaczenia SSH (--host).")
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
            raise ConnectionRefusedError(
                f"Nie można połączyć się z {self.host}:{self.port}.\n"
                "Sprawdź czy tunel SSH jest aktywny i backend działa na serwerze."
            )

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
        """Wysyła żądanie JSON i zbiera odpowiedzi do `done: true`."""
        if not self._writer or not self._reader:
            raise RuntimeError("Brak połączenia z backendem.")

        if self.token:
            data = {**data, "token": self.token}

        line = json.dumps(data, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

        # Postep i zalaczniki sa pokazywane od razu; reszta wraca jako lista.
        responses: list[dict] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                break
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
                break

        return responses


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
    caption = attachment.get("caption") or attachment.get("name") or "Załącznik"
    console.print(Rule(f"[bold cyan]{escape(caption)}[/bold cyan]", style="cyan"))
    preview = (attachment.get("text") or "").rstrip()
    if preview:
        width = max(len(line) for line in preview.splitlines())
        if width <= console.width:
            console.print(Text(preview), highlight=False)
        else:
            console.print(f"[dim](podgląd ma {width} kolumn — terminal ma {console.width}; otwórz PNG)[/dim]")
    try:
        data = base64.b64decode(attachment.get("data", ""))
        if data:
            DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
            name = Path(attachment.get("name") or "diagram.png").name
            path = DIAGRAMS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
            path.write_bytes(data)
            console.print(f"[dim]Zapisano: {path}[/dim]")
            if OPEN_IMAGES:
                _open_file(path)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Nie zapisano pliku: {escape(str(exc))}[/red]")
    source = attachment.get("source")
    if source:
        console.print("[dim]Kod Mermaid: /mermaid — pokaż ostatni[/dim]")
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
            "[dim]Autonomiczny agent AI do zarządzania serwerem Linux | v0.12.0[/dim]\n\n"
            f"[dim]Połączono z: [bold white]{host}[/bold white][/dim]\n"
            "[dim]Komendy: [bold cyan]/status[/bold cyan]  [bold cyan]/raport[/bold cyan]  [bold cyan]/zmiany[/bold cyan]  "
            "[bold cyan]/mapa[/bold cyan]  [bold cyan]/server[/bold cyan]  "
            "[bold cyan]/skille[/bold cyan]  [bold cyan]/pomoc[/bold cyan]  "
            "[bold cyan]/exit[/bold cyan][/dim]",
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
    text = text.replace("[BLAD]", "**BŁĄD:**")
    text = text.replace("[POTWIERDZ]", "**WYMAGA POTWIERDZENIA:**")
    text = text.replace("[ODMOWA]", "**ODMOWA:**")

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
                "[yellow]Czy chcesz wykonać tę operację?[/yellow]",
                default=False,
            )
        except (KeyboardInterrupt, EOFError):
            confirmed = False
            console.print()

        if confirmed:
            console.print("[dim]✔ Operacja zatwierdzona — wykonuję...[/dim]")
        else:
            console.print("[dim]✖ Operacja anulowana.[/dim]")

        confirm_responses = await client.send_confirm(confirmed)
        await _handle_responses(confirm_responses, client)
        return True

    return False


# ─── Komendy "/" ──────────────────────────────────────────────────────────────

SCAN_WORDS = ("aktualizuj", "odswiez", "odśwież", "skanuj")

HELP_TEXT = """**Komendy**

- `/status` — stan serwera
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
- `/rutyny` — zadania wykonywane według harmonogramu
- `/cele` — zdalne serwery, kontenery i klastry
- `/vibe` — co agent wie o Twoim stylu rozmowy (`/vibe reset` — wyczyść)
- `/dziennik` — zatwierdzone zmiany z kopiami; `/cofnij [id]` — cofnij ostatnią (albo wybraną) zmianę
- `/koszt` — zużycie tokenów i koszt LLM
- `/historia` — ostatnie wpisy audit logu
- `/pomoc` — ta lista
- `/exit` — wyjście

Do komendy skilla możesz dopisać wskazówki: `/odnow_certyfikat tylko dla example.com`.
Diagramy możesz też zamawiać zwykłym tekstem: „narysuj, jak zapytanie trafia do sklepu”."""


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
    error = next((r.get("response") for r in responses if r.get("status") == "error"), "") or "brak danych"
    raise RuntimeError(error)


async def _handle_slash(user_input: str, client: "RemoteClient") -> bool:
    """
    Komendy /server, /skille, /pomoc i skille. Zwraca False, gdy to nie jest
    komenda — wtedy tekst idzie do agenta jak zwykła wiadomość (np. '/var/log jest pełny?').
    """
    head, _, args = user_input.partition(" ")
    name, args = head[1:].lower(), args.strip()

    if name == "server":
        content = "" if args.lower() in SCAN_WORDS else _response_data(await client.send_command("server_md")).get("content", "")
        if content.strip():
            console.print(Markdown(content))
            console.print("[dim]/server aktualizuj — zbadaj serwer ponownie[/dim]")
        else:
            console.print("[dim]" + ("Badam serwer i aktualizuję SERVER.md..." if args.lower() in SCAN_WORDS
                                     else "SERVER.md jeszcze nie istnieje. Badam serwer i tworzę go...") + "[/dim]")
            await _handle_responses(await client.send_command("scan_server"), client)
        return True

    if name in ("pomoc", "help"):
        console.print(Markdown(HELP_TEXT))
        return True

    if name == "status":
        console.print("[dim]Analizuję stan serwera...[/dim]")
        await _handle_responses(await client.send_command("status"), client)
        return True

    if name == "mapa":
        console.print("[dim]Odkrywam infrastrukturę i rysuję mapę...[/dim]")
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
        console.print("[dim]Sprawdzam certyfikaty, strony i backupy...[/dim]")
        data = _response_data(await client.send_command("health"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "raport":
        console.print("[dim]Składam raport...[/dim]")
        data = _response_data(await client.send_command("digest"))
        lines = [f"**{data.get('title', 'Raport')}**"]
        for section in data.get("sections", []):
            lines.append(f"\n**{section.get('title', '')}**\n")
            lines += [f"- {line}" for line in section.get("lines", [])]
        console.print(Markdown("\n".join(lines)))
        return True

    if name == "audyt":
        console.print("[dim]Sprawdzam konfigurację bezpieczeństwa...[/dim]")
        data = _response_data(await client.send_command("audit"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        if data.get("findings"):
            console.print('[dim]Napisz „napraw 1”, a przygotuję poprawkę do zatwierdzenia.[/dim]')
        return True

    if name == "dziennik":
        data = _response_data(await client.send_command("journal"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        console.print("[dim]/cofnij <id> — cofnij wybraną zmianę[/dim]")
        return True

    if name == "cofnij":
        data = _response_data(await client.send_command("undo", id=args))
        console.print(Markdown(data.get("preview", "")))
        if not data.get("undoable"):
            return True
        try:
            confirmed = Confirm.ask("[yellow]Cofnąć tę zmianę?[/yellow]", default=False)
        except (KeyboardInterrupt, EOFError):
            confirmed = False
        if confirmed:
            result = _response_data(await client.send_command("undo", id=data["id"], execute=True))
            console.print(Text(result.get("text", ""), overflow="fold"), highlight=False)
        else:
            console.print("[dim]✖ Anulowano.[/dim]")
        return True

    if name == "koszt":
        data = _response_data(await client.send_command("usage"))
        console.print(Text(data.get("text", ""), overflow="fold"), highlight=False)
        return True

    if name == "mermaid":
        console.print(Markdown(f"```mermaid\n{_last_mermaid}\n```") if _last_mermaid else "[dim]Brak diagramu w tej sesji.[/dim]")
        return True

    if name == "katalogi":
        data = _response_data(await client.send_command("directory"))
        console.print(Markdown("**DIRECTORY**\n\n" + (data.get("text") or "_(pusto — napisz: znajdź repozytoria na serwerze)_")))
        return True

    if name == "alerty":
        data = _response_data(await client.send_command("alerts"))
        if not data.get("enabled", True):
            console.print("[dim]Czuwanie jest wyłączone (WATCH_ENABLED=0).[/dim]")
        _print_list("Aktywne alerty", [f"[{a.get('severity')}] {a.get('title')} — {a.get('detail')}"
                                       for a in data.get("active", [])], "Brak — wszystko w normie.")
        return True

    if name == "cele":
        _print_list("Zdalne cele", _response_data(await client.send_command("targets")).get("targets", []),
                    "Brak. Napisz np.: dodaj serwer 10.0.0.5 jako web-2 (ssh, root)")
        return True

    if name == "rutyny":
        _print_list("Rutyny", _response_data(await client.send_command("routines")).get("routines", []),
                    "Brak. Napisz np.: codziennie o 7 sprawdzaj backupy")
        return True

    if name == "vibe":
        data = _response_data(await client.send_command("vibe", args=args))
        if "reset" in data:
            console.print("Wyczyściłem notatkę o Twoim stylu." if data["reset"] else "Nie było notatki.")
        elif data.get("content", "").strip():
            console.print(Markdown(data["content"]))
            console.print("[dim]/vibe reset — wyczyść[/dim]")
        else:
            console.print("[dim]Jeszcze nie znam Twojego stylu — uczę się z rozmów.[/dim]")
        return True

    if name == "historia":
        entries = _response_data(await client.send_command("history")).get("entries", [])
        console.print(Text("\n".join(entries) or "(pusto)"), highlight=False)
        return True

    if not name:
        return False
    skills = _response_data(await client.send_command("list_skills")).get("skills", [])

    if name == "skille":
        if not skills:
            console.print('Brak zapisanych skilli. Po wykonaniu wieloetapowej procedury poproś: "zapisz to jako skill".')
        for skill in skills:
            label = f"/{skill['command']}" if skill["command"] else f"{skill['name']} (bez komendy)"
            console.print(f"  [bold cyan]{escape(label)}[/bold cyan] — {escape(skill['description'])}")
        return True

    entry = next((s for s in skills if name == s["name"] or (s["command"] and name == s["command"])), None)
    if entry is None:
        return False
    console.print(f"[dim]Uruchamiam skill {escape(entry['name'])}...[/dim]")
    await _handle_responses(await client.send_command("run_skill", name=entry["name"], args=args), client)
    return True


def _welcome_marker(host: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host)[:80] or "default"
    return Path.home() / ".pipe" / f"welcomed-{safe}"


async def _first_run_welcome(client: "RemoteClient", host: str) -> None:
    """Pierwsze polaczenie z serwerem: mapa, ocena bezpieczenstwa i co Pipe pilnuje (raz na serwer)."""
    marker = _welcome_marker(host)
    if marker.exists():
        return
    console.print("[dim]Pierwsze połączenie z tym serwerem — rozglądam się (mapa i audyt)...[/dim]")
    try:
        data = _response_data(await client.send_command("welcome"))
    except Exception as exc:
        console.print(f"[dim]Powitanie niedostępne: {escape(str(exc))}[/dim]")
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
    _print_banner(host)

    # Połącz z backendem
    try:
        await client.connect()
        console.print(f"[dim]Połączono z agentem na {host}[/dim]")
    except Exception as exc:
        console.print(f"[red]Błąd połączenia: {exc}[/red]")
        return

    await _first_run_welcome(client, host)

    # Status startowy ukryty na zyczenie
    console.print(Rule(style="dim"))
    console.print()

    # Pętla REPL
    try:
        while True:
            try:
                user_input = Prompt.ask("[bold cyan]>[/bold cyan]")
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
                    console.print(f"[red]Błąd: {escape(str(exc))}[/red]")
                    handled = True
                if handled:
                    console.print()
                    continue

            console.print()
            try:
                responses = await client.send_message(user_input)
                await _handle_responses(responses, client)
            except Exception as exc:
                console.print(f"[red]Błąd komunikacji: {exc}[/red]")
                # Spróbuj ponownie połączyć
                try:
                    await client.connect()
                    console.print("[dim]Reconnected.[/dim]")
                except Exception:
                    console.print("[red]Nie można ponownie połączyć się z backendem.[/red]")
                    break

            console.print()

    finally:
        await client.disconnect()
        console.print("[dim]Do widzenia![/dim]")


# ─── Punkt wejścia ────────────────────────────────────────────────────────────

def main() -> None:
    """Punkt wejścia CLI."""
    parser = argparse.ArgumentParser(
        description="VPS Management Agent — Interfejs CLI (działa na laptopie)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przykłady:
  # Połącz przez SSH (automatyczny tunel)
  python cli.py --host user@serwer.example.com
  python cli.py --host root@1.2.3.4
  python cli.py --host root@1.2.3.4 --ssh-port 2222
  python cli.py --host root@1.2.3.4 --key ~/.ssh/id_rsa

  # Jesli masz juz wlasny tunel SSH (np. ssh -L 7379:127.0.0.1:7379 ...)
  python cli.py --no-tunnel --local-port 7379
        """,
    )

    # ─── SSH ───────────────────────────────────────────────────────────────
    ssh_group = parser.add_argument_group("Połączenie SSH (domyślne)")
    ssh_group.add_argument(
        "--host",
        default=os.getenv("VPS_HOST"),
        help="Adres serwera: user@host lub host (np. root@serwer.example.com). "
             "Można też ustawić przez zmienną środowiskową VPS_HOST.",
        metavar="USER@HOST",
    )
    ssh_group.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("VPS_SSH_PORT", str(DEFAULT_SSH_PORT))),
        help=f"Port SSH serwera (domyślnie: {DEFAULT_SSH_PORT})",
        metavar="PORT",
    )
    ssh_group.add_argument(
        "--key",
        default=os.getenv("VPS_SSH_KEY"),
        help="Ścieżka do klucza prywatnego SSH (domyślnie: domyślny klucz z ~/.ssh/)",
        metavar="PATH",
    )
    ssh_group.add_argument(
        "--remote-port",
        type=int,
        default=int(os.getenv("VPS_REMOTE_PORT", str(DEFAULT_LOCAL_PORT))),
        help=f"Port TCP backendu na serwerze (domyslnie: {DEFAULT_LOCAL_PORT})",
        metavar="PORT",
    )

    # ─── Tryb bez tunelu ───────────────────────────────────────────────────
    manual_group = parser.add_argument_group("Tryb bez automatycznego tunelu")
    manual_group.add_argument(
        "--no-tunnel",
        action="store_true",
        help="Nie zestawiaj tunelu SSH — połącz się bezpośrednio z lokalnym portem "
             "(użyj gdy masz własny tunel lub testujesz lokalnie)",
    )
    manual_group.add_argument(
        "--local-port",
        type=int,
        default=int(os.getenv("VPS_LOCAL_PORT", str(DEFAULT_LOCAL_PORT))),
        help=f"Lokalny port TCP do połączenia (domyślnie: {DEFAULT_LOCAL_PORT})",
        metavar="PORT",
    )

    # ─── Kubernetes ────────────────────────────────────────────────────────
    kube_group = parser.add_argument_group("Pipe w Kubernetesie (kubectl port-forward)")
    kube_group.add_argument(
        "--kube",
        metavar="NAMESPACE",
        default=os.getenv("PIPE_KUBE_NAMESPACE"),
        help="Połącz z Pipe w klastrze: kubectl port-forward svc/pipe w podanym namespace (np. pipe).",
    )
    kube_group.add_argument("--kube-context", default=os.getenv("PIPE_KUBE_CONTEXT"), metavar="CONTEXT",
                            help="Kontekst kubeconfig (domyślnie bieżący).")

    parser.add_argument("--open", action="store_true",
                        help="Otwieraj zapisane diagramy PNG w domyślnej przeglądarce obrazów.")

    # ─── Sesja ─────────────────────────────────────────────────────────────
    parser.add_argument(
        "--token",
        default=os.getenv("AGENT_TOKEN", ""),
        help="Token autoryzacji backendu (jesli AGENT_TOKEN jest ustawiony w .env serwera). "
             "Mozna tez ustawic przez zmienna srodowiskowa AGENT_TOKEN.",
        metavar="TOKEN",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="ID sesji (domyślnie: losowy UUID)",
        metavar="ID",
    )

    args = parser.parse_args()

    session_id = args.session or str(uuid.uuid4())
    global OPEN_IMAGES
    OPEN_IMAGES = args.open

    # ─── Kubernetes: port-forward zamiast tunelu SSH ──────────────────────
    if args.kube:
        forward = KubePortForward(args.kube, args.local_port, args.kube_context)
        client = RemoteClient(session_id=session_id, host="127.0.0.1", port=args.local_port, token=args.token)
        console.print(f"[dim]kubectl port-forward svc/pipe (namespace {args.kube})...[/dim]")
        try:
            forward.start()
            if not forward.wait_ready(timeout=20.0):
                console.print("[red]Port-forward nie odpowiada. Sprawdź: kubectl -n "
                              f"{args.kube} get pods,svc[/red]")
                sys.exit(1)
            asyncio.run(run_cli(client, f"kubernetes/{args.kube}"))
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
        display_host = f"127.0.0.1:{args.local_port} (lokalny tunel)"
        try:
            asyncio.run(run_cli(client, display_host))
        except KeyboardInterrupt:
            pass
        return

    # ─── Tryb SSH tunel ────────────────────────────────────────────────────
    if not args.host:
        console.print(
            "[red]Brak adresu serwera.[/red]\n\n"
            "Podaj --host user@twoj-serwer lub ustaw zmienną VPS_HOST.\n\n"
            "[dim]Przykład: python cli.py --host root@serwer.example.com[/dim]"
        )
        sys.exit(1)

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

    console.print(f"[dim]Laczę z {args.host} przez SSH...[/dim]")
    console.print("[dim](Jesli pojawi sie monit o haslo SSH, wpisz je ponizej)[/dim]")

    try:
        tunnel.start()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)

    try:
        # Czekaj az tunel bedzie gotowy — BEZ spinnera zeby SSH mogl pytac o haslo
        console.print("[dim]Zestawiam tunel SSH...[/dim]")
        ready = tunnel.wait_ready(timeout=20.0)

        if not ready:
            console.print(
                "[red]Tunel SSH nie odpowiada (timeout 20s).\n"
                "Sprawdz:\n"
                "  * czy wpisales haslo SSH (jesli bylo wymagane)\n"
                "  * czy backend dziala na serwerze: docker logs backend_vps-agent_1\n"
                "  * czy port 7379 jest widoczny: docker ps"
                "[/red]"
            )
            sys.exit(1)

        asyncio.run(run_cli(client, args.host))

    except RuntimeError as exc:
        console.print(f"[red]Błąd tunelu SSH: {exc}[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        console.print("[dim]Zamykam tunel SSH...[/dim]")
        tunnel.stop()


if __name__ == "__main__":
    main()
