"""
Skrzynka — wiadomosci, ktore Pipe wysyla sam: alerty, podpowiedzi, raporty, ustalenia z badan.

Pipe pisze pierwszy. Czuwanie (core/watch.py), podpowiedzi (core/insights.py), rutyny i badania alertow publikuja
zdarzenia przez Notifier; kazde, ktore uzytkownik powinien zobaczyc, trafia tez tutaj (DATA_DIR/feed.json) —
z tytulem, trescia w Markdownie i akcjami. Dzieki temu wiadomosc nie ginie, gdy nikt nie byl podlaczony,
`pipe web` pokazuje ja na pulpicie, a CLI przy polaczeniu mowi, ile jest nowych.

Kazda wiadomosc dostaje decyzje `push` — czy powiadomic teraz (Telegram, wyskakujace okienko w przegladarce):

  poziom       all | important (domyslnie) | critical — krytyczne przychodza zawsze
  cisza nocna  np. 22:00-07:00: niekrytyczne czekaja, po ciszy przychodzi jedno podsumowanie (albo raport poranny)
  limit        najwyzej tyle podpowiedzi dziennie (domyslnie 3) — reszta czeka w skrzynce
  "mniej"      wyciszony rodzaj podpowiedzi zostaje w skrzynce bez powiadomienia; "pozniej" odklada ja o dobe

Akcje sa budowane w backendzie (nigdy przez model ani klienta): klient pokazuje przyciski i odsyla
`feed_act` z indeksem akcji. Bez importu `settings` — domyslne ustawienia przychodza od wywolujacego.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core import memory
from backend.core.i18n import tr

FORMAT = 1
MAX_ITEMS = 300
KEEP_DAYS = 30
NEW_USER_WINDOW = 86400           # nowy odbiorca widzi jako nowe tylko wiadomosci z ostatniej doby
LEVELS = ("all", "important", "critical")
# Zdarzenia Notifiera, ktore trafiaja do skrzynki. Przypomnienia i prosby o zgode ida prosto do odbiorcy.
FEED_TYPES = frozenset({"alert", "insight", "digest", "welcome", "routine", "investigation", "notice", "login"})
# Zawsze z powiadomieniem: zaplanowany raport, powitanie, podsumowanie ciszy nocnej i prosba o wejscie do pipe web
# (ktos czeka przed ekranem z kodem — takze w ciszy nocnej).
ALWAYS_PUSH = frozenset({"digest", "welcome", "notice", "login"})
ACTION_KINDS = ("command", "message", "snooze", "mute", "dismiss")
_QUIET = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_lock = threading.RLock()


class FeedError(ValueError):
    """Bledne ustawienie albo akcja skrzynki."""


# ─── Dane ───────────────────────────────────────────────────────────────────

@dataclass
class Prefs:
    level: str = "important"
    quiet: str = ""             # "22:00-07:00" albo "" (wylaczona)
    per_day: int = 3            # podpowiedzi z powiadomieniem dziennie
    muted: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Item:
    id: str
    ts: float
    type: str                   # alert | insight | digest | welcome | routine | investigation | notice | login
    severity: str               # critical | warning | info | ok
    title: str
    text: str = ""              # Markdown
    key: str = ""               # alert:<klucz>, podpowiedz — do grupowania i odkladania
    kind: str = ""              # rodzaj (do wyciszania): alert:disk, insight:disk-forecast, routine...
    actions: list[dict[str, Any]] = field(default_factory=list)
    push: bool = True
    held: bool = False          # czeka na koniec ciszy nocnej
    released: bool = False
    done: str = ""              # etykieta wykonanej akcji — przyciski znikaja
    ref: str = ""               # id alertu, do ktorego odnosi sie wiadomosc

    @property
    def at(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M")

    def to_data(self, seen: float | None = None) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at
        data["unread"] = seen is not None and self.ts > seen and not self.done
        data["actions"] = [{**a, "index": i} for i, a in enumerate(self.actions)]
        return data


def action(label: str, kind: str = "command", *, command: str = "", args: dict[str, Any] | None = None,
           admin: bool = False, primary: bool = False) -> dict[str, Any]:
    """Przycisk wiadomosci. `command` — komenda backendu (cleanup, patch, investigate...), `message` — tekst do agenta."""
    if kind not in ACTION_KINDS:
        raise FeedError(f"unknown action kind {kind!r}")
    entry: dict[str, Any] = {"label": label, "kind": kind}
    if command:
        entry["command"] = command
    if args:
        entry["args"] = dict(args)
    if admin:
        entry["admin"] = True
    if primary:
        entry["primary"] = True
    return entry


# ─── Magazyn ────────────────────────────────────────────────────────────────

def path() -> Path:
    return memory.data_dir() / "feed.json"


def _load() -> dict[str, Any]:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"format": FORMAT}
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        return {"format": FORMAT}
    return raw


def _save(data: dict[str, Any]) -> None:
    memory._write_atomic(path(), json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def _items(data: dict[str, Any]) -> list[Item]:
    items = []
    known = set(Item.__dataclass_fields__)
    for raw in data.get("items") or []:
        if isinstance(raw, dict) and raw.get("id") and raw.get("title"):
            try:
                items.append(Item(**{k: v for k, v in raw.items() if k in known}))
            except TypeError:
                continue
    return items


def _store_items(data: dict[str, Any], items: list[Item], now: float) -> None:
    cutoff = now - KEEP_DAYS * 86400
    kept = [i for i in items if i.ts >= cutoff][-MAX_ITEMS:]
    data["items"] = [asdict(i) for i in kept]


# ─── Ustawienia ─────────────────────────────────────────────────────────────

def parse_quiet(value: str) -> tuple[int, int] | None:
    """'22:00-07:00' -> (1320, 420) minut od polnocy; '', 'off' -> None. Bledny zapis -> FeedError."""
    value = (value or "").strip().lower()
    if value in ("", "off", "0", "nie", "no", "brak", "none"):
        return None
    match = _QUIET.match(value)
    if not match:
        raise FeedError(tr(f"Cisza nocna w formacie GG:MM-GG:MM (np. 22:00-07:00), a jest: {value!r}",
                           f"Quiet hours as HH:MM-HH:MM (e.g. 22:00-07:00), got: {value!r}"))
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise FeedError(tr(f"Nieprawidlowa godzina w {value!r}", f"Invalid hour in {value!r}"))
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return None if start == end else (start, end)


def in_quiet(quiet: str, when: datetime) -> bool:
    try:
        window = parse_quiet(quiet)
    except FeedError:
        return False
    if window is None:
        return False
    start, end = window
    minute = when.hour * 60 + when.minute
    return start <= minute < end if start < end else (minute >= start or minute < end)


def quiet_ends_now(quiet: str, when: datetime) -> bool:
    """Czy w tej minucie konczy sie cisza nocna."""
    try:
        window = parse_quiet(quiet)
    except FeedError:
        return False
    return window is not None and when.hour * 60 + when.minute == window[1]


def normalize_level(value: str) -> str:
    value = (value or "").strip().lower()
    aliases = {"wszystkie": "all", "wszystko": "all", "wazne": "important", "ważne": "important",
               "krytyczne": "critical", "tylko krytyczne": "critical", "critical-only": "critical"}
    value = aliases.get(value, value)
    if value not in LEVELS:
        raise FeedError(tr(f"Poziom powiadomien: all, important albo critical (a jest: {value!r})",
                           f"Notification level: all, important or critical (got: {value!r})"))
    return value


def defaults_from(level: str = "important", quiet: str = "", per_day: int = 3) -> Prefs:
    """Domyslne ustawienia z .env (NOTIFY_LEVEL, QUIET_HOURS, INSIGHTS_PER_DAY) — bledne wartosci ignorowane."""
    try:
        level = normalize_level(level)
    except FeedError:
        level = "important"
    try:
        parse_quiet(quiet)
    except FeedError:
        quiet = ""
    return Prefs(level=level, quiet=(quiet or "").strip(), per_day=max(0, int(per_day)))


def prefs(defaults: Prefs | None = None) -> Prefs:
    """Ustawienia: zapisane w skrzynce (komenda `prefs`) nadpisuja domyslne z .env."""
    base = defaults or Prefs()
    with _lock:
        stored = _load().get("prefs") or {}
    if not isinstance(stored, dict):
        stored = {}
    muted = [str(k) for k in stored.get("muted") or [] if isinstance(k, str)]
    result = replace(base, muted=muted)
    if stored.get("level") in LEVELS:
        result.level = stored["level"]
    if isinstance(stored.get("quiet"), str):
        try:
            parse_quiet(stored["quiet"])
            result.quiet = stored["quiet"]
        except FeedError:
            pass
    if isinstance(stored.get("per_day"), int) and 0 <= stored["per_day"] <= 50:
        result.per_day = stored["per_day"]
    return result


def set_prefs(*, level: str | None = None, quiet: str | None = None, per_day: int | None = None,
              defaults: Prefs | None = None) -> Prefs:
    """Zmienia ustawienia (walidacja przed zapisem). Zwraca obowiazujace ustawienia."""
    changes: dict[str, Any] = {}
    if level is not None:
        changes["level"] = normalize_level(level)
    if quiet is not None:
        window = parse_quiet(quiet)
        changes["quiet"] = "" if window is None else quiet.strip()
    if per_day is not None:
        if not 0 <= int(per_day) <= 50:
            raise FeedError(tr("Limit podpowiedzi dziennie: od 0 do 50.", "Suggestions per day: from 0 to 50."))
        changes["per_day"] = int(per_day)
    with _lock:
        data = _load()
        stored = data.get("prefs") if isinstance(data.get("prefs"), dict) else {}
        stored.update(changes)
        data["prefs"] = stored
        _save(data)
    return prefs(defaults)


def mute(kind: str, on: bool = True) -> list[str]:
    """Wycisza (albo przywraca) rodzaj wiadomosci. Zwraca liste wyciszonych."""
    kind = (kind or "").strip()
    if not kind:
        raise FeedError("empty kind")
    with _lock:
        data = _load()
        stored = data.get("prefs") if isinstance(data.get("prefs"), dict) else {}
        muted = [k for k in stored.get("muted") or [] if isinstance(k, str) and k != kind]
        if on:
            muted.append(kind)
        stored["muted"] = sorted(set(muted))
        data["prefs"] = stored
        _save(data)
        return stored["muted"]


def snooze(key: str, hours: float = 24, now: float | None = None) -> float:
    """Odklada wiadomosci o tym kluczu (podpowiedz wroci najwczesniej po `hours`). Zwraca termin."""
    until = (now if now is not None else time.time()) + hours * 3600
    with _lock:
        data = _load()
        snoozed = data.get("snoozed") if isinstance(data.get("snoozed"), dict) else {}
        snoozed[key] = until
        data["snoozed"] = {k: v for k, v in snoozed.items() if isinstance(v, (int, float)) and v > time.time()}
        _save(data)
    return until


def snoozed(key: str, now: float | None = None) -> bool:
    with _lock:
        until = (_load().get("snoozed") or {}).get(key)
    return isinstance(until, (int, float)) and until > (now if now is not None else time.time())


# ─── Zdarzenie -> wiadomosc ─────────────────────────────────────────────────

def _sections_text(sections: list[dict[str, Any]]) -> str:
    parts = []
    for section in sections or []:
        lines = "\n".join(f"- {line}" for line in section.get("lines") or [])
        parts.append(f"**{section.get('title', '')}**\n{lines}".strip())
    return "\n\n".join(parts)


PATCH_SCOPES = {"vuln:system": "system", "vuln:images": "images", "vuln:servers": "servers", "vuln:apps": "apps"}


def from_event(event: dict[str, Any]) -> Item | None:
    """Zdarzenie Notifiera -> wiadomosc skrzynki (None — zdarzenie nie trafia do skrzynki)."""
    kind = event.get("type")
    if kind not in FEED_TYPES:
        return None
    now = time.time()
    item = Item(id=secrets.token_hex(5), ts=now, type=kind, severity="info", title="")
    if kind == "alert":
        scope = str(event.get("key", "")).split(":", 1)[0]
        resolved = event.get("state") == "resolved"
        item.title = (tr("Rozwiazane: ", "Resolved: ") if resolved else "") + str(event.get("title", ""))
        item.severity = "ok" if resolved else str(event.get("severity") or "warning")
        body = str(event.get("report") or event.get("detail") or "")
        if event.get("history"):
            body += ("\n\n" if body else "") + tr("Poprzednio: ", "Last time: ") + str(event["history"])
        item.text, item.key, item.kind, item.ref = body, f"alert:{event.get('key', '')}", f"alert:{scope}", str(event.get("id", ""))
        if not resolved:
            if scope == "disk":
                item.actions.append(action(tr("Zwolnij miejsce", "Free up space"), command="cleanup", admin=True,
                                           primary=True))
            patch = PATCH_SCOPES.get(str(event.get("key", "")))
            if patch:
                item.actions.append(action(tr("Zalataj", "Patch"), command="patch", args={"scope": patch}, admin=True,
                                           primary=True))
            if event.get("id"):
                item.actions.append(action(tr("Zbadaj", "Investigate"), command="investigate", args={"id": event["id"]}))
    elif kind == "insight":
        item.title, item.text = str(event.get("title", "")), str(event.get("text", ""))
        item.severity = str(event.get("severity") or "info")
        item.key, item.kind = str(event.get("key", "")), "insight:" + str(event.get("kind", "") or "other")
        item.actions = [a for a in event.get("actions") or [] if isinstance(a, dict) and a.get("kind") in ACTION_KINDS]
        item.actions.append(action(tr("Pozniej", "Later"), "snooze"))
        item.actions.append(action(tr("Mniej takich", "Fewer like this"), "mute"))
    elif kind in ("digest", "welcome"):
        item.title = str(event.get("title", ""))
        item.text = str(event.get("summary") or "") or _sections_text(event.get("sections") or [])
        item.kind = kind
    elif kind == "routine":
        status = str(event.get("status", ""))
        item.title = tr(f"Rutyna {event.get('name', '')}: {status}", f"Routine {event.get('name', '')}: {status}")
        item.text, item.kind = str(event.get("report", "")), "routine"
        item.severity = "ok" if status.upper() == "OK" else "warning"
    elif kind == "investigation":
        item.title = tr("Ustalilem: ", "What I found: ") + str(event.get("title", ""))
        item.text, item.kind = str(event.get("report", "")), "investigation"
        item.severity = str(event.get("severity") or "warning")
        item.key, item.ref = f"alert:{event.get('key', '')}", str(event.get("alert_id", ""))
        fix = event.get("fix_message")
        if fix:
            item.actions.append(action(tr("Napraw", "Fix it"), "message", args={"text": str(fix)}, admin=True,
                                       primary=True))
    elif kind == "notice":
        item.title, item.text = str(event.get("title", "")), str(event.get("text", ""))
        item.kind = "notice"
    elif kind == "login":
        # prosba o wejscie do pipe web wystawionego publicznie (core/webaccess.py) — decyduje administrator
        pairing = str(event.get("id", ""))
        item.title, item.text = str(event.get("title", "")), str(event.get("text", ""))
        item.severity, item.kind, item.key, item.ref = "warning", "login", f"login:{pairing}", pairing
        item.actions = [
            action(tr("Wpusc", "Let in"), command="web", args={"args": "approve", "id": pairing}, admin=True, primary=True),
            action(tr("Tylko podglad", "View only"), command="web", args={"args": "approve", "id": pairing, "role": "viewer"},
                   admin=True),
            action(tr("Odrzuc", "Reject"), command="web", args={"args": "deny", "id": pairing}, admin=True),
        ]
    if not item.title:
        return None
    return item


def decide(item: Item, current: Prefs, when: datetime, pushed_today: int) -> tuple[bool, bool]:
    """(powiadomic teraz, czeka na koniec ciszy nocnej)."""
    if item.type in ALWAYS_PUSH or item.severity == "critical":
        return True, False
    if item.kind in current.muted:
        return False, False
    if item.type == "insight":
        wanted = current.level == "all" or (current.level == "important" and item.severity == "warning")
        if wanted and pushed_today >= current.per_day:
            wanted = False
    else:
        wanted = current.level != "critical"
    if wanted and in_quiet(current.quiet, when):
        return False, True
    return wanted, False


def add(event: dict[str, Any], *, defaults: Prefs | None = None, now: float | None = None) -> Item | None:
    """Zapisuje zdarzenie w skrzynce i decyduje o powiadomieniu. Zwraca wiadomosc albo None."""
    item = from_event(event)
    if item is None:
        return None
    now = now if now is not None else time.time()
    item.ts = now
    when = datetime.fromtimestamp(now)
    current = prefs(defaults)
    with _lock:
        data = _load()
        day = when.strftime("%Y-%m-%d")
        sent = data.get("sent") if isinstance(data.get("sent"), dict) else {}
        pushed = int((sent.get(day) or {}).get("insight", 0)) if isinstance(sent.get(day), dict) else 0
        item.push, item.held = decide(item, current, when, pushed)
        if item.push and item.type == "insight":
            sent = {d: v for d, v in sent.items() if d >= (datetime.fromtimestamp(now - 7 * 86400)).strftime("%Y-%m-%d")}
            sent.setdefault(day, {})["insight"] = pushed + 1
            data["sent"] = sent
        items = _items(data)
        if item.type == "alert" and item.severity == "ok":
            # rozwiazany problem — przyciski przy jego starszej wiadomosci nie maja juz sensu
            for old in items:
                if old.key == item.key and old.type in ("alert", "investigation") and not old.done:
                    old.done = item.title
        items.append(item)
        _store_items(data, items, now)
        _save(data)
    return item


# ─── Odczyt i stan przeczytania ─────────────────────────────────────────────

def recent(limit: int = 50, since: float | None = None) -> list[Item]:
    """Najnowsze wiadomosci (od najnowszej). `since` — tylko pozniejsze niz ten czas."""
    with _lock:
        items = _items(_load())
    if since is not None:
        items = [i for i in items if i.ts > since]
    return list(reversed(items))[:limit]


def get(item_id: str) -> Item | None:
    with _lock:
        return next((i for i in _items(_load()) if i.id == item_id), None)


def seen_at(user: str, now: float | None = None) -> float:
    """Kiedy odbiorca ostatnio przejrzal skrzynke. Nowy odbiorca: dobe temu (stare wiadomosci nie sa 'nowe')."""
    with _lock:
        seen = (_load().get("seen") or {}).get(user)
    if isinstance(seen, (int, float)):
        return float(seen)
    return (now if now is not None else time.time()) - NEW_USER_WINDOW


def mark_seen(user: str, when: float | None = None) -> None:
    with _lock:
        data = _load()
        seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
        seen[user] = when if when is not None else time.time()
        data["seen"] = dict(sorted(seen.items(), key=lambda kv: -kv[1])[:50])
        _save(data)


def unread(user: str, now: float | None = None) -> int:
    since = seen_at(user, now)
    return sum(1 for i in recent(MAX_ITEMS) if i.ts > since and not i.done)


def mark_done(item_id: str, label: str) -> Item | None:
    with _lock:
        data = _load()
        items = _items(data)
        found = next((i for i in items if i.id == item_id), None)
        if found is None:
            return None
        found.done = label or "ok"
        data["items"] = [asdict(i) for i in items]
        _save(data)
        return found


def release_held(now: float | None = None) -> list[Item]:
    """Wiadomosci wstrzymane przez cisze nocna — oznacza je jako wydane i zwraca (do podsumowania)."""
    with _lock:
        data = _load()
        items = _items(data)
        held = [i for i in items if i.held and not i.released and not i.done]
        if not held:
            return []
        for item in held:
            item.released = True
        data["items"] = [asdict(i) for i in items]
        _save(data)
    return held


def held_notice(items: list[Item]) -> dict[str, Any] | None:
    """Zdarzenie `notice` z podsumowaniem tego, co czekalo podczas ciszy nocnej."""
    if not items:
        return None
    lines = "\n".join(f"- {i.title}" for i in items[:12])
    more = len(items) - 12
    if more > 0:
        lines += tr(f"\n- ... i {more} wiecej", f"\n- ... and {more} more")
    return {"type": "notice",
            "title": tr(f"Podczas ciszy nocnej: {len(items)}", f"During quiet hours: {len(items)}"),
            "text": lines + tr("\n\nSzczegoly w skrzynce.", "\n\nDetails in the inbox.")}


def listing(user: str, limit: int = 30, now: float | None = None) -> dict[str, Any]:
    """Dane dla komendy `feed`: wiadomosci z flaga `unread` i liczba nowych dla odbiorcy."""
    since = seen_at(user, now)
    items = recent(limit)
    return {"items": [i.to_data(since) for i in items], "unread": unread(user, now), "seen": since}


def text_list(user: str, limit: int = 10, now: float | None = None) -> str:
    """Skrzynka jako zwykly tekst (CLI, notatka dla modelu)."""
    since = seen_at(user, now)
    items = recent(limit)
    if not items:
        return tr("Skrzynka jest pusta — Pipe napisze, gdy cos bedzie wymagalo uwagi.",
                  "The inbox is empty — Pipe will write when something needs attention.")
    lines = []
    for index, item in enumerate(items, start=1):
        mark = "*" if item.ts > since and not item.done else " "
        lines.append(f"{mark}{index}. [{item.at}] {item.title}")
    return "\n".join(lines)
