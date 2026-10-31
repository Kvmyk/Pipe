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
  images  projekty compose: poprzedni obraz zostaje pod tagiem pipe-rollback-* (dla /cofnij), `FROM` w Dockerfile
          uslug budowanych lokalnie podbity do nowszej wersji tej samej linii (core/baseimages.py), `pull`, `build --pull`,
          a obraz, ktorego to nie zmienilo, dostaje warstwe z poprawkami z repozytorium swojego systemu (ten sam tag);
          potem `up -d`, weryfikacja kontenerow projektu i komendy odwrotne
  apps    zaleznosci aplikacji: menedzer pakietow w jednorazowym kontenerze podbija wersje w plikach zaleznosci
          (core/appdeps.py; kopia plikow w dzienniku), potem `build` + `up -d` projektu compose z tego katalogu
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
from backend.core import appdeps, audit, executor, fleet, hostexec, hostinfo, memory, runtime, safety, vulns
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
RESULT_KEEP = 600            # s po koncu: klient, ktoremu zerwalo polaczenie, moze jeszcze odebrac wynik (patch_status)
DISK_PATHS = ("/", "/var", "/boot")
NAMES_SHOWN = 6

SCOPE_WORDS = {
    "": "all", "wszystko": "all", "all": "all", "everything": "all",
    "obrazy": "images", "images": "images", "kontenery": "images", "containers": "images",
    "serwer": "system", "server": "system", "system": "system", "pakiety": "system", "packages": "system",
    "serwery": "servers", "servers": "servers", "zdalne": "servers", "remote": "servers",
    "pilne": "urgent", "urgent": "urgent",
    "aplikacje": "apps", "apps": "apps", "applications": "apps", "zaleznosci": "apps", "zależności": "apps",
    "dependencies": "apps", "deps": "apps",
}


def parse_scope(text: str) -> str:
    """Slowo z /zalataj (albo pole `scope`) -> zakres; nazwa zdalnego serwera -> 'server:<nazwa>'; '' = nieznane."""
    word = " ".join(str(text or "").split()).lower().removeprefix("server:")
    if word in SCOPE_WORDS:
        return SCOPE_WORDS[word]
    return f"server:{word}" if any(t.name == word for t in fleet.ssh_targets()) else ""


def scope_help() -> str:
    return tr("Uzycie: /zalataj (wszystko), /zalataj obrazy, /zalataj serwer (pakiety tego serwera), /zalataj aplikacje "
              "(zaleznosci aplikacji), /zalataj serwery (zdalne), /zalataj <nazwa zdalnego serwera>, /zalataj pilne.",
              "Usage: /patch (everything), /patch images, /patch server (this server's packages), /patch apps "
              "(application dependencies), /patch servers (remote), /patch <remote server name>, /patch urgent.")


# ─── Plan ───────────────────────────────────────────────────────────────────

def _names(names: list[str], limit: int = NAMES_SHOWN) -> str:
    return ", ".join(names[:limit]) + (tr(f" i {len(names) - limit} innych", f" and {len(names) - limit} more")
                                       if len(names) > limit else "")


@dataclass
class Part:
    kind: str                                   # system | apps | images | server
    name: str                                   # 'system', 'apps', 'images', nazwa zdalnego serwera
    items: list = field(default_factory=list)   # PackageVulns (system, server), appdeps.AppBump (apps), TargetResult (images)
    image_patch: Any = None                     # vulns.ImagePatch (images)
    target: Any = None                          # targets.Target (server)
    found: Any = None                           # vulns.TargetResult (server)

    def title(self) -> str:
        if self.kind == "system":
            return tr("ten serwer", "this server")
        if self.kind == "images":
            return tr("obrazy", "images")
        if self.kind == "apps":
            return tr("zaleznosci aplikacji", "application dependencies")
        return tr(f"serwer {self.name}", f"server {self.name}")

    def what(self) -> str:
        """'12 pakietow (openssl, curl, ...)' / '2 obrazy (nginx:1.25, shop-web)'."""
        if self.kind == "images":
            return f"{vulns.n_images(len(self.items))} ({_names([t.name for t in self.items], 4)})"
        if self.kind == "apps":
            count = sum(len(b.items) for b in self.items)
            return f"{vulns.n_packages(count)} ({_names([os.path.basename(b.directory) or b.directory for b in self.items], 4)})"
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
    if scope in ("all", "apps"):
        bumps = appdeps.plan(scan, vulns.held_packages(now))
        if bumps:
            local.append(Part("apps", "apps", bumps))
    if scope in ("all", "images"):
        patch = vulns.image_patch(scan, skip_projects=skip_projects, now=now)
        if patch.projects or patch.web:
            local.append(Part("images", "images", list(patch.refreshable) + list(patch.web), image_patch=patch))
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
    based: list[str] = field(default_factory=list)      # obrazy: podbity FROM ('node:20.11-slim → node:20.18-slim')
    tagged: list[str] = field(default_factory=list)     # obrazy: podbity tag w pliku compose ('redis:7.2.3 → redis:7.2.5')
    stopped: bool = False        # weryfikacja nie przeszla — dalszych czesci nie robie
    outcome: Any = None          # vulns.PatchOutcome po ponownym skanie
    entries: list[str] = field(default_factory=list)    # aplikacje: wpis dziennika na kazdy katalog
    redeployed: list[str] = field(default_factory=list) # aplikacje: przebudowane projekty compose
    undeployed: list[str] = field(default_factory=list) # aplikacje: katalogi bez projektu compose (wdrozenie reczne)


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
        self.ended = 0.0
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

    async def _apps(self, part: Part) -> PartResult:
        """
        Kazdy katalog aplikacji osobno (osobny wpis dziennika — /cofnij jednej aplikacji nie rusza innych): menedzer
        w kontenerze podbija pliki zaleznosci, potem projekt compose z tego katalogu: `build` + `up -d`.
        """
        from backend.core import infra

        result = PartResult(part)
        self.emit(tr(f"Zaleznosci aplikacji: {part.what()}", f"Application dependencies: {part.what()}"))
        try:
            containers = await infra.docker_containers() or []
        except Exception:
            containers = []
        own = await hostexec.own_project() if runtime.kind() == "docker" else ""
        for bump in part.items:
            sub = PartResult(part)
            projects = app_projects(bump.directory, containers, skip={own} if own else set())
            plan = safety.Plan(files=bump.files, sites=bool(projects), notes=[tr(
                "pliki zaleznosci sa w kopii — /cofnij je przywraca" + (" i przebudowuje projekt" if projects else ""),
                "the dependency files are backed up — /undo restores them" + (" and rebuilds the project" if projects else ""))])
            for project in projects:
                flags = " ".join(f"-f {shlex.quote(f)}" for f in project.files) + f" -p {shlex.quote(project.project)}"
                plan.pre.append(safety.Check(tr(f"konfiguracja compose {project.project}", f"compose config {project.project}"),
                                             "shell", f"{project.base} config -q"))
                plan.post.append(safety.Check(tr(f"kontenery projektu {project.project} dzialaja",
                                                 f"containers of project {project.project} are running"),
                                              "compose", target=flags, retries=12, delay=5))
                plan.inverse += [project.command("build"), project.command("up -d")]
            plan = self._plan(plan)
            uid, gid = appdeps.owner(bump.directory)

            async def operation(bump=bump, projects=projects, sub=sub) -> tuple[str, int]:
                outputs, changed = [], 0
                for manager in bump.bumps:
                    self.emit(tr(f"{bump.directory}: {manager.manager} podbija ", f"{bump.directory}: {manager.manager} bumps ")
                              + _names([n for n, _ in manager.packages], 4))
                    output, rc = await self.sh(appdeps.docker_command(bump.directory, manager, uid, gid))
                    if hostinfo.cpu_limit_rejected(output, rc):     # kontener nie wystartowal — nic sie nie zmienilo
                        output, rc = await self.sh(appdeps.docker_command(bump.directory, manager, uid, gid))
                    outputs.append(output)
                    if rc == 0:
                        changed += 1
                    else:
                        sub.warnings.append(tr(f"{bump.directory}: {manager.manager} zakonczyl sie bledem (kod {rc})",
                                               f"{bump.directory}: {manager.manager} failed (exit code {rc})"))
                if not changed:
                    return "\n".join(o for o in outputs if o), 1
                code = 0
                for project in projects:
                    self.emit(tr(f"przebudowuje i uruchamiam od nowa: {project.project}",
                                 f"rebuilding and restarting: {project.project}"))
                    for sub_command in ("build", "up -d"):
                        output, rc = await self.sh(self.host(project.command(sub_command)))
                        outputs.append(output)
                        code = code or rc
                        if rc != 0:
                            break
                    if code == 0:
                        sub.redeployed.append(project.project)
                if not projects:
                    sub.undeployed.append(bump.directory)
                return "\n".join(o for o in outputs if o), code
            await self._fused(plan, operation, tr(f"zaleznosci aplikacji {bump.directory}: {vulns.n_packages(len(bump.items))}",
                                                  f"app dependencies {bump.directory}: {vulns.n_packages(len(bump.items))}"),
                              sub, sites=bool(projects))
            result.entries += [sub.entry_id] if sub.entry_id else []
            result.exit_code = result.exit_code or sub.exit_code
            result.fuse += [f"{os.path.basename(bump.directory) or bump.directory}: {line}" for line in sub.fuse]
            result.output = "\n".join(o for o in (result.output, sub.output) if o)
            result.warnings += sub.warnings
            result.redeployed += sub.redeployed
            result.undeployed += sub.undeployed
            if sub.stopped:
                result.stopped = True
                break
        result.entry_id = result.entries[0] if result.entries else ""
        result.output = _tail(result.output)
        return result

    async def _inspect(self, ref: str, template: str) -> str:
        out, code = await self.sh(f"docker image inspect -f {shlex.quote(template)} {shlex.quote(ref)}", timeout=30,
                                  log=False)
        return out.strip() if code == 0 else ""

    async def _base_bumps(self, patch: vulns.ImagePatch) -> dict[str, tuple[str, list]]:
        """
        Dockerfile'e uslug budowanych lokalnie (z podatnymi obrazami), w ktorych `FROM` ma nowsza wersje w tej samej
        linii: sciezka hosta -> (nowa tresc, podbicia). Odczyty: `compose config` i API tagow Docker Hub.
        """
        from backend.core import baseimages, websearch

        found: dict[str, tuple[str, list]] = {}
        for project in patch.projects:
            services = sorted({str(c.get("service")) for t in project.targets if vulns.built_locally(t)
                               for c in t.meta.get("compose") or []
                               if c.get("project") == project.project and c.get("service") in project.services})
            if not services:
                continue
            out, _, code = await executor.execute(self.host(f"{project.base} config --format json"), timeout=60)
            try:
                config = json.loads(out) if code == 0 else {}
            except ValueError:
                config = {}
            for path in baseimages.dockerfiles(config if isinstance(config, dict) else {}, services):
                if path in found:
                    continue
                try:
                    with open(runtime.to_local(path), encoding="utf-8") as fh:
                        text = fh.read(512 * 1024)
                except OSError:
                    continue
                bumps = await asyncio.to_thread(baseimages.plan, path, text,
                                                lambda url: websearch.get_json(url, timeout=20))
                if bumps:
                    found[path] = (baseimages.apply(text, bumps), bumps)
        return found

    async def _tag_bumps(self, patch: vulns.ImagePatch) -> dict[str, tuple[str, str, str]]:
        """
        Obrazy z rejestru z nowszym tagiem w tej samej linii, wpisane wprost w pliku compose:
        nazwa celu -> (plik hosta, stary ref, nowy ref). Podbicie dopiero, gdy `pull` nie dal nowszego obrazu pod tagiem.
        """
        from backend.core import baseimages, websearch

        found: dict[str, tuple[str, str, str]] = {}
        for project in patch.projects:
            for target in project.targets:
                ref = str(target.meta.get("image", ""))
                image = baseimages.parse_ref(ref)
                if vulns.built_locally(target) or target.name in found or not image:
                    continue
                for path in project.files:
                    try:
                        with open(runtime.to_local(path), encoding="utf-8") as fh:
                            text = fh.read(512 * 1024)
                    except OSError:
                        continue
                    if not baseimages.compose_has(text, ref):
                        continue
                    tag = await asyncio.to_thread(baseimages.safe_tag, image[0], image[1],
                                                  lambda url: websearch.get_json(url, timeout=20))
                    if tag:
                        found[target.name] = (path, ref, ref[: -len(image[1])] + tag)
                    break
        return found

    async def _bump_tag(self, project: vulns.ComposeProject, bump: tuple[str, str, str], result: PartResult) -> bool:
        """Nowy tag w pliku compose i `pull`; gdy obrazu nie da sie pobrac, plik wraca do poprzedniej tresci."""
        from backend.core import baseimages

        path, old, new = bump
        try:
            with open(runtime.to_local(path), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            result.warnings.append(tr(f"{path}: nie moge odczytac pliku compose ({exc})",
                                      f"{path}: cannot read the compose file ({exc})"))
            return False
        self.emit(tr(f"podbijam obraz w {path}: {old} → {new}", f"bumping the image in {path}: {old} → {new}"))
        output, rc = await self._write_host(path, baseimages.compose_apply(text, old, new))
        if rc != 0:
            result.warnings.append(tr(f"{path}: nie udalo sie zapisac nowego tagu ({output[:200]})",
                                      f"{path}: could not write the new tag ({output[:200]})"))
            return False
        output, rc = await self.sh(self.host(project.command("pull --ignore-buildable")))
        if rc != 0:
            await self._write_host(path, text)
            result.warnings.append(tr(f"{new}: nie udalo sie pobrac obrazu (kod {rc}), plik compose bez zmian",
                                      f"{new}: could not pull the image (exit code {rc}), the compose file is unchanged"))
            return False
        result.tagged.append(f"{old} → {new}")
        return True

    async def _write_host(self, path: str, text: str) -> tuple[str, int]:
        """
        Plik hosta na nowo (ten sam plik — wlasciciel i prawa zostaja). W trybie docker host poza /root jest w kontenerze
        tylko do odczytu: tresc idzie przez katalog danych Pipe i kontener pomocniczy (`cat > plik`).
        """
        import errno

        local = runtime.to_local(path)
        try:
            with open(local, "w", encoding="utf-8") as fh:
                fh.write(text)
            await audit.log_file_write(self.interface + " [zalataj]", path, 0)
            return "", 0
        except OSError as exc:
            if exc.errno != errno.EROFS or not self._image:
                await audit.log_file_write(self.interface + " [zalataj]", path, 1)
                return str(exc), 1
        from backend.core import attachments

        tmp = memory.data_dir() / "tmp" / f"pipe-write-{secrets.token_hex(6)}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text(text, encoding="utf-8")
            source = attachments.host_path(tmp)
            if not source:
                return tr("nie znam sciezki katalogu danych Pipe na hoscie", "Pipe's data directory host path is unknown"), 1
            output, code = await self.sh(self.host(f"cat {shlex.quote(source)} > {shlex.quote(path)}"), timeout=60)
            return output, code
        finally:
            tmp.unlink(missing_ok=True)

    async def _images(self, part: Part) -> PartResult:
        result = PartResult(part)
        patch: vulns.ImagePatch = part.image_patch
        base = await self._base_bumps(patch)
        tags = await self._tag_bumps(patch)
        files = list(dict.fromkeys([*base, *(bump[0] for bump in tags.values())]))
        plan = safety.Plan(sites=True, inverse=list(patch.inverse), files=files, notes=[tr(
            "poprzednie obrazy zostaja pod tagiem pipe-rollback-* — /cofnij do nich wraca"
            + (" (i do poprzednich plikow Dockerfile i compose)" if files else ""),
            "the previous images stay under a pipe-rollback-* tag — /undo goes back to them"
            + (" (and to the previous Dockerfiles and compose files)" if files else ""))])
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
            for path, (text, bumps) in base.items():
                described = ", ".join(b.describe() for b in bumps)
                self.emit(tr(f"podbijam obraz bazowy w {path}: {described}", f"bumping the base image in {path}: {described}"))
                output, rc = await self._write_host(path, text)
                if rc == 0:
                    result.based += [b.describe() for b in bumps]
                else:
                    outputs.append(output)
                    result.warnings.append(tr(f"{path}: nie udalo sie zapisac nowego FROM ({output[:200]})",
                                              f"{path}: could not write the new FROM ({output[:200]})"))
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
                    if target.name in tags and await self._unchanged(target) \
                            and await self._bump_tag(project, tags[target.name], result):
                        result.refreshed.append(target.name)
                        continue
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

    async def _web_container(self, results: list[PartResult]) -> None:
        """
        Kontener strony /web (Caddy albo tunel; bez compose, ale Pipe zna jego opcje) — na samym koncu, po wszystkich
        czesciach: latanie zlecone z przegladarki przez /web idzie wlasnie przez ten kontener, wiec jego odtworzenie
        zrywa strone (tunel dostaje nowy adres). Latanie trwa dalej w tle, wynik przychodzi jako wiadomosc.
        """
        from backend.core import webpublic
        for result in results:
            patch = result.part.image_patch
            if result.part.kind != "images" or not patch or not patch.web or result.stopped:
                continue
            self.emit(tr("na koniec odswiezam kontener strony /web (pipe-web) — strona w przegladarce moze na chwile "
                         "zniknac, wynik przyjdzie tez jako wiadomosc",
                         "finally refreshing the /web page container (pipe-web) — the page in the browser may "
                         "disappear for a moment, the result also comes as a message"))
            changed, text = await webpublic.refresh_proxy()
            if changed is False:
                # nowszego obrazu nie ma: poprawki z repozytorium jego systemu i ten sam kontener na tym obrazie
                for target in patch.web:
                    await self._refresh_or_layer(target, result)
                if any(t.name in result.layered for t in patch.web):
                    changed, text = await webpublic.refresh_proxy(pull=False)
            if changed is None:
                result.warnings.append(text)
            result.output = "\n".join(o for o in (result.output, text) if o)
            if changed:
                result.refreshed += [t.name for t in patch.web if t.name not in result.layered]

    async def _unchanged(self, target: Any) -> bool:
        """`pull` zostawil pod tagiem ten sam obraz."""
        new = await self._inspect(str(target.meta.get("image", "")), "{{.Id}}")
        return bool(new) and new[:19] == str(target.meta.get("image_id", ""))[:19]

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
        self.done, self.ended = done, time.time()
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
        if runtime.kind() == "docker" and any(p.kind in ("system", "images", "apps") for p in self.job.parts):
            self._image = await hostexec.image()
        results: list[PartResult] = []
        for part in self.job.parts:
            runner = {"system": self._system, "apps": self._apps, "images": self._images, "server": self._server}[part.kind]
            result = await runner(part)
            results.append(result)
            if result.stopped:
                break
        await self._web_container(results)
        # sprzatanie tego serwera po latce na nim (zdalne serwery sprzataja swoje kroki apt)
        freed = await self._cleanup() if any(r.part.kind in ("system", "images", "apps") for r in results) else 0
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
                    scope = {"system": "all", "images": "images", "apps": "apps"}.get(result.part.kind,
                                                                                     f"server:{result.part.name}")
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
    if part.kind == "apps":
        if outcome and outcome.fixed:
            lines.append(tr(f"podbite: {len(outcome.fixed)} ({_names(outcome.fixed)})",
                            f"bumped: {len(outcome.fixed)} ({_names(outcome.fixed)})"))
        if outcome and outcome.unchanged:
            lines.append(tr(f"bez zmian: {len(outcome.unchanged)} ({_names(outcome.unchanged)}) — ograniczenie wersji w "
                            "manifescie (package.json, pyproject.toml, composer.json...) na to nie pozwala; zmien je recznie "
                            "(nie sprobuje znowu przez dobe)",
                            f"unchanged: {len(outcome.unchanged)} ({_names(outcome.unchanged)}) — the version constraint "
                            "in the manifest (package.json, pyproject.toml, composer.json...) does not allow it; change it "
                            "by hand (I will not try again for a day)"))
        if result.redeployed:
            lines.append(tr(f"przebudowane i uruchomione od nowa: {_names(result.redeployed)}",
                            f"rebuilt and restarted: {_names(result.redeployed)}"))
        if result.undeployed:
            lines.append(tr(f"bez projektu compose: {_names(result.undeployed)} — nowe wersje zadzialaja po zainstalowaniu "
                            "zaleznosci i ponownym wdrozeniu aplikacji",
                            f"no compose project: {_names(result.undeployed)} — the new versions take effect after the "
                            "dependencies are installed and the app is redeployed"))
        if result.entries and ((outcome and outcome.fixed) or result.redeployed or result.exit_code == 0):
            lines.append(tr("cofniecie: /cofnij " + ", /cofnij ".join(result.entries),
                            "undo: /undo " + ", /undo ".join(result.entries)))
        return lines
    if part.kind == "images":
        refreshed = [n for n in result.refreshed if not outcome or n not in outcome.unchanged]
        layered = [n for n in result.layered if not outcome or n not in outcome.unchanged]
        if result.based:
            lines.append(tr(f"obraz bazowy podbity w Dockerfile: {_names(result.based)}",
                            f"base image bumped in the Dockerfile: {_names(result.based)}"))
        if result.tagged:
            lines.append(tr(f"tag podbity w pliku compose: {_names(result.tagged)}",
                            f"tag bumped in the compose file: {_names(result.tagged)}"))
        if refreshed:
            lines.append(tr(f"nowszy obraz: {_names(refreshed)}", f"newer image: {_names(refreshed)}"))
        if layered:
            lines.append(tr(f"poprawki zainstalowane w obrazie (nowszego nie bylo): {_names(layered)}",
                            f"fixes installed inside the image (there was no newer one): {_names(layered)}"))
        if outcome and outcome.unchanged:
            built = {t.name for t in part.items if vulns.built_locally(t)}
            web = {t.name for t in (part.image_patch.web if part.image_patch else [])}
            if web & set(outcome.unchanged):
                names = sorted(web & set(outcome.unchanged))
                lines.append(tr(f"bez zmian: {_names(names)} (kontener /web) — obraz nie ma jeszcze poprawki, Pipe sprawdzi "
                                "go przy kazdym /zalataj", f"unchanged: {_names(names)} (the /web container) — the image "
                                "does not have the fix yet, Pipe checks it on every /patch"))
            for names, why in (([n for n in outcome.unchanged if n in built and n not in web],
                                tr("zmien FROM w Dockerfile na nowsza glowna wersje (w tej samej linii nowszej nie ma)",
                                   "change FROM in the Dockerfile to a newer major version (there is no newer one in "
                                   "the same line)")),
                               ([n for n in outcome.unchanged if n not in built and n not in web],
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
    entries = list(dict.fromkeys(e for r in results for e in [r.entry_id, *r.entries] if e))
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
_REMAINING = ("Aplikacje", "Aplikacja ", "Applications", "Application ", "Obrazy budowane", "Images built", "Obrazy z rejestru", "Registry images",
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


def app_projects(directory: str, containers: list[Any], skip: set[str] | frozenset[str] = frozenset()) -> list[vulns.ComposeProject]:
    """
    Projekty compose budowane z katalogu aplikacji: katalog roboczy projektu to ten katalog albo jego rodzic
    (aplikacja w backend/ projektu). Bez projektu samego Pipe (`up -d` zatrzymaloby latanie).
    """
    found: dict[str, vulns.ComposeProject] = {}
    for container in containers:
        project, workdir = str(getattr(container, "project", "") or ""), str(getattr(container, "workdir", "") or "")
        files = [str(f) for f in getattr(container, "compose_files", None) or []]
        if not project or project in skip or not workdir or not files:
            continue
        if not (directory == workdir or directory.startswith(workdir.rstrip("/") + "/")):
            continue
        if not vulns._COMPOSE_NAME.match(project) or not all(vulns._HOST_PATH.match(f) for f in files):
            continue
        entry = found.setdefault(project, vulns.ComposeProject(project, files))
        service = str(getattr(container, "service", "") or "")
        if service and vulns._COMPOSE_NAME.match(service) and service not in entry.services:
            entry.services.append(service)
    return list(found.values())


_current: Run | None = None


def current() -> Run | None:
    """Trwajace latanie (jedno naraz) albo None."""
    return _current if _current is not None and _current.done is None else None


def latest() -> Run | None:
    """Trwajace latanie albo to, ktore skonczylo sie przed chwila (dla klienta, ktoremu zerwalo polaczenie)."""
    if _current is not None and (_current.done is None or time.time() - _current.ended < RESULT_KEEP):
        return _current
    return None


def start(job: Job, *, interface: str, session_id: str = "", watcher: Any = None, agent: Any = None) -> Run:
    global _current
    run = Run(job, interface, session_id)
    _current = run
    run.task = asyncio.create_task(run.main(watcher, agent))
    return run
