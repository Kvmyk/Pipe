"""
Wspolne elementy handlerow narzedzi.

Kazdy handler musi dopisac do `session.messages` dokladnie jedna odpowiedz
narzedzia (`reply`) albo ustawic `session.pending_confirmation` — wtedy
odpowiedz dopisze `agent.confirm()` po decyzji uzytkownika.
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator

from backend.core import audit, executor, runtime, safety
from backend.core.events import Event
from backend.core.security import Classification, classify_command
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code, visible
from backend.core.i18n import tr


def reply(session: Session, tool_call: Any, content: str) -> None:
    """Dopisuje wynik narzedzia dla modelu."""
    session.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})


def format_result(stdout: str, stderr: str, exit_code: int) -> str:
    """Wynik komendy dla modelu. Uzytkownik widzi z niego tylko wycinek (output_excerpt), odpowiedz pisze model."""
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"STDOUT:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"STDERR:\n{stderr.strip()}")
    parts.append(f"EXIT CODE: {exit_code}")
    return "\n".join(parts)


# Wycinek wyniku pod komenda w interfejsach (zdarzenie Activity): przy bledzie linie z bledem, przy sukcesie koniec.
EXCERPT_LINES = 3
EXCERPT_WIDTH = 160
OUTPUT_LINES = 60
OUTPUT_CHARS = 4000
_EXIT = re.compile(r"(?m)^EXIT CODE: (-?\d+)$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]")
_ERROR_LINE = re.compile(r"error|fail|denied|not found|cannot|can't|unable|fatal|invalid|refused|timed out|no such|"
                         r"blad|nie udalo|odmow", re.IGNORECASE)
_FUSE_ALARM = ("WERYFIKACJA NIE PRZESZLA", "VERIFICATION FAILED", "NIE WYKONANO", "NOT EXECUTED", "Przywrocilem",
               "Restored")


def output_excerpt(result: str) -> tuple[str, str, int | None]:
    """
    (wycinek, dluzszy wynik, kod wyjscia) z wyniku narzedzia — tego samego, ktory dostal model (po redakcji sekretow
    i przycieciu), wiec sekretow tu juz nie ma. Wycinek: przy kodzie != 0 do 3 linii z bledem i ostatnia linia,
    przy sukcesie ostatnie 3 niepuste linie; do tego alarm bezpiecznika (weryfikacja nie przeszla, przywrocono).
    Bez modelu, linie przyciete, znaki sterujace usuniete.
    """
    text = result or ""
    match = _EXIT.search(text)
    if match is None:
        lines = _clean_lines(text)
        return "\n".join(_clip(line) for line in lines[:EXCERPT_LINES]), _full(lines), None
    code = int(match.group(1))
    head, tail = text[:match.start()], text[match.end():]
    stdout, _, stderr = head.partition("STDERR:\n")
    stdout = stdout.removeprefix("STDOUT:\n")
    out, err, fuse = _clean_lines(stdout), _clean_lines(stderr), _clean_lines(tail)
    out = [line for line in out if line not in executor.NO_OUTPUT]       # "bez outputu" — klient pokaze sam znacznik
    if code != 0:
        picked = [line for line in err + out if _ERROR_LINE.search(line)][:EXCERPT_LINES - 1]
        for line in (err or out)[-1:]:
            if line not in picked:
                picked.append(line)
    else:
        picked = (out or err)[-EXCERPT_LINES:]
    picked += [line for line in fuse if line.startswith(_FUSE_ALARM)][:1]
    return "\n".join(_clip(line) for line in picked), _full(out + err + fuse), code


def _clean_lines(text: str) -> list[str]:
    return [visible(_ANSI.sub("", line)).rstrip() for line in text.splitlines() if line.strip()]


def _clip(line: str) -> str:
    return line if len(line) <= EXCERPT_WIDTH else line[:EXCERPT_WIDTH - 1] + "…"


def _full(lines: list[str]) -> str:
    text = "\n".join(lines[-OUTPUT_LINES:])
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-(OUTPUT_CHARS - 1):]


def local_cwd(session: Session) -> str:
    """Katalog roboczy sesji tak, jak widzi go proces Pipe."""
    return runtime.to_local(session.cwd)


async def run_classified(
    session: Session,
    tool_call: Any,
    command: str,
    *,
    tool_name: str,
    cwd: str | None = None,
    classification: Classification | None = None,
    what: str = "",
    inner: str | None = None,
    shown: str | None = None,
    plan: safety.Plan | None = None,
    always_ask: bool = False,
) -> AsyncGenerator[Event, None]:
    """
    Wspolny przebieg dla narzedzi uruchamiajacych komende powloki:
    forbidden -> odmowa, confirm -> pytanie o potwierdzenie, safe -> wykonanie.

    `command` to dokladnie to, co zostanie uruchomione (i pokazane w potwierdzeniu).
    `inner` — gdy komenda jest opakowaniem (ssh/kubectl exec), klasyfikujemy
    polecenie docelowe, a nie opakowanie.
    `shown` — co pokazac w potwierdzeniu zamiast `command`: tylko gdy opakowanie jest stale i zbudowane przez
    kod (hostexec: kontener pomocniczy + nsenter), a `inner` trafia do niego jako jeden argument bez zmian.
    `plan` — gotowy plan bezpiecznika zamiast wyliczanego z komendy.
    `always_ask` — pytanie nawet o rutynowa zmiane (model sam poprosil o potwierdzenie, core/autonomy.py).
    """
    classification = classification or classify_command(inner if inner is not None else command)

    if classification == "forbidden":
        await audit.log_blocked(session.interface, f"{tool_name}({command})")
        yield tr(f"[ODMOWA] Komenda {as_code(inner or command)} jest zabroniona przez polityke bezpieczenstwa.",
                 f"[ODMOWA] The command {as_code(inner or command)} is forbidden by the security policy.")
        reply(session, tool_call, tr("ODMOWA SYSTEMOWA: komenda jest na liscie zakazanych operacji. Nie probuj jej obejsc.",
                                     "SYSTEM REFUSAL: the command is on the list of forbidden operations. "
                                     "Do not try to get around it."))
        return

    if classification == "confirm":
        # Plan bezpiecznika tylko dla komend wykonywanych lokalnie — na zdalnym celu
        # (ssh/kubectl exec) pliki i uslugi sa po drugiej stronie.
        remote = plan is None and inner is not None and inner != command
        if plan is None:
            plan = await safety.plan_for_command(command, session.cwd) if not remote \
                else safety.Plan(notes=[tr("zdalny cel — bez kopii i weryfikacji po stronie Pipe",
                                           "remote target — no backup or verification on the Pipe side")])
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name=tool_name,
            command=command,
            classification="confirm",
            plan=plan,
            inner=inner if inner is not None and inner != command else None,
            remote=remote,
            auto=False if always_ask else None,
        )
        described = plan.describe()
        what = what or tr("Operacja", "Operation")
        yield tr(f"[POTWIERDZ] {what} wymaga potwierdzenia: {as_code(shown or command)}",
                 f"[POTWIERDZ] {what} requires confirmation: {as_code(shown or command)}") + (f"\n{described}" if described else "")
        return

    stdout, stderr, exit_code = await executor.execute(command, cwd=cwd if cwd is not None else local_cwd(session))
    await audit.log_safe(session.interface, command, exit_code)
    reply(session, tool_call, format_result(stdout, stderr, exit_code))
