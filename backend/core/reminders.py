"""
Przypomnienia — jednorazowe wiadomosci i zadania o okreslonym czasie
("napisz do mnie za 10 sekund", "jutro o 9 przypomnij o domenie",
"za godzine sprawdz, czy backup sie skonczyl").

Rutyny (core/routines.py) sa cykliczne i maja rozdzielczosc minuty; przypomnienie
odpala sie raz, z dokladnoscia do sekundy, i znika.

Dwa rodzaje:
  message  sama tresc — bez LLM, bez kosztow
  task     zadanie dla workera (tylko odczyty, jak rutyna); wynik przychodzi jako raport

Przypomnienia leza w DATA_DIR/reminders.json, wiec przetrwaja restart backendu —
zalegle odpalaja sie po starcie. Dostarcza je czuwanie (Watcher._reminder_loop)
kanalem zdarzen do subskrybentow (bot Telegram). Gdy nikt nie slucha, przypomnienie
czeka jako `fired` i odbiera je CLI przy najblizszej okazji (komenda "reminders", claim).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

from backend.core import memory
from backend.core.i18n import tr

MAX_REMINDERS = 50
MAX_TEXT_CHARS = 1_000
MAX_DELAY_SECONDS = 366 * 24 * 3600
KINDS = ("message", "task")
_UNITS = {"s": 1, "m": 60, "h": 3600, "g": 3600, "d": 86400, "w": 604800, "hr": 3600, "hrs": 3600}
# Dluzsze zapisy rozpoznajemy po poczatku slowa: sekund, seconds, minuty, godzin, hours, dni, days, tygodnie...
_UNIT_ROOTS = (("sek", 1), ("sec", 1), ("min", 60), ("godz", 3600), ("hour", 3600), ("dni", 86400), ("dzie", 86400),
               ("day", 86400), ("tyg", 604800), ("tydz", 604800), ("week", 604800))
_DELAY_PART = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-z]*)")


class ReminderError(ValueError):
    """Nieprawidlowe przypomnienie albo czas."""


@dataclass
class Reminder:
    id: str
    at: float                  # czas odpalenia (epoch)
    text: str
    kind: str = "message"      # message | task
    target: str = "local"      # dla task: cel workera
    to: str = ""               # interfejs, ktory ustawil przypomnienie (np. telegram:123, cli:kuba)
    created: float = 0.0
    fired: bool = False        # czas minal, ale nikt nie sluchal — czeka na odbior
    report: str = ""           # dla task: wynik workera (gdy czeka na odbior)
    published: bool = False    # poszlo juz do subskrybentow (Telegram, web); czeka tylko na odbior w CLI

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M:%S")

    def describe(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        left = self.at - now
        if self.fired and self.published:
            state = tr("wyslane, czeka na odbior w CLI", "sent, waiting to be picked up in the CLI")
        elif self.fired:
            state = tr("czeka na odbior", "waiting to be delivered")
        elif left <= 0:
            state = tr("za chwile", "any moment")
        else:
            state = tr(f"za {format_delay(left)}", f"in {format_delay(left)}")
        kind = "" if self.kind == "message" else tr(f" [zadanie na celu {self.target}]", f" [task on target {self.target}]")
        return f"#{self.id} {self.when} ({state}){kind}: {self.text[:200]}"

    def to_event(self) -> dict:
        return {"type": "reminder", "id": self.id, "kind": self.kind, "text": self.text, "to": self.to,
                "report": self.report, "set_at": datetime.fromtimestamp(self.created).strftime("%Y-%m-%d %H:%M"),
                "due": self.when}


# ─── Czas ───────────────────────────────────────────────────────────────────

def format_delay(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} min" + (f" {rest} s" if rest and minutes < 10 else "")
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        return f"{hours} h" + (f" {rest // 60} min" if rest >= 60 else "")
    days, rest = divmod(seconds, 86400)
    return tr(f"{days} dni", f"{days} d") + (f" {rest // 3600} h" if rest >= 3600 else "")


def parse_delay(text: str) -> float:
    """'10s', '5 min', '1h30m', '2d', sama liczba = sekundy. Zwraca sekundy."""
    raw = str(text or "").strip().lower()
    if not raw:
        raise ReminderError(tr("Pusty czas.", "Empty time."))
    total, position = 0.0, 0
    for match in _DELAY_PART.finditer(raw):
        if raw[position:match.start()].strip():
            break
        unit = match.group(2) or "s"
        factor = _UNITS.get(unit) or next((value for root, value in _UNIT_ROOTS if unit.startswith(root)), None)
        if factor is None:
            raise ReminderError(tr(f"Nieznana jednostka czasu {unit!r} — uzyj s, m, h albo d (np. '90s', '15m', '2h').",
                                   f"Unknown time unit {unit!r} — use s, m, h or d (e.g. '90s', '15m', '2h')."))
        total += float(match.group(1).replace(",", ".")) * factor
        position = match.end()
    if position == 0 or raw[position:].strip():
        raise ReminderError(tr(f"Nie rozumiem czasu {text!r} — podaj np. '10s', '15m', '2h', '1d'.",
                               f"I do not understand the time {text!r} — give e.g. '10s', '15m', '2h', '1d'."))
    return total


def parse_at(text: str, now: datetime | None = None) -> float:
    """'HH:MM' (dzis albo jutro, jesli juz minela), 'YYYY-MM-DD HH:MM[:SS]', ISO. Czas lokalny serwera."""
    now = now or datetime.now()
    raw = str(text or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    try:
        if match:
            when = now.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                               second=int(match.group(3) or 0), microsecond=0)
            if when <= now:
                when += timedelta(days=1)
        else:
            when = datetime.fromisoformat(raw.replace("T", " ").replace("Z", ""))
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
    except ValueError:
        raise ReminderError(tr(f"Nie rozumiem czasu {text!r} — podaj 'HH:MM' albo 'RRRR-MM-DD HH:MM' (czas serwera).",
                               f"I do not understand the time {text!r} — give 'HH:MM' or 'YYYY-MM-DD HH:MM' "
                               "(server time).")) from None
    return when.timestamp()


def resolve_time(delay: str = "", at: str = "", now: float | None = None) -> float:
    """Czas odpalenia z `delay` (wzgledny) albo `at` (bezwzgledny)."""
    now = time.time() if now is None else now
    if delay and at:
        raise ReminderError(tr("Podaj delay albo at, nie oba.", "Give delay or at, not both."))
    if delay:
        when = now + parse_delay(delay)
    elif at:
        when = parse_at(at, datetime.fromtimestamp(now))
    else:
        raise ReminderError(tr("Podaj, kiedy: delay (np. '10s', '2h') albo at ('HH:MM', 'RRRR-MM-DD HH:MM').",
                               "Say when: delay (e.g. '10s', '2h') or at ('HH:MM', 'YYYY-MM-DD HH:MM')."))
    if when < now - 1:
        raise ReminderError(tr("Ten czas juz minal.", "That time has already passed."))
    if when - now > MAX_DELAY_SECONDS:
        raise ReminderError(tr("Przypomnienie najdalej za rok — do zadan cyklicznych sluza rutyny.",
                               "A reminder can be at most a year ahead — use routines for recurring tasks."))
    return when


# ─── Rejestr ────────────────────────────────────────────────────────────────

def reminders_path() -> Path:
    return memory.data_dir() / "reminders.json"


def load_reminders() -> list[Reminder]:
    path = reminders_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    names = {f.name for f in fields(Reminder)}
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            try:
                result.append(Reminder(**{k: v for k, v in item.items() if k in names}))
            except TypeError:
                continue
    return sorted(result, key=lambda r: r.at)


def _save(items: list[Reminder]) -> None:
    memory._write_atomic(reminders_path(), json.dumps([asdict(r) for r in sorted(items, key=lambda r: r.at)],
                                                      ensure_ascii=False, indent=2) + "\n")


def validate(text: str, kind: str) -> tuple[str, str]:
    text = " ".join(str(text or "").split()) if kind == "message" else str(text or "").strip()
    kind = (kind or "message").strip().lower()
    if kind not in KINDS:
        raise ReminderError(tr("kind: message albo task.", "kind: message or task."))
    if not text:
        raise ReminderError(tr("Podaj tresc przypomnienia (text).", "Give the reminder text."))
    if len(text) > MAX_TEXT_CHARS:
        raise ReminderError(tr(f"Tresc za dluga (limit {MAX_TEXT_CHARS} znakow).",
                               f"Text too long (limit {MAX_TEXT_CHARS} characters)."))
    label = memory.find_secret(text)
    if label:
        raise ReminderError(tr(f"Tresc wyglada na sekret ({label}).", f"The text looks like a secret ({label})."))
    return text, kind


def add(at: float, text: str, *, kind: str = "message", target: str = "local", to: str = "",
        now: float | None = None) -> Reminder:
    text, kind = validate(text, kind)
    items = load_reminders()
    if len(items) >= MAX_REMINDERS:
        raise ReminderError(tr(f"Za duzo przypomnien (limit {MAX_REMINDERS}) — anuluj nieaktualne.",
                               f"Too many reminders (limit {MAX_REMINDERS}) — cancel stale ones."))
    reminder = Reminder(id=secrets.token_hex(3), at=float(at), text=text, kind=kind, target=target, to=to,
                        created=time.time() if now is None else now)
    _save(items + [reminder])
    return reminder


def get(reminder_id: str) -> Reminder | None:
    wanted = str(reminder_id or "").strip().lstrip("#").lower()
    return next((r for r in load_reminders() if r.id == wanted), None)


def remove(reminder_id: str) -> Reminder | None:
    items = load_reminders()
    found = next((r for r in items if r.id == str(reminder_id or "").strip().lstrip("#").lower()), None)
    if found is not None:
        _save([r for r in items if r.id != found.id])
    return found


def mark_fired(reminder_id: str, report: str = "", published: bool = False) -> None:
    items = load_reminders()
    for item in items:
        if item.id == reminder_id:
            item.fired, item.report, item.published = True, report, published
    _save(items)


def for_cli(reminder: Reminder) -> bool:
    """
    Ustawione z CLI. CLI nie subskrybuje zdarzen, wiec takie przypomnienie po wyslaniu subskrybentom
    zostaje jeszcze w rejestrze (`published`), az CLI je odbierze — inaczej widzialby je tylko Telegram.
    """
    return reminder.to == "cli" or reminder.to.startswith("cli:")


def due(now: float | None = None) -> list[Reminder]:
    """Przypomnienia, ktorych czas nadszedl, a jeszcze nie odpalily."""
    now = time.time() if now is None else now
    return [r for r in load_reminders() if not r.fired and r.at <= now]


def waiting() -> list[Reminder]:
    """Odpalone, ale niedostarczone subskrybentom (nikt nie sluchal)."""
    return [r for r in load_reminders() if r.fired and not r.published]


def next_due(now: float | None = None) -> float | None:
    """Sekundy do najblizszego nieodpalonego przypomnienia (None — brak)."""
    now = time.time() if now is None else now
    pending = [r.at for r in load_reminders() if not r.fired]
    return max(0.0, min(pending) - now) if pending else None


def claim(prefix: str) -> list[Reminder]:
    """Odbiera (i usuwa) czekajace przypomnienia ustawione przez interfejs o danym prefiksie, np. 'cli'."""
    items = load_reminders()
    mine = [r for r in items if r.fired and (r.to == prefix or r.to.startswith(prefix + ":") or not r.to)]
    if mine:
        taken = {r.id for r in mine}
        _save([r for r in items if r.id not in taken])
    return mine


def owned_by(reminder: Reminder, interface: str) -> bool:
    return bool(reminder.to) and reminder.to == interface


def zone() -> str:
    """Nazwa strefy czasowej serwera (np. UTC, CEST) — wszystkie czasy przypomnien sa w niej."""
    return time.strftime("%Z") or "UTC"


def render_list(items: list[Reminder] | None = None, now: float | None = None) -> str:
    items = load_reminders() if items is None else items
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S") + " " + zone()
    if not items:
        return tr(f"Brak przypomnien. Czas serwera: {stamp}.", f"No reminders. Server time: {stamp}.")
    return tr(f"Przypomnienia (czas serwera: {stamp}):\n", f"Reminders (server time: {stamp}):\n") \
        + "\n".join(f"- {r.describe(now)}" for r in items)
