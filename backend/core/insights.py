"""
Podpowiedzi — Pipe sam zauwaza, zanim cos sie zepsuje, i proponuje jeden krok.

Alerty czuwania (core/watch.py) mowia, ze cos JUZ jest zle. Podpowiedzi mowia, co bedzie zle albo co mozna
poprawic, i od razu daja przycisk: "Zwolnij miejsce", "Sprawdz przyczyne". Wszystko deterministyczne, z danych,
ktore Pipe i tak zbiera (historia pomiarow, kontenery, pliki hosta) — bez modelu jezykowego, wiec bez kosztow.

  disk-forecast   dysk zapelnia sie w stalym tempie — za ile dni sie skonczy (regresja z historii pomiarow)
  docker-space    obrazy bez tagu, cache budowania i pobrane paczki do odzyskania jednym przyciskiem
  restarts        kontener restartuje sie co jakis czas (nie w petli — tego pilnuje alert)
  reboot          serwer od kilku dni czeka na restart po aktualizacji jadra
  memory-trend    zajetosc RAM stale rosnie (mozliwy wyciek pamieci)

Kazda podpowiedz ma klucz: przychodzi raz, wraca dopiero po `cooldown` albo gdy sprawa jest powazniejsza
(info -> warning). O tym, czy powiadomic od razu, decyduje skrzynka (core/feed.py: poziom, limit, cisza nocna).
Bez importu `settings`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from backend.core import feed, hostinfo, runtime
from backend.core.i18n import tr

DAY = 86400.0
FORECAST_DAYS = 7               # podpowiedz, gdy dysk zapelni sie w ciagu tylu dni
FORECAST_MIN_SPAN = 6 * 3600    # najkrotsza historia, z ktorej liczymy tempo
FORECAST_MIN_POINTS = 12
FORECAST_MIN_RATE = 0.5         # punktow procentowych na dobe — wolniejszy przyrost to szum
SPACE_MIN_BYTES = 3 * 1024 ** 3
RESTARTS_MIN = 3
REBOOT_AFTER_DAYS = 3
MEMORY_MIN_RISE = 15            # punktow procentowych w ciagu doby
MEMORY_MIN_NOW = 75


@dataclass
class Insight:
    key: str
    kind: str
    severity: str               # info | warning
    title: str
    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    cooldown: float = DAY

    def to_event(self) -> dict[str, Any]:
        return {"type": "insight", "key": self.key, "kind": self.kind, "severity": self.severity,
                "title": self.title, "text": self.text, "actions": self.actions}


def _gb(value: float) -> str:
    gib = value / 1024 ** 3
    text = f"{gib:.1f}" if gib < 100 else f"{gib:.0f}"
    return tr(text.replace(".", ",") + " GB", text + " GB")


def _days(value: float) -> str:
    if value < 1:
        hours = max(1, round(value * 24))
        return tr(f"ok. {hours} h", f"about {hours} h")
    rounded = round(value)
    return tr(f"ok. {rounded} {'dzien' if rounded == 1 else 'dni'}", f"about {rounded} day{'s' if rounded != 1 else ''}")


def slope(points: list[tuple[float, float]]) -> float:
    """Nachylenie prostej najmniejszych kwadratow (jednostka y na sekunde)."""
    n = len(points)
    if n < 2:
        return 0.0
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0:
        return 0.0
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / var


def _cleanup_action() -> dict[str, Any]:
    return feed.action(tr("Zwolnij miejsce", "Free up space"), command="cleanup", admin=True, primary=True)


def _ask(label: str, text: str) -> dict[str, Any]:
    return feed.action(label, "message", args={"text": text})


# ─── Producenci ─────────────────────────────────────────────────────────────

def disk_forecast(points: list[dict[str, Any]], disks: list[hostinfo.Disk], now: float) -> list[Insight]:
    """Dysk zapelnia sie w stalym tempie: przy ktorym dniu dojdzie do 100%."""
    found = []
    totals = {d.mount: d for d in disks}
    for mount, disk in totals.items():
        series = [(float(p["t"]), float((p.get("disks") or {}).get(mount)))
                  for p in points if isinstance((p.get("disks") or {}).get(mount), (int, float))
                  and now - float(p["t"]) <= 2 * DAY]
        if len(series) < FORECAST_MIN_POINTS or series[-1][0] - series[0][0] < FORECAST_MIN_SPAN:
            continue
        rate = slope(series) * DAY                     # punkty procentowe na dobe
        current = float(disk.used_pct)
        if rate < FORECAST_MIN_RATE or current < 50 or current >= 99:
            continue
        days_left = (100 - current) / rate
        if days_left > FORECAST_DAYS:
            continue
        per_day = disk.total * rate / 100 if disk.total else 0
        speed = tr(f"przybywa {_gb(per_day)} na dobe", f"{_gb(per_day)} more every day") if per_day else \
            tr(f"przybywa {rate:.1f} pkt proc. na dobe".replace(".", ","), f"{rate:.1f} points more every day")
        found.append(Insight(
            key=f"disk-forecast:{mount}", kind="disk-forecast",
            severity="warning" if days_left <= 3 else "info",
            title=tr(f"Dysk {mount} zapelni sie za {_days(days_left)}", f"Disk {mount} will be full in {_days(days_left)}"),
            text=tr(f"Teraz {int(current)}%, {speed}. Przy tym tempie miejsce skonczy sie za {_days(days_left)} — "
                    "wtedy przestana dzialac bazy danych, logi i wdrozenia. Moge od razu usunac to, co odtwarzalne "
                    "(stare obrazy Dockera, cache budowania, pobrane paczki), albo sprawdzic, co tak rosnie.",
                    f"Now {int(current)}%, {speed}. At this rate the space runs out in {_days(days_left)} — then "
                    "databases, logs and deployments stop working. I can remove what is reproducible right away (old "
                    "Docker images, build cache, downloaded packages) or check what keeps growing."),
            actions=[_cleanup_action(),
                     _ask(tr("Co tak rosnie?", "What is growing?"),
                          tr(f"Sprawdz, co zajmuje i co tak szybko rosnie na dysku {mount} (katalogi, logi, wolumeny "
                             "Dockera) i zaproponuj, co mozna bezpiecznie usunac albo ograniczyc.",
                             f"Check what takes space and what grows so fast on disk {mount} (directories, logs, Docker "
                             "volumes) and propose what can be safely removed or limited."))],
        ))
    return found


def parse_size(text: str) -> float:
    """'1.2GB', '512MB', '3.4kB', '0B' (docker) -> bajty."""
    text = (text or "").strip().split(" ", 1)[0]
    units = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    for unit in sorted(units, key=len, reverse=True):
        if text.upper().endswith(unit):
            try:
                return float(text[: -len(unit)]) * units[unit]
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def reclaimable(df_rows: list[dict[str, Any]], dangling: list[str], apt_bytes: float) -> float:
    """Ile zwolni sprzatanie: cache budowania (do odzyskania), obrazy bez tagu i pobrane paczki apt."""
    total = apt_bytes
    for row in df_rows:
        if str(row.get("Type", "")).lower().startswith("build cache"):
            total += parse_size(str(row.get("Reclaimable", "")))
    total += sum(parse_size(size) for size in dangling)
    return total


def docker_space(estimate: float, disk_total: float) -> list[Insight]:
    if estimate < max(SPACE_MIN_BYTES, 0.1 * disk_total if disk_total else 0):
        return []
    return [Insight(
        key="docker-space", kind="docker-space", severity="info", cooldown=7 * DAY,
        title=tr(f"Moge zwolnic ok. {_gb(estimate)} na dysku", f"I can free about {_gb(estimate)} of disk space"),
        text=tr("Obrazy Dockera bez tagu, cache budowania starszy niz doba i pobrane paczki apt — wszystko da sie "
                "odtworzyc, zaden dzialajacy kontener tego nie uzywa. Jeden przycisk i po sprawie.",
                "Untagged Docker images, build cache older than a day and downloaded apt packages — all reproducible, "
                "no running container uses them. One button and it is done."),
        actions=[_cleanup_action()],
    )]


def restarts(containers: list[Any], previous: dict[str, Any], now: float) -> tuple[list[Insight], dict[str, Any]]:
    """Kontenery, ktore restartuja sie co jakis czas. Zwraca (podpowiedzi, nowy stan licznikow)."""
    found = []
    state: dict[str, Any] = {}
    for c in containers:
        count = int(getattr(c, "restart_count", 0) or 0)
        old = previous.get(c.name) if isinstance(previous.get(c.name), dict) else None
        base_count, base_at = (int(old.get("count", count)), float(old.get("at", now))) if old else (count, now)
        if count < base_count:                       # kontener odtworzony — licznik od zera
            base_count, base_at = count, now
        if now - base_at > DAY:                      # okno doby: liczymy od nowa
            base_count, base_at = count, now
        delta = count - base_count
        state[c.name] = {"count": base_count, "at": base_at}
        if delta >= RESTARTS_MIN and c.state == "running":
            found.append(Insight(
                key=f"restarts:{c.name}", kind="restarts", severity="warning",
                title=tr(f"Kontener {c.name} zrestartowal sie {delta} razy w ciagu doby",
                         f"Container {c.name} restarted {delta} times within a day"),
                text=tr("Teraz dziala, ale co jakis czas pada i wstaje — zwykle to brak pamieci, blad aplikacji albo "
                        "nieudany healthcheck. Moge sprawdzic logi z chwil restartu.",
                        "It runs now, but it keeps falling over and coming back — usually out of memory, an app error "
                        "or a failing healthcheck. I can check the logs around the restarts."),
                actions=[_ask(tr("Sprawdz przyczyne", "Find the cause"),
                              tr(f"Kontener {c.name} restartowal sie {delta} razy w ciagu doby. Sprawdz logi z chwil "
                                 "restartu, limity pamieci (OOM) i healthcheck, i powiedz, co jest przyczyna.",
                                 f"Container {c.name} restarted {delta} times within a day. Check the logs around the "
                                 "restarts, memory limits (OOM) and the healthcheck, and tell me the cause."))],
            ))
            state[c.name] = {"count": count, "at": now}   # nastepna podpowiedz dopiero po kolejnych restartach
    return found, state


def reboot(required_since: float | None, now: float) -> list[Insight]:
    if required_since is None or now - required_since < REBOOT_AFTER_DAYS * DAY:
        return []
    days = int((now - required_since) // DAY)
    return [Insight(
        key="reboot", kind="reboot", severity="info", cooldown=7 * DAY,
        title=tr(f"Serwer od {days} dni czeka na restart", f"The server has been waiting {days} days for a reboot"),
        text=tr("Po aktualizacji jadra i bibliotek poprawki zaczna dzialac dopiero po restarcie serwera. Dobrze "
                "zrobic go w spokojnej chwili — moge podpowiedziec, jak i co sprawdzic potem.",
                "After the kernel and library updates the fixes only take effect after a reboot. Best done at a quiet "
                "moment — I can tell you how and what to check afterwards."),
        actions=[_ask(tr("Jak to zrobic?", "How do I do it?"),
                      tr("Serwer czeka na restart po aktualizacjach. Jak bezpiecznie go zrestartowac i co sprawdzic "
                         "po restarcie (uslugi, kontenery, strony)?",
                         "The server is waiting for a reboot after updates. How do I reboot it safely and what should "
                         "I check afterwards (services, containers, sites)?"))],
    )]


def memory_trend(points: list[dict[str, Any]], now: float) -> list[Insight]:
    series = [(float(p["t"]), float(p["mem"])) for p in points
              if isinstance(p.get("mem"), (int, float)) and now - float(p["t"]) <= DAY]
    if len(series) < FORECAST_MIN_POINTS or series[-1][0] - series[0][0] < FORECAST_MIN_SPAN:
        return []
    rise = slope(series) * (series[-1][0] - series[0][0])
    first, last = series[0][1], series[-1][1]
    if rise < MEMORY_MIN_RISE or last < MEMORY_MIN_NOW or last - first < MEMORY_MIN_RISE:
        return []
    return [Insight(
        key="memory-trend", kind="memory-trend", severity="info",
        title=tr(f"Pamiec RAM stale rosnie: z {int(first)}% do {int(last)}%",
                 f"Memory keeps growing: from {int(first)}% to {int(last)}%"),
        text=tr("Od doby zajetosc RAM rosnie i nie spada — tak wyglada wyciek pamieci w ktorejs aplikacji. Zanim "
                "zabraknie pamieci i system zacznie zabijac procesy, warto sprawdzic, ktory proces rosnie.",
                "Memory use has been growing for a day without dropping — this is what a memory leak in some app "
                "looks like. Before memory runs out and the system starts killing processes, it is worth checking "
                "which process grows."),
        actions=[_ask(tr("Ktory proces?", "Which process?"),
                      tr("Zajetosc RAM rosnie od doby. Sprawdz, ktore procesy i kontenery zuzywaja coraz wiecej "
                         "pamieci, i zaproponuj, co z tym zrobic.",
                         "Memory use has been growing for a day. Check which processes and containers use more and "
                         "more memory, and propose what to do about it."))],
    )]


# ─── Zbieranie danych ───────────────────────────────────────────────────────

def _dir_bytes(local: str, limit: int = 20000) -> float:
    total, seen = 0.0, 0
    for root, _, files in os.walk(local):
        for name in files:
            seen += 1
            if seen > limit:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


async def _docker_space_estimate() -> float | None:
    from backend.core import executor

    out, _, code = await executor.execute("docker system df --format '{{json .}}'", timeout=60)
    if code != 0:
        return None
    rows = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    dangling_out, _, code = await executor.execute("docker images -f dangling=true --format '{{.Size}}'", timeout=30)
    dangling = [s for s in dangling_out.splitlines() if s.strip()] if code == 0 else []
    apt = _dir_bytes(runtime.to_local("/var/cache/apt/archives"))
    return reclaimable(rows, dangling, apt)


def reboot_required() -> bool:
    return any(os.path.isfile(runtime.to_local(p)) for p in ("/run/reboot-required", "/var/run/reboot-required"))


async def collect(state: dict[str, Any], *, containers: list[Any] | None, points: list[dict[str, Any]],
                  now: float | None = None) -> list[Insight]:
    """
    Wszystkie aktualne podpowiedzi. `state` (watch_state.json, klucz "insights") — liczniki restartow i od kiedy
    serwer czeka na restart; funkcja go aktualizuje.
    """
    now = now if now is not None else time.time()
    disks = hostinfo.disks()
    found = disk_forecast(points, disks, now)
    if containers is not None:
        root = next((d for d in disks if d.mount == "/"), None)
        estimate = await _docker_space_estimate()
        if estimate is not None:
            found += docker_space(estimate, float(root.total) if root else 0.0)
        more, state["restarts"] = restarts(containers, state.get("restarts") or {}, now)
        found += more
    if reboot_required():
        state.setdefault("reboot_since", now)
    else:
        state.pop("reboot_since", None)
    found += reboot(state.get("reboot_since"), now)
    found += memory_trend(points, now)
    return found


def due(insight: Insight, sent: dict[str, Any], now: float) -> bool:
    """Czy wyslac: nowa, po okresie ciszy albo powazniejsza niz ostatnio."""
    last = sent.get(insight.key)
    if not isinstance(last, dict):
        return True
    if insight.severity == "warning" and last.get("severity") != "warning":
        return True
    return now - float(last.get("at", 0)) >= insight.cooldown
