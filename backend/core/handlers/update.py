"""
Handler narzedzia pipe_update — aktualizacja samego Pipe (core/selfupdate.py).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from backend.core import audit, selfupdate
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.i18n import tr
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


async def handle_pipe_update(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """check | apply. Narzedzie nie przyjmuje adresu, galezi ani komendy — zrodlem jest origin repozytorium Pipe."""
    from backend.core.watch import get_watcher

    operation = str(args.get("operation", "check") or "check").strip().lower()
    if operation not in ("check", "apply"):
        reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: check, apply.",
                                     f"Unknown operation {operation!r}. Available: check, apply."))
        return
    try:
        if operation == "check":
            reply(session, tool_call, await selfupdate.status())
            return
        install = await selfupdate.locate()
    except selfupdate.UpdateError as exc:
        reply(session, tool_call, str(exc))
        return

    async def launch() -> str:
        try:
            name = await selfupdate.start(install, session.interface)
        except selfupdate.UpdateError as exc:
            return tr(f"Blad: {exc}", f"Error: {exc}")
        await audit.log_confirmed(session.interface, f"pipe_update apply ({name})", 0)
        # jesli przebudowa niczego nie wymieni albo sie nie uda, ten proces dozyje konca i sam wysle wynik
        asyncio.create_task(selfupdate.follow(name, selfupdate.notifier(get_watcher(agent)), restarted=False))
        return tr(f"Aktualizacja uruchomiona w tle (kontener {name}). Za ok. {selfupdate.START_DELAY} s zacznie sie "
                  "pobieranie zmian i przebudowa; backend zrestartuje sie i TA ROZMOWA ZOSTANIE PRZERWANA (sesja nie "
                  "przetrwa restartu). Wynik przyjdzie sam jako wiadomosc. Powiedz to uzytkownikowi w jednym-dwoch "
                  "zdaniach i nie wywoluj juz zadnych narzedzi.",
                  f"The update is running in the background (container {name}). In about {selfupdate.START_DELAY} s it "
                  "starts pulling changes and rebuilding; the backend will restart and THIS CONVERSATION WILL BE CUT "
                  "(the session does not survive a restart). The result arrives by itself as a message. Tell the user "
                  "in one or two sentences and do not call any more tools.")

    services = ", ".join(install.services)
    session.pending_confirmation = ConfirmationRequest(
        tool_call_id=tool_call.id, tool_name="pipe_update",
        command=tr(f"aktualizacja Pipe w {install.root}: git pull --ff-only + przebudowa uslug {services}",
                   f"Pipe update in {install.root}: git pull --ff-only + rebuild of services {services}"),
        classification="confirm", action=launch,
    )
    yield tr(f"[POTWIERDZ] Aktualizacja Pipe wymaga potwierdzenia: {as_code(f'git pull --ff-only w {install.root}')}, "
             f"potem przebudowa i restart uslug {as_code(services)}.\n"
             "Wykona to osobny kontener pomocniczy. Backend bedzie niedostepny przez minute lub kilka, a ta rozmowa "
             "zostanie przerwana; wynik przysle w wiadomosci po restarcie.",
             f"[POTWIERDZ] The Pipe update requires confirmation: {as_code(f'git pull --ff-only in {install.root}')}, "
             f"then a rebuild and restart of services {as_code(services)}.\n"
             "A separate helper container does it. The backend will be unavailable for a minute or a few and this "
             "conversation will be cut; the result arrives as a message after the restart.")
