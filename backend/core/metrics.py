"""
Historia pomiarow hosta i wykresy — bez Prometheusa.

Czuwanie (core/watch.py) przy kazdym sprawdzeniu dopisuje probke do
DATA_DIR/metrics.jsonl: load 1/5 min, RAM, swap i zajetosc dyskow. Z tej
historii powstaja wykresy liniowe Mermaid (`xychart-beta`), renderowane tym
samym silnikiem co diagramy — Telegram dostaje obraz, CLI plik PNG.

Plik jest przycinany do METRICS_KEEP_DAYS dni. Modul nie importuje settings
na poziomie modulu (testy ustawiaja DATA_DIR i progi przez monkeypatch).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core import hostinfo, memory
from backend.core.i18n import tr

# Najwyzej tyle punktow na wykresie — wiecej i tak zlewa sie w linie.
MAX_POINTS = 96
MAX_DISK_SERIES = 3
PALETTE = ("#2563eb", "#16a34a", "#9333ea", "#dc2626")
PALETTE_NAMES = ("niebieska", "zielona", "fioletowa", "czerwona")
PALETTE_NAMES_EN = ("blue", "green", "purple", "red")
METRICS = ("load", "memory", "disk")
_ALIASES = {"ram": "memory", "pamiec": "memory", "mem": "memory", "dysk": "disk", "dyski": "disk",
            "cpu": "load", "obciazenie": "load"}


def metrics_path() -> Path:
    return memory.data_dir() / "metrics.jsonl"


def normalize_metric(name: str) -> str | None:
    name = (name or "load").strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in METRICS else None


# ─── Probki ─────────────────────────────────────────────────────────────────

def sample(proc: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Jedna probka stanu hosta (odczyty z /proc, bez komend)."""
    point: dict[str, Any] = {"t": round(now if now is not None else time.time())}
    load = hostinfo.loadavg(proc)
    if load:
        point["load1"], point["load5"] = load[0], load[1]
    point["cpus"] = hostinfo.cpu_count(proc)
    mem = hostinfo.memory(proc)
    if mem:
        point["mem"] = mem.used_pct
        if mem.swap_total_mb:
            point["swap"] = round(mem.swap_used_mb * 100 / mem.swap_total_mb)
    disks = {d.mount: d.used_pct for d in hostinfo.disks(proc)}
    if disks:
        point["disks"] = disks
    return point


PRUNE_EVERY = 200
_recorded = 0


def record(point: dict[str, Any], keep_days: int = 8) -> None:
    """Dopisuje probke; co PRUNE_EVERY probek usuwa te starsze niz keep_days."""
    global _recorded
    path = metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(point, separators=(",", ":")) + "\n")
    _recorded += 1
    if _recorded % PRUNE_EVERY == 1:  # takze przy pierwszej probce po starcie
        prune(keep_days, now=point["t"])


def prune(keep_days: int, now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    kept = [p for p in load(0, now=now, since=cutoff)]
    memory._write_atomic(metrics_path(), "".join(json.dumps(p, separators=(",", ":")) + "\n" for p in kept))


def load(hours: float, now: float | None = None, since: float | None = None) -> list[dict[str, Any]]:
    """Probki z ostatnich `hours` godzin (albo od `since`), od najstarszej."""
    path = metrics_path()
    if not path.is_file():
        return []
    start = since if since is not None else (now if now is not None else time.time()) - hours * 3600
    points = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    point = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(point, dict) and isinstance(point.get("t"), (int, float)) and point["t"] >= start:
                    points.append(point)
    except OSError:
        return []
    return sorted(points, key=lambda p: p["t"])


# ─── Serie ──────────────────────────────────────────────────────────────────

@dataclass
class Series:
    name: str
    values: list[float]


def _bucket(points: list[dict[str, Any]], start: float, end: float, getter) -> list[float | None]:
    """Srednie w MAX_POINTS rownych przedzialach czasu; pusty przedzial = None."""
    count = MAX_POINTS
    width = (end - start) / count or 1
    sums = [0.0] * count
    counts = [0] * count
    for point in points:
        value = getter(point)
        if value is None:
            continue
        index = min(count - 1, max(0, int((point["t"] - start) / width)))
        sums[index] += value
        counts[index] += 1
    return [round(s / c, 2) if c else None for s, c in zip(sums, counts)]


def _fill(values: list[float | None]) -> list[float] | None:
    """Luki (np. restart Pipe) wypelnia ostatnia znana wartoscia; same luki = None."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    last = known[0]
    out = []
    for value in values:
        last = value if value is not None else last
        out.append(last)
    return out


def series(metric: str, points: list[dict[str, Any]], start: float, end: float) -> list[Series]:
    if metric == "load":
        getters = [("load 5 min", lambda p: p.get("load5")), ("load 1 min", lambda p: p.get("load1"))]
    elif metric == "memory":
        getters = [("RAM %", lambda p: p.get("mem"))]
        if any("swap" in p for p in points):
            getters.append(("swap %", lambda p: p.get("swap")))
    else:
        mounts: dict[str, int] = {}
        for point in points:
            for mount in (point.get("disks") or {}):
                mounts[mount] = mounts.get(mount, 0) + 1
        chosen = sorted(mounts, key=lambda m: (-mounts[m], len(m)))[:MAX_DISK_SERIES]
        getters = [(tr(f"dysk {m}", f"disk {m}"), (lambda p, m=m: (p.get("disks") or {}).get(m))) for m in sorted(chosen, key=len)]
    result = []
    for name, getter in getters:
        filled = _fill(_bucket(points, start, end, getter))
        if filled is not None:
            result.append(Series(name, filled))
    return result


def threshold(metric: str, points: list[dict[str, Any]], *, disk_pct: int, mem_pct: int, load_factor: int) -> float:
    if metric == "memory":
        return float(mem_pct)
    if metric == "disk":
        return float(disk_pct)
    cpus = next((p.get("cpus") for p in reversed(points) if p.get("cpus")), 1) or 1
    return float(cpus * load_factor)


# ─── Wykres ─────────────────────────────────────────────────────────────────

TITLES = {"load": "Obciazenie (load average)", "memory": "Pamiec RAM", "disk": "Zajetosc dyskow"}
TITLES_EN = {"load": "Load average", "memory": "Memory (RAM)", "disk": "Disk usage"}


def title(metric: str) -> str:
    return tr(TITLES[metric], TITLES_EN[metric])


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def to_mermaid(metric: str, lines: list[Series], limit: float, hours: float) -> str:
    """Wykres liniowy Mermaid. Ostatnia linia (czerwona) to prog alertu czuwania."""
    percent = metric in ("memory", "disk")
    top = 100.0 if percent else max([limit, *(max(s.values) for s in lines)]) * 1.15 or 1.0
    colors = ", ".join(PALETTE[:len(lines)] + ("#dc2626",))
    span = f"{_fmt(hours)} h" if hours < 48 else tr(f"{_fmt(hours / 24)} dni", f"{_fmt(hours / 24)} days")
    names = tr(PALETTE_NAMES, PALETTE_NAMES_EN)
    legend = ", ".join(f"{names[i]}: {s.name}" for i, s in enumerate(lines))
    chart_title = tr(f"{TITLES[metric]} — ostatnie {span}", f"{TITLES_EN[metric]} — last {span}")
    axis = tr(f"godziny temu ({legend}; czerwona: prog alertu)", f"hours ago ({legend}; red: alert threshold)")
    out = [
        "---",
        "config:",
        "  xyChart:",
        "    width: 900",
        "    height: 400",
        "  themeVariables:",
        "    xyChart:",
        f'      plotColorPalette: "{colors}"',
        "---",
        "xychart-beta",
        f'  title "{chart_title}"',
        f'  x-axis "{axis}" -{_fmt(hours)} --> 0',
        f'  y-axis "{"%" if percent else "load"}" 0 --> {_fmt(round(top, 2))}',
    ]
    for line in lines:
        out.append("  line [" + ", ".join(_fmt(v) for v in line.values) + "]")
    out.append("  line [" + ", ".join(_fmt(limit) for _ in range(MAX_POINTS)) + "]")
    return "\n".join(out)


def summary(metric: str, lines: list[Series], limit: float, points: list[dict[str, Any]]) -> str:
    """Liczby dla modelu (i podpis obrazka) — model nie widzi obrazu."""
    if not lines:
        return tr("Brak pomiarow w tym okresie.", "No measurements in this period.")
    unit = "%" if metric in ("memory", "disk") else ""
    parts = []
    for line in lines:
        values = line.values
        now_v, min_v, max_v = _fmt(values[-1]), _fmt(min(values)), _fmt(max(values))
        avg_v = _fmt(sum(values) / len(values))
        parts.append(tr(f"{line.name}: teraz {now_v}{unit}, min {min_v}{unit}, max {max_v}{unit}, srednio {avg_v}{unit}",
                        f"{line.name}: now {now_v}{unit}, min {min_v}{unit}, max {max_v}{unit}, average {avg_v}{unit}"))
    over = [line.name for line in lines if max(line.values) >= limit]
    text = "; ".join(parts) + tr(f". Prog alertu: {_fmt(limit)}{unit}.", f". Alert threshold: {_fmt(limit)}{unit}.")
    if over:
        text += tr(" Prog przekroczony w: ", " Threshold exceeded in: ") + ", ".join(over) + "."
    text += tr(f" Probek: {len(points)}.", f" Samples: {len(points)}.")
    return text


def chart(metric: str, hours: float, *, disk_pct: int, mem_pct: int, load_factor: int,
          now: float | None = None) -> tuple[str | None, str]:
    """(kod Mermaid albo None, podsumowanie). None — brak danych."""
    end = now if now is not None else time.time()
    hours = max(1.0, min(float(hours), 24 * 31))
    points = load(hours, now=end)
    lines = series(metric, points, end - hours * 3600, end)
    limit = threshold(metric, points, disk_pct=disk_pct, mem_pct=mem_pct, load_factor=load_factor)
    text = summary(metric, lines, limit, points)
    if not lines:
        return None, text + tr(" Historia zbiera sie przy kazdym sprawdzeniu czuwania (WATCH_ENABLED=1).",
                               " History is collected at every monitoring check (WATCH_ENABLED=1).")
    return to_mermaid(metric, lines, limit, hours), text


def parse_hours(text: str, default: float = 24) -> float:
    """'24h', '7d', '3 dni', '90m', '12' -> godziny."""
    import re
    text = (text or "").strip().lower()
    if not text:
        return default
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(h|godz\w*|d|dni|dzien|dzień|m|min\w*|t|tydz\w*|w)?", text)
    if not match:
        return default
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2) or "h"
    if unit.startswith(("d", "dn")):
        value *= 24
    elif unit.startswith(("t", "w")):
        value *= 24 * 7
    elif unit.startswith("m"):
        value /= 60
    return max(value, 1 / 60)
