"""
Handler narzedzia reminder — jednorazowe przypomnienia i zadania o okreslonym czasie.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import audit, reminders, safety, targets
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.i18n import tr
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


async def handle_reminder(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """add | list | cancel."""
    from backend.core.watch import get_watcher

    operation = str(args.get("operation", "list") or "list").strip().lower()

    if operation == "list":
        reply(session, tool_call, reminders.render_list())
        return

    if operation == "cancel":
        found = reminders.get(str(args.get("id", "") or ""))
        if found is None:
            reply(session, tool_call, tr("Nie ma takiego przypomnienia (reminder operation=list).",
                                         "No such reminder (reminder operation=list)."))
            return
        if session.role == "viewer" and not reminders.owned_by(found, session.interface):
            reply(session, tool_call, tr("ODMOWA: rola viewer moze anulowac tylko wlasne przypomnienia.",
                                         "REFUSED: the viewer role can cancel only its own reminders."))
            return
        reminders.remove(found.id)
        get_watcher(agent).reminders_changed()
        await audit.log_file_write(session.interface, f"reminders.json#{found.id} (cancel)", 0)
        reply(session, tool_call, tr(f"Anulowano przypomnienie #{found.id}.", f"Cancelled reminder #{found.id}."))
        yield tr(f"[PAMIEC] Anulowalem przypomnienie #{found.id}: {found.text[:120]}",
                 f"[PAMIEC] Cancelled reminder #{found.id}: {found.text[:120]}")
        return

    if operation == "add":
        kind = str(args.get("kind", "message") or "message").strip().lower()
        target = str(args.get("target", "local") or "local").strip().lower()
        try:
            text, kind = reminders.validate(str(args.get("text", "") or ""), kind)
            at = reminders.resolve_time(str(args.get("delay", "") or ""), str(args.get("at", "") or ""))
            if kind == "task" and targets.get_target(target) is None:
                raise reminders.ReminderError(tr(f"Nie ma celu {target!r} (target_manage operation=list).",
                                                 f"No target {target!r} (target_manage operation=list)."))
            if len(reminders.load_reminders()) >= reminders.MAX_REMINDERS:
                raise reminders.ReminderError(tr(f"Za duzo przypomnien (limit {reminders.MAX_REMINDERS}).",
                                                 f"Too many reminders (limit {reminders.MAX_REMINDERS})."))
        except reminders.ReminderError as exc:
            reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
            return

        async def create() -> str:
            # czas wzgledny liczymy od chwili zatwierdzenia, nie od pytania o zgode
            when = reminders.resolve_time(str(args.get("delay", "") or ""), "") if args.get("delay") else at
            reminder = reminders.add(when, text, kind=kind, target=target, to=session.interface)
            get_watcher(agent).reminders_changed()
            await audit.log_file_write(session.interface, f"reminders.json#{reminder.id} ({kind})", 0)
            return tr(f"Ustawiono przypomnienie #{reminder.id} na {reminder.when} (czas serwera). "
                      "Wiadomosc przyjdzie sama — nie czekaj i niczego nie odliczaj.",
                      f"Reminder #{reminder.id} set for {reminder.when} (server time). "
                      "The message will arrive by itself — do not wait or count down.")

        if kind == "message":
            result = await create()
            reply(session, tool_call, result)
            reminder_id = result.split("#", 1)[1].split(" ", 1)[0]
            saved = reminders.get(reminder_id)
            yield tr(f"[PAMIEC] Przypomnienie #{reminder_id} na {saved.when if saved else '?'}: {text[:200]}",
                     f"[PAMIEC] Reminder #{reminder_id} for {saved.when if saved else '?'}: {text[:200]}")
            return

        # Zadanie wykona worker bez nadzoru (jak rutyna) — uzytkownik musi zobaczyc cala tresc.
        preview = reminders.Reminder("nowe", at, text, kind, target, session.interface)
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="reminder",
            command=tr(f"zadanie jednorazowe {preview.when} na celu {target}: {text[:160]}",
                       f"one-off task {preview.when} on target {target}: {text[:160]}"),
            classification="confirm", action=create,
            plan=safety.Plan(local_files=[(str(reminders.reminders_path()), tr("pamiec Pipe: reminders.json",
                                                                                "Pipe memory: reminders.json"))]),
        )
        yield tr(f"[POTWIERDZ] Zadanie jednorazowe wymaga potwierdzenia: {as_code(f'{preview.when} na celu {target}')}\n"
                 f"Wykona je worker (tylko odczyty) i przysle raport. Pelna tresc zadania:\n{as_code(text)}",
                 f"[POTWIERDZ] A one-off task requires confirmation: {as_code(f'{preview.when} on target {target}')}\n"
                 f"A worker (read-only) will run it and send a report. Full task text:\n{as_code(text)}")
        return

    reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: add, list, cancel.",
                                 f"Unknown operation {operation!r}. Available: add, list, cancel."))
