"""
Poranny raport — jedna wiadomosc o stanie serwera, bez LLM i bez kosztow.

O DIGEST_TIME (domyslnie 7:00) czuwanie sklada raport z tego, co Pipe juz
wie, i wysyla go subskrybentom (Telegram). Na zadanie: /raport.

  Stan        uptime, load wzgledem rdzeni, RAM, dyski
  Alerty      aktywne + zdarzenia z ostatniej doby
  Zmiany      co sie zmienilo na serwerze od wczoraj (migawki)
  Zdrowie     certyfikaty, strony, backupy (sprawdzenia bez konfiguracji)
  Aktualizacje  pakiety do aktualizacji i wymagany restart (Ubuntu/Debian)
  Rutyny      ostatni status kazdej rutyny
  LLM         zuzycie tokenow wczoraj

Do raportu dolaczony jest wykres obciazenia z ostatniej doby.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.core import hostinfo, routines, runtime, snapshots
from backend.core.i18n import tr


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Digest:
    title: str
    sections: list[Section]
    attachment: Any = None   # events.Attachment z wykresem albo None

    def text(self) -> str:
        parts = [self.title]
        for section in self.sections:
            parts.append(f"\n{section.title}:\n" + "\n".join(f"- {line}" for line in section.lines))
        return "\n".join(parts)

    def to_event(self) -> dict[str, Any]:
        event = {"type": "digest", "title": self.title, "text": self.text(),
                 "sections": [{"title": s.title, "lines": s.lines} for s in self.sections]}
        if self.attachment is not None:
            event["attachment"] = self.attachment.to_wire()
        return event


def health_lines(proc: str | None = None, *, load_factor: int = 2) -> list[str]:
    lines = []
    uptime = hostinfo.uptime_seconds(proc)
    if uptime is not None:
        lines.append(tr(f"dziala od {hostinfo.format_duration(uptime)}", f"up for {hostinfo.format_duration(uptime)}"))
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load:
        mark = tr(" (wysokie)", " (high)") if load[1] > cpus * load_factor else ""
        lines.append(tr(f"load 5 min {load[1]:.2f} przy {cpus} rdzeniach{mark}", f"load 5 min {load[1]:.2f} on {cpus} cores{mark}"))
    mem = hostinfo.memory(proc)
    if mem:
        lines.append(tr(f"RAM {mem.used_pct}% ({mem.used_mb} z {mem.total_mb} MB)",
                        f"RAM {mem.used_pct}% ({mem.used_mb} of {mem.total_mb} MB)")
                     + (f", swap {mem.swap_used_mb} MB" if mem.swap_used_mb else ""))
    for disk in hostinfo.disks(proc):
        free = hostinfo.human_bytes(disk.available)
        lines.append(tr(f"dysk {disk.mount}: {disk.used_pct}%, wolne {free}", f"disk {disk.mount}: {disk.used_pct}%, {free} free"))
    return lines


def updates_lines() -> list[str]:
    """Stan aktualizacji z plikow update-notifier (Ubuntu) — bez uruchamiania apt w kontenerze."""
    lines = []
    text = hostinfo._read(runtime.to_local("/var/lib/update-notifier/updates-available")).strip()
    for line in text.splitlines():
        line = line.strip()
        if line and any(ch.isdigit() for ch in line) and "http" not in line:
            lines.append(line)
    required = runtime.to_local("/run/reboot-required")
    if hostinfo._read(required) or hostinfo._read(runtime.to_local("/var/run/reboot-required")):
        packages = hostinfo._read(runtime.to_local("/run/reboot-required.pkgs")).split()
        names = ", ".join(sorted(set(packages))[:5])
        lines.append(tr("wymagany restart serwera", "server restart required")
                     + (tr(f" (po aktualizacji: {names})", f" (after updating: {names})") if packages else ""))
    return lines[:4]


def change_lines(current: dict[str, dict[str, str]] | None, hours: float = 24, now: float | None = None) -> list[str]:
    if current is None:
        return []
    changes, since = snapshots.summary_since(hours, current, now=now)
    if since is None:
        return [tr("historia zmian dopiero sie zbiera", "the change history is only starting to collect")]
    if not changes:
        when = datetime.fromtimestamp(since).strftime("%d.%m %H:%M")
        return [tr(f"bez zmian od {when}", f"no changes since {when}")]
    ordered = sorted(changes, key=lambda c: (not c.security, snapshots.SECTIONS.index(c.section)))
    lines = []
    packages = [c for c in ordered if c.section == "packages"]
    for change in ordered:
        if change.section == "packages":
            continue
        mark = f"{snapshots.security_mark()} " if change.security else ""
        lines.append(mark + snapshots.describe(change).split("\n", 1)[0])
    if packages:
        names = ", ".join(c.key for c in packages[:6]) + (" ..." if len(packages) > 6 else "")
        lines.append(tr(f"pakiety: {len(packages)} zmian ({names})", f"packages: {len(packages)} changes ({names})"))
    if len(lines) > 12:
        more = len(lines) - 12
        lines = lines[:12] + [tr(f"... i {more} wiecej — /zmiany", f"... and {more} more — /changes")]
    return lines


def checks_lines(report: Any, *, cert_days: int, backup_hours: int) -> list[str]:
    if report is None:
        return []
    lines = []
    soon = sorted((c for c in report.certs if c.days_left is not None), key=lambda c: c.days_left)
    for cert in report.certs:
        if cert.error:
            lines.append(tr(f"certyfikat {cert.domain}: {cert.error}", f"certificate {cert.domain}: {cert.error}"))
    for cert in soon[:3]:
        mark = " (!)" if cert.days_left <= cert_days else ""
        lines.append(tr(f"certyfikat {cert.domain}: {int(cert.days_left)} dni{mark}",
                        f"certificate {cert.domain}: {int(cert.days_left)} days{mark}"))
    if len(soon) > 3:
        lines.append(tr(f"pozostale certyfikaty ({len(soon) - 3}) wazne dluzej",
                        f"the other certificates ({len(soon) - 3}) are valid longer"))
    failing = [s for s in report.sites if not s.ok]
    if report.sites:
        ok = len(report.sites) - len(failing)
        lines.append(tr(f"strony: {ok}/{len(report.sites)} odpowiada poprawnie",
                        f"sites: {ok}/{len(report.sites)} responding correctly")
                     + (tr(" — problem: ", " — problem: ") + ", ".join(s.domain for s in failing) if failing else ""))
    for backup in report.backups:
        age = backup.age_hours(report.at)
        if backup.error:
            lines.append(f"backup {backup.path}: {backup.error} (!)")
        elif age is not None:
            mark = " (!)" if age > backup_hours else ""
            lines.append(tr(f"backup {backup.path}: ostatni plik {int(age)} h temu{mark}",
                            f"backup {backup.path}: latest file {int(age)} h ago{mark}"))
    return lines


def routine_lines() -> list[str]:
    return [f"{r.name}: {r.last_status or tr('jeszcze nie uruchomiona', 'not run yet')}"
            + (f" ({r.last_run})" if r.last_run else "")
            for r in routines.load_routines() if r.enabled]


def alert_lines(active: list[Any], history: list[dict[str, Any]], now: float | None = None) -> list[str]:
    lines = [f"[{a.severity}] {a.title}" for a in active]
    cutoff = datetime.fromtimestamp((now if now is not None else time.time()) - 86400).strftime("%Y-%m-%d %H:%M")
    recent = [e for e in history if e.get("type") == "alert" and str(e.get("at", "")) >= cutoff]
    resolved = [e for e in recent if e.get("state") == "resolved"]
    events = [e for e in recent if e.get("state") in ("event", "new")]
    if not lines:
        lines.append(tr("brak aktywnych alertow", "no active alerts"))
    if events or resolved:
        lines.append(tr(f"ostatnia doba: {len(events)} zdarzen, {len(resolved)} rozwiazanych",
                        f"last 24 h: {len(events)} events, {len(resolved)} resolved"))
    return lines


def build(*, hostname: str, active: list[Any], history: list[dict[str, Any]], current: dict | None,
          report: Any, usage_line: str, cert_days: int, backup_hours: int, load_factor: int,
          proc: str | None = None, now: float | None = None) -> Digest:
    when = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%d.%m.%Y %H:%M")
    sections = [Section(tr("Stan", "Health"), health_lines(proc, load_factor=load_factor)),
                Section(tr("Alerty", "Alerts"), alert_lines(active, history, now))]
    for title, lines in ((tr("Zmiany od wczoraj", "Changes since yesterday"), change_lines(current, now=now)),
                         (tr("Zdrowie", "Checks"), checks_lines(report, cert_days=cert_days, backup_hours=backup_hours)),
                         (tr("Aktualizacje", "Updates"), updates_lines()),
                         (tr("Rutyny", "Routines"), routine_lines()),
                         ("LLM", [usage_line] if usage_line else [])):
        if lines:
            sections.append(Section(title, lines))
    return Digest(tr(f"Raport {hostname or 'serwera'} — {when}", f"Report for {hostname or 'the server'} — {when}"), sections)


def due(digest_time: str, when: datetime) -> bool:
    """Czy w tej minucie wypada raport ('07:00'; pusty/'off' = nigdy)."""
    value = (digest_time or "").strip().lower()
    if value in ("", "off", "0", "nie", "no", "false"):
        return False
    try:
        hour, minute = (int(x) for x in value.split(":", 1))
    except ValueError:
        return False
    return when.hour == hour and when.minute == minute
