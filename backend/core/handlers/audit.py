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
from backend.core.i18n import tr

AUDIT_HINT_PL = (
    "\n\nPrzedstaw uzytkownikowi ocene i najwazniejsze punkty (numeracja jak wyzej). Gdy poprosi o poprawke "
    "('napraw 1'), wykonaj jej komende narzedziem execute_command (albo write_file) — dostanie ja do "
    "zatwierdzenia z planem bezpiecznika. Komendy oznaczone 'na hoscie' wykonaj execute_command z host: true (w trybie docker) — nie kaz ich wpisywac recznie. "
    "Przy SSH trzymaj sie kolejnosci ze skilla utwardz-ssh. Przy podatnosciach przy kazdej napisz zwyklymi "
    "slowami, czego dotyczy i co grozi (opisy sa w sekcji PODATNOSCI) — nigdy same numery CVE i wersje."
)
AUDIT_HINT_EN = (
    "\n\nPresent the score and the most important points to the user (numbering as above). When they ask for a fix "
    "('fix 1'), run its command with execute_command (or write_file) — they will get it to approve with the "
    "safety-fuse plan. Run commands marked 'on the host' with execute_command and host: true (docker mode) — do not ask the user to type them. "
    "For SSH follow the order from the harden-ssh skill. For vulnerabilities, say in plain words what each one is "
    "about and what it risks (the descriptions are in the VULNERABILITIES section) — never just CVE numbers and versions."
)


async def run_audit_text(with_vulns: bool = False) -> tuple[posture.Audit, str]:
    """
    Audyt z opisami podatnosci (model dopisuje brakujace, najwyzej EXPLAIN_WAIT s). `with_vulns` dokleja
    pelny raport podatnosci — kazda z opisem — dla modelu, ktory ma je przedstawic uzytkownikowi.
    """
    from backend.core import vulns
    from backend.core.i18n import lang
    from backend.core.watch import EXPLAIN_WAIT, get_watcher

    watcher = get_watcher()
    scan, note = await watcher.vuln_for_audit()
    if scan is not None:
        try:
            await watcher.explain_vulns(scan, extra=vulns.alert_items(scan) + scan.items[:15], timeout=EXPLAIN_WAIT)
        except Exception:
            pass                                  # bez wyjasnien audyt pokazuje opisy z OSV
    audit = await posture.audit_now(report=watcher.checks_report, cert_days=settings.WATCH_CERT_DAYS,
                                    vuln_scan=scan, vuln_note=note)
    if scan is not None and (scan.items or any(t.items for t in scan.targets)):
        audit.vulns = vulns.report_markdown(scan, vulns.explanations(lang()))
    text = audit.render()
    if with_vulns and audit.vulns:
        text += tr("\n\n--- PODATNOSCI (kazda z opisem) ---\n", "\n\n--- VULNERABILITIES (each with a description) ---\n") \
            + audit.vulns
    return audit, text


async def handle_security_audit(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    _, text = await run_audit_text(with_vulns=True)
    reply(session, tool_call, text + tr(AUDIT_HINT_PL, AUDIT_HINT_EN))
    return
    yield  # noqa: unreachable — async generator
