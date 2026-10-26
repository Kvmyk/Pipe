"""
Stan serwera jednym spojrzeniem — bez modelu jezykowego, wiec od razu i za darmo.

Komenda `overview` (pulpit `pipe web`, `/status` w CLI i na Telegramie) sklada to, co Pipe juz wie:
  - naglowek zwyklymi slowami: "Wszystko dziala" albo "Wymaga uwagi: <najwazniejszy problem>",
  - wskazniki procesora, pamieci i dysku (procent + poziom ok / warn / crit) i ich przebieg z ostatniej doby,
  - kontenery, strony, projekty z warsztatu, nowe wiadomosci w skrzynce, zuzycie modelu dzis.

Wczesniej `/status` prosil model o wywolanie system_stats i streszczenie — kilka tysiecy tokenow na pytanie,
na ktore odpowiedz lezy w /proc. Bez importu `settings` (progi przychodza od wywolujacego).
"""

from __future__ import annotations

import time
from typing import Any

from backend.core import hostinfo, metrics
from backend.core.i18n import tr

SERIES_POINTS = 48
BAR_WIDTH = 10


def _bucket(points: list[dict[str, Any]], start: float, end: float, getter, count: int = SERIES_POINTS) -> list[float]:
    """Srednie w `count` przedzialach; luki wypelnione ostatnia znana wartoscia (pusta lista — brak danych)."""
    width = (end - start) / count or 1
    sums, counts = [0.0] * count, [0] * count
    for point in points:
        value = getter(point)
        if value is None:
            continue
        index = min(count - 1, max(0, int((point["t"] - start) / width)))
        sums[index] += float(value)
        counts[index] += 1
    values = [round(s / c, 1) if c else None for s, c in zip(sums, counts)]
    known = [v for v in values if v is not None]
    if not known:
        return []
    last, out = known[0], []
    for value in values:
        last = value if value is not None else last
        out.append(last)
    return out


def _level(pct: float, warn: float, crit: float) -> str:
    return "crit" if pct >= crit else "warn" if pct >= warn else "ok"


def _gb(value: float) -> str:
    text = f"{value / 1024 ** 3:.1f}"
    return tr(text.replace(".", ","), text)


def gauges(proc: str | None = None, *, disk_pct: int = 90, mem_pct: int = 92, load_factor: int = 2) -> list[dict[str, Any]]:
    result = []
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load:
        pct = min(100, round(load[0] * 100 / max(cpus, 1)))
        level = "crit" if load[1] > cpus * load_factor else "warn" if load[0] > cpus * 0.8 else "ok"
        result.append({"id": "cpu", "label": tr("Procesor", "CPU"), "pct": pct, "level": level,
                       "value": tr(f"obciazenie {load[0]:.1f} / {cpus} rdz.".replace(".", ",", 1),
                                   f"load {load[0]:.1f} / {cpus} cores")})
    mem = hostinfo.memory(proc)
    if mem:
        result.append({"id": "mem", "label": tr("Pamiec", "Memory"), "pct": mem.used_pct,
                       "level": _level(mem.used_pct, 80, mem_pct),
                       "value": tr(f"{_gb(mem.used_mb * 1024 ** 2)} z {_gb(mem.total_mb * 1024 ** 2)} GB",
                                   f"{_gb(mem.used_mb * 1024 ** 2)} of {_gb(mem.total_mb * 1024 ** 2)} GB")})
    disks = sorted(hostinfo.disks(proc), key=lambda d: (d.mount != "/", -d.used_pct))
    for disk in disks[:3]:
        result.append({"id": "disk:" + disk.mount, "label": tr(f"Dysk {disk.mount}", f"Disk {disk.mount}"),
                       "pct": disk.used_pct, "level": _level(disk.used_pct, 80, disk_pct),
                       "value": tr(f"wolne {_gb(disk.available)} GB", f"{_gb(disk.available)} GB free")})
    return result


def series(points: list[dict[str, Any]], now: float, hours: float = 24) -> dict[str, list[float]]:
    """Przebieg procesora (% rdzeni), pamieci i najbardziej zajetego dysku z historii czuwania."""
    start = now - hours * 3600
    cpu = _bucket(points, start, now,
                  lambda p: min(100.0, p["load1"] * 100 / max(p.get("cpus") or 1, 1)) if p.get("load1") is not None else None)
    mem = _bucket(points, start, now, lambda p: p.get("mem"))
    mounts: dict[str, int] = {}
    for point in points:
        for mount in point.get("disks") or {}:
            mounts[mount] = mounts.get(mount, 0) + 1
    disk: list[float] = []
    if mounts:
        main = "/" if "/" in mounts else max(mounts, key=mounts.get)
        disk = _bucket(points, start, now, lambda p, m=main: (p.get("disks") or {}).get(m))
    return {"cpu": cpu, "mem": mem, "disk": disk}


def bar(pct: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "▰" * filled + "▱" * (width - filled)


def build(*, active: list[Any], checks_report: Any = None, containers: list[Any] | None = None,
          projects: list[dict[str, Any]] | None = None, unread: int = 0, usage_today: dict[str, Any] | None = None,
          hostname: str = "", proc: str | None = None, now: float | None = None, disk_pct: int = 90,
          mem_pct: int = 92, load_factor: int = 2) -> dict[str, Any]:
    now = now if now is not None else time.time()
    critical = [a for a in active if getattr(a, "severity", "") == "critical"]
    warning = [a for a in active if getattr(a, "severity", "") != "critical"]
    if critical:
        status = "critical"
        headline = tr(f"Wymaga uwagi: {critical[0].title}", f"Needs attention: {critical[0].title}")
    elif warning:
        status = "warning"
        headline = tr(f"Do sprawdzenia: {warning[0].title}", f"Worth a look: {warning[0].title}")
    else:
        status, headline = "ok", tr("Wszystko dziala", "Everything works")
    if len(active) > 1:
        headline += tr(f" (i {len(active) - 1} wiecej)", f" (and {len(active) - 1} more)")

    facts = []
    uptime = hostinfo.uptime_seconds(proc)
    if uptime is not None:
        facts.append(tr(f"dziala od {hostinfo.format_duration(uptime)}", f"up for {hostinfo.format_duration(uptime)}"))
    running = total = 0
    if containers is not None:
        own = [c for c in containers if not str(getattr(c, "name", "")).startswith("pipe-sbx-")]
        running, total = sum(1 for c in own if c.state == "running"), len(own)
        if total:
            facts.append(tr(f"kontenery: {running} z {total} dziala", f"containers: {running} of {total} running"))
    sites_ok = sites_total = 0
    if checks_report is not None and getattr(checks_report, "sites", None):
        sites_total = len(checks_report.sites)
        sites_ok = sum(1 for s in checks_report.sites if s.ok)
        facts.append(tr(f"strony: {sites_ok}/{sites_total} odpowiada", f"sites: {sites_ok}/{sites_total} responding"))

    data_gauges = gauges(proc, disk_pct=disk_pct, mem_pct=mem_pct, load_factor=load_factor)
    try:
        points = metrics.load(24, now=now)
    except OSError:
        points = []
    today = usage_today or {}
    data: dict[str, Any] = {
        "hostname": hostname, "status": status, "headline": headline, "facts": facts,
        "gauges": data_gauges, "series": series(points, now),
        "alerts": [{"id": a.id, "severity": a.severity, "title": a.title} for a in active][:8],
        "containers": {"running": running, "total": total} if containers is not None else None,
        "sites": {"ok": sites_ok, "total": sites_total},
        "projects": projects or [], "unread": unread,
        "usage": {"tokens": int(today.get("prompt", 0)) + int(today.get("completion", 0)),
                  "cost": float(today.get("cost", 0.0)), "cached": int(today.get("cached", 0)),
                  "prompt": int(today.get("prompt", 0)), "saved": int(today.get("saved", 0)),
                  "calls": int(today.get("calls", 0))},
    }
    data["text"] = text(data)
    return data


def text(data: dict[str, Any]) -> str:
    """Wersja tekstowa (CLI, Telegram, notatka dla modelu)."""
    mark = {"ok": "●", "warning": "▲", "critical": "■"}.get(data.get("status", ""), "●")
    head = f"{mark} {data.get('headline', '')}"
    if data.get("hostname"):
        head += f" — {data['hostname']}"
    lines = [head]
    for gauge in data.get("gauges") or []:
        lines.append(f"{gauge['label']:<10} {bar(gauge['pct'])} {gauge['pct']:>3}%  ({gauge['value']})")
    if data.get("facts"):
        lines.append(" · ".join(data["facts"]))
    projects = data.get("projects") or []
    if projects:
        running = sum(1 for p in projects if p.get("status") == "running")
        lines.append(tr(f"projekty w warsztacie: {len(projects)} (dziala: {running})",
                        f"workshop projects: {len(projects)} (running: {running})"))
    if data.get("unread"):
        lines.append(tr(f"nowe wiadomosci od Pipe: {data['unread']} (/nowosci)",
                        f"new messages from Pipe: {data['unread']} (/news)"))
    return "\n".join(lines)
