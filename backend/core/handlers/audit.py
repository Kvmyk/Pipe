"""
Handlery narzedzi security_audit i security_patch.

security_audit — audyt bezpieczenstwa hosta z ocena i poprawkami; same odczyty. Poprawki model wykonuje zwyklymi
narzedziami (execute_command, write_file), czyli z potwierdzeniem i bezpiecznikiem.
security_patch — latki podatnosci jak /zalataj (core/patching.py): jedno potwierdzenie na cala latke zamiast
pytania o kazda komende.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import posture
from backend.core.events import Event
from backend.core.handlers.common import reply
from backend.core.session import ConfirmationRequest, Session
from backend.core.i18n import tr
from backend.core.text import as_code

AUDIT_HINT_PL = (
    "\n\nPrzedstaw uzytkownikowi ocene i najwazniejsze punkty (numeracja jak wyzej). Gdy poprosi o poprawke "
    "('napraw 1'), wykonaj jej komende narzedziem execute_command (albo write_file) — dostanie ja do "
    "zatwierdzenia z planem bezpiecznika. Komendy oznaczone 'na hoscie' wykonaj execute_command z host: true (w trybie docker) — nie kaz ich wpisywac recznie. "
    "Przy SSH trzymaj sie kolejnosci ze skilla utwardz-ssh. Przy podatnosciach przy kazdej napisz zwyklymi "
    "slowami, czego dotyczy i co grozi (opisy sa w sekcji PODATNOSCI) — nigdy same numery CVE i wersje. Gdy "
    "uzytkownik chce je zalatac, wywolaj security_patch (jedno potwierdzenie na cala latke) — nie skladaj latek "
    "z execute_command."
)
AUDIT_HINT_EN = (
    "\n\nPresent the score and the most important points to the user (numbering as above). When they ask for a fix "
    "('fix 1'), run its command with execute_command (or write_file) — they will get it to approve with the "
    "safety-fuse plan. Run commands marked 'on the host' with execute_command and host: true (docker mode) — do not ask the user to type them. "
    "For SSH follow the order from the harden-ssh skill. For vulnerabilities, say in plain words what each one is "
    "about and what it risks (the descriptions are in the VULNERABILITIES section) — never just CVE numbers and versions. "
    "When the user wants them patched, call security_patch (one confirmation for the whole patch) — do not assemble "
    "patches from execute_command."
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
        audit.vulns_view = vulns.report_view(scan, vulns.explanations(lang()))
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


async def handle_security_patch(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    """
    Latka z /zalataj na prosbe w rozmowie: plan z biezacego skanu, JEDNO potwierdzenie z lista tego, co zalata,
    a po TAK (albo od razu w YOLO) to samo latanie w tle co /zalataj, z postepem na zywo. Viewer i tryb obserwacji
    odrzuca agent z gory (VIEWER_WRITE_OPERATIONS, OBSERVE_WRITE_OPERATIONS).
    """
    from backend.core import hostexec, infra, patching, safety, vulns
    from backend.core.watch import get_watcher

    scope = patching.parse_scope(str(args.get("scope") or ""))
    if not scope:
        reply(session, tool_call, patching.scope_help())
        return
    if patching.current() is not None:
        reply(session, tool_call, tr("Latanie juz trwa (np. z /zalataj) — wynik przyjdzie sam, drugiego nie uruchamiaj.",
                                     "Patching is already running (e.g. from /patch) — the result arrives by itself, "
                                     "do not start a second one."))
        return
    watcher = get_watcher(agent)
    scan, note = await watcher.vuln_for_audit()
    if scan is None:
        reply(session, tool_call, tr(f"Brak wyniku skanu podatnosci ({note or 'brak'}).",
                                     f"No vulnerability scan result ({note or 'none'})."))
        return
    vulns.refresh_containers(scan, await infra.docker_containers())
    own = {p for p in [await hostexec.own_project()] if p}
    job = patching.plan(scan, scope, skip_projects=own, local_blocker=await patching.local_blocker())
    if job.empty:
        reply(session, tool_call, "\n\n".join(job.notes + [vulns.nothing_to_patch(scan, own)]))
        return

    async def stream():
        run = patching.current() or patching.start(job, interface=session.interface, watcher=watcher)
        async for event in run.follow():
            if isinstance(event, patching.Done):
                run.delivered = True
                yield safety.Outcome(event.text, event.exit_code, str(event.data.get("entry_id") or ""))
            else:
                yield event

    shown = tr("/zalataj", "/patch") + ("" if scope == "all" else f" {scope.removeprefix('server:')}")
    session.pending_confirmation = ConfirmationRequest(
        tool_call_id=tool_call.id, tool_name="security_patch", command=f"{shown}: {job.describe()}",
        classification="confirm", stream=stream, auto=True)
    what = "\n".join((p.title()[:1].upper() + p.title()[1:]) + ": " + p.what() for p in job.parts)
    fuse = tr("Bezpiecznik:\n"
              "- weryfikacja po: strony, ktore dzialaja teraz, maja dzialac po latce; kontenery projektow compose dzialaja\n"
              "- cofniecie pozniej (/cofnij): poprzednie obrazy kontenerow; pakietow nie cofam\n"
              "- uwaga: bez pytania o kazda komende; po latce usuwam stare obrazy, cache budowania i pobrane paczki",
              "Safety fuse:\n"
              "- verification after: sites that work now must work after the patch; compose project containers run\n"
              "- undo later (/undo): the previous container images; packages are not rolled back\n"
              "- note: no question for every command; after the patch I remove old images, build cache and downloaded "
              "packages")
    yield tr(f"[POTWIERDZ] Latanie podatnosci wymaga potwierdzenia: {as_code(shown)}\n{what}\n{fuse}",
             f"[POTWIERDZ] Patching vulnerabilities requires confirmation: {as_code(shown)}\n{what}\n{fuse}")
