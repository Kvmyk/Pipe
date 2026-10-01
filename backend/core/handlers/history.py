"""
Handler narzedzia server_history — pamiec serwera w czasie:

  changes  co sie zmienilo (migawki: pakiety, kontenery, porty, cron, konta, klucze SSH, konfiguracje)
  chart    wykres load / RAM / dyskow z historii czuwania (obraz dla uzytkownika, liczby dla modelu)
  checks   sprawdzenia bez konfiguracji: certyfikaty, strony, DNS, swiezosc backupow
  incidents  pamiec incydentow: co sie juz zdarzalo, co ustalono, co pomoglo

Wszystko to odczyty — bez potwierdzen.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import checks, diagram, metrics, snapshots
from backend.core.events import Attachment, Event
from backend.core.handlers.common import reply
from backend.core.session import Session


async def chart_attachment(metric: str, hours: float) -> tuple[Attachment | None, str]:
    """(obraz wykresu albo None, podsumowanie liczbowe). Wspolne dla narzedzia, /wykres i raportu."""
    source, text = metrics.chart(metric, hours, disk_pct=settings.WATCH_DISK_PCT, mem_pct=settings.WATCH_MEM_PCT,
                                 load_factor=settings.WATCH_LOAD_FACTOR)
    if source is None:
        return None, text
    try:
        rendered = await diagram.render(source, ascii_art=False)
    except diagram.DiagramError as exc:
        return None, f"{text}\n(Nie udalo sie narysowac wykresu: {exc})"
    if rendered.png is None:
        return None, text + "\n(Renderer wykresow niedostepny — zainstaluj mermaidx.)"
    title = metrics.TITLES[metric]
    return Attachment(name=f"wykres-{metric}.png", mime="image/png", data=rendered.png,
                      caption=f"{title} — {text.split('. Prog')[0]}"[:1000], source=rendered.source), text


async def changes_text(hours: float) -> str:
    current = await snapshots.capture()
    return snapshots.timeline(hours, current)


async def checks_text() -> str:
    report = await checks.run_checks(sites=settings.WATCH_SITES, ignore=checks.parse_ignore(settings.WATCH_IGNORE))
    return report.render(cert_days=settings.WATCH_CERT_DAYS, backup_hours=settings.WATCH_BACKUP_HOURS)


async def handle_server_history(
    agent: Any,
    session: Session,
    tool_call: Any,
    args: dict[str, Any],
) -> AsyncGenerator[Event, None]:
    operation = str(args.get("operation", "changes") or "changes").strip().lower()
    hours = metrics.parse_hours(str(args.get("since", "") or args.get("hours", "") or ""), default=24)

    if operation == "changes":
        reply(session, tool_call, await changes_text(hours))
        return

    if operation == "chart":
        metric = metrics.normalize_metric(str(args.get("metric", "load") or "load"))
        if metric is None:
            reply(session, tool_call, "Blad: metric to load, memory albo disk.")
            return
        attachment, text = await chart_attachment(metric, hours)
        if attachment is not None:
            yield attachment
            reply(session, tool_call, f"Wykres wyslany uzytkownikowi jako obraz. Liczby z wykresu: {text}")
        else:
            reply(session, tool_call, text)
        return

    if operation == "incidents":
        from backend.core import incidents
        reply(session, tool_call, incidents.render(25))
        return

    if operation == "checks":
        reply(session, tool_call, await checks_text())
        return

    reply(session, tool_call, f"Nieznana operacja {operation!r}. Dostepne: changes, chart, checks, incidents.")
