"""
Handler narzedzia routine_manage — zadania wykonywane przez agenta wedlug harmonogramu.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import audit, routines, safety, targets
from backend.core.events import Event, Progress
from backend.core.handlers.common import reply
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code
from backend.core.i18n import tr


async def handle_routine_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """list | add | remove | enable | disable | run."""
    from backend.core.watch import get_watcher

    operation = str(args.get("operation", "list") or "list").strip().lower()
    name = str(args.get("name", "") or "").strip().lower()

    if operation == "list":
        found = routines.load_routines()
        reply(session, tool_call, tr("Rutyny:\n", "Routines:\n")
              + ("\n".join(f"- {r.describe()}" for r in found) or tr("(brak)", "(none)")))
        return

    if operation == "add":
        routine = routines.Routine(
            name=name,
            schedule=str(args.get("schedule", "") or ""),
            task=str(args.get("task", "") or ""),
            target=str(args.get("target", "local") or "local").strip().lower(),
            notify=str(args.get("notify", "always") or "always").strip().lower(),
        )
        try:
            routines.validate(routine)
            if targets.get_target(routine.target) is None:
                raise routines.RoutineError(tr(f"Nie ma celu {routine.target!r} (target_manage operation=list).",
                                               f"No target {routine.target!r} (target_manage operation=list)."))
        except routines.RoutineError as exc:
            reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
            return

        async def add() -> str:
            created = routines.save_routine(routine)
            return tr(f"{'Dodano' if created else 'Zaktualizowano'} rutyne {routine.name}: {routine.describe()}",
                      f"{'Added' if created else 'Updated'} routine {routine.name}: {routine.describe()}")

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="routine_manage",
            command=tr(f"rutyna {routine.describe()}", f"routine {routine.describe()}"),
            classification="confirm", action=add,
            plan=safety.Plan(local_files=[(str(routines.routines_path()), tr("pamiec Pipe: routines.json",
                                                                             "Pipe memory: routines.json"))]),
        )
        # describe() skraca zadanie — w potwierdzeniu musi byc CALE, bo cale bedzie wykonywane bez nadzoru
        yield tr(f"[POTWIERDZ] Nowa rutyna wymaga potwierdzenia: {as_code(routine.describe())}\n"
                 f"Pelna tresc zadania:\n{as_code(routine.task)}",
                 f"[POTWIERDZ] A new routine requires confirmation: {as_code(routine.describe())}\n"
                 f"Full task text:\n{as_code(routine.task)}")
        return

    if operation in ("remove", "enable", "disable"):
        if operation == "remove":
            ok = routines.remove_routine(name)
        else:
            ok = routines.update_routine(name, enabled=operation == "enable") is not None
        if ok:
            await audit.log_file_write(session.interface, f"routines.json#{name} ({operation})", 0)
        reply(session, tool_call, tr(f"Rutyna {name}: {operation} — {'gotowe' if ok else 'nie ma takiej rutyny'}.",
                                     f"Routine {name}: {operation} — {'done' if ok else 'no such routine'}."))
        return

    if operation == "run":
        routine = next((r for r in routines.load_routines() if r.name == name), None)
        if routine is None:
            reply(session, tool_call, tr(f"Nie ma rutyny {name!r}.", f"No routine {name!r}."))
            return
        yield Progress(tr(f"Uruchamiam rutyne {name}...", f"Running routine {name}..."), source=f"routine:{name}")
        event = await get_watcher(agent).run_routine(routine, forced=True)
        reply(session, tool_call, tr(f"Wynik rutyny {name} (status {event['status']}):\n",
                                     f"Routine {name} result (status {event['status']}):\n") + event["report"])
        return

    reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: list, add, remove, enable, disable, run.",
                                 f"Unknown operation {operation!r}. Available: list, add, remove, enable, disable, run."))
