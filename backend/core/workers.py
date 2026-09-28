"""
Workery — pod-agenty, z ktorymi rozmawia agent, a nie uzytkownik.

Agent (narzedzie `delegate`) wysyla kilka workerow naraz: kazdy dostaje jeden
cel (core/targets.py) i jedno zadanie, dziala na wlasnej historii rozmowy
z LLM (opcjonalnie tanszym modelem: WORKER_MODEL) i ma jedno narzedzie —
`run` na swoim celu.

Workery sa bezpieczne z konstrukcji:
  - wykonuja tylko komendy sklasyfikowane jako `safe`,
  - komendy `confirm` NIE sa wykonywane — trafiaja do raportu jako propozycje,
    ktore glowny agent wykonuje przez remote_exec, czyli za zgoda uzytkownika,
  - `forbidden` jest odrzucane i logowane jak zawsze.
Dlatego workery moga dzialac rownolegle, w tle i wedlug harmonogramu (rutyny)
bez nikogo przy klawiaturze.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from backend.config import settings
from backend.config.prompts import WORKER_SYSTEM_PROMPT
from backend.core import audit, executor, runtime, targets
from backend.core.events import Event, Progress
from backend.core.handlers.common import format_result, reply
from backend.core.security import classify_command
from backend.core.session import Session

WORKER_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": (
                "Wykonuje komende shell na Twoim celu i zwraca wynik. Tylko odczyty — komendy zmieniajace "
                "stan nie zostana wykonane (zapisz je w PROPOZYCJACH raportu). Zawsze ograniczaj output."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Komenda shell do wykonania na celu."}},
                "required": ["command"],
            },
        },
    }
]

_WORKER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass
class WorkerResult:
    name: str
    target: str
    report: str = ""
    proposals: list[str] = field(default_factory=list)
    commands: int = 0
    error: str = ""

    def render(self) -> str:
        head = f"=== WORKER {self.name} (cel: {self.target}, komend: {self.commands}) ==="
        if self.error:
            body = f"BLAD: {self.error}"
            if self.report:
                body += f"\nCzesciowy raport:\n{self.report}"
        else:
            body = self.report.strip() or "(worker nie zwrocil raportu)"
        if self.proposals:
            body += "\nKomendy zablokowane jako zmieniajace stan (do wykonania przez remote_exec za zgoda uzytkownika):\n"
            body += "\n".join(f"- {p}" for p in self.proposals)
        return f"{head}\n{body}"


def worker_name(requested: str, target: str, taken: set[str]) -> str:
    base = (requested or target or "worker").strip().lower()
    base = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-")[:28] or "worker"
    name, index = base, 2
    while name in taken:
        name = f"{base}-{index}"
        index += 1
    return name


async def run_worker(
    agent: Any,
    parent: Session,
    name: str,
    target: targets.Target,
    task: str,
    progress: asyncio.Queue | None = None,
    *,
    extra_instructions: str = "",
) -> WorkerResult:
    """
    Uruchamia jednego workera do konca (raport albo limit krokow). Historia
    zostaje w parent.workers[name] — kolejne zadanie dla tej samej nazwy
    kontynuuje rozmowe.
    """
    result = WorkerResult(name=name, target=target.name)
    history = parent.workers.setdefault(name, [])
    history.append({"role": "user", "content": task + extra_instructions})
    session = Session(
        session_id=f"{parent.session_id}/worker:{name}",
        interface=f"worker:{name}@{parent.interface}",
        messages=history,
        learns_vibe=False,
    )

    async def notify(text: str) -> None:
        if progress is not None:
            await progress.put(Progress(text, source=f"worker:{name}"))

    async def dispatch(session_: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
        command = str(args.get("command", "")).strip()
        if tool_call.function.name != "run" or not command:
            reply(session_, tool_call, "Blad: jedyne narzedzie to run z parametrem command.")
            return
        try:
            wrapped = targets.wrap(target, command)
        except targets.TargetError as exc:
            reply(session_, tool_call, f"Blad: {exc}")
            return
        classification = classify_command(command)
        if classification == "forbidden":
            await audit.log_blocked(session_.interface, f"worker({wrapped})")
            reply(session_, tool_call, "ODMOWA SYSTEMOWA: komenda jest zakazana.")
            return
        if classification == "confirm":
            result.proposals.append(command)
            reply(session_, tool_call, "NIE WYKONANO: komenda zmienia stan albo nie jest rozpoznanym odczytem. "
                                       "Jesli jest potrzebna, umiesc ja w PROPOZYCJACH raportu.")
            return
        result.commands += 1
        await notify(f"{name} $ {command[:160]}")
        cwd = runtime.to_local("/") if target.kind == "local" else None
        stdout, stderr, exit_code = await executor.execute(wrapped, cwd=cwd)
        await audit.log_safe(session_.interface, wrapped, exit_code)
        reply(session_, tool_call, format_result(stdout, stderr, exit_code))
        return
        yield  # noqa: unreachable — async generator

    prompt = WORKER_SYSTEM_PROMPT.format(target=f"{target.describe()}\n{target.command_hint()}")
    await notify(f"{name}: start ({target.name})")
    try:
        async for event in agent.run_loop(
            session,
            system_prompt=prompt,
            tools=WORKER_TOOLS,
            model=settings.WORKER_MODEL or None,
            max_iterations=settings.WORKER_MAX_ITERATIONS,
            dispatch=dispatch,
        ):
            if isinstance(event, str) and event.strip():
                result.report = event
    except Exception as exc:
        result.error = str(exc) or type(exc).__name__
    await notify(f"{name}: gotowe ({result.commands} komend" + (f", {len(result.proposals)} propozycji)" if result.proposals else ")"))
    return result


async def run_many(
    agent: Any,
    parent: Session,
    specs: list[tuple[str, targets.Target, str]],
    *,
    extra_instructions: str = "",
) -> AsyncGenerator[Progress | list[WorkerResult], None]:
    """
    Uruchamia workery rownolegle. Yielduje Progress na biezaco, a na koncu
    liste WorkerResult (w kolejnosci specs).
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def one(name: str, target: targets.Target, task: str) -> WorkerResult:
        try:
            return await asyncio.wait_for(
                run_worker(agent, parent, name, target, task, queue, extra_instructions=extra_instructions),
                timeout=settings.WORKER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return WorkerResult(name, target.name, error=f"przekroczono limit czasu {settings.WORKER_TIMEOUT} s")
        except Exception as exc:
            return WorkerResult(name, target.name, error=str(exc) or type(exc).__name__)

    runner = asyncio.ensure_future(asyncio.gather(*(one(*spec) for spec in specs)))
    while not runner.done():
        getter = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({runner, getter}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield getter.result()
        else:
            getter.cancel()
    while not queue.empty():
        yield queue.get_nowait()
    yield runner.result()
