"""
Handlery zdalnych celow i workerow: target_manage, remote_exec, delegate.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import audit, runtime, targets, workers
from backend.core.events import Event, Progress
from backend.core.handlers.common import reply, run_classified
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code


async def handle_target_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """list | add | remove | test — rejestr zdalnych celow."""
    operation = str(args.get("operation", "list") or "list").strip().lower()
    name = str(args.get("name", "") or "").strip().lower()

    if operation == "list":
        found = targets.load_targets()
        lines = [f"- {t.describe()}" for t in found]
        reply(session, tool_call, "Zdalne cele:\n" + ("\n".join(lines) or "(brak — dodaj: operation=add)")
              + "\n- local: ten host (zawsze dostepny)")
        return

    if operation == "add":
        fields = {k: args.get(k) for k in ("kind", "description", "host", "user", "port", "identity_file",
                                            "container", "context", "namespace", "pod", "pod_container")
                  if args.get(k) not in (None, "")}
        try:
            target = targets.validate(targets.Target(name=name, **fields))
            if target.name == "local":
                raise targets.TargetError("Nazwa 'local' jest zarezerwowana dla tego hosta.")
        except (targets.TargetError, TypeError) as exc:
            reply(session, tool_call, f"Blad: {exc}")
            return

        async def add() -> str:
            created = targets.save_target(target)
            return (f"{'Dodano' if created else 'Zaktualizowano'} cel {target.name}. "
                    f"Sprawdz lacznosc: target_manage operation=test name={target.name}.")

        description = f"dodanie celu {target.describe()}"
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="target_manage", command=description,
            classification="confirm", action=add,
        )
        yield f"[POTWIERDZ] Nowy cel zdalny wymaga potwierdzenia: {as_code(target.describe())}"
        return

    if operation == "remove":
        if targets.remove_target(name):
            await audit.log_file_write(session.interface, f"targets.json#{name} (usuniety)", 0)
            reply(session, tool_call, f"Usunieto cel {name}.")
        else:
            reply(session, tool_call, f"Nie ma celu {name!r}.")
        return

    if operation == "test":
        target = targets.get_target(name)
        if target is None:
            reply(session, tool_call, f"Nie ma celu {name!r}. Lista: target_manage operation=list.")
            return
        command = targets.test_command(target)
        async for event in run_classified(session, tool_call, targets.wrap(target, command), tool_name="remote_exec",
                                          inner=command, cwd=runtime.to_local("/")):
            yield event
        return

    reply(session, tool_call, f"Nieznana operacja {operation!r}. Dostepne: list, add, remove, test.")


async def handle_remote_exec(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Jedna komenda na zdalnym celu — klasyfikacja jak execute_command, na komendzie docelowej."""
    name = str(args.get("target", "") or "").strip()
    command = str(args.get("command", "") or "").strip()
    target = targets.get_target(name)
    if target is None:
        reply(session, tool_call, f"Nie ma celu {name!r}. Lista: target_manage operation=list.")
        return
    try:
        wrapped = targets.wrap(target, command)
    except targets.TargetError as exc:
        reply(session, tool_call, f"Blad: {exc}")
        return
    cwd = None if target.kind == "local" else runtime.to_local("/")
    async for event in run_classified(session, tool_call, wrapped, tool_name="remote_exec", inner=command,
                                      cwd=cwd, what=f"Komenda na celu {target.name}"):
        yield event


async def handle_delegate(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Wysyla workerow rownolegle i zwraca modelowi ich raporty."""
    tasks = args.get("tasks")
    if isinstance(tasks, dict):
        tasks = [tasks]
    if not isinstance(tasks, list) or not tasks:
        reply(session, tool_call, "Blad: podaj tasks — liste obiektow {target, task, name?}.")
        return
    if len(tasks) > settings.MAX_WORKERS:
        reply(session, tool_call, f"Blad: najwyzej {settings.MAX_WORKERS} workerow naraz.")
        return

    specs: list[tuple[str, targets.Target, str]] = []
    taken: set[str] = set()
    errors = []
    for item in tasks:
        if not isinstance(item, dict):
            errors.append("kazde zadanie musi byc obiektem {target, task}")
            continue
        target = targets.get_target(str(item.get("target", "local") or "local"))
        task = str(item.get("task", "") or "").strip()
        if target is None:
            errors.append(f"nie ma celu {item.get('target')!r}")
            continue
        if not task:
            errors.append(f"brak tresci zadania dla celu {target.name}")
            continue
        requested = str(item.get("name", "") or "")
        # Ta sama nazwa co wczesniej = kontynuacja rozmowy z tym workerem
        name = requested.strip().lower() if requested.strip().lower() in session.workers and requested.strip().lower() not in taken \
            else workers.worker_name(requested, target.name, taken | set(session.workers))
        taken.add(name)
        specs.append((name, target, task))
    if errors:
        reply(session, tool_call, "Blad: " + "; ".join(errors) + ". Lista celow: target_manage operation=list.")
        return

    yield Progress("Wysylam workerow: " + ", ".join(f"{n} -> {t.name}" for n, t, _ in specs), source="delegate")
    results: list[workers.WorkerResult] = []
    async for item in workers.run_many(agent, session, specs):
        if isinstance(item, Progress):
            yield item
        else:
            results = item
    reply(session, tool_call, "\n\n".join(r.render() for r in results)
          + "\n\nPodsumuj uzytkownikowi wyniki. Zmiany z propozycji wykonuj przez remote_exec (za zgoda).")
