"""
/zalataj — latki podatnosci jednym poleceniem: bez LLM i bez pytania o kazda komende.

  parse_scope(slowo)  zakres: all (wszystko), images, system (pakiety tego serwera), servers (zdalne serwery),
                      server:<nazwa> (jeden zdalny serwer), urgent (pilne pakiety tego serwera); '' = nieznany
  plan(skan, zakres)  Job: co zalatac — z ostatniego skanu (core/vulns.py), w czesciach
  start(job, ...)     Run: latanie w tle. Klient (komenda patch w server.py, narzedzie security_patch po TAK) sledzi
                      postep przez follow(); rozlaczenie klienta latania nie przerywa — wynik przychodzi wtedy
                      jako wiadomosc (przypomnienie)

Czesci po kolei, kazda przez bezpiecznik (core/safety.py: wpis w dzienniku, strony przed i po):
  system  apt/apk na tym serwerze z porzadkami (przerwane dpkg, stare jadra, pobrane paczki); w trybie docker przez
          kontener pomocniczy (core/hostexec.py), bo system plikow hosta jest w kontenerze tylko do odczytu
  images  projekty compose: poprzedni obraz zostaje pod tagiem pipe-rollback-* (dla /cofnij), `pull`, `build --pull`,
          a obraz, ktorego to nie zmienilo, dostaje warstwe z poprawkami z repozytorium swojego systemu (ten sam tag);
          potem `up -d`, weryfikacja kontenerow projektu i komendy odwrotne
  server  apt/apk na zdalnym serwerze przez ssh (root albo sudo -n)
Potem sprzatanie (obrazy bez tagu, cache budowania starszy niz doba, pobrane paczki, cache npm/pip/yarn/composer)
ze zwolnionym miejscem i nowy skan: co zalatane, co bez zmian (i dlaczego), co zostalo.

Uprawnienia (viewer, tryb obserwacji) sprawdza wolajacy przez blocker().
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from backend.config import settings
from backend.core import audit, executor, fleet, hostexec, memory, runtime, safety, vulns
from backend.core.events import Progress
from backend.core.hostinfo import human_bytes
from backend.core.i18n import is_en, lang, tr

SOURCE = "zalataj"
OUTPUT_LINES = 25            # koncowka wyjscia kazdej czesci w `output` (pipe web, rozwijane)
ERROR_LINES = 8              # przy bledzie tyle ostatnich linii w tekscie dla Telegrama i CLI
FUSE_LINES = 12              # linii bezpiecznika na czesc (blad `docker compose config` potrafi miec ich dziesiatki)
NOTE_CHARS = 3500            # tyle wyniku trafia do rozmowy jako notatka [PIPE]
BUILD_CACHE_KEEP = "24h"     # cache budowania uzyty w ostatniej dobie zostaje (szybkie wdrozenia), starszy znika
DELIVER_AFTER = 3            # s po koncu: nikt nie odebral wyniku (klient sie rozlaczyl) -> wiadomosc
DISK_PATHS = ("/", "/var", "/boot")
NAMES_SHOWN = 6

SCOPE_WORDS = {
    "": "all", "wszystko": "all", "all": "all", "everything": "all",
    "obrazy": "images", "images": "images", "kontenery": "images", "containers": "images",
    "serwer": "system", "server": "system", "system": "system", "pakiety": "system", "packages": "system",
    "serwery": "servers", "servers": "servers", "zdalne": "servers", "remote": "servers",
    "pilne": "urgent", "urgent": "urgent",
}


def parse_scope(text: str) -> str:
    """Slowo z /zalataj (albo pole `scope`) -> zakres; nazwa zdalnego serwera -> 'server:<nazwa>'; '' = nieznane."""
    word = " ".join(str(text or "").split()).lower().removeprefix("server:")
    if word in SCOPE_WORDS:
        return SCOPE_WORDS[word]
    return f"server:{word}" if any(t.name == word for t in fleet.ssh_targets()) else ""


def scope_help() -> str:
    return tr("Uzycie: /zalataj (wszystko), /zalataj obrazy, /zalataj serwer (pakiety tego serwera), /zalataj serwery "
              "(zdalne), /zalataj <nazwa zdalnego serwera>, /zalataj pilne.",
              "Usage: /patch (everything), /patch images, /patch server (this server's packages), /patch servers "
              "(remote), /patch <remote server name>, /patch urgent.")


# ─── Plan ───────────────────────────────────────────────────────────────────

def _names(names: list[str], limit: int = NAMES_SHOWN) -> str:
    return ", ".join(names[:limit]) + (tr(f" i {len(names) - limit} innych", f" and {len(names) - limit} more")
                                       if len(names) > limit else "")


@dataclass
class Part:
    kind: str                                   # system | images | server
    name: str                                   # 'system', 'images', nazwa zdalnego serwera
    items: list = field(default_factory=list)   # PackageVulns (system, server) albo TargetResult (images)
    image_patch: Any = None                     # vulns.ImagePatch (images)
    target: Any = None                          # targets.Target (server)
    found: Any = None                           # vulns.TargetResult (server)

    def title(self) -> str:
        if self.kind == "system":
            return tr("ten serwer", "this server")
        if self.kind == "images":
            return tr("obrazy", "images")
        return tr(f"serwer {self.name}", f"server {self.name}")

    def what(self) -> str:
        """'12 pakietow (openssl, curl, ...)' / '2 obrazy (nginx:1.25, shop-web)'."""
        if self.kind == "images":
            return f"{vulns.n_images(len(self.items))} ({_names([t.name for t in self.items], 4)})"
        return f"{vulns.n_packages(len(self.items))} ({_names([p.name for p in vulns.sort_items(self.items)], 4)})"

    def describe(self) -> str:
        return f"{self.title()}: {self.what()}"


@dataclass
class Job:
    scope: str
    parts: list[Part]
    scan: Any
    notes: list[str] = field(default_factory=list)   # czego nie bedzie i dlaczego (np. brak kontenera pomocniczego)

    @property
    def empty(self) -> bool:
        return not self.parts

    def describe(self) -> str:
        return "; ".join(p.describe() for p in self.parts)


def plan(scan: Any, scope: str, *, skip_projects: frozenset[str] | set[str] = frozenset(),
         local_blocker: str = "", now: float | None = None) -> Job:
    """
    Co zalatac w tym zakresie. `local_blocker` — powod, dla ktorego nie da sie nic zrobic na tym serwerze (brak
    kontenera pomocniczego, Kubernetes): zostaja wtedy tylko zdalne serwery, a powod trafia do `notes`.
    """
    job = Job(scope, [], scan)
    local: list[Part] = []
    if scope in ("all", "system", "urgent") and scan.ecosystem:
        items = vulns.select(scan, "urgent" if scope == "urgent" else "all", now)
        if items and vulns.host_steps(scan.ecosystem, items, scan.kernel_meta):
            local.append(Part("system", "system", items))
    if scope in ("all", "images"):
        patch = vulns.image_patch(scan, skip_projects=skip_projects, now=now)
        if patch.projects:
            local.append(Part("images", "images", list(patch.refreshable), image_patch=patch))
    if local and local_blocker:
        job.notes.append(local_blocker)
    elif local:
        job.parts += local
    if scope in ("all", "servers") or scope.startswith("server:"):
        wanted = scope.split(":", 1)[1] if scope.startswith("server:") else ""
        registry = {t.name: t for t in fleet.ssh_targets()}
        for found in scan.of_kind("server"):
            if (wanted and found.name != wanted) or found.name not in registry or not found.ecosystem:
                continue
            items = vulns.select_server(found, now)
            if items and vulns.host_steps(found.ecosystem, items, found.meta.get("kernel_meta") or []):
                job.parts.append(Part("server", found.name, items, target=registry[found.name], found=found))
    return job


async def blocker(role: str) -> str:
    """Powod, dla ktorego Pipe w ogole nie lata ('' = moze): rola viewer, tryb obserwacji."""
    if role == "viewer":
        return tr("Rola viewer: tylko odczyt — latki instaluje administrator.",
                  "Viewer role: read-only — an administrator installs the patches.")
    if runtime.observe():
        return tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe niczego nie instaluje.",
                  "Observe mode (PIPE_OBSERVE=1): Pipe installs nothing.")
    return ""


async def local_blocker() -> str:
    """Powod, dla ktorego Pipe nie lata tego serwera i jego kontenerow ('' = moze); zdalne serwery to nie dotyczy."""
    if runtime.kind() == "docker":
        reason = await hostexec.blocker()
        return reason + tr(" Pakiety i obrazy tego serwera zainstaluj w jego powloce.",
                           " Install this server's packages and images in its shell.") if reason else ""
    if runtime.kind() != "native":
        return tr("W Kubernetesie latki tego wezla instaluje sie w jego powloce (potem /audyt pokaze, co zostalo).",
                  "On Kubernetes this node's patches are installed in its shell (then /audit shows what is left).")
    return ""


# ─── Wykonanie ──────────────────────────────────────────────────────────────

def _text(out: str, err: str) -> str:
    return "\n".join(x.strip() for x in (out, err) if x.strip() and x.strip() not in executor.NO_OUTPUT).strip()


def _tail(text: str, lines: int = OUTPUT_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@dataclass
class PartResult:
    part: Part
    exit_code: int = 0           # wynik czesci (kroki wymagane, weryfikacja bezpiecznika)
    entry_id: str = ""
    fuse: list[str] = field(default_factory=list)       # linie bezpiecznika: weryfikacja, dziennik
    output: str = ""
    warnings: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)  # obrazy: nowszy obraz
    layered: list[str] = field(default_factory=list)    # obrazy: poprawki zainstalowane w obrazie
    failed: list[str] = field(default_factory=list)     # obrazy: nie udalo sie (powod w tekscie)
    stopped: bool = False        # weryfikacja nie przeszla — dalszych czesci nie robie
    outcome: Any = None          # vulns.PatchOutcome po ponownym skanie


@dataclass
class Done:
    text: str
    data: dict[str, Any]
    exit_code: int


class Run:
    """Jedno latanie w tle: zdarzenia (Progress, na koncu Done) dla wszystkich sledzacych, od poczatku."""

    def __init__(self, job: Job, interface: str, session_id: str = "") -> None:
        self.job, self.interface, self.session_id = job, interface, session_id
        self.events: list[Any] = []
        self.done: Done | None = None
        self.delivered = False
        self.started = time.time()
        self.task: asyncio.Task | None = None
        self._followers: set[asyncio.Queue] = set()
        self._image = ""

    def emit(self, event: Any) -> None:
        if isinstance(event, str):
            event = Progress(event, source=SOURCE)
        self.events.append(event)
        for queue in self._followers:
            queue.put_nowait(event)

    async def follow(self) -> AsyncIterator[Any]:
        """Wszystkie zdarzenia od poczatku, potem na biezaco, do Done wlacznie."""
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        self._followers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if isinstance(event, Done):
                    return
        finally:
            self._followers.discard(queue)

    # ── kroki

    def host(self, command: str) -> str:
        """Komenda w powloce tego serwera: w trybie docker przez kontener pomocniczy."""
        return hostexec.wrap(command, self._image) if self._image else command

    async def sh(self, command: str, timeout: float | None = None, log: bool = True) -> tuple[str, int]:
        """Komenda latki; zmiany ida do audit logu (odczyty, np. docker image inspect, nie)."""
        out, err, code = await executor.execute(command, timeout=timeout or settings.CONFIRMED_COMMAND_TIMEOUT)
        if log:
            await audit.log_confirmed(self.interface + " [zalataj]", command, code)
        return memory.redact_secrets(_text(out, err))[0], code

    async def _steps(self, steps: list[vulns.Step], wrap, result: PartResult, prefix: str = "",
                     fatal=None) -> tuple[str, int]:
        """Kroki po kolei; `fatal(output)` -> powod przerwania (np. sudo chce hasla — kolejne kroki tez by nie przeszly)."""
        outputs, code = [], 0
        for step in steps:
            self.emit(prefix + step.label)
            output, rc = await self.sh(wrap(step.command))
            outputs.append(output)
            reason = fatal(output) if fatal and rc != 0 else ""
            if reason:
                result.warnings.append(reason)
                return "\n".join(o for o in outputs if o), rc or 1
            if step.required:
                code = rc
            elif rc != 0 and step.warn:
                result.warnings.append(step.warn)
        return "\n".join(o for o in outputs if o), code

    async def _fused(self, plan: safety.Plan, operation, description: str, result: PartResult, *,
                     sites: bool = True) -> None:
        """Operacja przez bezpiecznik: jego kroki ida jako postep, wynik do `result`."""
        from backend.core import checks

        captured: dict[str, Any] = {"output": "", "code": 0, "head": "", "ran": False}

        async def run() -> tuple[str, int]:
            captured["output"], captured["code"] = await operation()
            captured["ran"] = True
            captured["head"] = _tail(captured["output"]) or executor.NO_OUTPUT[1 if is_en() else 0]
            return captured["head"], captured["code"]

        outcome = None
        async for event in safety.guarded(plan, run, interface=self.interface, tool="patch", description=description,
                                          sites_enabled=sites and settings.WATCH_SITES,
                                          site_ignore=checks.parse_ignore(settings.WATCH_IGNORE)):
            if isinstance(event, Progress):
                self.emit(event)
            else:
                outcome = event
        result.output = _tail(captured["output"])
        result.entry_id = outcome.entry_id if outcome else ""
        result.exit_code = outcome.exit_code if outcome else 1
        text = outcome.text if outcome else ""
        if captured["head"] and text.startswith(captured["head"]):
            text = text[len(captured["head"]):]          # wyjscie apt/compose jest w `output`, tu tylko bezpiecznik
        result.fuse = [line for line in text.strip().splitlines() if line.strip()][:FUSE_LINES]
        # weryfikacja po zmianie nie przeszla (operacja sie udala, bezpiecznik zglasza blad) — nie dokladam kolejnych
        # zmian; nieudane sprawdzenie PRZED (nic sie nie zmienilo) pozostalych czesci nie zatrzymuje
        result.stopped = captured["ran"] and captured["code"] == 0 and result.exit_code != 0

    def _plan(self, plan: safety.Plan) -> safety.Plan:
        return hostexec.host_plan(plan, self._image) if self._image else plan

    async def _system(self, part: Part) -> PartResult:
        result = PartResult(part)
        scan = self.job.scan
        steps = vulns.host_steps(scan.ecosystem, part.items, scan.kernel_meta)
        plan = self._plan(safety.Plan(sites=True, notes=[tr(
            "pakietow nie cofam automatycznie — co sie zmienilo, pokaze /zmiany",
            "packages are not rolled back automatically — /changes shows what changed")]))
        self.emit(tr(f"Ten serwer: {part.what()}", f"This server: {part.what()}"))

        async def operation() -> tuple[str, int]:
            return await self._steps(steps, self.host, result)
        await self._fused(plan, operation, tr(f"latki: {vulns.n_packages(len(part.items))}",
                                              f"patches: {vulns.n_packages(len(part.items))}"), result)
        return result

    async def _server(self, part: Part) -> PartResult:
        result = PartResult(part)
        steps = vulns.host_steps(part.found.ecosystem, part.items, part.found.meta.get("kernel_meta") or [])
        plan = safety.Plan(notes=[tr(f"na serwerze {part.name} przez ssh; pakietow nie cofam automatycznie",
                                     f"on server {part.name} over ssh; packages are not rolled back automatically")])
        self.emit(tr(f"Serwer {part.name}: {part.what()}", f"Server {part.name}: {part.what()}"))

        async def operation() -> tuple[str, int]:
            return await self._steps(steps, lambda c: fleet.as_root(part.target, c), result, prefix=f"{part.name}: ",
                                     fatal=lambda out: fleet.sudo_hint(part.target) if fleet.needs_password(out) else "")
        await self._fused(plan, operation, tr(f"latki na {part.name}: {vulns.n_packages(len(part.items))}",
                                              f"patches on {part.name}: {vulns.n_packages(len(part.items))}"),
                          result, sites=False)
        return result

    async def _inspect(self, ref: str, template: str) -> str:
        out, code = await self.sh(f"docker image inspect -f {shlex.quote(template)} {shlex.quote(ref)}", timeout=30,
                                  log=False)
        return out.strip() if code == 0 else ""

    async def _images(self, part: Part) -> PartResult:
        result = PartResult(part)
        patch: vulns.ImagePatch = part.image_patch
        plan = safety.Plan(sites=True, inverse=list(patch.inverse), notes=[tr(
            "poprzednie obrazy zostaja pod tagiem pipe-rollback-* — /cofnij do nich wraca",
            "the previous images stay under a pipe-rollback-* tag — /undo goes back to them")])
        for project in patch.projects:
            flags = " ".join(f"-f {shlex.quote(f)}" for f in project.files) + f" -p {shlex.quote(project.project)}"
            plan.pre.append(safety.Check(tr(f"konfiguracja compose {project.project}", f"compose config {project.project}"),
                                         "shell", f"{project.base} config -q"))
            plan.post.append(safety.Check(tr(f"kontenery projektu {project.project} dzialaja",
                                             f"containers of project {project.project} are running"),
                                          "compose", target=flags, retries=12, delay=5))
        plan = self._plan(plan)
        self.emit(tr(f"Obrazy: {part.what()}", f"Images: {part.what()}"))

        async def operation() -> tuple[str, int]:
            outputs, code, done = [], 0, set()
            for project in patch.projects:
                services = ", ".join(project.services)
                for target in project.targets:
                    rollback = vulns.rollback_ref(str(target.meta.get("image", "")))
                    short = str(target.meta.get("image_id", "")).removeprefix("sha256:")[:12]
                    if rollback and target.name not in done and re.fullmatch(r"[0-9a-f]{12}", short):
                        await self.sh(f"docker tag {short} {shlex.quote(rollback)}", timeout=30)
                self.emit(tr(f"pobieram nowsze obrazy i przebudowuje wlasne: {project.project} ({services})",
                             f"pulling newer images and rebuilding own ones: {project.project} ({services})"))
                for sub in ("pull --ignore-buildable", "build --pull"):
                    output, rc = await self.sh(self.host(project.command(sub)))
                    outputs.append(output)
                    if rc != 0:
                        result.warnings.append(tr(f"{project.project}: docker compose {sub.split()[0]} zakonczyl sie "
                                                  f"bledem (kod {rc})", f"{project.project}: docker compose "
                                                  f"{sub.split()[0]} failed (exit code {rc})"))
                for target in project.targets:
                    if target.name in done:
                        continue
                    done.add(target.name)
                    outputs.append(await self._refresh_or_layer(target, result))
                self.emit(tr(f"uruchamiam od nowa: {project.project} ({services})",
                             f"restarting: {project.project} ({services})"))
                output, rc = await self.sh(self.host(project.command("up -d")))
                outputs.append(output)
                code = code or rc
            return "\n".join(o for o in outputs if o), code
        await self._fused(plan, operation, tr(f"latki obrazow: {vulns.n_images(len(part.items))}",
                                              f"image patches: {vulns.n_images(len(part.items))}"), result)
        return result

    async def _refresh_or_layer(self, target: Any, result: PartResult) -> str:
        """
        Po pull/build: nowszy obraz pod tagiem — zostaje. Ten sam (nie ma nowszego) — warstwa z poprawkami
        z repozytorium systemu obrazu. Obraz z poprzedniej warstwy Pipe liczy sie jako "ten sam", gdy pod tagiem
        jest dalej jego obraz bazowy (etykieta PATCH_LABEL) — inaczej co latke wracalby obraz bez poprawek.
        """
        ref = str(target.meta.get("image", ""))
        old = str(target.meta.get("image_id", ""))
        short = old.removeprefix("sha256:")[:12]
        new = await self._inspect(ref, "{{.Id}}")
        try:
            labels = json.loads(await self._inspect(short, "{{json .Config.Labels}}") or "null") or {}
        except ValueError:
            labels = {}
        base = str(labels.get(vulns.PATCH_LABEL, "")) if isinstance(labels, dict) else ""
        if new and new[:19] not in (old[:19], base[:19] if base else old[:19]):
            result.refreshed.append(target.name)
            return ""
        try:
            config = json.loads(await self._inspect(ref, "{{json .Config}}") or "null") or {}
        except ValueError:
            config = {}
        command = vulns.layer_commands(target, ref, new, config if isinstance(config, dict) else {},
                                       "pipe-patch-" + secrets.token_hex(4))
        output, rc = "", 1
        if command:
            self.emit(tr(f"instaluje poprawki w obrazie {target.name} ({vulns.n_packages(len(target.items))}) — nowszego "
                         "obrazu nie ma", f"installing the fixes inside image {target.name} "
                         f"({vulns.n_packages(len(target.items))}) — there is no newer image"))
            output, rc = await self.sh(command)
        if rc == 0:
            result.layered.append(target.name)
            return output
        result.failed.append(target.name)
        if command:
            result.warnings.append(tr(f"{target.name}: poprawek w obrazie nie udalo sie zainstalowac (kod {rc}) — "
                                      "system obrazu moze byc bez wsparcia", f"{target.name}: the fixes could not be "
                                      f"installed inside the image (exit code {rc}) — the image system may be out of support"))
        if base and new[:19] == base[:19] and re.fullmatch(r"[0-9a-f]{12}", short):
            # pull przywrocil pod tagiem obraz bez poprawek — kontener zostaje na poprzednim (z poprawkami Pipe)
            await self.sh(f"docker tag {short} {shlex.quote(ref)}", timeout=30)
        return output

    async def _free(self, paths: list[str]) -> dict[int, int]:
        free: dict[int, int] = {}
        for path in paths:
            local = runtime.to_local(path)
            try:
                stat, device = os.statvfs(local), os.stat(local).st_dev
            except OSError:
                continue
            free[device] = stat.f_bavail * stat.f_frsize
        return free

    async def _cleanup(self) -> int:
        """Stare obrazy, cache budowania, pobrane paczki, cache menedzerow pakietow. Zwraca zwolnione bajty."""
        self.emit(tr("sprzatam: stare obrazy, cache budowania, pobrane paczki i cache npm/pip",
                     "cleaning up: old images, build cache, downloaded packages and npm/pip caches"))
        root, code = await self.sh("docker info -f '{{.DockerRootDir}}'", timeout=30, log=False)
        paths = list(DISK_PATHS) + ([root.strip()] if code == 0 and root.strip().startswith("/") else [])
        before = await self._free(paths)
        await self.sh(self.host(CACHE_CLEANUP), timeout=300)
        if code == 0:
            await self.sh("docker image prune -f", timeout=300)
            await self.sh(f"docker builder prune -af --filter until={BUILD_CACHE_KEEP}", timeout=300)
        after = await self._free(paths)
        return sum(max(0, after[d] - before[d]) for d in after if d in before)

    # ── calosc

    async def main(self, watcher: Any = None, agent: Any = None) -> None:
        try:
            done = await self._execute(watcher)
        except Exception as exc:                       # wynik ma przyjsc zawsze, takze po bledzie
            text = tr(f"Latanie przerwal blad: {exc}", f"Patching stopped with an error: {exc}")
            done = Done(text, {"text": text, "exit_code": 1, "scope": self.job.scope, "summary": text,
                               "fixed": [], "unchanged": [], "output": "", "report": None, "entries": []}, 1)
        self.done = done
        self.emit(done)
        if agent is not None and self.session_id:
            entries, shown = ", ".join(done.data.get("entries") or []) or "-", done.text[:NOTE_CHARS]
            try:
                agent.note(self.session_id, tr(
                    f"Uzytkownik zalatal podatnosci przez /zalataj ({self.job.describe()}), kod wyjscia {done.exit_code}, "
                    f"wpisy dziennika {entries}. Wynik:\n{shown}",
                    f"The user patched vulnerabilities with /patch ({self.job.describe()}), exit code {done.exit_code}, "
                    f"journal entries {entries}. Result:\n{shown}"))
            except Exception:
                pass
        await asyncio.sleep(DELIVER_AFTER)
        if not self.delivered:
            await _deliver(watcher, self.interface, done)

    async def _execute(self, watcher: Any) -> Done:
        if runtime.kind() == "docker" and any(p.kind in ("system", "images") for p in self.job.parts):
            self._image = await hostexec.image()
        results: list[PartResult] = []
        for part in self.job.parts:
            runner = {"system": self._system, "images": self._images, "server": self._server}[part.kind]
            result = await runner(part)
            results.append(result)
            if result.stopped:
                break
        # sprzatanie tego serwera po latce na nim (zdalne serwery sprzataja swoje kroki apt)
        freed = await self._cleanup() if any(r.part.kind in ("system", "images") for r in results) else 0
        finished = time.time()
        self.emit(tr("Sprawdzam, co zostalo do zalatania...", "Checking what is left to patch..."))
        left = None
        if watcher is not None:
            try:
                await watcher.vulns_once(since=finished)
                left = watcher.vuln_scan
            except Exception as exc:
                self.emit(tr(f"ponowny skan nie powiodl sie: {exc}", f"the rescan failed: {exc}"))
        if left is not None and left.ok:
            # tylko udane czesci: po bledzie (apt, compose, siec) nastepne /zalataj ma probowac od razu, a nie za dobe
            for result in results:
                if result.exit_code == 0:
                    scope = {"system": "all", "images": "images"}.get(result.part.kind, f"server:{result.part.name}")
                    result.outcome = await asyncio.to_thread(vulns.record_outcome, scope, result.part.items, left)
        return summary(self.job, results, left, freed, len(results) < len(self.job.parts))


# Cache menedzerow pakietow na serwerze (root): odtwarzalne z sieci, a po latkach i wdrozeniach potrafia miec gigabajty
CACHE_CLEANUP = ("command -v apt-get >/dev/null 2>&1 && apt-get -o DPkg::Lock::Timeout=60 clean; "
                 "command -v npm >/dev/null 2>&1 && npm cache clean --force >/dev/null 2>&1; "
                 "for p in pip3 pip; do command -v $p >/dev/null 2>&1 && $p cache purge >/dev/null 2>&1 && break; done; "
                 "command -v yarn >/dev/null 2>&1 && yarn cache clean >/dev/null 2>&1; "
                 "command -v composer >/dev/null 2>&1 && composer clear-cache -n >/dev/null 2>&1; true")


# ─── Wynik ──────────────────────────────────────────────────────────────────

def _fixed_lines(result: PartResult) -> list[str]:
    lines = []
    outcome = result.outcome
    part = result.part
    if part.kind == "images":
        refreshed = [n for n in result.refreshed if not outcome or n not in outcome.unchanged]
        layered = [n for n in result.layered if not outcome or n not in outcome.unchanged]
        if refreshed:
            lines.append(tr(f"nowszy obraz: {_names(refreshed)}", f"newer image: {_names(refreshed)}"))
        if layered:
            lines.append(tr(f"poprawki zainstalowane w obrazie (nowszego nie bylo): {_names(layered)}",
                            f"fixes installed inside the image (there was no newer one): {_names(layered)}"))
        if outcome and outcome.unchanged:
            built = {t.name for t in part.items if vulns.built_locally(t)}
            for names, why in (([n for n in outcome.unchanged if n in built],
                                tr("podbij obraz bazowy (FROM) w Dockerfile", "bump the base image (FROM) in the Dockerfile")),
                               ([n for n in outcome.unchanged if n not in built],
                                tr("zmien tag w compose na nowsza wersje", "change the tag in compose to a newer version"))):
                if names:
                    lines.append(tr(f"bez zmian: {_names(names)} — ani nowszy obraz, ani poprawki z repozytorium nie "
                                    f"pomogly; {why} (nie sprobuje znowu przez dobe)",
                                    f"unchanged: {_names(names)} — neither a newer image nor the repository fixes helped; "
                                    f"{why} (I will not try again for a day)"))
        return lines
    if outcome and outcome.fixed:
        lines.append(tr(f"zainstalowane: {len(outcome.fixed)} ({_names(outcome.fixed)})",
                        f"installed: {len(outcome.fixed)} ({_names(outcome.fixed)})"))
    if outcome and outcome.unchanged:
        lines.append(tr(f"bez zmian: {len(outcome.unchanged)} ({_names(outcome.unchanged)}) — repozytoria nie maja jeszcze "
                        "poprawki; nie sprobuje znowu przez dobe",
                        f"unchanged: {len(outcome.unchanged)} ({_names(outcome.unchanged)}) — the repositories do not have "
                        "the fix yet; I will not try again for a day"))
    docker = vulns.docker_packages(part.items)
    if docker and result.exit_code == 0:
        lines.append(tr(f"Docker ({', '.join(docker)}) zaktualizowany — nowa wersja zadziala po restarcie Dockera albo "
                        "serwera (restart zatrzymuje kontenery, zrob go w oknie serwisowym)",
                        f"Docker ({', '.join(docker)}) updated — the new version runs after a Docker or server restart "
                        "(the restart stops containers, do it in a maintenance window)"))
    return lines


def summary(job: Job, results: list[PartResult], left: Any, freed: int, stopped: bool) -> Done:
    """Wynik dla Telegrama i CLI (`text`) oraz dla pipe web (`data`: report = stan po latce)."""
    failed = [r for r in results if r.exit_code != 0]
    if not failed and not stopped:
        head = tr("Gotowe.", "Done.")
    elif len(failed) < len(results):
        head = tr("Zalatane czesciowo — szczegoly nizej.", "Partly patched — details below.")
    else:
        head = tr("Nie udalo sie zalatac — szczegoly nizej.", "Patching failed — details below.")
    lines = [head]
    for result in results:
        part = result.part
        lines += ["", (part.title()[:1].upper() + part.title()[1:]) + ":"]
        if result.exit_code != 0 and not result.fuse:
            lines.append("- " + tr(f"nie wykonano (kod {result.exit_code})", f"not done (exit code {result.exit_code})"))
        lines += [f"- {line}" for line in _fixed_lines(result)]
        lines += [f"- {w}" for w in dict.fromkeys(result.warnings)]
        lines += [f"- {line}" for line in result.fuse]
        if result.exit_code != 0 and result.output:
            lines += ["- " + tr("ostatnie linie wyjscia:", "last output lines:")]
            lines += ["    " + line for line in result.output.splitlines()[-ERROR_LINES:]]
    if stopped:
        lines += ["", tr("Weryfikacja nie przeszla, wiec dalszych czesci nie latalem: ",
                         "Verification failed, so I did not patch the remaining parts: ")
                  + "; ".join(p.title() for p in job.parts[len(results):]) + "."]
    if freed > 0:
        lines += ["", tr(f"Sprzatanie: zwolnione {human_bytes(freed)} (stare obrazy, cache budowania, pobrane paczki, "
                         "stare jadra).", f"Cleanup: freed {human_bytes(freed)} (old images, build cache, downloaded "
                         "packages, old kernels).")]
    if left is not None and left.ok:
        if left.reboot:
            lines += ["", tr("Ten serwer czeka na restart — nowe jadro i biblioteki zaczna dzialac po nim.",
                             "This server is waiting for a reboot — the new kernel and libraries take effect after it.")]
        remaining = [line for line in vulns.todo(left) if line.startswith(_REMAINING) or "Ubuntu Pro" in line]
        if remaining:
            lines += ["", tr("Zostaje (tego /zalataj nie naprawi):", "Left (which /patch cannot fix):")] \
                + [f"- {line}" for line in remaining]
    else:
        lines += ["", tr("Nie udalo sie sprawdzic wyniku — /podatnosci pokaze stan po nastepnym skanie.",
                         "Could not check the result — /vulns shows the state after the next scan.")]
    fixed = [n for r in results if r.outcome for n in r.outcome.fixed]
    unchanged = [n for r in results if r.outcome for n in r.outcome.unchanged]
    code = next((r.exit_code for r in failed), 0)
    entries = [r.entry_id for r in results if r.entry_id]
    short = (tr(f"zalatane: {len(fixed)}", f"patched: {len(fixed)}") +
             (tr(f", bez zmian: {len(unchanged)}", f", unchanged: {len(unchanged)}") if unchanged else "")
             + (tr(f", zwolnione {human_bytes(freed)}", f", freed {human_bytes(freed)}") if freed > 0 else ""))
    report = vulns.report_view(left, vulns.explanations(lang())) if left is not None else None
    text = "\n".join(lines)
    return Done(text, {"text": text, "exit_code": code, "entry_id": entries[0] if entries else "", "entries": entries,
                       "scope": job.scope, "summary": short, "fixed": fixed, "unchanged": unchanged, "freed": freed,
                       "output": "\n\n".join(r.output for r in results if r.output), "report": report,
                       "parts": [{"kind": r.part.kind, "name": r.part.name, "exit_code": r.exit_code,
                                  "entry_id": r.entry_id} for r in results]}, code)


# Linie "co z tym zrobic", ktore zostaja po latce do zrobienia recznie (aplikacje, tagi, kontenery bez compose...)
_REMAINING = ("Aplikacje", "Applications", "Obrazy budowane", "Images built", "Obrazy z rejestru", "Registry images",
              "Kontenery bez compose", "Containers without compose")


async def _deliver(watcher: Any, interface: str, done: Done) -> None:
    """Klient rozlaczyl sie przed koncem — wynik jako wiadomosc (dochodzi tam, skad przyszlo /zalataj)."""
    from backend.core import reminders

    text = tr("Latanie (/zalataj) zakonczone. ", "Patching (/patch) finished. ") + str(done.data.get("summary") or "") \
        + tr(". Szczegoly: /podatnosci i /dziennik.", ". Details: /vulns and /journal.")
    try:
        reminders.add(time.time(), text[:reminders.MAX_TEXT_CHARS], kind="message", to=interface)
    except (reminders.ReminderError, OSError) as exc:
        print(f"[zalataj] {text} ({exc})", flush=True)
        return
    if watcher is not None:
        watcher.reminders_changed()


_current: Run | None = None


def current() -> Run | None:
    """Trwajace latanie (jedno naraz) albo None."""
    return _current if _current is not None and _current.done is None else None


def start(job: Job, *, interface: str, session_id: str = "", watcher: Any = None, agent: Any = None) -> Run:
    global _current
    run = Run(job, interface, session_id)
    _current = run
    run.task = asyncio.create_task(run.main(watcher, agent))
    return run
