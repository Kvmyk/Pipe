"""
Handler dla operacji Docker.
"""

from __future__ import annotations

import shlex
from typing import Any, AsyncGenerator

from backend.core.events import Event
from backend.core.handlers.common import reply, run_classified
from backend.core.security import classify_command
from backend.core.session import Session

# operacja ze schematu -> podkomenda docker. {target} to nazwa kontenera/obrazu.
_OPERATIONS: dict[str, str] = {
    "ps": "ps",
    "logs": "logs {target}",
    "inspect": "inspect {target}",
    "stats": "stats --no-stream {target}",
    "top": "top {target}",
    "restart": "restart {target}",
    "stop": "stop {target}",
    "start": "start {target}",
    "rm": "rm {target}",
    "rmi": "rmi {target}",
    "images": "images",
    "prune": "system prune -f",
    "compose-ps": "compose ls",
    "compose-logs": "compose -p {target} logs --tail 100",
    "networks": "network ls",
    "volumes": "volume ls",
    "df": "system df",
}
_NEEDS_TARGET = {"logs", "inspect", "top", "restart", "stop", "start", "rm", "rmi", "compose-logs"}


def build_docker_command(args: dict[str, Any]) -> tuple[str | None, str]:
    """Zwraca (komenda, blad). Akceptuje tez stary format {"docker_command": "ps -a"}."""
    legacy = str(args.get("docker_command", "")).strip()
    if legacy:
        return f"docker {legacy.removeprefix('docker ').strip()}", ""

    operation = str(args.get("operation", "")).strip().lower()
    if operation not in _OPERATIONS:
        return None, f"Blad: nieznana operacja docker {operation!r}. Dostepne: {', '.join(_OPERATIONS)}."
    target = str(args.get("target", "")).strip()
    if operation in _NEEDS_TARGET and not target:
        return None, f"Blad: operacja {operation} wymaga parametru target (nazwa lub ID kontenera/obrazu)."
    if operation == "logs" and "--tail" not in str(args.get("options", "")):
        args = {**args, "options": f"--tail 200 {args.get('options', '')}".strip()}

    sub = _OPERATIONS[operation].format(target=shlex.quote(target) if target else "").strip()
    options = str(args.get("options", "")).strip()
    if options:
        # opcje przed nazwa kontenera: `docker logs --tail 50 web`
        head, _, tail = sub.partition(" ")
        sub = f"{head} {options} {tail}".strip() if operation not in ("prune", "compose-ps", "compose-logs") \
            else f"{sub} {options}"
    return f"docker {sub}", ""


async def handle_docker_manage(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie docker_manage."""
    command, error = build_docker_command(args)
    if command is None:
        reply(session, tool_call, error)
        return

    classification = classify_command(command)
    if classification == "safe" and args.get("requires_confirmation") is True \
            and str(args.get("operation", "")) not in ("ps", "logs", "inspect", "stats", "top", "images"):
        classification = "confirm"

    async for event in run_classified(session, tool_call, command, tool_name="docker_manage",
                                      classification=classification, what="Operacja Docker"):
        yield event
