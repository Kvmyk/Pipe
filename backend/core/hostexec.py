"""
Komenda na hoscie z wnetrza kontenera (tryb docker) — po TAK uzytkownika.

W trybie docker system plikow hosta jest zamontowany tylko do odczytu, wiec `apt-get install`, `systemctl restart`
czy `sed -i /etc/...` wykonane w kontenerze nie zmienilyby serwera. Pipe ma jednak `docker.sock` (to w praktyce
root na hoscie — docs/security.md), wiec moze uruchomic krotki kontener pomocniczy z wlasnego obrazu:
`--privileged --pid=host`, a w nim `nsenter -t 1` do przestrzeni nazw procesu 1 hosta. Komenda dziala wtedy
jak w powloce roota na serwerze: system plikow, siec, uslugi i menedzer pakietow hosta.

Nie dodaje to uprawnien, ktorych Pipe nie mial — `docker run` i tak bylo mozliwe po potwierdzeniu. Daje
przewidywalna, jedna droge, ktora zawsze przechodzi przez TAK: wolajacy (handler `execute_command` z `host`,
latki `/zalataj`) klasyfikuje i pokazuje komende docelowa, nigdy opakowanie. Tryb obserwacji i inne tryby niz
docker nie maja tej drogi (`blocker()`).

Bez importu `settings` — testowalne przez podstawienie `_run`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import socket

from backend.core import runtime
from backend.core.i18n import tr

LABEL = "pipe.hostexec"
# Obraz po id (sha256:...) albo nazwie z rejestru — nic innego nie trafi do `docker run`.
_IMAGE = re.compile(r"^(sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._/:@-]{0,254})$")
_image: str | None = None


async def _run(*args: str, timeout: float = 20) -> tuple[str, int]:
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return str(exc), 127
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return "timeout", 124
    return output.decode("utf-8", errors="replace").strip(), process.returncode or 0


async def image() -> str:
    """Obraz wlasnego kontenera (z `docker inspect`), zapamietany po pierwszym udanym odczycie. '' = brak."""
    global _image
    if _image:
        return _image
    output, code = await _run("docker", "inspect", "--format", "{{json .Image}}", socket.gethostname())
    try:
        found = str(json.loads(output)) if code == 0 else ""
    except (json.JSONDecodeError, TypeError):
        found = ""
    if found and _IMAGE.match(found):
        _image = found
    return _image or ""


_project: str | None = None


async def own_project() -> str:
    """Projekt compose kontenera Pipe ('' = brak albo nie tryb docker). Jego uslug /zalataj obrazy nie odtwarza."""
    global _project
    if _project is not None:
        return _project
    if runtime.kind() != "docker":
        return ""
    output, code = await _run("docker", "inspect", "--format",
                              '{{json (index .Config.Labels "com.docker.compose.project")}}', socket.gethostname())
    try:
        found = str(json.loads(output) or "") if code == 0 else ""
    except (json.JSONDecodeError, TypeError):
        found = ""
    if code == 0:
        _project = found
    return found


async def blocker() -> str:
    """Powod, dla ktorego Pipe nie wykona komendy na hoscie ('' = moze)."""
    if runtime.observe():
        return tr("Tryb obserwacji (PIPE_OBSERVE=1): Pipe niczego na serwerze nie zmienia.",
                  "Observe mode (PIPE_OBSERVE=1): Pipe changes nothing on the server.")
    if runtime.kind() != "docker":
        return tr("Komendy na hoscie przez kontener pomocniczy sa tylko w trybie docker.",
                  "Host commands through a helper container exist only in docker mode.")
    if not await image():
        return tr("Nie moge odczytac wlasnego kontenera przez Dockera (brak docker.sock?) — nie mam z czego "
                  "uruchomic kontenera pomocniczego.",
                  "Cannot inspect my own container through Docker (no docker.sock?) — nothing to start the "
                  "helper container from.")
    return ""


def wrap(command: str, image_ref: str, cwd: str = "") -> str:
    """
    Pelna komenda `docker run ... nsenter ... sh -c '<command>'`. `command` jest jednym argumentem (shlex.quote),
    `cwd` to katalog hosta, w ktorym ma ruszyc. Etykiety compose wyzerowane — obraz zbudowany przez compose
    niesie je, a compose uznalby kontener pomocniczy za kopie uslugi.
    """
    if not _IMAGE.match(image_ref):
        raise ValueError("bad image reference")
    inner = f"cd {shlex.quote(cwd)} 2>/dev/null; {command}" if cwd and cwd != "/" else command
    return " ".join([
        "docker", "run", "--rm", "--privileged", "--pid=host", "--network=host", "--ipc=host", "--uts=host",
        "--label", f"{LABEL}=1", "--label", "com.docker.compose.project=", "--label", "com.docker.compose.service=",
        "--entrypoint", "nsenter", image_ref,
        "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c", shlex.quote(inner),
    ])


def note() -> str:
    """Linia planu bezpiecznika pod komenda wykonywana na hoscie."""
    return tr("na hoscie: kontener pomocniczy z obrazu Pipe (nsenter do procesu 1), jak root w powloce serwera",
              "on the host: a helper container from the Pipe image (nsenter into process 1), like root in the server shell")


def host_plan(plan, image_ref: str):
    """
    Plan bezpiecznika dla komendy wykonywanej na hoscie: sprawdzenia powloki (`docker compose config -q`,
    `nginx -t`...) i stan projektu compose ida tym samym kontenerem pomocniczym — w kontenerze Pipe nie ma
    `docker compose` ani programow hosta. Komendy odwrotne tez (`/cofnij` wykona je na hoscie).
    """
    from backend.core.safety import Check

    def moved(check: Check) -> Check:
        if check.kind == "shell" and check.command:
            return Check(check.label, "shell", wrap(check.command, image_ref), check.target, check.retries, check.delay)
        if check.kind == "compose":
            flags = f" {check.target}" if check.target else ""
            # kazdy kontener projektu: running, a jesli ma healthcheck — healthy
            probe = (f"out=$(docker compose{flags} ps -a --format '{{{{.State}}}} {{{{.Health}}}}') && "
                     "! printf '%s\\n' \"$out\" | grep -Ev '^(running( healthy)?)? *$'")
            return Check(check.label, "shell", wrap(probe, image_ref), "", check.retries, check.delay)
        return check

    plan.pre = [moved(c) for c in plan.pre]
    plan.post = [moved(c) for c in plan.post]
    plan.inverse = [wrap(c, image_ref) for c in plan.inverse]
    plan.notes.append(note())
    return plan
