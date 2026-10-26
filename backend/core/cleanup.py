"""
Zwolnij miejsce — jedno klikniecie zamiast szukania, co mozna usunac (`cleanup`, `/sprzataj`, przycisk podpowiedzi).

Usuwa wylacznie to, co da sie odtworzyc i czego nic nie uzywa — tak samo jak sprzatanie po `/zalataj`
(core/patching.py):
  - obrazy Dockera bez tagu (`docker image prune -f` — obrazy z tagiem, takze kopie `pipe-rollback-*` dla /cofnij,
    zostaja),
  - cache budowania starszy niz doba (`docker builder prune --filter until=24h`),
  - pobrane paczki apt oraz cache npm, pip, yarn i composer na hoscie (w trybie docker przez kontener pomocniczy).

Nie rusza wolumenow, kontenerow (takze zatrzymanych), logow ani danych aplikacji. Zwolnione miejsce liczy z
`statvfs` przed i po. Zgoda: to akcja administratora (viewer i tryb obserwacji sa odrzucane w server.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.core import audit, executor, hostexec, runtime
from backend.core.i18n import tr
from backend.core.patching import BUILD_CACHE_KEEP, CACHE_CLEANUP, DISK_PATHS

SOURCE = "sprzatanie"


@dataclass
class Result:
    freed: int = 0
    steps: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    exit_code: int = 0

    def text(self) -> str:
        from backend.core.hostinfo import human_bytes

        if self.freed > 0:
            head = tr(f"Zwolnione miejsce: {human_bytes(self.freed)}.", f"Space freed: {human_bytes(self.freed)}.")
        else:
            head = tr("Nie bylo czego usuwac — dysk jest juz posprzatany.",
                      "There was nothing to remove — the disk is already clean.")
        lines = [head]
        if self.steps:
            lines.append(tr("Zrobione: ", "Done: ") + "; ".join(self.steps) + ".")
        if self.skipped:
            lines.append(tr("Pominiete: ", "Skipped: ") + "; ".join(self.skipped) + ".")
        return "\n".join(lines)

    def to_data(self) -> dict[str, Any]:
        return {"freed": self.freed, "text": self.text(), "steps": self.steps, "skipped": self.skipped,
                "exit_code": self.exit_code}


def _free(paths: list[str]) -> dict[int, int]:
    free: dict[int, int] = {}
    for path in paths:
        local = runtime.to_local(path)
        try:
            stat, device = os.statvfs(local), os.stat(local).st_dev
        except OSError:
            continue
        free[device] = stat.f_bavail * stat.f_frsize
    return free


async def run(interface: str, emit: Callable[[str], Any] | None = None) -> Result:
    """Sprzata i zwraca wynik. `emit(text)` — postep na zywo."""
    result = Result()

    async def step(label: str, command: str, timeout: float = 300) -> int:
        if emit is not None:
            await emit(label)
        out, err, code = await executor.execute(command, timeout=timeout)
        await audit.log_confirmed(interface + f" [{SOURCE}]", command, code)
        if code != 0:
            result.exit_code = result.exit_code or code
        return code

    root, _, docker_code = await executor.execute("docker info -f '{{.DockerRootDir}}'", timeout=30)
    paths = list(DISK_PATHS) + ([root.strip()] if docker_code == 0 and root.strip().startswith("/") else [])
    before = _free(paths)

    if runtime.kind() == "docker":
        reason = await hostexec.blocker()
        if reason:
            result.skipped.append(tr(f"cache pakietow na hoscie ({reason})", f"package caches on the host ({reason})"))
        else:
            image = await hostexec.image()
            if await step(tr("pobrane paczki apt i cache npm/pip", "downloaded apt packages and npm/pip caches"),
                          hostexec.wrap(CACHE_CLEANUP, image)) == 0:
                result.steps.append(tr("pobrane paczki i cache menedzerow pakietow",
                                       "downloaded packages and package manager caches"))
    elif runtime.kind() == "native":
        if await step(tr("pobrane paczki apt i cache npm/pip", "downloaded apt packages and npm/pip caches"),
                      CACHE_CLEANUP) == 0:
            result.steps.append(tr("pobrane paczki i cache menedzerow pakietow",
                                   "downloaded packages and package manager caches"))
    else:
        result.skipped.append(tr("cache pakietow (Kubernetes — wezel sprzata sie w jego powloce)",
                                 "package caches (Kubernetes — the node is cleaned in its shell)"))

    if docker_code == 0:
        if await step(tr("obrazy Dockera bez tagu", "untagged Docker images"), "docker image prune -f") == 0:
            result.steps.append(tr("obrazy bez tagu", "untagged images"))
        if await step(tr("cache budowania starszy niz doba", "build cache older than a day"),
                      f"docker builder prune -af --filter until={BUILD_CACHE_KEEP}") == 0:
            result.steps.append(tr("cache budowania", "build cache"))
    else:
        result.skipped.append(tr("Docker (brak dostepu)", "Docker (no access)"))

    after = _free(paths)
    result.freed = sum(max(0, after[d] - before[d]) for d in after if d in before)
    return result
