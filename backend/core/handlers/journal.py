"""
Handler narzedzia journal — dziennik zatwierdzonych zmian i ich cofanie.

list  ostatnie wpisy (co, kiedy, czy da sie cofnac)
undo  cofniecie wpisu (domyslnie ostatniego) — zawsze z potwierdzeniem; potwierdzenie
      pokazuje roznice plikow i komendy odwrotne, ktore zostana wykonane
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import journal
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.session import ConfirmationRequest, Session
from backend.core.i18n import tr


async def handle_journal(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    operation = str(args.get("operation", "list") or "list").strip().lower()
    entry_id = str(args.get("id", "") or "").strip().lstrip("#").lower()

    if operation == "list":
        reply(session, tool_call, journal.render_list())
        return

    if operation == "undo":
        try:
            entry = journal.load(entry_id) if entry_id else journal.latest_undoable()
        except ValueError as exc:
            reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
            return
        if entry is None:
            reply(session, tool_call, tr("Nie ma takiego wpisu w dzienniku (journal operation=list).",
                                         "No such journal entry (journal operation=list).") if entry_id
                  else tr("Nie ma zmiany, ktora da sie cofnac.", "There is no change that can be undone."))
            return
        if not entry.undoable:
            reply(session, tool_call, tr(f"Wpisu #{entry.id} nie da sie cofnac (status: {entry.status}, brak kopii).",
                                         f"Entry #{entry.id} cannot be undone (status: {entry.status}, no backup)."))
            return

        async def rollback(entry=entry, interface=session.interface) -> str:
            return await journal.rollback(entry, interface)

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="journal", command=tr(f"cofniecie #{entry.id}", f"undo #{entry.id}"),
            classification="confirm", action=rollback,
        )
        yield tr("[POTWIERDZ] Cofniecie zmiany wymaga potwierdzenia.\n",
                 "[POTWIERDZ] Undoing the change requires confirmation.\n") + journal.preview(entry)
        return

    reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: list, undo.",
                                 f"Unknown operation {operation!r}. Available: list, undo."))
