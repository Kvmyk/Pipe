"""
Bezpiecznik — zmiana z planem: kopia, sprawdzenie przed, weryfikacja po, cofniecie.

Dla komendy albo zapisu pliku czekajacego na TAK Pipe sklada plan (pokazywany
w potwierdzeniu, wiec uzytkownik widzi go przed decyzja):

  kopia        pliki, ktore operacja zmieni (sed -i, tee, >, cp, mv, rm, chmod,
               write_file), stan gita, crontab — do dziennika zmian (core/journal.py)
  przed        walidacja konfiguracji przed przeladowaniem: nginx -t, sshd -t,
               caddy validate, apachectl configtest, docker compose config -q...
               Nieudane sprawdzenie = operacja NIE jest wykonywana.
  po           usluga aktywna, kontener dziala i nie jest unhealthy, projekt compose
               wstal, zmieniony plik konfiguracji przechodzi walidacje, strony, ktore
               odpowiadaly przed zmiana, odpowiadaja po niej
  cofniecie    nieudana weryfikacja zmiany samych plikow -> automatyczne przywrocenie
               kopii (SAFE_AUTO_ROLLBACK); pozostale -> /cofnij z komendami odwrotnymi

Plan powstaje z analizy komendy (ta sama leksyka co klasyfikator), bez LLM.
Komendy sprawdzajace sa skladane z szablonow z parametrami przez shlex.quote.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable

from backend.core import runtime
from backend.core.i18n import tr
from backend.core.events import Progress
from backend.core.security import split_command

# walidator konfiguracji uslugi (systemd) uruchamiany przed start/restart/reload
SERVICE_VALIDATORS: dict[str, str] = {
    "nginx": "nginx -t",
    "ssh": "sshd -t",
    "sshd": "sshd -t",
    "caddy": "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile",
    "apache2": "apachectl configtest",
    "httpd": "apachectl configtest",
    "haproxy": "haproxy -c -f /etc/haproxy/haproxy.cfg",
    "postfix": "postfix check",
}
SYSTEMCTL_INVERSE = {"start": "stop", "stop": "start", "enable": "disable", "disable": "enable",
                     "mask": "unmask", "unmask": "mask"}
SYSTEMCTL_ACTIVE_AFTER = {"start", "restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart"}
SYSTEMCTL_RELOADS = {"restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart"}
WEB_UNITS = {"nginx", "caddy", "apache2", "httpd", "haproxy", "traefik"}
GIT_MUTATING = {"pull", "merge", "rebase", "reset", "commit", "checkout", "switch", "cherry-pick", "revert", "am",
                "stash", "restore", "clean", "rm", "mv"}
PACKAGE_MANAGERS = {"apt", "apt-get", "dnf", "yum", "apk", "pacman", "zypper", "snap", "pip", "pip3", "npm"}
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
MAX_SITES = 8


@dataclass
class Check:
    label: str
    kind: str = "shell"          # shell | active | container | compose | json
    command: str = ""            # dla shell: komenda (exit 0 = ok, 127 = brak programu -> pominieto)
    target: str = ""             # jednostka / kontener / flagi compose / sciezka pliku
    retries: int = 0
    delay: float = 0.0


@dataclass
class Plan:
    files: list[str] = field(default_factory=list)            # sciezki hosta do skopiowania
    local_files: list[tuple[str, str]] = field(default_factory=list)   # (lokalna, etykieta) — np. pamiec Pipe
    inverse: list[str] = field(default_factory=list)
    pre: list[Check] = field(default_factory=list)
    post: list[Check] = field(default_factory=list)
    sites: bool = False
    auto_restore: bool = False
    reload_after_restore: list[str] = field(default_factory=list)
    git_repos: list[str] = field(default_factory=list)
    crontab: bool = False
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "Plan") -> None:
        for name in ("files", "local_files", "inverse", "pre", "post", "reload_after_restore", "git_repos", "notes"):
            current = getattr(self, name)
            for item in getattr(other, name):
                if item not in current:
                    current.append(item)
        self.sites = self.sites or other.sites
        self.auto_restore = self.auto_restore or other.auto_restore
        self.crontab = self.crontab or other.crontab

    @property
    def empty(self) -> bool:
        return not (self.files or self.local_files or self.inverse or self.pre or self.post or self.sites
                    or self.git_repos or self.crontab or self.notes)

    def describe(self) -> str:
        """Plan dla uzytkownika — czesc potwierdzenia."""
        from backend.core.text import visible

        lines = [tr("Bezpiecznik:", "Safety fuse:")]
        copies = [visible(f) for f in self.files] + [label for _, label in self.local_files]
        if self.git_repos:
            copies.append(tr("stan repozytorium git (HEAD)", "git repository state (HEAD)"))
        if self.crontab:
            copies.append("crontab")
        if copies:
            lines.append(tr("- kopia przed zmiana: ", "- backup before the change: ") + ", ".join(copies[:8])
                         + (" ..." if len(copies) > 8 else ""))
        if self.pre:
            lines.append(tr("- sprawdzenie przed (niepowodzenie = nie wykonam): ",
                            "- check before (failure = I will not run it): ") + "; ".join(c.label for c in self.pre))
        post = [c.label for c in self.post] + ([tr("strony, ktore dzialaja teraz, maja dzialac po zmianie",
                                                   "sites that work now must still work after the change")]
                                               if self.sites else [])
        if post:
            lines.append(tr("- weryfikacja po: ", "- verification after: ") + "; ".join(post))
        if self.auto_restore:
            lines.append(tr("- jesli weryfikacja nie przejdzie: przywroce pliki z kopii automatycznie",
                            "- if verification fails: I restore the files from the backup automatically")
                         + (tr(" i przeladuje ponownie", " and reload again") if self.reload_after_restore else ""))
        if self.inverse:
            lines.append(tr("- cofniecie pozniej (/cofnij): ", "- undo later (/undo): ")
                         + "; ".join(visible(c) for c in self.inverse))
        elif copies:
            lines.append(tr("- cofniecie pozniej: /cofnij", "- undo later: /undo"))
        for note in self.notes:
            lines.append(tr(f"- uwaga: {note}", f"- note: {note}"))
        return "\n".join(lines) if len(lines) > 1 else ""


# ─── Analiza komendy ────────────────────────────────────────────────────────

def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return []


def _redirect_targets(segment: str) -> list[str]:
    """Pliki z przekierowan `> plik`, `>> plik` (bez /dev/null i deskryptorow)."""
    import re

    targets = []
    for match in re.finditer(r"(?<![<&])\d?>>?\s*(?!&)(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)", segment):
        target = match.group(1).strip("'\"")
        if target and not target.startswith("/dev/"):
            targets.append(target)
    return targets


def _positional(tokens: list[str], value_flags: tuple[str, ...] = ()) -> list[str]:
    out, skip = [], False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in value_flags:
            skip = True
            continue
        if token.startswith("-") and token != "-":
            continue
        out.append(token)
    return out


def _host(path: str, cwd_host: str) -> str:
    return runtime.to_host(path, cwd_host)


def _files_for_program(tokens: list[str], cwd_host: str) -> tuple[list[str], list[str]]:
    """(pliki do skopiowania, uwagi) dla programu zmieniajacego pliki."""
    program = os.path.basename(tokens[0])
    args = tokens[1:]
    notes: list[str] = []
    files: list[str] = []
    if program == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
        script_given = any(a in ("-e", "--expression", "-f", "--file") or a.startswith(("--expression=", "--file="))
                           for a in args)
        positional = _positional(args, ("-e", "--expression", "-f", "--file", "-l", "--line-length"))
        files = positional if script_given else positional[1:]
    elif program == "tee":
        files = _positional(args)
    elif program in ("cp", "install", "ln"):
        positional = _positional(args, ("-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g", "--group",
                                        "-S", "--suffix"))
        if len(positional) >= 2:
            dest, sources = positional[-1], positional[:-1]
            dest_local = runtime.to_local(_host(dest, cwd_host))
            if os.path.isdir(dest_local):
                files = [os.path.join(dest, os.path.basename(s.rstrip("/"))) for s in sources]
            else:
                files = [dest]
    elif program == "mv":
        positional = _positional(args, ("-t", "--target-directory", "-S", "--suffix"))
        if len(positional) >= 2:
            dest, sources = positional[-1], positional[:-1]
            dest_local = runtime.to_local(_host(dest, cwd_host))
            targets = [os.path.join(dest, os.path.basename(s.rstrip("/"))) for s in sources] \
                if os.path.isdir(dest_local) else [dest]
            files = sources + targets
    elif program in ("rm", "unlink", "shred", "truncate", "touch"):
        files = _positional(args, ("-s", "--size", "-r", "--reference", "-d", "--date", "-t"))
        if program == "rm" and any(a.startswith("-") and ("r" in a.lower() or a == "--recursive") for a in args):
            notes.append(tr("rm -r: katalogi nie sa kopiowane (tylko pojedyncze pliki)",
                            "rm -r: directories are not backed up (single files only)"))
    elif program in ("chmod", "chown", "chgrp"):
        positional = _positional(args, ("--reference",))
        files = positional[1:] if not any(a.startswith("--reference") for a in args) else positional
    return [_host(f, cwd_host) for f in files if f and "*" not in f and "?" not in f], notes


def _compose_flags(tokens: list[str], start: int) -> tuple[list[str], str]:
    """Flagi globalne `docker compose` (-f, -p, --project-directory...) i podkomenda."""
    flags: list[str] = []
    i = start
    while i < len(tokens):
        token = tokens[i]
        if token in ("-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile"):
            flags += tokens[i:i + 2]
            i += 2
            continue
        if token.startswith(("--file=", "--project-name=", "--project-directory=", "--env-file=", "--profile=")):
            flags.append(token)
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        return flags, token
    return flags, ""


def validators_for_file(host_path: str, containers: list[Any] | None = None) -> list[Check]:
    """Walidatory konfiguracji dla zmienionego pliku (sciezka hosta)."""
    native = runtime.kind() == "native"
    local = runtime.to_local(host_path)
    name = os.path.basename(host_path)
    checks: list[Check] = []

    def in_container(program_hint: str, command: str) -> Check | None:
        for container in containers or []:
            image = f"{container.image} {container.name}".lower()
            if program_hint not in image or container.state != "running":
                continue
            for source, _destination in getattr(container, "mounts", []):
                if host_path == source or host_path.startswith(source.rstrip("/") + "/"):
                    return Check(tr(f"{command} w kontenerze {container.name}", f"{command} in container {container.name}"), "shell",
                                 f"docker exec {shlex.quote(container.name)} {command}")
        return None

    if host_path.startswith("/etc/nginx/"):
        check = Check("nginx -t", "shell", "nginx -t") if native else in_container("nginx", "nginx -t")
        if check:
            checks.append(check)
    elif host_path.startswith("/etc/ssh/sshd_config"):
        if native:
            checks.append(Check("sshd -t", "shell", "sshd -t"))
    elif host_path.startswith("/etc/caddy/"):
        command = "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
        check = Check("caddy validate", "shell", command) if native else in_container("caddy", command)
        if check:
            checks.append(check)
    elif host_path.startswith("/etc/haproxy/") and native:
        checks.append(Check("haproxy -c", "shell", f"haproxy -c -f {shlex.quote(host_path)}"))
    elif host_path.startswith(("/etc/sudoers", "/etc/sudoers.d/")) and native:
        checks.append(Check("visudo -c", "shell", f"visudo -c -f {shlex.quote(host_path)}"))
    elif host_path == "/etc/fstab":
        checks.append(Check("findmnt --verify", "shell", f"findmnt --verify --tab-file {shlex.quote(local)}"))
    elif name in COMPOSE_FILES:
        checks.append(Check("docker compose config", "shell", f"docker compose -f {shlex.quote(local)} config -q"))
    elif name.endswith(".json"):
        checks.append(Check(tr(f"poprawny JSON: {name}", f"valid JSON: {name}"), "json", target=local))
    return checks


def _unsupported_validation_note(host_path: str) -> str:
    if runtime.kind() == "native":
        return ""
    if host_path.startswith(("/etc/nginx/", "/etc/ssh/sshd_config", "/etc/sudoers", "/etc/caddy/")):
        return tr(f"skladni {host_path} nie sprawdze z kontenera Pipe (program dziala na hoscie) — "
                  "po zmianie zweryfikuj ja na hoscie, zanim przeladujesz usluge",
                  f"I cannot check the syntax of {host_path} from the Pipe container (the program runs on the host) — "
                  "verify it on the host after the change, before you reload the service")
    return ""


def plan_command(command: str, cwd_host: str = "/", containers: list[Any] | None = None) -> Plan:
    """Plan dla komendy shell wykonywanej lokalnie (po TAK)."""
    plan = Plan()
    segments, _writes, _dynamic = split_command(command)
    by_name = {c.name: c for c in containers or []}
    for segment in segments:
        tokens = _tokens(segment)
        if not tokens:
            continue
        for target in _redirect_targets(segment):
            plan.files.append(_host(target, cwd_host))
        program = os.path.basename(tokens[0])
        if program in ("sudo", "doas") and len(tokens) > 1:
            tokens = tokens[1:]
            program = os.path.basename(tokens[0])
        files, notes = _files_for_program(tokens, cwd_host)
        plan.files += files
        plan.notes += notes

        if program == "crontab" and len(tokens) > 1 and tokens[1] != "-l":
            plan.crontab = True

        elif program in ("systemctl", "service"):
            positional = _positional(tokens[1:])
            if program == "service" and len(positional) >= 2:
                units, action = [positional[0]], positional[1]
            elif positional:
                action, units = positional[0], positional[1:]
            else:
                continue
            for unit in units:
                base = unit.removesuffix(".service")
                if action in SYSTEMCTL_INVERSE:
                    inverse_action = SYSTEMCTL_INVERSE[action]
                    plan.inverse.append(f"systemctl {inverse_action} {shlex.quote(unit)}")
                if action in SYSTEMCTL_ACTIVE_AFTER or (action == "enable" and "--now" in tokens):
                    validator = SERVICE_VALIDATORS.get(base)
                    if validator:
                        plan.pre.append(Check(validator, "shell", validator))
                    plan.post.append(Check(tr(f"usluga {base} aktywna", f"service {base} active"), "active", target=unit, retries=5, delay=2))
                if action in SYSTEMCTL_RELOADS:
                    plan.reload_after_restore.append(f"systemctl {action} {shlex.quote(unit)}")
                if base in WEB_UNITS:
                    plan.sites = True

        elif program in ("nginx", "apachectl", "apache2ctl") and ("-s" in tokens or "graceful" in tokens
                                                                  or "restart" in tokens):
            validator = "nginx -t" if program == "nginx" else "apachectl configtest"
            plan.pre.append(Check(validator, "shell", validator))
            plan.reload_after_restore.append(segment)
            plan.sites = True

        elif program in ("docker", "docker-compose"):
            _plan_docker(tokens, segment, plan, by_name)

        elif program == "git":
            repo = cwd_host
            args = tokens[1:]
            i = 0
            while i < len(args) and args[i].startswith("-"):
                if args[i] == "-C" and i + 1 < len(args):
                    repo = _host(args[i + 1], cwd_host)
                    i += 2
                    continue
                i += 2 if args[i] in ("--git-dir", "--work-tree", "-c") else 1
            sub = args[i] if i < len(args) else ""
            if sub in GIT_MUTATING:
                plan.git_repos.append(runtime.to_local(repo))
                if sub == "clean":
                    plan.notes.append(tr("git clean usuwa nieśledzone pliki — tego nie cofne",
                                          "git clean deletes untracked files — I cannot undo that"))

        elif program in PACKAGE_MANAGERS and any(t in tokens for t in ("install", "remove", "purge", "upgrade",
                                                                      "dist-upgrade", "full-upgrade", "add", "del")):
            plan.notes.append(tr("pakietow nie cofam automatycznie — co sie zmienilo, pokaze /zmiany",
                                  "packages are not rolled back automatically — /changes shows what changed"))

        elif program == "kubectl" and any(t in tokens for t in ("apply", "delete", "scale", "set", "patch", "edit",
                                                                "replace")):
            plan.notes.append(tr("zmian w klastrze nie cofam automatycznie (dla deploymentu: kubectl rollout undo)",
                                  "cluster changes are not rolled back automatically (for a deployment: kubectl rollout undo)"))

    # zmienione pliki konfiguracji: walidacja po zmianie + automatyczne przywrocenie
    seen: set[str] = set()
    plan.files = [f for f in plan.files if not (f in seen or seen.add(f))]
    for path in plan.files:
        validators = validators_for_file(path, containers)
        if validators:
            plan.post += [v for v in validators if v not in plan.post]
            plan.auto_restore = True
        note = _unsupported_validation_note(path)
        if note and note not in plan.notes:
            plan.notes.append(note)
        if path.startswith(("/etc/nginx/", "/etc/caddy/", "/etc/haproxy/", "/etc/apache2/", "/etc/httpd/")) \
                or os.path.basename(path) in COMPOSE_FILES:
            plan.sites = True
    return plan


def _plan_docker(tokens: list[str], segment: str, plan: Plan, by_name: dict[str, Any]) -> None:
    from backend.core.infra import PROXY_HINTS

    if os.path.basename(tokens[0]) == "docker-compose":
        compose_start = 1
    elif len(tokens) > 1 and tokens[1] == "compose":
        compose_start = 2
    else:
        compose_start = 0
    if compose_start:
        flags, sub = _compose_flags(tokens, compose_start)
        base = "docker compose " + " ".join(shlex.quote(f) for f in flags)
        if sub in ("up", "restart", "start", "create", "run"):
            plan.pre.append(Check("docker compose config", "shell", f"{base.rstrip()} config -q"))
        if sub in ("up", "restart", "start"):
            plan.post.append(Check(tr("kontenery projektu dzialaja", "project containers are running"), "compose",
                                   target=" ".join(shlex.quote(f) for f in flags), retries=12, delay=5))
            plan.sites = True
        if sub in ("down", "stop"):
            plan.inverse.append(f"{base.rstrip()} up -d")
            plan.sites = True
        if sub in ("down",) and ("-v" in tokens or "--volumes" in tokens):
            plan.notes.append(tr("down -v usuwa wolumeny z danymi — tego nie cofne",
                                  "down -v deletes data volumes — I cannot undo that"))
        if sub in ("up", "pull"):
            plan.notes.append(tr("poprzedniej wersji obrazow nie przywracam automatycznie — /zmiany pokaze, co sie zmienilo",
                                  "previous image versions are not restored automatically — /changes shows what changed"))
        return
    sub = tokens[1] if len(tokens) > 1 else ""
    names = [t for t in _positional(tokens[2:], ("-t", "--time", "-s", "--signal"))]
    if sub == "exec":
        # docker exec [opcje] KONTENER komenda...
        rest = tokens[2:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file") else 1
        if i < len(rest):
            container, inner = rest[i], rest[i + 1:]
            if inner[:1] == ["nginx"] and "-s" in inner:
                plan.pre.append(Check(tr(f"nginx -t w kontenerze {container}", f"nginx -t in container {container}"), "shell",
                                      f"docker exec {shlex.quote(container)} nginx -t"))
                plan.reload_after_restore.append(segment)
                plan.sites = True
        return
    for name in names:
        known = by_name.get(name)
        is_proxy = known is not None and any(h in f"{known.image} {known.name}".lower() for h in PROXY_HINTS)
        if sub == "stop" or sub == "kill":
            plan.inverse.append(f"docker start {shlex.quote(name)}")
        elif sub == "start":
            plan.inverse.append(f"docker stop {shlex.quote(name)}")
        if sub in ("start", "restart"):
            plan.post.append(Check(tr(f"kontener {name} dziala", f"container {name} is running"), "container", target=name, retries=10, delay=3))
        if sub == "rm":
            plan.notes.append(tr(f"usunietego kontenera {name} nie odtworze (dane w wolumenach zostaja)",
                                  f"I cannot recreate the removed container {name} (data in volumes stays)"))
        if sub in ("stop", "kill", "restart", "rm") or is_proxy:
            plan.sites = True


def plan_write(host_path: str, containers: list[Any] | None = None) -> Plan:
    """Plan dla write_file: kopia, walidacja po zapisie, automatyczne przywrocenie."""
    plan = Plan(files=[host_path])
    plan.post = validators_for_file(host_path, containers)
    plan.auto_restore = bool(plan.post)
    note = _unsupported_validation_note(host_path)
    if note:
        plan.notes.append(note)
    return plan


def needs_containers(command: str) -> bool:
    return any(word in command for word in ("docker", "/etc/nginx", "/etc/caddy", "nginx", "caddy"))


async def plan_for_command(command: str, cwd_host: str) -> Plan:
    containers = None
    if needs_containers(command):
        from backend.core import infra
        containers = await infra.docker_containers()
    return plan_command(command, cwd_host, containers)


async def plan_for_write(host_path: str) -> Plan:
    containers = None
    if host_path.startswith(("/etc/nginx/", "/etc/caddy/")):
        from backend.core import infra
        containers = await infra.docker_containers()
    return plan_write(host_path, containers)


# ─── Wykonanie ──────────────────────────────────────────────────────────────

async def run_check(check: Check, cwd: str | None = None) -> tuple[str, str]:
    """('ok' | 'fail' | 'skip', szczegoly). Ponawia `retries` razy co `delay` s."""
    from backend.core import executor

    attempts = check.retries + 1
    detail = ""
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(check.delay)
        if check.kind == "json":
            try:
                with open(check.target, encoding="utf-8") as fh:
                    json.load(fh)
                return "ok", ""
            except (OSError, ValueError) as exc:
                return "fail", str(exc)
        if check.kind == "active":
            out, err, code = await executor.execute(f"systemctl is-active {shlex.quote(check.target)}", timeout=15)
            if code == 127:
                return "skip", tr("brak systemctl", "no systemctl")
            state = out.strip()
            if code == 0 and state == "active":
                return "ok", ""
            detail = tr(f"stan: {state or err.strip()}", f"state: {state or err.strip()}")
            continue
        if check.kind == "container":
            fmt = "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
            out, err, code = await executor.execute(
                f"docker inspect -f {shlex.quote(fmt)} {shlex.quote(check.target)}", timeout=15)
            if code != 0:
                detail = (err or out).strip()[:200]
                continue
            status, _, health = out.strip().partition(" ")
            if status == "running" and health not in ("unhealthy", "starting"):
                return "ok", ""
            detail = f"{status} {health}".strip()
            continue
        if check.kind == "compose":
            fmt = shlex.quote("{{.Name}}\\t{{.State}}\\t{{.Health}}")
            flags = f" {check.target}" if check.target else ""
            out, err, code = await executor.execute(f"docker compose{flags} ps -a --format {fmt}", cwd=cwd, timeout=20)
            if code != 0:
                detail = (err or out).strip()[:200]
                continue
            bad = []
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and (parts[1] != "running" or (len(parts) > 2 and parts[2] in ("unhealthy", "starting"))):
                    bad.append(" ".join(p for p in parts if p))
            if not bad:
                return "ok", ""
            detail = "; ".join(bad[:5])
            continue
        out, err, code = await executor.execute(check.command, cwd=cwd, timeout=60)
        if code == 127:
            return "skip", tr(f"brak programu ({check.command.split()[0]})", f"program not found ({check.command.split()[0]})")
        if code == 0:
            return "ok", ""
        detail = (err or out).strip()[-600:]
    return "fail", detail


async def _site_states(domains: list[str]) -> dict[str, bool]:
    from backend.core import checks

    results = await asyncio.gather(*(checks.check_site(d) for d in domains), return_exceptions=True)
    return {d: (not isinstance(r, Exception) and r.ok) for d, r in zip(domains, results)}


@dataclass
class Outcome:
    text: str
    exit_code: int
    entry_id: str = ""


async def guarded(
    plan: Plan,
    operation: Callable[[], Awaitable[tuple[str, int]]],
    *,
    interface: str,
    tool: str,
    description: str,
    cwd: str | None = None,
    auto_restore: bool = True,
    sites_enabled: bool = True,
    site_ignore: set[str] = frozenset(),
) -> AsyncGenerator[Progress | Outcome, None]:
    """
    Wykonuje operacje wedlug planu. Yielduje Progress (na biezaco) i na koncu
    jeden Outcome — tekst wyniku dla modelu.
    """
    from backend.core import checks, journal

    files = [(runtime.to_local(f), f) for f in plan.files] + list(plan.local_files)
    entry = await journal.begin(interface, tool, description, files=files, inverse=plan.inverse, notes=plan.notes,
                                git_repos=plan.git_repos, crontab=plan.crontab)
    source = f"bezpiecznik:{entry.id}"

    for check in plan.pre:
        yield Progress(tr(f"sprawdzam przed zmiana: {check.label}", f"checking before the change: {check.label}"), source=source)
        status, detail = await run_check(check, cwd)
        if status == "fail":
            journal.finish(entry, "aborted", verify=f"{check.label}: {detail}")
            yield Outcome(tr(f"NIE WYKONANO: sprawdzenie przed zmiana nie przeszlo ({check.label}).\n{detail}\n"
                             "Stan serwera sie nie zmienil. Popraw przyczyne i sprobuj ponownie.",
                             f"NOT EXECUTED: the pre-change check failed ({check.label}).\n{detail}\n"
                             "The server state did not change. Fix the cause and try again."), 1, entry.id)
            return

    domains: list[str] = []
    before: dict[str, bool] = {}
    if plan.sites and sites_enabled:
        try:
            domains = (await checks.discover_domains(site_ignore))[:MAX_SITES]
            if domains:
                yield Progress(tr("sprawdzam strony przed zmiana: ", "checking sites before the change: ") + ", ".join(domains),
                               source=source)
                before = await _site_states(domains)
        except Exception:
            domains = []

    result, exit_code = await operation()

    failures: list[str] = []
    skipped: list[str] = []
    for check in plan.post:
        yield Progress(tr(f"weryfikuje: {check.label}", f"verifying: {check.label}"), source=source)
        status, detail = await run_check(check, cwd)
        if status == "fail":
            failures.append(f"{check.label}: {detail}".rstrip(": "))
        elif status == "skip":
            skipped.append(f"{check.label} ({detail})")
    working_before = [d for d in domains if before.get(d)]
    if working_before:
        yield Progress(tr("sprawdzam strony po zmianie", "checking sites after the change"), source=source)
        after: dict[str, bool] = {}
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(5)
            after = await _site_states(working_before)
            if all(after.values()):
                break
        failures += [tr(f"strona {d} dzialala przed zmiana, a teraz nie odpowiada poprawnie",
                        f"site {d} worked before the change and does not respond correctly now")
                     for d in working_before if not after.get(d)]

    lines = [result]
    status = "done" if exit_code == 0 else "failed"
    if failures:
        lines.append(tr("WERYFIKACJA NIE PRZESZLA:\n", "VERIFICATION FAILED:\n") + "\n".join(f"- {f}" for f in failures))
        backed = any(f.blob or not f.existed for f in entry.files)
        if plan.auto_restore and auto_restore and backed:
            yield Progress(tr("przywracam poprzednia wersje plikow", "restoring the previous file versions"), source=source)
            restored = journal.restore_files(entry)
            lines.append(tr("Przywrocilem pliki z kopii:\n", "Restored files from the backup:\n")
                         + "\n".join(f"- {r}" for r in restored))
            from backend.core import executor
            for command in plan.reload_after_restore:
                out, err, code = await executor.execute(command, cwd=cwd, timeout=120)
                lines.append(tr(f"Ponownie: {command} -> exit {code}", f"Again: {command} -> exit {code}"))
            status = "restored"
        else:
            lines.append(tr(f"Zmiana zostala. Cofniecie: /cofnij {entry.id}",
                            f"The change was kept. Undo: /undo {entry.id}")
                         + (tr(f" (odwroci: {'; '.join(plan.inverse)})", f" (reverses: {'; '.join(plan.inverse)})")
                            if plan.inverse else ""))
            status = "failed"
    elif plan.post or working_before:
        verified = [c.label for c in plan.post if not any(c.label in s for s in skipped)]
        if working_before:
            verified.append(tr(f"strony dzialaja ({len(working_before)})", f"sites work ({len(working_before)})"))
        lines.append(tr("Zweryfikowano: ", "Verified: ") + "; ".join(verified) + ".")
    if skipped:
        lines.append(tr("Nie sprawdzono: ", "Not checked: ") + "; ".join(skipped) + ".")
    if status != "restored":
        lines.append(tr(f"Dziennik zmian: #{entry.id} — cofniecie: /cofnij {entry.id}.",
                        f"Change journal: #{entry.id} — undo: /undo {entry.id}."))
    journal.finish(entry, status, exit_code, "; ".join(failures))
    yield Outcome("\n".join(lines), exit_code if not failures else (exit_code or 1), entry.id)
