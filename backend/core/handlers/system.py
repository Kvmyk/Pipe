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
from backend.core.security import classify_command, resolve_local, validate_workspace_access
from backend.core.session import Session
from backend.core.i18n import tr

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
        reply(session, tool_call, tr("Błąd: pusta ścieżka", "Error: empty path"))
        return

    new_cwd = host_path(raw, session.cwd)
    local = runtime.to_local(new_cwd)
    allowed, reason = validate_workspace_access(local)
    if allowed:
        try:
            # symlink hosta (/var/www -> /srv/www) prowadzi do katalogu hosta, nie kontenera
            local = resolve_local(local)
            new_cwd = runtime.to_host(local)
        except (OSError, ValueError) as exc:
            allowed, reason = False, tr(f"Nieprawidlowa sciezka: {exc}", f"Invalid path: {exc}")
    if not allowed:
        result = tr(f"Błąd: {reason}", f"Error: {reason}")
    elif not os.path.isdir(local):
        result = tr(f"Błąd: katalog nie istnieje na serwerze: {new_cwd}", f"Error: directory does not exist on the server: {new_cwd}")
    else:
        session.cwd = new_cwd
        result = tr(f"Katalog zmieniony na: {new_cwd}", f"Working directory changed to: {new_cwd}")

    reply(session, tool_call, result)
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


# ─── system_stats ───────────────────────────────────────────────────────────

def stats_summary(proc: str | None = None) -> str:
    """Podsumowanie stanu hosta dla modelu — czytane prosto z /proc hosta."""
    lines = [tr("[STAN HOSTA]", "[HOST STATE]")]
    uptime = hostinfo.uptime_seconds(proc)
    if uptime is not None:
        lines.append(f"Uptime: {hostinfo.format_duration(uptime)}")
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load:
        lines.append(f"Load average (1m/5m/15m): {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f} " + tr(
            f"przy {cpus} rdzeniach (obciazenie 5m: {round(load[1] * 100 / cpus)}%)",
            f"on {cpus} cores (5m load: {round(load[1] * 100 / cpus)}%)"))
    mem = hostinfo.memory(proc)
    if mem:
        lines.append(tr(f"RAM: {mem.used_mb} MB uzyte z {mem.total_mb} MB ({mem.used_pct}%), "
                        f"dostepne {mem.available_mb} MB",
                        f"RAM: {mem.used_mb} MB used of {mem.total_mb} MB ({mem.used_pct}%), "
                        f"available {mem.available_mb} MB"))
        if mem.swap_total_mb:
            lines.append(tr(f"Swap: {mem.swap_used_mb} MB z {mem.swap_total_mb} MB",
                            f"Swap: {mem.swap_used_mb} MB of {mem.swap_total_mb} MB"))
    for disk in hostinfo.disks(proc):
        free, total = hostinfo.human_bytes(disk.available), hostinfo.human_bytes(disk.total)
        lines.append(tr(f"Dysk {disk.mount} ({disk.fstype}): {disk.used_pct}% zajete, wolne {free} z {total}",
                        f"Disk {disk.mount} ({disk.fstype}): {disk.used_pct}% used, {free} free of {total}"))
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
            result += tr("\n\n[PROCESY — najwiecej CPU]\n", "\n\n[PROCESSES — most CPU]\n") + f"{stdout}{stderr}"
            stdout, stderr, code = await executor.execute("ps aux --sort=-%mem | head -8")
            result += tr("\n[PROCESY — najwiecej RAM]\n", "\n[PROCESSES — most RAM]\n") + f"{stdout}{stderr}"
        if stat_type in ("cpu", "all"):
            result += "\n\n[/proc/stat]\n" + "\n".join(hostinfo._read(f"{proc}/stat").splitlines()[:cpu_lines(proc)])
        if stat_type in ("memory", "all"):
            result += "\n\n[/proc/meminfo]\n" + "\n".join(hostinfo._read(f"{proc}/meminfo").splitlines()[:16])
    except Exception as exc:
        result = tr(f"Błąd pobrania statystyk: {exc}", f"Error reading statistics: {exc}")

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
            label = tr("POLACZENIA HOSTA", "HOST CONNECTIONS") if established else tr("NASLUCHUJACE PORTY HOSTA",
                                                                                          "HOST LISTENING PORTS")
            lines = []
            for sock in found:
                note = tr("  (publiczny)", "  (public)") if (not established and sock.public) else ""
                lines.append(f"{sock}{note}")
            result = f"[{label}]\n" + ("\n".join(lines) or tr("(brak)", "(none)"))
        elif check_type in ("ping", "curl", "dns"):
            if not target:
                result = tr(f"Błąd: {check_type} wymaga parametru target", f"Error: {check_type} requires the target parameter")
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
            result = tr(f"Błąd: nieznany check_type: {check_type}", f"Error: unknown check_type: {check_type}")
    except Exception as exc:
        result = tr(f"Błąd pobrania info sieciowych: {exc}", f"Error reading network info: {exc}")

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
        sections.append(f"# {runtime.to_host(path)}\n" + ("\n".join(lines) or tr("(brak wpisow)", "(no entries)")))
    return "\n\n".join(sections) or tr("(nie znaleziono plikow crona na hoscie)", "(no cron files found on the host)")


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
            field = tr("schedule i command", "schedule and command") if operation == "add" \
                else tr("command (dokladna linia crontaba)", "command (the exact crontab line)")
            reply(session, tool_call, tr(f"Błąd: podaj {field} (albo cron_entry — pelna linie crontaba).",
                                         f"Error: provide {field} (or cron_entry — the full crontab line)."))
            return
        if not _can_edit_crontab():
            reply(session, tool_call,
                  tr("Edycja crona hosta jest niedostepna w tym trybie (Pipe dziala w kontenerze, a system plikow "
                     "hosta jest tylko do odczytu). Zaproponuj uzytkownikowi reczne dodanie wpisu (podaj dokladna "
                     "linie) albo rutyne Pipe (routine_manage) dla zadan, ktore moze wykonywac agent.",
                     "Editing the host cron is not available in this mode (Pipe runs in a container and the host file "
                     "system is read-only). Suggest the user adds the entry manually (give the exact line) or a Pipe "
                     "routine (routine_manage) for tasks the agent can run."))
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
        what = tr("Dodanie wpisu cron", "Adding a cron entry") if operation == "add" \
            else tr("Usunięcie wpisu cron", "Removing a cron entry")
        async for event in run_classified(session, tool_call, cmd, tool_name="cron_manage",
                                          classification=classification, what=what):
            yield event
        return

    if operation == "check-logs":
        cmd = "journalctl -u cron -u crond --since '24 hours ago' --no-pager | tail -n 60"
        if runtime.kind() != "native":
            cmd = f"grep -h CRON {runtime.to_local('/var/log/syslog')} {runtime.to_local('/var/log/cron')} 2>/dev/null | tail -n 60"
        stdout, stderr, code = await executor.execute(cmd)
        reply(session, tool_call, tr("[CRON — LOGI]\n", "[CRON — LOGS]\n") + f"{stdout}{stderr}")
        return

    if _can_edit_crontab() and shutil.which("crontab"):
        stdout, stderr, code = await executor.execute("crontab -l")
        result = tr("[CRONTAB UZYTKOWNIKA]\n", "[USER CRONTAB]\n") + f"{stdout}{stderr}\n\n" + \
            tr("[PLIKI SYSTEMOWE]\n", "[SYSTEM FILES]\n") + read_host_crontabs()
    else:
        result = tr("[CRON HOSTA]\n", "[HOST CRON]\n") + read_host_crontabs()
    reply(session, tool_call, result)
