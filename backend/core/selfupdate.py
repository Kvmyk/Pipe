"""
Aktualizacja samego Pipe ("zaktualizuj sie") — narzedzie pipe_update.

Agent w kontenerze nie moze sam przebudowac swojego kontenera: proces, ktory uruchomilby
`docker compose up -d --build`, zginalby w polowie razem z kontenerem. Dlatego aktualizacje
wykonuje KONTENER POMOCNICZY, uruchomiony z tego samego obrazu i odlaczony od agenta:

    docker run -d --name pipe-update-<czas> -v docker.sock -v <repo>:<repo> <obraz agenta>
        sh -c 'sleep; git pull --ff-only; docker-compose -p <projekt> up -d --build <uslugi>'

Repozytorium jest montowane pod TA SAMA sciezka co na hoscie — compose wylicza z niej sciezki
woluminow, wiec musza byc sciezkami hosta. Projekt, katalog i uslugi bierzemy z etykiet compose
wlasnego kontenera, wiec przebudowywane sa tylko uslugi, ktore juz dzialaja (np. bez bota
Telegrama, jesli nie byl uruchomiony).

Model nie ma wplywu na to, co sie wykona: narzedzie nie przyjmuje adresu, galezi ani komendy.
Kod przychodzi wylacznie z `origin` repozytorium, w ktorym Pipe jest zainstalowany, przez
`git pull --ff-only`, i tylko po potwierdzeniu przez administratora.

Kto zlecil aktualizacje i z jakiej wersji, zapisujemy w etykietach kontenera pomocniczego.
`follow()` czeka na jego koniec i wysyla wynik jako przypomnienie (dociera takze wtedy, gdy klient
podlaczy sie dopiero po restarcie): w starym procesie, jesli przebudowa niczego nie zmienila albo
sie nie udala, a po restarcie — w nowym (`resume()` przy starcie serwera).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.core import memory, reminders, runtime
from backend.core.i18n import tr
from backend.version import VERSION

# Samodzielny plik compose w obrazie (backend/Dockerfile). Celowo NIE jako wtyczka `docker compose`:
# uruchomiony z wnetrza kontenera na projektach z /hostfs liczylby sciezki woluminow od /hostfs.
COMPOSE_BIN = "/usr/local/libexec/pipe/docker-compose"
LABEL = "pipe.update"
START_DELAY = 20            # s — agent zdazy odpowiedziec, zanim jego kontener zostanie wymieniony
WAIT_TIMEOUT = 1800
LOG_LINES = 8


class UpdateError(Exception):
    """Aktualizacji nie da sie przeprowadzic w tym srodowisku (z czytelnym powodem)."""


@dataclass
class Install:
    image: str
    project: str
    workdir: str                 # katalog projektu compose na hoscie (…/backend)
    root: str                    # korzen repozytorium na hoscie
    config_files: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)


async def _run(*args: str, timeout: float = 30) -> tuple[str, int]:
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return str(exc), 127
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return tr("przekroczono limit czasu", "timed out"), 124
    return output.decode("utf-8", errors="replace").strip(), process.returncode or 0


def _git(root: str, *args: str) -> tuple[str, ...]:
    # katalog repo moze nalezec do innego uzytkownika niz proces w kontenerze
    return ("git", "-c", "safe.directory=*", "-C", runtime.to_local(root), *args)


def manual_steps() -> str:
    if runtime.kind() == "native":
        return tr("W trybie native zaktualizuj recznie na serwerze: w katalogu repozytorium `git pull`, potem "
                  "`sudo systemctl restart pipe` (po zmianie zaleznosci: `pip install -r backend/requirements.txt` w venv).",
                  "In native mode update by hand on the server: `git pull` in the repository directory, then "
                  "`sudo systemctl restart pipe` (after dependency changes: `pip install -r backend/requirements.txt` in the venv).")
    if runtime.kind() == "kubernetes":
        return tr("W Kubernetesie aktualizacja to nowy obraz: zbuduj go i wdroz (`kubectl apply -k ...` / `kubectl rollout restart`).",
                  "On Kubernetes an update means a new image: build and deploy it (`kubectl apply -k ...` / `kubectl rollout restart`).")
    return tr("Na serwerze: `cd <repozytorium> && git pull && cd backend && docker compose up -d --build`.",
              "On the server: `cd <repository> && git pull && cd backend && docker compose up -d --build`.")


async def locate() -> Install:
    """Gdzie Pipe jest zainstalowany — z etykiet compose wlasnego kontenera."""
    if runtime.kind() != "docker":
        raise UpdateError(manual_steps())
    output, code = await _run("docker", "inspect", socket.gethostname())
    try:
        info = json.loads(output)[0] if code == 0 else None
    except (json.JSONDecodeError, IndexError, TypeError):
        info = None
    if not info:
        raise UpdateError(tr("Nie moge odczytac wlasnego kontenera przez Dockera (brak docker.sock?). ",
                             "Cannot inspect my own container through Docker (no docker.sock?). ") + manual_steps())
    labels = (info.get("Config") or {}).get("Labels") or {}
    project, workdir = labels.get("com.docker.compose.project", ""), labels.get("com.docker.compose.project.working_dir", "")
    if not project or not workdir:
        raise UpdateError(tr("Ten kontener nie zostal uruchomiony przez docker compose. ",
                             "This container was not started by docker compose. ") + manual_steps())
    root = str(Path(workdir).parent)
    if not (Path(runtime.to_local(root)) / ".git").exists():
        raise UpdateError(tr(f"Katalog {root} nie jest repozytorium git — nie mam skad pobrac zmian. ",
                             f"{root} is not a git repository — nothing to pull from. ") + manual_steps())
    if not Path(COMPOSE_BIN).exists():
        raise UpdateError(tr("Ten obraz nie ma jeszcze narzedzia do samodzielnej aktualizacji — pierwszy raz zrob to recznie. ",
                             "This image does not have the self-update tooling yet — do it by hand the first time. ")
                          + manual_steps())
    listed, _ = await _run("docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                           "--format", '{{.Label "com.docker.compose.service"}}')
    services = sorted({line.strip() for line in listed.splitlines() if line.strip()})
    files = [f for f in labels.get("com.docker.compose.project.config_files", "").split(",") if f.strip()]
    return Install(image=str(info.get("Image") or (info.get("Config") or {}).get("Image") or ""), project=project,
                   workdir=workdir, root=root, config_files=files,
                   services=services or [labels.get("com.docker.compose.service", "vps-agent")])


async def status() -> str:
    """Wersja, galaz, czy sa nowsze zmiany na zdalnym repozytorium. Niczego nie zmienia."""
    install = await locate()
    head, _ = await _run(*_git(install.root, "rev-parse", "HEAD"))
    branch, _ = await _run(*_git(install.root, "rev-parse", "--abbrev-ref", "HEAD"))
    dirty, _ = await _run(*_git(install.root, "status", "--porcelain", "--untracked-files=no"))
    remote, code = await _run(*_git(install.root, "ls-remote", "origin", f"refs/heads/{branch}"), timeout=30)
    remote_head = remote.split()[0] if code == 0 and remote.split() else ""
    lines = [tr(f"Pipe {VERSION}, repozytorium {install.root}, galaz {branch}, commit {head[:7]}.",
                f"Pipe {VERSION}, repository {install.root}, branch {branch}, commit {head[:7]}.")]
    if not remote_head:
        lines.append(tr("Nie udalo sie sprawdzic zdalnego repozytorium (siec albo dostep) — stan nieznany.",
                        "Could not check the remote repository (network or access) — state unknown."))
    elif remote_head == head:
        lines.append(tr("Kod jest aktualny wzgledem zdalnego repozytorium. Jesli pobrano go juz wczesniej, a kontener "
                        "dziala na starym obrazie, operation=apply przebuduje go.",
                        "The code is up to date with the remote repository. If it was pulled earlier and the container "
                        "still runs an old image, operation=apply will rebuild it."))
    else:
        lines.append(tr(f"Na zdalnym repozytorium jest nowszy commit ({remote_head[:7]}) — dostepna aktualizacja.",
                        f"The remote repository has a newer commit ({remote_head[:7]}) — an update is available."))
    if dirty.strip():
        lines.append(tr("UWAGA: w repozytorium sa lokalne zmiany w plikach — `git pull` moze sie nie udac.",
                        "WARNING: the repository has local file changes — `git pull` may fail."))
    lines.append(tr(f"Uslugi compose ({install.project}): {', '.join(install.services)}.",
                    f"Compose services ({install.project}): {', '.join(install.services)}."))
    return "\n".join(lines)


def script(install: Install) -> str:
    """Co wykona kontener pomocniczy. Wartosci pochodza z etykiet Dockera — kazda osobno cytowana."""
    compose = [COMPOSE_BIN, "-p", install.project]
    for path in install.config_files:
        compose += ["-f", path]
    compose += ["up", "-d", "--build", *install.services]
    return (f"set -e; sleep {START_DELAY}; cd {shlex.quote(install.root)}; "
            "git -c safe.directory='*' pull --ff-only; "
            f"cd {shlex.quote(install.workdir)}; {' '.join(shlex.quote(part) for part in compose)}; echo PIPE_UPDATE_OK")


async def _helpers() -> list[dict]:
    output, code = await _run("docker", "ps", "-a", "--filter", f"label={LABEL}=1", "--format", "{{.Names}}\t{{.State}}")
    if code != 0:
        return []
    return [{"name": name, "state": state} for name, _, state in (line.partition("\t") for line in output.splitlines()) if name]


async def start(install: Install, interface: str) -> str:
    """Uruchamia kontener pomocniczy i zwraca jego nazwe."""
    for helper in await _helpers():
        if helper["state"] == "running":
            raise UpdateError(tr(f"Aktualizacja juz trwa (kontener {helper['name']}).",
                                 f"An update is already running (container {helper['name']})."))
        await _run("docker", "rm", "-f", helper["name"])
    name = f"pipe-update-{int(time.time())}"
    command = ["docker", "run", "-d", "--name", name, "--label", f"{LABEL}=1", "--label", f"{LABEL}.to={interface}",
               "--label", f"{LABEL}.from={VERSION}",
               # Obraz zbudowany przez compose niesie etykiety projektu — bez wyzerowania compose uznalby
               # kontener pomocniczy za kolejna kopie uslugi i mogl go usunac w trakcie przebudowy.
               "--label", "com.docker.compose.project=", "--label", "com.docker.compose.service=", "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", f"{install.root}:{install.root}"]
    remote, _ = await _run(*_git(install.root, "remote", "get-url", "origin"))
    if remote.startswith(("git@", "ssh://")) and Path(runtime.to_local("/root/.ssh")).is_dir():
        command += ["-v", "/root/.ssh:/root/.ssh:ro"]            # repozytorium po SSH — te same klucze co agent
    command += ["--entrypoint", "sh", install.image, "-c", script(install)]
    output, code = await _run(*command, timeout=120)
    if code != 0:
        raise UpdateError(tr(f"Nie udalo sie uruchomic kontenera aktualizacji: {output[-400:]}",
                             f"Could not start the update container: {output[-400:]}"))
    return name


def result_text(exit_code: int, old: str, log: str, restarted: bool) -> str:
    tail = " | ".join(line.strip() for line in log.splitlines()[-LOG_LINES:] if line.strip())[-500:]
    tail = memory.redact_secrets(tail)
    if exit_code != 0:
        return tr(f"Aktualizacja Pipe nie powiodla sie (kod {exit_code}) — dziala dalej wersja {VERSION}. Koniec logu: {tail}",
                  f"The Pipe update failed (code {exit_code}) — version {VERSION} keeps running. End of the log: {tail}")
    if restarted and old != VERSION:
        return tr(f"Pipe zaktualizowany: {old} -> {VERSION}. Backend dziala na nowym obrazie.",
                  f"Pipe updated: {old} -> {VERSION}. The backend runs on the new image.")
    if restarted:
        return tr(f"Pipe przebudowany i uruchomiony ponownie (wersja {VERSION}).",
                  f"Pipe rebuilt and restarted (version {VERSION}).")
    return tr(f"Aktualizacja zakonczona — backend nie wymagal wymiany (wersja {VERSION}, kod byl aktualny).",
              f"Update finished — the backend did not need replacing (version {VERSION}, the code was current).")


async def follow(name: str, notify, *, restarted: bool) -> None:
    """Czeka na koniec kontenera pomocniczego, wysyla wynik zlecajacemu i sprzata."""
    waited, code = await _run("docker", "wait", name, timeout=WAIT_TIMEOUT)
    if code != 0:
        return                                   # kontener zniknal albo trwa ponad limit — nie zgadujemy wyniku
    try:
        exit_code = int(waited.splitlines()[-1])
    except (ValueError, IndexError):
        exit_code = 1
    inspected, _ = await _run("docker", "inspect", "--format", "{{json .Config.Labels}}", name)
    try:
        labels = json.loads(inspected)
    except json.JSONDecodeError:
        labels = {}
    log, _ = await _run("docker", "logs", "--tail", "40", name)
    await _run("docker", "rm", "-f", name)
    await notify(labels.get(f"{LABEL}.to", ""), result_text(exit_code, labels.get(f"{LABEL}.from", VERSION), log, restarted))


def notifier(watcher):
    """Wynik jako przypomnienie: dociera od razu albo gdy klient (np. bot po restarcie) sie podlaczy."""
    async def notify(interface: str, text: str) -> None:
        try:
            reminders.add(time.time(), text[:reminders.MAX_TEXT_CHARS], kind="message", to=interface)
        except (reminders.ReminderError, OSError) as exc:
            print(f"[update] {text} ({exc})", flush=True)
            return
        watcher.reminders_changed()
    return notify


async def resume(watcher) -> None:
    """Przy starcie serwera: dokoncz raport z aktualizacji, ktora wlasnie wymienila ten kontener."""
    if runtime.kind() != "docker":
        return
    for helper in await _helpers():
        asyncio.create_task(follow(helper["name"], notifier(watcher), restarted=True))
