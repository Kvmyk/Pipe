"""
Rutyny — zadania, ktore agent wykonuje sam wedlug harmonogramu i raportuje
uzytkownikowi (np. "codziennie o 7:00 sprawdz backupy i certyfikaty").

Rutyne wykonuje worker (core/workers.py), wiec obowiazuja te same zasady:
tylko odczyty, zmiany trafiaja do raportu jako propozycje. Raport idzie do
subskrybentow (bot Telegram) — zawsze albo tylko przy problemie (notify).

Harmonogram to wyrazenie cron (5 pol: minuta godzina dzien miesiac dzien_tygodnia)
albo skrot: @hourly, @daily, @weekly, @monthly. Czas lokalny serwera.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from backend.core import memory

MAX_ROUTINES = 30
MAX_TASK_CHARS = 2_000
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 7 * * *",
    "@weekly": "0 7 * * 1",
    "@monthly": "0 7 1 * *",
}
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class RoutineError(ValueError):
    """Nieprawidlowa rutyna albo harmonogram."""


# ─── Cron ───────────────────────────────────────────────────────────────────

def _field_values(expr: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        step = 1
        has_step = "/" in part
        if has_step:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise RoutineError(f"Nieprawidlowy krok w {expr!r}.")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise RoutineError(f"Nieprawidlowy zakres w {expr!r}.")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if has_step:
                end = high  # `5/15` = od 5 co 15
        else:
            raise RoutineError(f"Nieprawidlowe pole cron {expr!r}.")
        if start < low or end > high or start > end:
            raise RoutineError(f"Wartosc poza zakresem {low}-{high} w {expr!r}.")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class Schedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]     # 0 = niedziela
    day_any: bool
    weekday_any: bool

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes or when.hour not in self.hours or when.month not in self.months:
            return False
        weekday = (when.weekday() + 1) % 7   # Python: pon=0 -> cron: nd=0
        day_ok = when.day in self.days
        weekday_ok = weekday in self.weekdays
        # Regula crona: gdy oba pola sa ograniczone, wystarczy jedno z nich
        if self.day_any or self.weekday_any:
            return day_ok and weekday_ok
        return day_ok or weekday_ok


def parse_schedule(expr: str) -> Schedule:
    text = _ALIASES.get((expr or "").strip().lower(), (expr or "").strip())
    parts = text.split()
    if len(parts) != 5:
        raise RoutineError("Harmonogram to 5 pol cron (np. '0 7 * * *') albo @hourly/@daily/@weekly/@monthly.")
    sets = [_field_values(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    weekdays = {0 if d == 7 else d for d in sets[4]}
    return Schedule(frozenset(sets[0]), frozenset(sets[1]), frozenset(sets[2]), frozenset(sets[3]),
                    frozenset(weekdays), parts[2] == "*", parts[4] == "*")


# ─── Rejestr ────────────────────────────────────────────────────────────────

@dataclass
class Routine:
    name: str
    schedule: str
    task: str
    target: str = "local"
    notify: str = "always"        # always | problems
    enabled: bool = True
    last_run: str = ""
    last_status: str = ""

    def describe(self) -> str:
        state = "" if self.enabled else " [wylaczona]"
        last = f", ostatnio {self.last_run} {self.last_status}".rstrip() if self.last_run else ""
        notify = "zawsze" if self.notify == "always" else "tylko przy problemie"
        return f"{self.name}{state}: '{self.schedule}' na celu {self.target}, raport {notify}{last} — {self.task[:160]}"


def routines_path() -> Path:
    return memory.data_dir() / "routines.json"


def load_routines() -> list[Routine]:
    path = routines_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    names = {f.name for f in fields(Routine)}
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            try:
                result.append(Routine(**{k: v for k, v in item.items() if k in names}))
            except TypeError:
                continue
    return sorted(result, key=lambda r: r.name)


def _save(routines: list[Routine]) -> None:
    memory._write_atomic(routines_path(), json.dumps([asdict(r) for r in sorted(routines, key=lambda r: r.name)],
                                                     ensure_ascii=False, indent=2) + "\n")


def validate(routine: Routine) -> Routine:
    routine.name = (routine.name or "").strip().lower()
    if not _NAME.match(routine.name):
        raise RoutineError("Nazwa rutyny: male litery, cyfry, '-' i '_' (1-32 znaki).")
    parse_schedule(routine.schedule)
    routine.task = (routine.task or "").strip()
    if not routine.task:
        raise RoutineError("Podaj zadanie (task): co sprawdzic i co zawrzec w raporcie.")
    if len(routine.task) > MAX_TASK_CHARS:
        raise RoutineError(f"Zadanie za dlugie (limit {MAX_TASK_CHARS} znakow).")
    if routine.notify not in ("always", "problems"):
        raise RoutineError("notify: always albo problems.")
    label = memory.find_secret(routine.task)
    if label:
        raise RoutineError(f"Zadanie wyglada na sekret ({label}).")
    return routine


def save_routine(routine: Routine) -> bool:
    routine = validate(routine)
    routines = load_routines()
    existing = next((r for r in routines if r.name == routine.name), None)
    if existing is None and len(routines) >= MAX_ROUTINES:
        raise RoutineError(f"Za duzo rutyn (limit {MAX_ROUTINES}).")
    if existing:
        routine.last_run, routine.last_status = existing.last_run, existing.last_status
    _save([r for r in routines if r.name != routine.name] + [routine])
    return existing is None


def update_routine(name: str, **changes: object) -> Routine | None:
    routines = load_routines()
    for routine in routines:
        if routine.name == name:
            for key, value in changes.items():
                setattr(routine, key, value)
            _save(routines)
            return routine
    return None


def remove_routine(name: str) -> bool:
    routines = load_routines()
    kept = [r for r in routines if r.name != name]
    if len(kept) == len(routines):
        return False
    _save(kept)
    return True


def due(routines: list[Routine], when: datetime) -> list[Routine]:
    result = []
    for routine in routines:
        if not routine.enabled:
            continue
        try:
            if parse_schedule(routine.schedule).matches(when):
                result.append(routine)
        except RoutineError:
            continue
    return result


def report_status(report: str) -> str:
    """OK / PROBLEM z pierwszej linii 'STATUS: ...' raportu (brak linii = PROBLEM, lepiej dmuchac na zimne)."""
    match = re.search(r"STATUS:\s*(OK|PROBLEM)", report or "", re.IGNORECASE)
    return match.group(1).upper() if match else "PROBLEM"
