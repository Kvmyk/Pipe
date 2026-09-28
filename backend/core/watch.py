"""
Czuwanie — Pipe odzywa sie sam, zanim uzytkownik zauwazy problem.

Co WATCH_INTERVAL sekund deterministyczne sprawdzenia (bez LLM, bez kosztow):
  - dyski hosta powyzej WATCH_DISK_PCT (krytycznie >= 97%)
  - RAM powyzej WATCH_MEM_PCT
  - load average (5 min) powyzej liczby rdzeni x WATCH_LOAD_FACTOR
  - kontenery: restartujace sie, unhealthy, a te, ktore dzialaly, a przestaly
  - NOWY publiczny port nasluchujacy na hoscie (sygnal bezpieczenstwa)

Alerty trwale (dysk, RAM) sa zglaszane raz i "rozwiazywane", gdy problem minie.
Zdarzenia jednorazowe (nowy port, zatrzymany kontener) sa zglaszane raz.
Co minute wykonywane sa tez rutyny (core/routines.py) — przez workerow.

Zdarzenia trafiaja do subskrybentow (Notifier) — bot Telegram laczy sie
komenda {"command": "subscribe"} i przesyla je dozwolonym uzytkownikom
z przyciskiem "Zbadaj". Aktywne alerty sa tez w system prompcie agenta.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from backend.config import settings
from backend.config.prompts import ROUTINE_REPORT_INSTRUCTION
from backend.core import hostinfo, memory, routines, targets

MAX_HISTORY = 50


@dataclass
class Finding:
    key: str
    severity: str          # warning | critical
    title: str
    detail: str
    transient: bool = False


@dataclass
class Alert:
    id: str
    key: str
    severity: str
    title: str
    detail: str
    since: str
    state: str = "new"      # new | resolved | event

    def to_event(self) -> dict[str, Any]:
        return {"type": "alert", **asdict(self)}


# ─── Powiadomienia ──────────────────────────────────────────────────────────

class Notifier:
    """Prosty pub/sub zdarzen dla podlaczonych klientow."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self.history: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        event = {**event, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        self.history = (self.history + [event])[-MAX_HISTORY:]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # wolny klient traci najstarsze zdarzenia, serwer nie czeka


# ─── Sprawdzenia ────────────────────────────────────────────────────────────

def _state_path():
    return memory.data_dir() / "watch_state.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    memory._write_atomic(_state_path(), json.dumps(state, indent=2) + "\n")


def resource_findings(proc: str | None = None) -> list[Finding]:
    findings = []
    for disk in hostinfo.disks(proc):
        pct = disk.used_pct
        if pct >= settings.WATCH_DISK_PCT:
            severity = "critical" if pct >= max(97, settings.WATCH_DISK_PCT) else "warning"
            findings.append(Finding(
                f"disk:{disk.mount}", severity, f"Dysk {disk.mount} zapelniony w {pct}%",
                f"wolne {hostinfo.human_bytes(disk.available)} z {hostinfo.human_bytes(disk.total)} ({disk.device})"))
    mem = hostinfo.memory(proc)
    if mem and mem.used_pct >= settings.WATCH_MEM_PCT:
        findings.append(Finding("memory", "warning", f"RAM zajety w {mem.used_pct}%",
                                f"dostepne {mem.available_mb} MB z {mem.total_mb} MB"
                                + (f", swap {mem.swap_used_mb}/{mem.swap_total_mb} MB" if mem.swap_total_mb else "")))
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load and load[1] > cpus * settings.WATCH_LOAD_FACTOR:
        findings.append(Finding("load", "warning", f"Wysokie obciazenie: load 5m {load[1]:.1f} przy {cpus} rdzeniach",
                                f"load 1/5/15 min: {load[0]:.1f} / {load[1]:.1f} / {load[2]:.1f}"))
    return findings


def container_findings(containers: list[Any], previous_running: set[str] | None) -> list[Finding]:
    findings = []
    by_name = {c.name: c for c in containers}
    for c in containers:
        if c.state == "restarting":
            findings.append(Finding(f"container:{c.name}:restarting", "critical",
                                    f"Kontener {c.name} restartuje sie w petli",
                                    f"obraz {c.image}, restartow: {c.restart_count}"))
        elif c.health == "unhealthy":
            findings.append(Finding(f"container:{c.name}:unhealthy", "warning",
                                    f"Kontener {c.name} jest unhealthy", f"obraz {c.image}"))
    if previous_running is not None:
        for name in sorted(previous_running):
            c = by_name.get(name)
            if c is not None and c.state in ("exited", "dead"):
                findings.append(Finding(f"container:{name}:stopped", "critical", f"Kontener {name} przestal dzialac",
                                        f"stan {c.state}, obraz {c.image}", transient=True))
    return findings


def port_findings(public_now: dict[str, str], previous: set[str] | None) -> list[Finding]:
    if previous is None:
        return []
    return [Finding(f"port:{key}", "warning", f"Nowy publiczny port: {key}",
                    f"na hoscie pojawila sie usluga nasluchujaca na {address} — sprawdz, czy to zamierzone",
                    transient=True)
            for key, address in sorted(public_now.items()) if key not in previous]


# ─── Czuwanie ───────────────────────────────────────────────────────────────

class Watcher:
    def __init__(self, agent: Any = None) -> None:
        self.agent = agent
        self.notifier = Notifier()
        self.active: dict[str, Alert] = {}
        self._tasks: list[asyncio.Task] = []
        self._routines_running: set[str] = set()

    # --- alerty -------------------------------------------------------------

    def _alert(self, finding: Finding, state: str) -> Alert:
        since = datetime.now().strftime("%Y-%m-%d %H:%M")
        alert_id = hashlib.sha1(f"{finding.key}|{since}".encode()).hexdigest()[:10]
        return Alert(alert_id, finding.key, finding.severity, finding.title, finding.detail, since, state)

    def apply(self, findings: list[Finding]) -> list[Alert]:
        """Porownuje z aktywnymi alertami; zwraca zdarzenia do rozeslania."""
        events: list[Alert] = []
        current = {f.key: f for f in findings if not f.transient}
        for finding in findings:
            if finding.transient:
                events.append(self._alert(finding, "event"))
        for key, finding in current.items():
            known = self.active.get(key)
            if known is None or (known.severity == "warning" and finding.severity == "critical"):
                alert = self._alert(finding, "new")
                self.active[key] = alert
                events.append(alert)
            else:
                known.title, known.detail = finding.title, finding.detail
        for key in list(self.active):
            if key not in current:
                resolved = self.active.pop(key)
                resolved.state = "resolved"
                events.append(resolved)
        return events

    async def check_once(self) -> list[Alert]:
        from backend.core import infra

        state = load_state()
        # statvfs na zawieszonym NFS potrafi blokowac w nieskonczonosc — poza petla zdarzen
        findings = await asyncio.to_thread(resource_findings)

        containers = await infra.docker_containers()
        if containers is not None:
            previous = set(state["running"]) if "running" in state else None
            findings += container_findings(containers, previous)
            state["running"] = sorted(c.name for c in containers if c.state == "running")

        sockets = await asyncio.to_thread(hostinfo.sockets)
        public = {f"{s.port}/{s.proto.rstrip('6')}": f"{s.ip}:{s.port}"
                  for s in sockets if s.public and s.proto.startswith("tcp")}
        # Punkt odniesienia zapisujemy przy pierwszym udanym odczycie gniazd, takze bez portow
        # publicznych — inaczej pierwszy port, ktory sie pojawi, zostalby po cichu uznany za "stary".
        # Pusty odczyt (brak /proc/1/net) niczego nie zmienia — nie kasuje punktu odniesienia.
        if sockets:
            previous_ports = set(state["public_ports"]) if "public_ports" in state else None
            findings += port_findings(public, previous_ports)
            state["public_ports"] = sorted(public)

        try:
            save_state(state)
        except OSError:
            pass
        events = self.apply(findings)
        for alert in events:
            self.notifier.publish(alert.to_event())
        return events

    def find_alert(self, alert_id: str) -> dict[str, Any] | None:
        for alert in self.active.values():
            if alert.id == alert_id:
                return alert.to_event()
        return next((e for e in reversed(self.notifier.history)
                     if e.get("type") == "alert" and e.get("id") == alert_id), None)

    # --- rutyny -------------------------------------------------------------

    async def run_routine(self, routine: routines.Routine, *, forced: bool = False) -> dict[str, Any]:
        from backend.core import workers
        from backend.core.session import Session

        if routine.name in self._routines_running:
            return {"type": "routine", "name": routine.name, "status": "PROBLEM",
                    "report": "Poprzednie uruchomienie jeszcze trwa."}
        target = targets.get_target(routine.target)
        started = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._routines_running.add(routine.name)
        try:
            if target is None:
                report, status = f"Cel {routine.target!r} nie istnieje.", "PROBLEM"
            else:
                parent = Session(session_id=f"routine:{routine.name}", interface=f"routine:{routine.name}",
                                 learns_vibe=False)
                result = await asyncio.wait_for(
                    workers.run_worker(self.agent, parent, routine.name, target, routine.task,
                                       extra_instructions=ROUTINE_REPORT_INSTRUCTION),
                    timeout=settings.WORKER_TIMEOUT)
                report = result.render()
                status = "PROBLEM" if result.error else routines.report_status(result.report)
        except asyncio.TimeoutError:
            report, status = f"Przekroczono limit czasu {settings.WORKER_TIMEOUT} s.", "PROBLEM"
        except Exception as exc:
            report, status = f"Blad: {exc}", "PROBLEM"
        finally:
            self._routines_running.discard(routine.name)
        routines.update_routine(routine.name, last_run=started, last_status=status)
        event = {"type": "routine", "name": routine.name, "status": status, "report": report,
                 "forced": forced}
        if forced or routine.notify == "always" or status != "OK":
            self.notifier.publish(event)
        return event

    # --- petle --------------------------------------------------------------

    async def _watch_loop(self) -> None:
        await asyncio.sleep(10)  # niech serwer wstanie
        while True:
            try:
                await self.check_once()
            except Exception as exc:
                print(f"[Czuwanie] Blad sprawdzenia: {exc}", flush=True)
            await asyncio.sleep(max(30, settings.WATCH_INTERVAL))

    async def _routine_loop(self) -> None:
        last_minute = None
        while True:
            now = datetime.now().replace(second=0, microsecond=0)
            if now != last_minute:
                last_minute = now
                for routine in routines.due(routines.load_routines(), now):
                    asyncio.create_task(self.run_routine(routine))
            await asyncio.sleep(15)

    def start(self) -> None:
        if self._tasks:
            return
        if settings.WATCH_ENABLED:
            self._tasks.append(asyncio.create_task(self._watch_loop()))
        if self.agent is not None:
            self._tasks.append(asyncio.create_task(self._routine_loop()))


_watcher: Watcher | None = None


def get_watcher(agent: Any = None) -> Watcher:
    global _watcher
    if _watcher is None:
        _watcher = Watcher(agent)
    elif agent is not None and _watcher.agent is None:
        _watcher.agent = agent
    return _watcher


def prompt_alerts() -> str:
    """Aktywne alerty dla system promptu (pusty napis, gdy ich nie ma)."""
    if _watcher is None or not _watcher.active:
        return ""
    lines = [f"- [{a.severity}] {a.title} ({a.detail}) od {a.since}" for a in _watcher.active.values()]
    return ("\n\n--- CZUWANIE: AKTYWNE ALERTY ---\n"
            "Problemy wykryte automatycznie na hoscie. Jesli rozmowa dotyczy zdrowia serwera, uwzglednij je.\n"
            + "\n".join(lines))
