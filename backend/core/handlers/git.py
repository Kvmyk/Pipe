"""
Handler dla operacji Git.
"""

from __future__ import annotations

import shlex
from typing import Any, AsyncGenerator

from backend.core import runtime
from backend.core.events import Event
from backend.core.handlers.common import reply, run_classified
from backend.core.security import classify_command
from backend.core.session import Session


async def handle_git_command(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """Obsluguje narzedzie git_command: `git -C <repo> <subcommand>`."""
    repo_path = str(args.get("repo_path", "")).strip()
    subcommand = str(args.get("subcommand", "")).strip()
    if subcommand.startswith("git "):
        subcommand = subcommand[4:].strip()

    if not repo_path or not subcommand:
        reply(session, tool_call, "Blad: repo_path lub subcommand jest pusty")
        return

    # Model podaje sciezke hosta (albo wzgledna) — git dostaje sciezke widziana przez Pipe.
    local_repo = runtime.to_local(runtime.to_host(repo_path, session.cwd))
    git_cmd = f"git -C {shlex.quote(local_repo)} {subcommand}"
    classification = classify_command(git_cmd)
    if classification == "safe" and args.get("requires_confirmation") is True:
        classification = "confirm"

    async for event in run_classified(session, tool_call, git_cmd, tool_name="git_command",
                                      classification=classification, what="Operacja Git"):
        yield event
