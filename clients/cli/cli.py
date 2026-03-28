"""
CLI — interaktywny REPL do zarządzania zdalnym serwerem VPS przez agenta AI.

Działa NA LAPTOPIE użytkownika.
Łączy się bezpośrednio przez TCP z backendem na serwerze.

Użycie:
    python cli.py --host 1.2.3.4
    python cli.py --host 1.2.3.4 --port 7379
    python cli.py --host 1.2.3.4 --token moj-sekretny-token
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.rule import Rule
    from rich.text import Text
except ImportError:
    print("Blad: zainstaluj zaleznosci: pip install -r requirements.txt")
    sys.exit(1)

console = Console()

DEFAULT_PORT: int = 7379
STARTUP_STATUS_MESSAGE = (
    "Sprawdz stan serwera: wykonaj hostname && uptime && df -h / && free -h "
    "i podsumuj wyniki po polsku."
)


# ─── Klient TCP ───────────────────────────────────────────────────────────────

class AgentClient:
    """Klient TCP łączący się bezpośrednio z backendem."""

    def __init__(self, host: str, port: int, session_id: str, token: str = "") -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.token = token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            raise ConnectionError(f"Timeout: nie mozna polaczyc z {self.host}:{self.port}")
        except ConnectionRefusedError:
            raise ConnectionError(
                f"Odmowa polaczenia: {self.host}:{self.port}\n"
                "Sprawdz czy backend dziala i port jest otwarty."
            )

    async def disconnect(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def send_message(self, message: str) -> list[dict]:
        return await self._send({"message": message, "session_id": self.session_id, "interface": "cli"})

    async def send_confirm(self, confirmed: bool) -> list[dict]:
        return await self._send({"confirm": confirmed, "session_id": self.session_id})

    async def _send(self, data: dict) -> list[dict]:
        if not self._writer or not self._reader:
            raise RuntimeError("Brak polaczenia z backendem.")

        if self.token:
            data["token"] = self.token

        line = json.dumps(data, ensure_ascii=False) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

        responses: list[dict] = []
        try:
            while True:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=120.0)
                if not raw:
                    break
                try:
                    response = json.loads(raw.decode("utf-8", errors="replace"))
                    responses.append(response)
                    if response.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
        except asyncio.TimeoutError:
            responses.append({"response": "Timeout: agent nie odpowiedzial w ciagu 120s.", "status": "error", "done": True})

        return responses


# ─── Wyświetlanie ─────────────────────────────────────────────────────────────

def _print_banner(host: str, port: int) -> None:
    console.print(
        Panel.fit(
            "[bold cyan]VPS Management Agent[/bold cyan]\n"
            "[dim]Autonomiczny agent AI do zarzadzania serwerem Linux[/dim]\n\n"
            f"[dim]Polaczono z: [bold white]{host}:{port}[/bold white][/dim]\n"
            "[dim]Wpisz [bold]exit[/bold] lub nacisnij Ctrl+C aby wyjsc[/dim]",
            border_style="cyan",
        )
    )


def _print_response(text: str, status: str) -> None:
    """Wyswietla odpowiedz agenta."""
    text = text.strip()
    if not text:
        return
    if status == "error" or "❌" in text or "🚫" in text:
        style = "red"
    elif status == "confirm" or "⚠️" in text:
        style = "yellow"
    else:
        style = "default"
    console.print(text, style=style)


async def _handle_responses(responses: list[dict], client: AgentClient) -> bool:
    """Przetwarza odpowiedzi. Zwraca True jesli bylo potwierdzenie."""

    # DEBUG
    console.print(f"[dim]>>> {len(responses)} pakietow:[/dim]")
    for i, r in enumerate(responses):
        console.print(f"[dim]>>> [{i}] done={r.get('done')} status={r.get('status')} resp={repr(r.get('response','')[:80])}[/dim]")

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
            confirmed = Confirm.ask("[yellow]Czy chcesz wykonac te operacje?[/yellow]", default=False)
        except (KeyboardInterrupt, EOFError):
            confirmed = False
            console.print()

        if confirmed:
            console.print("[dim]Operacja zatwierdzona...[/dim]")
        else:
            console.print("[dim]Operacja anulowana.[/dim]")

        confirm_responses = await client.send_confirm(confirmed)
        await _handle_responses(confirm_responses, client)
        return True

    return False


# ─── Główna pętla REPL ────────────────────────────────────────────────────────

async def run_cli(client: AgentClient, host: str, port: int) -> None:
    _print_banner(host, port)

    try:
        await client.connect()
        console.print(f"[dim]Polaczono z {host}:{port}[/dim]")
    except ConnectionError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    # Status startowy
    console.print()
    console.print(Rule("[dim]Status serwera[/dim]", style="dim"))
    try:
        responses = await client.send_message(STARTUP_STATUS_MESSAGE)
        await _handle_responses(responses, client)
    except Exception as exc:
        console.print(f"[red]Blad statusu: {exc}[/red]")
    console.print(Rule(style="dim"))
    console.print()

    # REPL
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
            if user_input.lower() in ("exit", "quit", "wyjdz", "koniec"):
                break

            console.print()
            try:
                responses = await client.send_message(user_input)
                await _handle_responses(responses, client)
            except Exception as exc:
                console.print(f"[red]Blad: {exc}[/red]")
                try:
                    await client.connect()
                except Exception:
                    console.print("[red]Nie mozna ponownie polaczyc.[/red]")
                    break
            console.print()

    finally:
        await client.disconnect()
        console.print("[dim]Do widzenia![/dim]")


# ─── Punkt wejścia ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VPS Management Agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przyklady:
  python cli.py --host 1.2.3.4
  python cli.py --host 1.2.3.4 --port 7379
  python cli.py --host 1.2.3.4 --token moj-token

Zmienne srodowiskowe:
  VPS_HOST   - adres IP serwera
  VPS_PORT   - port TCP (domyslnie 7379)
  VPS_TOKEN  - token autoryzacji
        """,
    )
    parser.add_argument(
        "--host",
        default=os.getenv("VPS_HOST"),
        help="Adres IP serwera (np. 1.2.3.4). Mozna tez ustawic VPS_HOST.",
        metavar="IP",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("VPS_PORT", str(DEFAULT_PORT))),
        help=f"Port TCP backendu (domyslnie: {DEFAULT_PORT})",
        metavar="PORT",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        help="Ignorowany — uzytkownik laczy sie bezposrednio przez TCP",
        metavar="PORT",
    )
    parser.add_argument(
        "--key",
        help="Ignorowany — uzytkownik laczy sie bezposrednio przez TCP",
        metavar="PATH",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("VPS_TOKEN", ""),
        help="Token autoryzacji (jesli skonfigurowany na serwerze)",
        metavar="TOKEN",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="ID sesji (domyslnie: losowy UUID)",
        metavar="ID",
    )

    args = parser.parse_args()

    if not args.host:
        console.print(
            "[red]Brak adresu serwera.[/red]\n\n"
            "Podaj --host IP lub ustaw zmienna VPS_HOST.\n"
            "[dim]Przyklad: python cli.py --host 1.2.3.4[/dim]"
        )
        sys.exit(1)

    session_id = args.session or str(uuid.uuid4())
    client = AgentClient(
        host=args.host,
        port=args.port,
        session_id=session_id,
        token=args.token,
    )

    try:
        asyncio.run(run_cli(client, args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
