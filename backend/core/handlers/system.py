"""
Handler dla operacji systemowych (change_directory, system_stats, network_info, cron_manage).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from typing import Any, AsyncGenerator

from backend.core import executor, hostinfo, runtime
from backend.core.events import Event
from backend.core.handlers.common import reply, run_classified
from backend.core.security import classify_command, validate_workspace_access
from backend.core.session import Session

# Nadpisanie katalogu /proc hosta (testy). None = runtime.host_proc().
HOSTPROC: str | None = None


def _proc() -> str:
    return HOSTPROC or runtime.host_proc()


def host_path(path: str, cwd: str) -> str:
    """Sciezka podana przez model -> sciezka hosta (patrz runtime.to_host)."""
    return runtime.to_host(path, cwd)


def parse_proc_net(text: str, proto: str, established: bool = False) -> list[str]:
    """Gniazda z /proc/<pid>/net/<proto> jako czytelne linie (patrz hostinfo.parse_sockets)."""
    return [str(s) for s in hostinfo.parse_sockets(text, proto, established)]


# ─── change_directory ───────────────────────────────────────────────────────

async def handle_change_directory(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie change_directory."""
    raw = str(args.get("path", "")).strip()
    if not raw:
        reply(session, tool_call, "Błąd: pusta ścieżka")
        return

    new_cwd = host_path(raw, session.cwd)
    local = runtime.to_local(new_cwd)
    allowed, reason = validate_workspace_access(local)
    if not allowed:
        result = f"Błąd: {reason}"
    elif not os.path.isdir(local):
        result = f"Błąd: katalog nie istnieje na serwerze: {new_cwd}"
    else:
        session.cwd = new_cwd
        result = f"Katalog zmieniony na: {new_cwd}"

    reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


# ─── system_stats ───────────────────────────────────────────────────────────

def stats_summary(proc: str | None = None) -> str:
    """Podsumowanie stanu hosta dla modelu — czytane prosto z /proc hosta."""
    lines = ["[STAN HOSTA]"]
    uptime = hostinfo.uptime_seconds(proc)
    if uptime is not None:
        lines.append(f"Uptime: {hostinfo.format_duration(uptime)}")
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load:
        lines.append(f"Load average (1m/5m/15m): {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f} "
                     f"przy {cpus} rdzeniach (obciazenie 5m: {round(load[1] * 100 / cpus)}%)")
    mem = hostinfo.memory(proc)
    if mem:
        lines.append(f"RAM: {mem.used_mb} MB uzyte z {mem.total_mb} MB ({mem.used_pct}%), "
                     f"dostepne {mem.available_mb} MB")
        if mem.swap_total_mb:
            lines.append(f"Swap: {mem.swap_used_mb} MB z {mem.swap_total_mb} MB")
    for disk in hostinfo.disks(proc):
        lines.append(f"Dysk {disk.mount} ({disk.fstype}): {disk.used_pct}% zajete, wolne "
                     f"{hostinfo.human_bytes(disk.available)} z {hostinfo.human_bytes(disk.total)}")
    return "\n".join(lines)


async def handle_system_stats(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """
    Obsluguje narzedzie system_stats. Dane pochodza z /proc hosta
    (runtime.host_proc()), wiec opisuja serwer, a nie kontener.
    """
    stat_type = str(args.get("stat_type", "summary") or "summary").strip().lower()
    proc = _proc()
    try:
        result = await asyncio.to_thread(stats_summary, proc)
        if stat_type in ("processes", "process", "all"):
            stdout, stderr, code = await executor.execute("ps aux --sort=-%cpu | head -15")
            result += f"\n\n[PROCESY — najwiecej CPU]\n{stdout}{stderr}"
            stdout, stderr, code = await executor.execute("ps aux --sort=-%mem | head -8")
            result += f"\n[PROCESY — najwiecej RAM]\n{stdout}{stderr}"
        if stat_type in ("cpu", "all"):
            result += "\n\n[/proc/stat]\n" + "\n".join(hostinfo._read(f"{proc}/stat").splitlines()[:cpu_lines(proc)])
        if stat_type in ("memory", "all"):
            result += "\n\n[/proc/meminfo]\n" + "\n".join(hostinfo._read(f"{proc}/meminfo").splitlines()[:16])
    except Exception as exc:
        result = f"Błąd pobrania statystyk: {exc}"

    reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


def cpu_lines(proc: str) -> int:
    return min(hostinfo.cpu_count(proc) + 1, 17)


# ─── network_info ───────────────────────────────────────────────────────────

async def handle_network_info(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie network_info."""
    check_type = str(args.get("check_type", "listeners")).strip()
    target = str(args.get("target", "")).strip()

    try:
        if check_type in ("ports", "listeners", "connections"):
            established = check_type == "connections"
            found = hostinfo.sockets(_proc(), established)
            label = "POLACZENIA HOSTA" if established else "NASLUCHUJACE PORTY HOSTA"
            lines = []
            for sock in found:
                note = "  (publiczny)" if (not established and sock.public) else ""
                lines.append(f"{sock}{note}")
            result = f"[{label}]\n" + ("\n".join(lines) or "(brak)")
        elif check_type in ("ping", "curl", "dns"):
            if not target:
                result = f"Błąd: {check_type} wymaga parametru target"
            else:
                quoted = shlex.quote(target)
                cmd = {
                    "ping": f"ping -c 3 -W 2 {quoted}",
                    "curl": f"curl -sS -o /dev/null -m 10 -w 'HTTP %{{http_code}} w %{{time_total}}s\\n' {quoted}",
                    "dns": f"getent ahosts {quoted}",
                }[check_type]
                stdout, stderr, exit_code = await executor.execute(cmd)
                result = f"[{check_type.upper()}] {cmd}\n{stdout}{stderr}"
                if exit_code != 0:
                    result += f"\n[EXIT CODE] {exit_code}"
        else:
            result = f"Błąd: nieznany check_type: {check_type}"
    except Exception as exc:
        result = f"Błąd pobrania info sieciowych: {exc}"

    reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


# ─── cron_manage ────────────────────────────────────────────────────────────

# Pliki crona hosta (sciezki hosta) — czytane bezposrednio, bo w kontenerze nie
# ma binarki crontab, a /var/spool jest poza workspace narzedzi plikowych.
HOST_CRON_FILES = ("/etc/crontab",)
HOST_CRON_DIRS = ("/etc/cron.d", "/var/spool/cron/crontabs", "/var/spool/cron")


def read_host_crontabs() -> str:
    sections = []
    paths = [runtime.to_local(p) for p in HOST_CRON_FILES]
    for directory in HOST_CRON_DIRS:
        local = runtime.to_local(directory)
        try:
            paths += [os.path.join(local, name) for name in sorted(os.listdir(local))
                      if os.path.isfile(os.path.join(local, name))]
        except OSError:
            continue
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = [l for l in fh.read().splitlines() if l.strip() and not l.lstrip().startswith("#")]
        except OSError:
            continue
        sections.append(f"# {runtime.to_host(path)}\n" + ("\n".join(lines) or "(brak wpisow)"))
    return "\n\n".join(sections) or "(nie znaleziono plikow crona na hoscie)"


def _cron_entry(args: dict[str, Any]) -> str:
    """Wpis crona z argumentow: schedule + command (schemat) albo cron_entry (stary format)."""
    legacy = str(args.get("cron_entry", "") or "")
    if legacy.strip():
        return legacy
    schedule = str(args.get("schedule", "") or "").strip()
    command = str(args.get("command", "") or "")
    if schedule and command.strip():
        return f"{schedule} {command.strip()}"
    return command if command.strip() else ""


def _can_edit_crontab() -> bool:
    """Crontab edytujemy tylko natywnie — w kontenerze zmienilibysmy cron kontenera, nie hosta."""
    return runtime.kind() == "native"


async def handle_cron_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie cron_manage: list, add, remove, check-logs."""
    operation = str(args.get("operation") or args.get("action") or "list").strip()

    if operation in ("add", "remove"):
        entry = _cron_entry(args)
        if not entry.strip():
            field = "schedule i command" if operation == "add" else "command (dokladna linia crontaba)"
            reply(session, tool_call, f"Błąd: podaj {field} (albo cron_entry — pelna linie crontaba).")
            return
        if not _can_edit_crontab():
            reply(session, tool_call,
                  "Edycja crona hosta jest niedostepna w tym trybie (Pipe dziala w kontenerze, a system plikow "
                  "hosta jest tylko do odczytu). Zaproponuj uzytkownikowi reczne dodanie wpisu (podaj dokladna "
                  "linie) albo rutyne Pipe (routine_manage) dla zadan, ktore moze wykonywac agent.")
            return
        if operation == "add":
            # printf zamiast echo — echo w sh interpretuje backslashe.
            cmd = f"{{ crontab -l 2>/dev/null; printf '%s\\n' {shlex.quote(entry)}; }} | crontab -"
        else:
            # Usuwa wylacznie linie identyczne z wpisem — nigdy calego crontaba.
            cmd = f"crontab -l | grep -vxF -- {shlex.quote(entry)} | crontab -"
        # Klasyfikujemy sam wpis: zakazane polecenie w cronie = zakazane polecenie.
        inner_class = classify_command(entry.split(None, 5)[-1] if not entry.startswith("@") else entry)
        classification = "forbidden" if inner_class == "forbidden" or classify_command(cmd) == "forbidden" else "confirm"
        what = "Dodanie wpisu cron" if operation == "add" else "Usunięcie wpisu cron"
        async for event in run_classified(session, tool_call, cmd, tool_name="cron_manage",
                                          classification=classification, what=what):
            yield event
        return

    if operation == "check-logs":
        cmd = "journalctl -u cron -u crond --since '24 hours ago' --no-pager | tail -n 60"
        if runtime.kind() != "native":
            cmd = f"grep -h CRON {runtime.to_local('/var/log/syslog')} {runtime.to_local('/var/log/cron')} 2>/dev/null | tail -n 60"
        stdout, stderr, code = await executor.execute(cmd)
        reply(session, tool_call, f"[CRON — LOGI]\n{stdout}{stderr}")
        return

    if _can_edit_crontab() and shutil.which("crontab"):
        stdout, stderr, code = await executor.execute("crontab -l")
        result = f"[CRONTAB UZYTKOWNIKA]\n{stdout}{stderr}\n\n[PLIKI SYSTEMOWE]\n{read_host_crontabs()}"
    else:
        result = f"[CRON HOSTA]\n{read_host_crontabs()}"
    reply(session, tool_call, result)
