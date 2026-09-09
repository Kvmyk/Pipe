"""
CLI — interaktywny REPL do zarządzania zdalnym serwerem VPS przez agenta AI.

Działa NA LAPTOPIE użytkownika.
Łączy się z backendem uruchomionym NA SERWERZE (Mikrus) przez SSH tunnel.

CLI automatycznie zestawia tunel SSH który forwarduje Unix socket z serwera
do lokalnego portu TCP. Dzięki temu użytkownik nie musi ręcznie konfigurować
żadnych tuneli.

Schemat połączenia:
    Laptop → SSH tunnel → Serwer (Mikrus)
      CLI --------------------→ backend/server.py
      :7379 (local TCP)       /tmp/vps-agent.sock (remote Unix)

Użycie:
    python cli.py --host user@mikrus.example.com
    python cli.py --host root@1.2.3.4 --port 22
    python cli.py --host root@1.2.3.4 --key ~/.ssh/id_rsa
    python cli.py --host root@1.2.3.4 --no-tunnel  # jeśli tunnel jest już aktywny
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
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
except ImportError:
    print("Błąd: zainstaluj zależności: pip install -r requirements.txt")
    sys.exit(1)

console = Console()

# ─── Stałe ───────────────────────────────────────────────────────────────────
DEFAULT_SSH_PORT: int = 22
DEFAULT_LOCAL_PORT: int = 7379           # lokalny port TCP dla tunelu SSH
REMOTE_SOCKET: str = "/tmp/vps-agent.sock"  # socket na serwerze


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
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
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

    async def send_message(self, message: str) -> list[dict]:
        """Wysyła wiadomość i zwraca listę odpowiedzi."""
        return await self._send(
            {"message": message, "session_id": self.session_id, "interface": "cli"}
        )

    async def send_confirm(self, confirmed: bool) -> list[dict]:
        """Wysyła potwierdzenie/odmowę."""
        return await self._send({"confirm": confirmed, "session_id": self.session_id})

    async def _send(self, data: dict) -> list[dict]:
        """Wysyła żądanie JSON i zbiera odpowiedzi do `done: true`."""
        if not self._writer or not self._reader:
            raise RuntimeError("Brak połączenia z backendem.")

        if self.token:
            data = {**data, "token": self.token}

        line = json.dumps(data, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

        responses: list[dict] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                break
            try:
                response = json.loads(raw.decode("utf-8", errors="replace"))
                responses.append(response)
                if response.get("done"):
                    break
            except json.JSONDecodeError:
                continue

        return responses


# ─── Wyświetlanie ─────────────────────────────────────────────────────────────

def _print_banner(host: str) -> None:
    """Wyświetla baner startowy z informacją o serwerze."""
    ascii_art = """[bold cyan]
  ____  _             ____ _                
 |  _ \\(_)_ __   ___ / ___| | __ ___      __
 | |_) | | '_ \\ / _ \\ |   | |/ _` \\ \\ /\\ / /
 |  __/| | |_) |  __/ |___| | (_| |\\ V  V / 
 |_|   |_| .__/ \\___|\\____|_|\\__,_| \\_/\\_/  
         |_|                                
[/bold cyan]"""
    console.print(
        Panel.fit(
            f"{ascii_art}\n"
            "[dim]Autonomiczny agent AI do zarządzania serwerem Linux | v0.4.0[/dim]\n\n"
            f"[dim]Połączono z: [bold white]{host}[/bold white][/dim]\n"
            "[dim]Komendy: [bold cyan]/status[/bold cyan] [dim]— stan serwera[/dim]  "
            "[bold cyan]/exit[/bold cyan] [dim]— wyjście[/dim][/dim]",
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

            # Lokalna komenda /status
            if user_input.lower() == "/status":
                console.print()
                console.print("[dim]Analizuje stan serwera...[/dim]")
                status_prompt = (
                    "Uzyj narzedzia system_stats aby pobrac szczegolowe statystyki systemowe serwera. "
                    "Na podstawie wynikow przygotuj zwiezle podsumowanie: uptime, obciazenie CPU "
                    "(load average), zuzycie RAM (na podstawie sekcji MEMORY z bezposrednich wartosci), "
                    "wolne miejsce na dysku. Odpowiedz zwiezla lista w formacie terminalowym. "
                    "Uzyj formatowania *Pogrubienie* dla tytulow sekcji i `wartosci` dla liczb."
                )
                try:
                    responses = await client.send_message(status_prompt)
                    await _handle_responses(responses, client)
                except Exception as exc:
                    console.print(f"[red]Błąd: {exc}[/red]")
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
  python cli.py --host user@mikrus.example.com
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
        help="Adres serwera: user@host lub host (np. root@mikrus.example.com). "
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
            "[dim]Przykład: python cli.py --host root@mikrus.example.com[/dim]"
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
