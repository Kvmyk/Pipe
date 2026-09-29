"""
Handler dla operacji na plikach (read/write).

Model podaje sciezki hosta (`/etc/nginx/nginx.conf`), wzgledne (od katalogu
roboczego) albo lokalne z prefiksem HOST_ROOT — handler zamienia je na
sciezke widziana przez proces Pipe (core/runtime.py).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.core import audit, executor, runtime, safety
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.memory import REDACTION_MARK
from backend.core.security import classify_file_write, is_sensitive, resolve_local, validate_workspace_access
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code

MAX_READ_CHARS = 200_000


def resolve_path(path: str, session: Session) -> tuple[str, str]:
    """(sciezka hosta, sciezka lokalna) dla sciezki podanej przez model."""
    host = runtime.to_host(path, session.cwd)
    return host, runtime.to_local(host)


def checked_local(local: str) -> tuple[str | None, str]:
    """
    (sciezka do otwarcia, powod odmowy). Sciezka jest rozwiazana wzgledem
    korzenia hosta — symlink hosta `/etc/a -> /etc/b` prowadzi do `/hostfs/etc/b`,
    a nie do pliku kontenera.
    """
    allowed, reason = validate_workspace_access(local)
    if not allowed:
        return None, reason
    try:
        return resolve_local(local), ""
    except (OSError, ValueError) as exc:
        return None, f"Nieprawidlowa sciezka: {exc}"


async def _read_for_model(session: Session, local: str, host: str) -> str:
    """Tresc pliku dla modelu (po stronie agenta i tak przechodzi przez redakcje sekretow)."""
    try:
        content = await executor.read_file(local)
        await audit.log_file_read(session.interface, host)
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + f"\n[... plik ma {len(content)} znakow, pokazano poczatek — uzyj tail/grep ...]"
        return content if content else "(plik jest pusty)"
    except FileNotFoundError:
        return f"Blad: plik nie istnieje: {host}"
    except PermissionError:
        return f"Blad: brak uprawnien do odczytu: {host}"
    except Exception as exc:
        return f"Blad odczytu pliku: {exc}"


async def handle_read_file(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie read_file."""
    path = str(args.get("path", "")).strip()
    if not path:
        reply(session, tool_call, "Blad: pusta sciezka")
        return

    host, local = resolve_path(path, session)
    target, reason = checked_local(local)
    if target is None:
        await audit.log_blocked(session.interface, f"read_file({host}): {reason}")
        yield f"[ODMOWA] {reason}"
        reply(session, tool_call, f"ODMOWA SYSTEMOWA: {reason}")
        return

    real_host = runtime.to_host(target)
    if is_sensitive(host) or is_sensitive(real_host):
        # Te same pliki co przy `cat` (.env, klucze, /proc/*/environ) — tresc poszlaby do providera LLM.
        async def read_confirmed() -> str:
            return await _read_for_model(session, target, host)

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="read_file", command=f"read_file({host})",
            classification="confirm", action=read_confirmed,
        )
        yield (f"[POTWIERDZ] Odczyt pliku {as_code(host)} wymaga potwierdzenia — to plik z sekretami, "
               "jego tresc trafi do providera LLM.")
        return

    # Wynik trafia wylacznie do LLM (po redakcji sekretow) — uzytkownik dostaje
    # odpowiedz sformulowana przez model, a nie surowa tresc pliku.
    reply(session, tool_call, await _read_for_model(session, target, host))


async def handle_write_file(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie write_file — zawsze wymaga potwierdzenia."""
    path = str(args.get("path", "")).strip()
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)

    if not path:
        reply(session, tool_call, "Blad: pusta sciezka")
        return

    if REDACTION_MARK in content:
        # Model widzial plik z ukrytymi sekretami — zapis calosci nadpisalby prawdziwe wartosci znacznikami.
        reply(session, tool_call,
              "ODMOWA SYSTEMOWA: tresc zawiera znaczniki [ZREDAGOWANO: ...]. Zapis nadpisalby prawdziwe sekrety. "
              "Zmien plik punktowo (np. sed -i na konkretnej linii) albo popros uzytkownika o reczna edycje.")
        return

    host, local = resolve_path(path, session)
    target, reason = checked_local(local)
    if target is None:
        await audit.log_blocked(session.interface, f"write_file({host}): {reason}")
        yield f"[ODMOWA] {reason}"
        reply(session, tool_call, f"ODMOWA SYSTEMOWA: {reason}")
        return
    local = target  # zapis idzie tam, dokad prowadzi symlink na hoscie

    if classify_file_write(host) == "forbidden" or classify_file_write(runtime.to_host(target)) == "forbidden":
        await audit.log_blocked(session.interface, f"write_file({host})")
        yield f"[ODMOWA] Nie moge zapisac do {as_code(host)}. Ta sciezka jest chroniona."
        reply(session, tool_call, "ODMOWA SYSTEMOWA: Zapis do tej sciezki jest zakazany.")
        return

    # Zapis pliku zawsze wymaga potwierdzenia — pokazujemy uzytkownikowi, CO sie zmieni
    plan = await safety.plan_for_write(runtime.to_host(local))
    session.pending_confirmation = ConfirmationRequest(
        tool_call_id=tool_call.id,
        tool_name="write_file",
        command=f"write_file(path={host}, content=<{len(content)} znakow>)",
        classification="confirm",
        file_path=local,
        file_content=content,
        plan=plan,
    )
    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    message = [f"[POTWIERDZ] Zapis pliku {as_code(host)} ({len(content)} znakow, {lines} linii)"]
    warning = startup_file_warning(host) or startup_file_warning(runtime.to_host(local))
    if warning:
        message.append(f"UWAGA: {warning}")
    message.append(preview_change(local, content))
    described = plan.describe()
    if described:
        message.append(described)
    yield "\n".join(message)


# Pliki uruchamiane automatycznie przy logowaniu/starcie — zapis = trwaly backdoor.
_STARTUP_PATTERNS = (
    r"/\.(bashrc|bash_profile|profile|zshrc|zprofile|zshenv|bash_login|bash_logout)$",
    r"/\.ssh/(authorized_keys|config)$",
    r"/\.config/systemd/", r"/etc/systemd/", r"/\.gitconfig$", r"/\.git/hooks/",
    r"/etc/cron", r"/var/spool/cron", r"/etc/profile\.d/", r"/etc/rc\.local$",
    r"/\.(profile|bashrc)\.d/",
)


def startup_file_warning(host: str) -> str:
    import re
    if any(re.search(p, host) for p in _STARTUP_PATTERNS):
        return ("to plik uruchamiany automatycznie (przy logowaniu, starcie albo przez cron/systemd). "
                "Zapis moze zalozyc trwaly dostep do serwera — potwierdzaj tylko, jesli sam o to prosiles.")
    return ""


def preview_change(local: str, content: str, max_lines: int = 40) -> str:
    """Diff nowej tresci wzgledem obecnej (albo podglad, gdy plik jest nowy)."""
    import difflib
    from pathlib import Path

    try:
        old = Path(local).read_text(encoding="utf-8", errors="replace") if Path(local).is_file() else None
    except OSError:
        old = None

    if old is None:
        body = content.splitlines()
        shown = body[:max_lines]
        text = "\n".join(shown) + (f"\n[... i {len(body) - len(shown)} linii]" if len(body) > len(shown) else "")
        return "Nowy plik, tresc:\n" + as_code(text or "(pusty)")
    if old == content:
        return "Tresc bez zmian (plik nadpisany identyczna zawartoscia)."
    diff = list(difflib.unified_diff(old.splitlines(), content.splitlines(),
                                     fromfile="obecny", tofile="nowy", lineterm=""))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"[... i {len(diff) - max_lines} linii roznicy]"]
    return "Zmiany:\n" + as_code("\n".join(diff))
