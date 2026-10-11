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

Przy kazdym sprawdzeniu zapisywana jest probka do historii (core/metrics.py,
wykresy). Rzadziej, we wlasnych petlach:
  - migawka stanu hosta co SNAPSHOT_INTERVAL (core/snapshots.py, "co sie zmienilo"),
  - podatnosci w pakietach (core/vulns.py, OSV.dev) przy zmianie pakietow i co VULN_INTERVAL,
  - sprawdzenia bez konfiguracji co CHECKS_INTERVAL (core/checks.py): certyfikaty,
    strony i DNS domen z konfiguracji proxy, swiezosc backupow z DIRECTORY,
  - poranny raport o DIGEST_TIME (core/digest.py).
Kazda grupa sprawdzen rozwiazuje tylko wlasne alerty (zakres = prefiks klucza).

Zdarzenia trafiaja do subskrybentow (Notifier) — bot Telegram laczy sie
komenda {"command": "subscribe"} i przesyla je dozwolonym uzytkownikom
z przyciskiem "Zbadaj". Aktywne alerty sa tez w system prompcie agenta.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from backend.config import settings
from backend.core import hostinfo, memory, metrics, routines, targets
from backend.core.i18n import prompt, tr

MAX_HISTORY = 50
# Zakresy alertow (prefiks klucza przed ':') — kazda petla rozwiazuje tylko swoje.
RESOURCE_SCOPE = frozenset({"disk", "memory", "load", "container", "port", "auth"})
CHECKS_SCOPE = frozenset({"cert", "site", "dns", "backup"})
VULN_SCOPE = frozenset({"vuln"})
VULN_KEY = "vuln:system"
VULN_IMAGES_KEY = "vuln:images"
VULN_APPS_KEY = "vuln:apps"
# Tyle najwyzej czeka /audyt na skan podatnosci (pierwszy skan pobiera szczegoly setek ID).
VULN_AUDIT_WAIT = 45
# Wyjasnienia podatnosci: tyle podatnosci na jedno zapytanie do modelu i tyle czeka na nie /podatnosci.
EXPLAIN_BATCH = 30
EXPLAIN_WAIT = 40
# Strona musi nie odpowiadac dwa sprawdzenia z rzedu, zanim przyjdzie alert (chwilowe 502 przy deployu).
SITE_FAILURES_BEFORE_ALERT = 2


@dataclass
class Finding:
    key: str
    severity: str          # warning | critical
    title: str
    detail: str
    transient: bool = False

    @property
    def scope(self) -> str:
        return self.key.split(":", 1)[0]


@dataclass
class Alert:
    id: str
    key: str
    severity: str
    title: str
    detail: str
    since: str
    state: str = "new"      # new | resolved | event
    history: str = ""       # ostatnie wystapienie tego problemu (pamiec incydentow)

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

    def publish(self, event: dict[str, Any], *, transient: bool = False) -> None:
        """`transient` — zdarzenie techniczne (np. zmiana jezyka): trafia do klientow, ale nie do historii."""
        event = {**event, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if not transient:
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
            free, total = hostinfo.human_bytes(disk.available), hostinfo.human_bytes(disk.total)
            findings.append(Finding(
                f"disk:{disk.mount}", severity, tr(f"Dysk {disk.mount} zapelniony w {pct}%", f"Disk {disk.mount} is {pct}% full"),
                tr(f"wolne {free} z {total} ({disk.device})", f"{free} free of {total} ({disk.device})")))
    mem = hostinfo.memory(proc)
    if mem and mem.used_pct >= settings.WATCH_MEM_PCT:
        findings.append(Finding("memory", "warning", tr(f"RAM zajety w {mem.used_pct}%", f"RAM is {mem.used_pct}% used"),
                                tr(f"dostepne {mem.available_mb} MB z {mem.total_mb} MB",
                                   f"{mem.available_mb} MB available of {mem.total_mb} MB")
                                + (f", swap {mem.swap_used_mb}/{mem.swap_total_mb} MB" if mem.swap_total_mb else "")))
    load = hostinfo.loadavg(proc)
    cpus = hostinfo.cpu_count(proc)
    if load and load[1] > cpus * settings.WATCH_LOAD_FACTOR:
        findings.append(Finding("load", "warning", tr(f"Wysokie obciazenie: load 5m {load[1]:.1f} przy {cpus} rdzeniach",
                                                      f"High load: 5m load {load[1]:.1f} on {cpus} cores"),
                                f"load 1/5/15 min: {load[0]:.1f} / {load[1]:.1f} / {load[2]:.1f}"))
    return findings


def container_findings(containers: list[Any], previous_running: set[str] | None) -> list[Finding]:
    findings = []
    by_name = {c.name: c for c in containers}
    for c in containers:
        if c.state == "restarting":
            findings.append(Finding(f"container:{c.name}:restarting", "critical",
                                    tr(f"Kontener {c.name} restartuje sie w petli", f"Container {c.name} is in a restart loop"),
                                    tr(f"obraz {c.image}, restartow: {c.restart_count}",
                                       f"image {c.image}, restarts: {c.restart_count}")))
        elif c.health == "unhealthy":
            findings.append(Finding(f"container:{c.name}:unhealthy", "warning",
                                    tr(f"Kontener {c.name} jest unhealthy", f"Container {c.name} is unhealthy"),
                                    tr(f"obraz {c.image}", f"image {c.image}")))
    if previous_running is not None:
        for name in sorted(previous_running):
            c = by_name.get(name)
            if c is not None and c.state in ("exited", "dead"):
                findings.append(Finding(f"container:{name}:stopped", "critical",
                                        tr(f"Kontener {name} przestal dzialac", f"Container {name} stopped"),
                                        tr(f"stan {c.state}, obraz {c.image}", f"state {c.state}, image {c.image}"),
                                        transient=True))
    return findings


def port_findings(public_now: dict[str, str], previous: set[str] | None) -> list[Finding]:
    if previous is None:
        return []
    return [Finding(f"port:{key}", "warning", tr(f"Nowy publiczny port: {key}", f"New public port: {key}"),
                    tr(f"na hoscie pojawila sie usluga nasluchujaca na {address} — sprawdz, czy to zamierzone",
                       f"a service listening on {address} appeared on the host — check whether it is intended"),
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
        self.checks_report: Any = None          # ostatni checks.Report
        self.vuln_scan: Any = None              # ostatni vulns.Scan
        self._vuln_task: asyncio.Task | None = None
        self._explain_lock = asyncio.Lock()
        self._site_failures: dict[str, int] = {}
        self._last_digest: str = ""
        self._reminder_wakeup = asyncio.Event()
        from backend.core.posture import AuthWatch
        self._auth = AuthWatch()

    # --- alerty -------------------------------------------------------------

    def _alert(self, finding: Finding, state: str) -> Alert:
        since = datetime.now().strftime("%Y-%m-%d %H:%M")
        alert_id = hashlib.sha1(f"{finding.key}|{since}".encode()).hexdigest()[:10]
        return Alert(alert_id, finding.key, finding.severity, finding.title, finding.detail, since, state)

    def apply(self, findings: list[Finding], scope: frozenset[str] | None = None) -> list[Alert]:
        """
        Porownuje z aktywnymi alertami; zwraca zdarzenia do rozeslania.
        `scope` — prefiksy kluczy, ktore ta runda sprawdzila; aktywny alert spoza
        zakresu nie jest rozwiazywany (None = wszystkie).
        """
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
            if key not in current and (scope is None or key.split(":", 1)[0] in scope):
                resolved = self.active.pop(key)
                resolved.state = "resolved"
                events.append(resolved)
        return events

    async def check_once(self) -> list[Alert]:
        from backend.core import infra

        state = load_state()
        # statvfs na zawieszonym NFS potrafi blokowac w nieskonczonosc — poza petla zdarzen
        findings = await asyncio.to_thread(resource_findings)
        try:
            point = await asyncio.to_thread(metrics.sample)
            await asyncio.to_thread(metrics.record, point, settings.METRICS_KEEP_DAYS)
        except OSError as exc:
            print(tr(f"[Czuwanie] Nie zapisano probki pomiarow: {exc}", f"[Watcher] Metric sample not saved: {exc}"), flush=True)

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

        findings += await self._security_findings(state)

        try:
            save_state(state)
        except OSError:
            pass
        return self._publish(self.apply(findings, RESOURCE_SCOPE))

    async def _security_findings(self, state: dict[str, Any]) -> list[Finding]:
        """Straznik (konta, klucze SSH, SUID, sudoers/sshd) i przyrost logu SSH."""
        from backend.core import journal, posture

        findings: list[Finding] = []
        try:
            current = await asyncio.to_thread(posture.sentinel_state)
            recent = [e for e in journal.entries(20) if time.time() - e.at < max(600, 3 * settings.WATCH_INTERVAL)]
            pipe_changed = {f.label for e in recent for f in e.files}
            findings += posture.sentinel_findings(state.get("sentinel"), current, pipe_changed)
            state["sentinel"] = current
        except Exception as exc:
            print(tr(f"[Czuwanie] Straznik bezpieczenstwa: {exc}", f"[Watcher] Security sentinel: {exc}"), flush=True)
        try:
            events = await asyncio.to_thread(posture.read_auth_increment, state)
            if events is not None:
                findings += posture.auth_findings(events, self._auth, state, threshold=settings.WATCH_SSH_FAILURES,
                                                  notify_logins=settings.WATCH_SSH_LOGINS)
        except Exception as exc:
            print(tr(f"[Czuwanie] Log SSH: {exc}", f"[Watcher] SSH log: {exc}"), flush=True)
        return findings

    def _publish(self, events: list[Alert]) -> list[Alert]:
        for alert in events:
            if alert.state in ("new", "resolved"):
                self._track_incident(alert)
            self.notifier.publish(alert.to_event())
        return events

    @staticmethod
    def _track_incident(alert: Alert) -> None:
        """Pamiec incydentow: otwarcie przy nowym alercie (z historia), zamkniecie przy rozwiazaniu."""
        from backend.core import incidents, journal

        try:
            if alert.state == "new":
                past = incidents.history(alert.key, limit=1)
                if past:
                    alert.history = past[0].short()
                incidents.opened(alert)
            else:
                incidents.closed(alert.key, journal.entries(50))
        except (OSError, ValueError) as exc:
            print(tr(f"[Czuwanie] Pamiec incydentow: {exc}", f"[Watcher] Incident memory: {exc}"), flush=True)

    # --- alerty z zewnatrz (webhooki) --------------------------------------

    def external(self, alerts: list[Any]) -> list[Alert]:
        """Alerty z webhookow: firing -> aktywny alert (bez rozwiazywania innych), resolved -> zamkniecie."""
        firing = [Finding(a.key, a.severity, a.title, a.detail) for a in alerts if a.firing]
        events = self.apply(firing, frozenset())
        for alert in alerts:
            if not alert.firing and alert.key in self.active:
                resolved = self.active.pop(alert.key)
                resolved.state = "resolved"
                events.append(resolved)
        return self._publish(events)

    async def investigate_external(self, alert: Alert) -> None:
        """Worker (tylko odczyty) bada alert z zewnatrz; raport idzie do subskrybentow i pamieci incydentow."""
        from backend.core import incidents, workers
        from backend.core.session import Session

        if self.agent is None or not getattr(self.agent, "llm_enabled", lambda: True)():
            return                                   # bez modelu alert i tak dociera; badania nie ma komu zrobic
        target = targets.get_target("local")
        task = prompt("EXTERNAL_ALERT_TASK").format(title=alert.title, detail=alert.detail or "-")
        task += incidents.context_for(alert.key)
        parent = Session(session_id=f"hook:{alert.key}", interface="webhook", learns_vibe=False)
        try:
            result = await asyncio.wait_for(
                workers.run_worker(self.agent, parent, "zbadaj-alert", target, task), timeout=settings.WORKER_TIMEOUT)
            report = result.render()
        except asyncio.TimeoutError:
            report = tr(f"Badanie przekroczylo {settings.WORKER_TIMEOUT} s.",
                        f"The investigation exceeded {settings.WORKER_TIMEOUT} s.")
        except Exception as exc:
            report = tr(f"Badanie nie powiodlo sie: {exc}", f"The investigation failed: {exc}")
        incidents.note_investigation(alert.key, report)
        self.notifier.publish({"type": "investigation", "key": alert.key, "title": alert.title, "report": report})

    # --- migawki, sprawdzenia bez konfiguracji, raport ---------------------

    async def snapshot_once(self) -> bool:
        from backend.core import snapshots

        sections = await snapshots.capture()
        return await asyncio.to_thread(snapshots.save, sections, None, settings.SNAPSHOT_KEEP_DAYS)

    async def checks_once(self) -> list[Alert]:
        from backend.core import checks

        report = await checks.run_checks(sites=settings.WATCH_SITES,
                                         ignore=checks.parse_ignore(settings.WATCH_IGNORE))
        self.checks_report = report
        findings = report.findings(cert_days=settings.WATCH_CERT_DAYS, backup_hours=settings.WATCH_BACKUP_HOURS)
        failing = {f.key for f in findings if f.scope == "site"}
        self._site_failures = {key: self._site_failures.get(key, 0) + 1 for key in failing}
        findings = [f for f in findings
                    if f.scope != "site" or self._site_failures.get(f.key, 0) >= SITE_FAILURES_BEFORE_ALERT
                    or f.key in self.active]
        return self._publish(self.apply(findings, CHECKS_SCOPE))

    async def vuln_scan_now(self, *, force: bool = False) -> Any:
        """Skan podatnosci (core/vulns.py). Jeden naraz — audyt i czuwanie czekaja na to samo zadanie."""
        from backend.core import infra, vulns

        if self._vuln_task is None or self._vuln_task.done():
            async def run() -> Any:
                containers = await infra.docker_containers()
                return await asyncio.to_thread(vulns.scan, force=force, max_age=settings.VULN_INTERVAL,
                                               containers=containers)
            self._vuln_task = asyncio.create_task(run())
        result, _ = await asyncio.shield(self._vuln_task)
        self.vuln_scan = result
        return result

    async def vuln_for_audit(self) -> tuple[Any, str]:
        """(wynik skanu albo None, uwaga dla audytu) — nie dluzej niz VULN_AUDIT_WAIT sekund."""
        from backend.core import vulns

        if settings.VULN_INTERVAL <= 0:
            return None, tr("wylaczone: VULN_INTERVAL=0", "off: VULN_INTERVAL=0")
        # Swiezy wynik czuwania wystarczy — zapytanie do OSV trwa okolo minuty (jadro: tysiace ID).
        last = self.vuln_scan or await asyncio.to_thread(vulns.last_scan)
        if last is not None and last.ok and time.time() - last.at < 2 * settings.VULN_INTERVAL:
            return last, ""
        try:
            return await asyncio.wait_for(self.vuln_scan_now(), VULN_AUDIT_WAIT), ""
        except asyncio.TimeoutError:
            return last, tr("skan jeszcze trwa, sprawdz za minute", "the scan is still running, check again in a minute")

    def _vuln_alert(self, key: str, ids: set[str], finding: Finding | None) -> list[Finding]:
        """
        Alert przychodzi tylko o NOWYCH ID: znane (DATA_DIR/vulns.json) nie wracaja po restarcie,
        a nowe przy aktywnym alercie wysylaja go jeszcze raz.
        """
        from backend.core import vulns

        known = vulns.notified_ids(key)
        if known != ids:
            vulns.mark_notified(key, ids)
        if finding is None:
            return []
        new_ids = ids - known
        if key in self.active and new_ids:
            self.active.pop(key)                                  # nowa podatnosc -> alert jeszcze raz
        elif key not in self.active and not new_ids:
            self.active[key] = self._alert(finding, "new")        # juz zgloszone przed restartem — po cichu
        return [finding]

    def vuln_findings(self, result: Any) -> list[Finding] | None:
        """
        Alerty o pilnych podatnosciach z poprawka: `vuln:system` (pakiety hosta: KEV, krytyczne, wysokie),
        `vuln:images` (obrazy kontenerow: to samo), `vuln:apps` (zaleznosci aplikacji: tylko KEV i krytyczne —
        drzewa npm maja mnostwo "wysokich" w narzedziach deweloperskich). None = skan sie nie udal.
        """
        from backend.core import vulns

        if result is None or not result.ok:
            return None
        findings: list[Finding] = []

        urgent = result.urgent
        finding = None
        if urgent:
            kev = [p for p in urgent if p.kev]
            title = tr(f"Pilne podatnosci z poprawka w {len(urgent)} pakietach", f"Urgent fixable vulnerabilities in {len(urgent)} packages")
            if kev:
                title += tr(f" ({len(kev)} aktywnie wykorzystywanych)", f" ({len(kev)} actively exploited)")
            lines = [vulns.describe_item(p) for p in urgent[:5]]
            if len(urgent) > 5:
                lines.append(tr(f"i {len(urgent) - 5} innych pakietow", f"and {len(urgent) - 5} more packages"))
            command = vulns.upgrade_command(result.ecosystem, urgent)
            if command:
                lines.append(tr(f"Komenda: {command}", f"Command: {command}"))
            lines.append(tr("Opisy: /podatnosci, latka: /zalataj", "Descriptions: /vulns, patch: /patch"))
            finding = Finding(VULN_KEY, "critical" if kev else "warning", title, "\n".join(lines))
        findings += self._vuln_alert(VULN_KEY, result.urgent_ids, finding)

        images = [t for t in result.of_kind("image") if t.urgent]
        finding = None
        if images:
            title = tr(f"Pilne podatnosci w {len(images)} obrazach kontenerow", f"Urgent vulnerabilities in {len(images)} container images")
            lines = [vulns.describe_target(t) for t in images[:5]]
            lines.append(tr("Opisy: /podatnosci, nowsze obrazy: /zalataj obrazy", "Descriptions: /vulns, newer images: /patch images"))
            finding = Finding(VULN_IMAGES_KEY, "critical" if any(t.kev for t in images) else "warning", title,
                              "\n".join(lines))
        findings += self._vuln_alert(VULN_IMAGES_KEY, {f"{t.name}|{v.id}" for t in images for p in t.urgent
                                                      for v in p.fixable if v.urgent}, finding)

        def serious(item: Any) -> bool:
            return any(v.kev or v.severity == "critical" for v in item.fixable)
        apps = [(t, [p for p in t.items if serious(p)]) for t in result.of_kind("app")]
        apps = [(t, items) for t, items in apps if items]
        finding = None
        if apps:
            title = tr(f"Krytyczne podatnosci w zaleznosciach {len(apps)} aplikacji",
                       f"Critical vulnerabilities in the dependencies of {len(apps)} applications")
            lines = [f"{t.label()}: " + "; ".join(vulns.describe_item(p, 1) for p in items[:3]) for t, items in apps[:5]]
            lines.append(tr("Podbij wersje w lockfile; opisy: /podatnosci", "Bump the versions in the lockfile; descriptions: /vulns"))
            finding = Finding(VULN_APPS_KEY, "critical" if any(p.kev for _, items in apps for p in items) else "warning",
                              title, "\n".join(lines))
        findings += self._vuln_alert(VULN_APPS_KEY, {f"{t.name}|{v.id}" for t, items in apps for p in items
                                                    for v in p.fixable if v.kev or v.severity == "critical"}, finding)
        return findings

    async def vulns_once(self) -> list[Alert]:
        result = await self.vuln_scan_now()
        findings = self.vuln_findings(result)
        if findings is None:
            return []
        events = self._publish(self.apply(findings, VULN_SCOPE))
        if result is not None and result.ok:
            asyncio.create_task(self._safe(self.explain_vulns(result), "wyjasnienia podatnosci"))
        return events

    async def explain_vulns(self, result: Any, extra: list[Any] | None = None, timeout: float | None = None) -> bool:
        """
        Krotkie wyjasnienia pokazywanych podatnosci w jezyku Pipe, pisane przez model (WORKER_MODEL) na podstawie
        opisu z OSV; zapisane w DATA_DIR/vulns.json, wiec kazda podatnosc kosztuje raz. Bez modelu (albo gdy nie
        zdazy w `timeout`) raporty pokazuja oryginalny opis z OSV. Zwraca, czy wszystko jest wyjasnione.
        """
        from backend.core import vulns
        from backend.core.i18n import lang

        if result is None or self.agent is None or not self.agent.llm_enabled():
            return False
        language = lang()
        pending = await asyncio.to_thread(vulns.needing_explanation, result, language, extra)
        if not pending:
            return True

        async def batch(chunk: list[dict[str, str]]) -> None:
            answer = await self.agent.complete(prompt("VULN_EXPLAIN_PROMPT"), json.dumps(chunk, ensure_ascii=False),
                                               model=settings.WORKER_MODEL or None, who="podatnosci")
            found = vulns.parse_explanations(answer, {item["id"] for item in chunk})
            if found:
                await asyncio.to_thread(vulns.save_explanations, language, found)

        async def run() -> None:
            async with self._explain_lock:
                await asyncio.gather(*(batch(pending[i:i + EXPLAIN_BATCH]) for i in range(0, len(pending), EXPLAIN_BATCH)),
                                     return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.shield(run()), timeout) if timeout else await run()
        except asyncio.TimeoutError:
            return False
        return not await asyncio.to_thread(vulns.needing_explanation, result, language, extra)

    async def build_digest(self, *, fresh_checks: bool = False) -> Any:
        """Poranny raport (core/digest.py) z wykresem obciazenia z ostatniej doby."""
        from backend.core import digest, infra, snapshots, usage
        from backend.core.handlers.history import chart_attachment

        report = self.checks_report
        if fresh_checks or report is None or time.time() - report.at > 2 * settings.CHECKS_INTERVAL:
            try:
                from backend.core import checks
                report = await checks.run_checks(sites=settings.WATCH_SITES,
                                                 ignore=checks.parse_ignore(settings.WATCH_IGNORE))
            except Exception as exc:
                print(tr(f"[Czuwanie] Sprawdzenia do raportu nie powiodly sie: {exc}",
                         f"[Watcher] Checks for the report failed: {exc}"), flush=True)
        try:
            current = await snapshots.capture()
        except Exception:
            current = None
        hostname, _ = infra.host_identity()
        priced = bool(settings.LLM_PRICE_IN or settings.LLM_PRICE_OUT)
        result = await asyncio.to_thread(
            digest.build, hostname=hostname, active=list(self.active.values()), history=self.notifier.history,
            current=current, report=report, usage_line=usage.yesterday_line(priced),
            cert_days=settings.WATCH_CERT_DAYS, backup_hours=settings.WATCH_BACKUP_HOURS,
            load_factor=settings.WATCH_LOAD_FACTOR, vuln_scan=self.vuln_scan)
        attachment, _ = await chart_attachment("load", 24)
        result.attachment = attachment
        return result

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
                    "report": tr("Poprzednie uruchomienie jeszcze trwa.", "The previous run is still in progress.")}
        target = targets.get_target(routine.target)
        started = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._routines_running.add(routine.name)
        try:
            if target is None:
                report, status = tr(f"Cel {routine.target!r} nie istnieje.", f"Target {routine.target!r} does not exist."), "PROBLEM"
            else:
                parent = Session(session_id=f"routine:{routine.name}", interface=f"routine:{routine.name}",
                                 learns_vibe=False)
                result = await asyncio.wait_for(
                    workers.run_worker(self.agent, parent, routine.name, target, routine.task,
                                       extra_instructions=prompt("ROUTINE_REPORT_INSTRUCTION")),
                    timeout=settings.WORKER_TIMEOUT)
                report = result.render()
                status = "PROBLEM" if result.error else routines.report_status(result.report)
        except asyncio.TimeoutError:
            report, status = tr(f"Przekroczono limit czasu {settings.WORKER_TIMEOUT} s.",
                                f"Time limit of {settings.WORKER_TIMEOUT} s exceeded."), "PROBLEM"
        except Exception as exc:
            report, status = tr(f"Blad: {exc}", f"Error: {exc}"), "PROBLEM"
        finally:
            self._routines_running.discard(routine.name)
        routines.update_routine(routine.name, last_run=started, last_status=status)
        event = {"type": "routine", "name": routine.name, "status": status, "report": report,
                 "forced": forced}
        if forced or routine.notify == "always" or status != "OK":
            self.notifier.publish(event)
        return event

    # --- przypomnienia ------------------------------------------------------

    def reminders_changed(self) -> None:
        """Rejestr przypomnien sie zmienil (dodanie/anulowanie) — petla przelicza najblizszy termin."""
        self._reminder_wakeup.set()

    async def fire_reminder(self, reminder: Any) -> None:
        """Odpala jedno przypomnienie: wiadomosc od razu, zadanie przez workera (tylko odczyty)."""
        from backend.core import reminders, workers
        from backend.core.session import Session

        report = ""
        if reminder.kind == "task":
            reminders.remove(reminder.id)          # zadanie wykonuje sie najwyzej raz, takze po restarcie w trakcie
            target = targets.get_target(reminder.target)
            try:
                if self.agent is None:
                    report = tr("Agent nie jest dostepny.", "The agent is not available.")
                elif target is None:
                    report = tr(f"Cel {reminder.target!r} nie istnieje.", f"Target {reminder.target!r} does not exist.")
                else:
                    parent = Session(session_id=f"reminder:{reminder.id}", interface=f"routine:reminder-{reminder.id}",
                                     learns_vibe=False)
                    result = await asyncio.wait_for(
                        workers.run_worker(self.agent, parent, f"reminder-{reminder.id}", target, reminder.text,
                                           extra_instructions=prompt("ROUTINE_REPORT_INSTRUCTION")),
                        timeout=settings.WORKER_TIMEOUT)
                    report = result.render()
            except asyncio.TimeoutError:
                report = tr(f"Przekroczono limit czasu {settings.WORKER_TIMEOUT} s.",
                            f"Time limit of {settings.WORKER_TIMEOUT} s exceeded.")
            except Exception as exc:
                report = tr(f"Blad: {exc}", f"Error: {exc}")
            reminder.report = report
            published = bool(self.notifier.subscribers)
            if published:
                self.notifier.publish(reminder.to_event())
            if not published or reminders.for_cli(reminder):   # nikt nie slucha albo czeka jeszcze CLI
                reminder.fired, reminder.published = True, published
                reminders._save(reminders.load_reminders() + [reminder])
            return
        if not self.notifier.subscribers:
            reminders.mark_fired(reminder.id)
            return
        self.notifier.publish(reminder.to_event())
        if reminders.for_cli(reminder):             # subskrybenci dostali; CLI odbierze samo (claim)
            reminders.mark_fired(reminder.id, published=True)
        else:
            reminders.remove(reminder.id)

    def deliver_waiting_reminders(self) -> None:
        """Podlaczyl sie subskrybent — dostaje przypomnienia, ktore odpalily, gdy nikt nie sluchal."""
        from backend.core import reminders

        for reminder in reminders.waiting():
            if reminders.for_cli(reminder):
                reminders.mark_fired(reminder.id, reminder.report, published=True)
            else:
                reminders.remove(reminder.id)
            self.notifier.publish(reminder.to_event())

    async def _reminder_loop(self) -> None:
        from backend.core import reminders

        await asyncio.sleep(2)  # niech serwer wstanie; zalegle przypomnienia odpala sie zaraz po starcie
        while True:
            try:
                for reminder in reminders.due():
                    if reminder.kind == "task":
                        asyncio.create_task(self._safe(self.fire_reminder(reminder), "przypomnienie"))
                    else:
                        await self.fire_reminder(reminder)
                if self.notifier.subscribers and reminders.waiting():
                    self.deliver_waiting_reminders()
                wait = reminders.next_due()
            except Exception as exc:
                print(tr(f"[Czuwanie] Blad (przypomnienia): {exc}", f"[Watcher] Error (reminders): {exc}"), flush=True)
                wait = 30.0
            self._reminder_wakeup.clear()
            try:
                # bez terminu: czekamy na zmiane rejestru; z terminem: najwyzej do niego (i nie dluzej niz 30 s)
                await asyncio.wait_for(self._reminder_wakeup.wait(), timeout=30.0 if wait is None else min(wait, 30.0))
            except asyncio.TimeoutError:
                pass

    # --- petle --------------------------------------------------------------

    async def _watch_loop(self) -> None:
        await asyncio.sleep(10)  # niech serwer wstanie
        while True:
            try:
                await self.check_once()
            except Exception as exc:
                print(tr(f"[Czuwanie] Blad sprawdzenia: {exc}", f"[Watcher] Check error: {exc}"), flush=True)
            await asyncio.sleep(max(30, settings.WATCH_INTERVAL))

    async def _periodic(self, name: str, action, interval: int, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await action()
            except Exception as exc:
                print(tr(f"[Czuwanie] Blad ({name}): {exc}", f"[Watcher] Error ({name}): {exc}"), flush=True)
            await asyncio.sleep(max(300, interval))

    async def publish_digest(self) -> None:
        result = await self.build_digest()
        self.notifier.publish(result.to_event())

    async def _clock_loop(self) -> None:
        """Co minute: rutyny wedlug harmonogramu i poranny raport."""
        from backend.core import digest

        last_minute = None
        while True:
            now = datetime.now().replace(second=0, microsecond=0)
            if now != last_minute:
                last_minute = now
                if self.agent is not None:
                    for routine in routines.due(routines.load_routines(), now):
                        asyncio.create_task(self.run_routine(routine))
                stamp = now.strftime("%Y-%m-%d")
                if digest.due(settings.DIGEST_TIME, now) and self._last_digest != stamp:
                    self._last_digest = stamp
                    asyncio.create_task(self._safe(self.publish_digest(), "raport"))
            await asyncio.sleep(15)

    async def _welcome_loop(self) -> None:
        """Powitanie po instalacji — raz, gdy podlaczy sie pierwszy subskrybent (bot Telegram)."""
        from backend.core import welcome

        if welcome.already_sent():
            return
        await asyncio.sleep(90)            # niech migawka i sprawdzenia zdaza sie wykonac
        while self.notifier.subscribers == 0:
            await asyncio.sleep(30)
        event = await welcome.build(checks_report=self.checks_report, digest_time=settings.DIGEST_TIME,
                                    cert_days=settings.WATCH_CERT_DAYS)
        self.notifier.publish(event)
        welcome.mark_sent()

    @staticmethod
    async def _safe(coro, name: str) -> None:
        try:
            await coro
        except Exception as exc:
            print(tr(f"[Czuwanie] Blad ({name}): {exc}", f"[Watcher] Error ({name}): {exc}"), flush=True)

    def start(self) -> None:
        if self._tasks:
            return
        if settings.WATCH_ENABLED:
            self._tasks.append(asyncio.create_task(self._watch_loop()))
            self._tasks.append(asyncio.create_task(
                self._periodic("migawka", self.snapshot_once, settings.SNAPSHOT_INTERVAL, 30)))
            self._tasks.append(asyncio.create_task(
                self._periodic("sprawdzenia", self.checks_once, settings.CHECKS_INTERVAL, 60)))
            if settings.VULN_INTERVAL > 0:
                # lista pakietow co godzine (lokalny odczyt), OSV tylko przy zmianie albo co VULN_INTERVAL
                self._tasks.append(asyncio.create_task(
                    self._periodic("podatnosci", self.vulns_once, min(3600, settings.VULN_INTERVAL), 120)))
        self._tasks.append(asyncio.create_task(self._clock_loop()))
        self._tasks.append(asyncio.create_task(self._reminder_loop()))
        self._tasks.append(asyncio.create_task(self._safe(self._welcome_loop(), "powitanie")))


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
    lines = [f"- [{a.severity}] {a.title} ({a.detail}) " + tr(f"od {a.since}", f"since {a.since}")
             for a in _watcher.active.values()]
    return tr("\n\n--- CZUWANIE: AKTYWNE ALERTY ---\n"
              "Problemy wykryte automatycznie na hoscie. Jesli rozmowa dotyczy zdrowia serwera, uwzglednij je.\n",
              "\n\n--- MONITORING: ACTIVE ALERTS ---\n"
              "Problems detected automatically on the host. If the conversation is about server health, take them "
              "into account.\n") + "\n".join(lines)
