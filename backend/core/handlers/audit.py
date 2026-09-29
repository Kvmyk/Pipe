"""
Handler narzedzia security_audit — audyt bezpieczenstwa hosta z ocena i poprawkami.

Same odczyty. Poprawki model wykonuje zwyklymi narzedziami (execute_command,
write_file), czyli z potwierdzeniem i bezpiecznikiem.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import posture
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.session import Session

AUDIT_HINT = (
    "\n\nPrzedstaw uzytkownikowi ocene i najwazniejsze punkty (numeracja jak wyzej). Gdy poprosi o poprawke "
    "('napraw 1'), wykonaj jej komende narzedziem execute_command (albo write_file) — dostanie ja do "
    "zatwierdzenia z planem bezpiecznika. Komendy oznaczone 'w powloce hosta' podaj mu do wykonania samemu. "
    "Przy SSH trzymaj sie kolejnosci ze skilla utwardz-ssh."
)


async def run_audit_text() -> tuple[posture.Audit, str]:
    from backend.core.watch import get_watcher

    audit = await posture.audit_now(report=get_watcher().checks_report, cert_days=settings.WATCH_CERT_DAYS)
    return audit, audit.render()


async def handle_security_audit(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    _, text = await run_audit_text()
    reply(session, tool_call, text + AUDIT_HINT)
    return
    yield  # noqa: unreachable — async generator
