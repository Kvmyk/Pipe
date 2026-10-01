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
        lines.append(f"dziala od {hostinfo.format_duration(uptime)}")
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load:
        mark = " (wysokie)" if load[1] > cpus * load_factor else ""
        lines.append(f"load 5 min {load[1]:.2f} przy {cpus} rdzeniach{mark}")
    mem = hostinfo.memory(proc)
    if mem:
        lines.append(f"RAM {mem.used_pct}% ({mem.used_mb} z {mem.total_mb} MB)"
                     + (f", swap {mem.swap_used_mb} MB" if mem.swap_used_mb else ""))
    for disk in hostinfo.disks(proc):
        lines.append(f"dysk {disk.mount}: {disk.used_pct}%, wolne {hostinfo.human_bytes(disk.available)}")
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
        lines.append("wymagany restart serwera" + (f" (po aktualizacji: {', '.join(sorted(set(packages))[:5])})"
                                                   if packages else ""))
    return lines[:4]


def change_lines(current: dict[str, dict[str, str]] | None, hours: float = 24, now: float | None = None) -> list[str]:
    if current is None:
        return []
    changes, since = snapshots.summary_since(hours, current, now=now)
    if since is None:
        return ["historia zmian dopiero sie zbiera"]
    if not changes:
        return [f"bez zmian od {datetime.fromtimestamp(since).strftime('%d.%m %H:%M')}"]
    ordered = sorted(changes, key=lambda c: (not c.security, snapshots.SECTIONS.index(c.section)))
    lines = []
    packages = [c for c in ordered if c.section == "packages"]
    for change in ordered:
        if change.section == "packages":
            continue
        mark = "[BEZPIECZENSTWO] " if change.security else ""
        lines.append(mark + snapshots.describe(change).split("\n", 1)[0])
    if packages:
        names = ", ".join(c.key for c in packages[:6]) + (" ..." if len(packages) > 6 else "")
        lines.append(f"pakiety: {len(packages)} zmian ({names})")
    if len(lines) > 12:
        lines = lines[:12] + [f"... i {len(lines) - 12} wiecej — /zmiany"]
    return lines


def checks_lines(report: Any, *, cert_days: int, backup_hours: int) -> list[str]:
    if report is None:
        return []
    lines = []
    soon = sorted((c for c in report.certs if c.days_left is not None), key=lambda c: c.days_left)
    for cert in report.certs:
        if cert.error:
            lines.append(f"certyfikat {cert.domain}: {cert.error}")
    for cert in soon[:3]:
        mark = " (!)" if cert.days_left <= cert_days else ""
        lines.append(f"certyfikat {cert.domain}: {int(cert.days_left)} dni{mark}")
    if len(soon) > 3:
        lines.append(f"pozostale certyfikaty ({len(soon) - 3}) wazne dluzej")
    failing = [s for s in report.sites if not s.ok]
    if report.sites:
        lines.append(f"strony: {len(report.sites) - len(failing)}/{len(report.sites)} odpowiada poprawnie"
                     + (" — problem: " + ", ".join(s.domain for s in failing) if failing else ""))
    for backup in report.backups:
        age = backup.age_hours(report.at)
        if backup.error:
            lines.append(f"backup {backup.path}: {backup.error} (!)")
        elif age is not None:
            mark = " (!)" if age > backup_hours else ""
            lines.append(f"backup {backup.path}: ostatni plik {int(age)} h temu{mark}")
    return lines


def routine_lines() -> list[str]:
    return [f"{r.name}: {r.last_status or 'jeszcze nie uruchomiona'}"
            + (f" ({r.last_run})" if r.last_run else "")
            for r in routines.load_routines() if r.enabled]


def alert_lines(active: list[Any], history: list[dict[str, Any]], now: float | None = None) -> list[str]:
    lines = [f"[{a.severity}] {a.title}" for a in active]
    cutoff = datetime.fromtimestamp((now if now is not None else time.time()) - 86400).strftime("%Y-%m-%d %H:%M")
    recent = [e for e in history if e.get("type") == "alert" and str(e.get("at", "")) >= cutoff]
    resolved = [e for e in recent if e.get("state") == "resolved"]
    events = [e for e in recent if e.get("state") in ("event", "new")]
    if not lines:
        lines.append("brak aktywnych alertow")
    if events or resolved:
        lines.append(f"ostatnia doba: {len(events)} zdarzen, {len(resolved)} rozwiazanych")
    return lines


def build(*, hostname: str, active: list[Any], history: list[dict[str, Any]], current: dict | None,
          report: Any, usage_line: str, cert_days: int, backup_hours: int, load_factor: int,
          proc: str | None = None, now: float | None = None) -> Digest:
    when = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%d.%m.%Y %H:%M")
    sections = [Section("Stan", health_lines(proc, load_factor=load_factor)),
                Section("Alerty", alert_lines(active, history, now))]
    for title, lines in (("Zmiany od wczoraj", change_lines(current, now=now)),
                         ("Zdrowie", checks_lines(report, cert_days=cert_days, backup_hours=backup_hours)),
                         ("Aktualizacje", updates_lines()),
                         ("Rutyny", routine_lines()),
                         ("LLM", [usage_line] if usage_line else [])):
        if lines:
            sections.append(Section(title, lines))
    return Digest(f"Raport {hostname or 'serwera'} — {when}", sections)


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
