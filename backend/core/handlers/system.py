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
    stat_type = args.get("stat_type", "memory").strip()

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
        from backend.core.agent import ConfirmationRequest
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
        from backend.core.agent import ConfirmationRequest
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
