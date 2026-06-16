"""
Handler dla operacji systemowych (change_directory, system_stats, network_info, cron_manage).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator
from pathlib import Path

from backend.core.security import classify_command
from backend.core.session import Session, ConfirmationRequest


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
        yield f"Katalog zmieniony na: {session.cwd}"
    except Exception as exc:
        yield f"Błąd zmiany katalogu: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": "Katalog zmieniony",
    })


async def handle_system_stats(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[str, None]:
    """Obsługuje narzędzie system_stats."""
    # Jeśli użytkownik poprosił o surowe dane, zwróć zachowanie domyślne
    stat_type = args.get("stat_type", "summary").strip()

    # Jeśli wyraźnie proszą o raw, pokaż surowy output z komendy
    if stat_type in ("raw", "memory", "cpu", "disk", "process"):
        commands = {
            "cpu": "top -bn1 | head -20",
            "memory": "free -h",
            "disk": "df -h",
            "process": "ps aux | head -20",
        }
        cmd = commands.get(stat_type, "free -h")
        try:
            stdout, stderr, exit_code = await agent._executor.execute(cmd)
            result = f"[{stat_type.upper()}]\n{stdout}"
            if exit_code != 0:
                result += f"\n[EXIT CODE] {exit_code}"
            yield result
        except Exception as exc:
            yield f"Błąd pobrania statystyk: {exc}"

        session.messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": f"Statystyki {stat_type} pobrane",
        })
        return

    # Domyślnie zwracamy zwięzłe, czytelne podsumowanie serwera
    try:
        # Uptime + load
        stdout_uptime, _, ec_uptime = await agent._executor.execute("uptime")

        # Memory
        stdout_mem, _, ec_mem = await agent._executor.execute("free -m")

        # Disk (root)
        stdout_disk, _, ec_disk = await agent._executor.execute("df -h /")

        # Parsowanie uptime
        uptime_text = ""
        load_avg = ""
        try:
            # uptime output e.g.: " 23:22:41 up 234 days, 10:53, 0 users, load average: 0.05, 0.03, 0.01"
            line = stdout_uptime.strip().replace("\n", " ")
            # wyciągnij fragment 'up ...,' jako uptime
            import re

            m_up = re.search(r"up\s+([^,]+),", line)
            if m_up:
                uptime_text = m_up.group(1).strip()
            m_load = re.search(r"load average[s]?:\s*([0-9.,\s]+)", line)
            if m_load:
                load_avg = m_load.group(1).strip()
        except Exception:
            uptime_text = stdout_uptime.strip()

        # Parsowanie memory
        mem_total = mem_used = mem_avail = "?"
        try:
            for ln in stdout_mem.splitlines():
                if ln.lower().startswith("mem:") or ln.lower().startswith("mem "):
                    parts = ln.split()
                    # free -m: Mem: total used free shared buff/cache available
                    if len(parts) >= 3:
                        mem_total = parts[1]
                        mem_used = parts[2]
                        # available may be at index 6
                        if len(parts) >= 7:
                            mem_avail = parts[6]
                        else:
                            mem_avail = parts[3]
                    break
        except Exception:
            pass

        # Parsowanie disk
        disk_size = disk_used = disk_avail = disk_usepct = "?"
        try:
            lines = [l for l in stdout_disk.splitlines() if l.strip()]
            if len(lines) >= 2:
                # header + line
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

        # Jeśli któraś z komend zwróciła błąd, dołącz surowe bloki indywidualnie
        raw_blocks = []
        if ec_uptime != 0 or ec_mem != 0 or ec_disk != 0:
            if stdout_uptime:
                raw_blocks.append(f"[UPTIME]\n{stdout_uptime}")
            if stdout_mem:
                raw_blocks.append(f"[MEMORY]\n{stdout_mem}")
            if stdout_disk:
                raw_blocks.append(f"[DISK]\n{stdout_disk}")

        # Wyemituj surowe bloki jako pierwsze, potem podsumowanie
        if raw_blocks:
            for block in raw_blocks:
                yield block

        yield "\n".join(summary_lines)

    except Exception as exc:
        # Fallback: zwróć surowy output jednej z komend
        try:
            out, _, _ = await agent._executor.execute("free -h")
            yield f"[MEMORY]\n{out}"
        except Exception as exc2:
            yield f"Błąd pobrania statystyk: {exc} / {exc2}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": "Statystyki systemowe pobrane",
    })


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
        stdout, stderr, exit_code = await agent._executor.execute(cmd)
        result = f"[{info_type.upper()}]\n{stdout}"
        if exit_code != 0:
            result += f"\n[EXIT CODE] {exit_code}"
        yield result
    except Exception as exc:
        yield f"Błąd pobrania info sieciowych: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": f"Info sieciowe {info_type} pobrane",
    })


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
         # Wymaga potwierdzenia
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="cron_manage",
            command=f"crontab -e (add: {cron_entry})",
            classification="confirm",
        )
        yield f"[POTWIERDZ] Dodanie wpisu cron wymaga potwierdzenia: {cron_entry}"
        return
    elif action == "remove":
        # Wymaga potwierdzenia
        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id,
            tool_name="cron_manage",
            command=f"crontab -r",
            classification="confirm",
        )
        yield f"[POTWIERDZ] Usunięcie wpisu cron wymaga potwierdzenia"
        return
    else:
        cmd = "crontab -l"

    try:
        stdout, stderr, exit_code = await agent._executor.execute(cmd)
        result = f"[CRON]\n{stdout}"
        if exit_code != 0:
            result += f"\n[EXIT CODE] {exit_code}"
        yield result
    except Exception as exc:
        yield f"Błąd operacji cron: {exc}"

    session.messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": f"Operacja cron {action} wykonana",
    })
