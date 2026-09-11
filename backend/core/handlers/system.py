"""
Handler dla operacji systemowych (change_directory, system_stats, network_info, cron_manage).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator
import shlex
from pathlib import Path

from backend.core import executor
from backend.core.security import classify_command
from backend.core.session import Session, ConfirmationRequest
from backend.core.text import as_code


async def handle_change_directory(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie change_directory."""
    new_cwd = args.get("path", "").strip()

    if not new_cwd:
        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "Błąd: pusta ścieżka",
        })
        return

    try:
        # Sprawdź czy katalog istnieje
        path = Path(new_cwd)
        if not path.exists():
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"Błąd: katalog nie istnieje: {new_cwd}",
            })
            return

        if not path.is_dir():
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": f"Błąd: ścieżka nie jest katalogiem: {new_cwd}",
            })
            return

        # Zmień katalog sesji
        session.cwd = str(path.resolve())
        result = f"Katalog zmieniony na: {session.cwd}"
    except Exception as exc:
        result = f"Błąd zmiany katalogu: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": result,
    })
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


async def handle_system_stats(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie system_stats.

    Czyta statystyki bezpośrednio z /hostproc (zamontowany /proc hosta VPS),
    co gwarantuje dane z poziomu VPS, a nie maszyny fizycznej.
    """
    import re

    # Ścieżka do zamontowanego /proc hosta VPS
    HOSTPROC = "/hostproc"

    stat_type = args.get("stat_type", "summary").strip()

    # Jeśli wyraźnie proszą o raw, pokaż surowy output z komendy
    if stat_type in ("raw", "memory", "cpu", "disk", "process"):
        commands = {
            "cpu": f"cat {HOSTPROC}/stat | head -5 && echo '---' && cat {HOSTPROC}/loadavg",
            "memory": f"cat {HOSTPROC}/meminfo | head -12",
            "disk": "df -h",
            "process": "ps aux | head -20",
        }
        cmd = commands.get(stat_type, f"cat {HOSTPROC}/meminfo | head -12")
        try:
            stdout, stderr, exit_code = await executor.execute(cmd)
            result = f"[{stat_type.upper()}]\n{stdout}"
            if exit_code != 0:
                result += f"\n[EXIT CODE] {exit_code}"
        except Exception as exc:
            result = f"Błąd pobrania statystyk: {exc}"

        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })
        return

    # Domyślnie zwracamy zwięzłe, czytelne podsumowanie serwera
    try:
        # --- Uptime + Load Average ---
        uptime_text = ""
        load_avg = ""
        try:
            uptime_raw, _, _ = await executor.execute(f"cat {HOSTPROC}/uptime")
            uptime_secs = float(uptime_raw.strip().split()[0])
            days = int(uptime_secs // 86400)
            hours = int((uptime_secs % 86400) // 3600)
            mins = int((uptime_secs % 3600) // 60)
            if days > 0:
                uptime_text = f"{days} dni, {hours}h {mins}m"
            elif hours > 0:
                uptime_text = f"{hours}h {mins}m"
            else:
                uptime_text = f"{mins}m"
        except Exception:
            uptime_text = "?"

        try:
            loadavg_raw, _, _ = await executor.execute(f"cat {HOSTPROC}/loadavg")
            parts = loadavg_raw.strip().split()
            if len(parts) >= 3:
                load_avg = f"{parts[0]}, {parts[1]}, {parts[2]}"
        except Exception:
            pass

        # --- Pamięć RAM z /hostproc/meminfo ---
        mem_total = mem_used = mem_avail = "?"
        try:
            meminfo_raw, _, _ = await executor.execute(f"cat {HOSTPROC}/meminfo")
            meminfo = {}
            for line in meminfo_raw.splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    # Wartość w kB
                    num_match = re.search(r"(\d+)", val)
                    if num_match:
                        meminfo[key.strip()] = int(num_match.group(1))

            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            used_kb = total_kb - avail_kb

            mem_total = str(total_kb // 1024)  # MB
            mem_used = str(used_kb // 1024)     # MB
            mem_avail = str(avail_kb // 1024)   # MB
        except Exception:
            pass

        # --- Dysk ---
        disk_size = disk_used = disk_avail = disk_usepct = "?"
        try:
            stdout_disk, _, _ = await executor.execute("df -h /hostfs")
            lines = [l for l in stdout_disk.splitlines() if l.strip()]
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    disk_size = parts[1]
                    disk_used = parts[2]
                    disk_avail = parts[3]
                    disk_usepct = parts[4]
        except Exception:
            pass

        # Zbuduj czytelne podsumowanie
        summary_lines = []
        summary_lines.append(f"[Katalog: {session.cwd}]")
        summary_lines.append("")
        summary_lines.append("Status Serwera")
        if uptime_text:
            summary_lines.append(f"• Uptime: {uptime_text}")
        if load_avg:
            summary_lines.append(f"• Obciążenie CPU: {load_avg} (1m/5m/15m)")
        if mem_used != "?" and mem_total != "?":
            try:
                used_int = int(mem_used)
                total_int = int(mem_total)
                pct = int(used_int * 100 / total_int) if total_int else 0
                summary_lines.append(f"• Zużycie RAM: {used_int} MB użyte / {total_int} MB dostępne (zajętość ok. {pct}%)")
            except Exception:
                summary_lines.append(f"• Zużycie RAM: {mem_used} / {mem_total} MB")
        if disk_avail != "?" and disk_size != "?":
            summary_lines.append(f"• Dysk: Pozostało {disk_avail} wolnego miejsca z {disk_size} ({disk_usepct} zajętość)")

        result_text = "\n".join(summary_lines)

    except Exception as exc:
        # Fallback: zwróć surowy output meminfo
        try:
            out, _, _ = await executor.execute(f"cat {HOSTPROC}/meminfo | head -12")
            result_text = f"[MEMORY]\n{out}"
        except Exception as exc2:
            result_text = f"Błąd pobrania statystyk: {exc} / {exc2}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": result_text,
    })
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


async def handle_network_info(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie network_info."""
    info_type = args.get("info_type", "interfaces").strip()

    commands = {
        "interfaces": "ip addr show",
        "routes": "ip route show",
        "connections": "ss -tuln",
        "dns": "cat /etc/resolv.conf",
    }

    cmd = commands.get(info_type, "ip addr show")

    try:
        stdout, stderr, exit_code = await executor.execute(cmd)
        result = f"[{info_type.upper()}]\n{stdout}"
        if exit_code != 0:
            result += f"\n[EXIT CODE] {exit_code}"
    except Exception as exc:
        result = f"Błąd pobrania info sieciowych: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": result,
    })
    return
    yield  # noqa: unreachable — wymagane, by funkcja byla async generatorem


async def handle_cron_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie cron_manage."""
    action = args.get("action", "list").strip()
    cron_entry = args.get("cron_entry", "")

    if action == "list":
        cmd = "crontab -l"
    elif action == "add":
        if not cron_entry:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "Błąd: brak cron_entry do dodania",
            })
            return
        # Po potwierdzeniu _execute_tool_confirmed wykona dokladnie te komende,
        # wiec musi byc poprawna i pokazana uzytkownikowi znak w znak.
        # printf zamiast echo — echo w sh interpretuje backslashe.
        cmd = f"{{ crontab -l 2>/dev/null; printf '%s\\n' {shlex.quote(cron_entry)}; }} | crontab -"
        if classify_command(cmd) == "forbidden":
            from backend.core import audit
            await audit.log_blocked(session.interface, f"cron_manage(add: {cron_entry})")
            yield f"[ODMOWA] Wpis cron {as_code(cron_entry)} zawiera zabronione polecenie."
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "ODMOWA SYSTEMOWA: Wpis cron zawiera zakazana operacje.",
            })
            return
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="cron_manage",
            command=cmd,
            classification="confirm",
        )
        yield f"[POTWIERDZ] Dodanie wpisu cron wymaga potwierdzenia: {as_code(cmd)}"
        return
    elif action == "remove":
        if not cron_entry:
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "Błąd: podaj cron_entry — dokladna linie crontaba do usuniecia",
            })
            return
        # Usuwa wylacznie linie identyczne z cron_entry — nigdy calego crontaba.
        cmd = f"crontab -l | grep -vxF -- {shlex.quote(cron_entry)} | crontab -"
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="cron_manage",
            command=cmd,
            classification="confirm",
        )
        yield f"[POTWIERDZ] Usunięcie wpisu cron wymaga potwierdzenia: {as_code(cmd)}"
        return
    else:
        cmd = "crontab -l"

    try:
        stdout, stderr, exit_code = await executor.execute(cmd)
        result = f"[CRON]\n{stdout}"
        if exit_code != 0:
            result += f"\n[EXIT CODE] {exit_code}"
    except Exception as exc:
        result = f"Błąd operacji cron: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": result,
    })
