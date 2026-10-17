"""
Zdalne serwery (cele ssh z target_manage) w skanie podatnosci i w latkach.

  dumps()         jedno polaczenie ssh na serwer: os-release, baza dpkg/apk, reboot-required i lista
                  sources.list.d (Ubuntu Pro) — parsowanie robi core/vulns.py (ServerDump), tu tylko odczyt
  as_root(t, c)   komenda latki dla serwera: jako root wprost, inaczej przez `sudo -n` (bez hasla — BatchMode)

Odczyt to zwykle `cat`, jak remote_exec dla bezpiecznej komendy. Tresc plikow z serwera to dane: nazwy pakietow
przechodza w vulns.py przez wzorzec nazwy, a komenda latki jest jednym argumentem ssh (targets.wrap, shlex.quote).
Cele docker (kontenery tego hosta) sa w skanie obrazow, Kubernetes nie ma tu bazy pakietow.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex

from backend.core import executor, targets
from backend.core.i18n import tr

PLAIN = ("/etc/os-release", "/usr/lib/os-release", "/run/reboot-required", "/var/run/reboot-required")
# Z baz pakietow tylko pola, ktore czyta vulns.py — pelna baza dpkg ma kilka MB (opisy), a wynik komendy jest przycinany
FILTERED = {"/var/lib/dpkg/status": "^(Package|Status|Source|Version|Architecture):|^$",
            "/lib/apk/db/installed": "^(P|V|o):|^$"}
FILES = PLAIN + tuple(FILTERED)
LISTING = "/etc/apt/sources.list.d"
READ_TIMEOUT = 60
PARALLEL = 8


def ssh_targets() -> list[targets.Target]:
    from backend.core.vulns import MAX_SERVERS

    return [t for t in targets.load_targets() if t.kind == "ssh"][:MAX_SERVERS]


def dump_command(marker: str) -> str:
    """Wszystkie pliki naraz, kazdy po linii `<marker> <sciezka>`; nieistniejace pomijane."""
    head = f"printf '\\n%s %s\\n' {marker}"
    parts = [f"for f in {' '.join(PLAIN)}; do if [ -r \"$f\" ]; then {head} \"$f\"; cat \"$f\"; fi; done"]
    parts += [f"if [ -r {path} ]; then {head} {path}; grep -E {shlex.quote(fields)} {path}; fi"
              for path, fields in FILTERED.items()]
    parts.append(f"{head} {LISTING}; ls {LISTING} 2>/dev/null; true")
    return "; ".join(parts)


def parse_dump(text: str, marker: str) -> tuple[dict[str, str], list[str]]:
    """(sciezka -> tresc, nazwy w sources.list.d) z wyniku dump_command."""
    parts = re.split(rf"\n{re.escape(marker)} (\S+)\n", "\n" + text)
    files: dict[str, str] = {}
    listing: list[str] = []
    for path, content in zip(parts[1::2], parts[2::2]):
        if path == LISTING:
            listing = [line.strip() for line in content.splitlines() if line.strip()]
        elif path in FILES:
            files[path] = content
    return files, listing


def _address(target: targets.Target) -> str:
    return (f"{target.user}@" if target.user else "") + target.host + (f":{target.port}" if target.port else "")


async def dump(target: targets.Target):
    """Pliki jednego serwera jako vulns.ServerDump (z bledem, gdy ssh sie nie udal)."""
    from backend.core.vulns import ServerDump

    marker = "==PIPE-" + secrets.token_hex(6) + "=="
    try:
        command = targets.wrap(target, dump_command(marker))
    except targets.TargetError as exc:
        return ServerDump(target.name, _address(target), error=str(exc))
    out, err, code = await executor.execute(command, timeout=READ_TIMEOUT)
    if code != 0 and marker not in out:
        reason = (err or out).strip().splitlines()[-1:] or [tr(f"kod wyjscia {code}", f"exit code {code}")]
        return ServerDump(target.name, _address(target), error=reason[0][:200])
    files, listing = parse_dump(out, marker)
    return ServerDump(target.name, _address(target), files=files, listing=listing)


async def dumps() -> list:
    """Wszystkie cele ssh rownolegle (najwyzej PARALLEL naraz)."""
    chosen = ssh_targets()
    if not chosen:
        return []
    limit = asyncio.Semaphore(PARALLEL)

    async def one(target: targets.Target):
        async with limit:
            return await dump(target)
    return list(await asyncio.gather(*(one(t) for t in chosen)))


def as_root(target: targets.Target, command: str) -> str:
    """
    Komenda dla serwera: root wykonuje ja wprost, inny uzytkownik przez `sudo -n` (bez hasla; gdy sudo chce
    hasla, ssh w trybie BatchMode konczy sie bledem zamiast czekac).
    """
    quoted = shlex.quote(command)
    return targets.wrap(target, f"if [ \"$(id -u)\" = 0 ]; then sh -c {quoted}; else sudo -n sh -c {quoted}; fi")


def needs_password(output: str) -> bool:
    return "a password is required" in output or "sudo: a terminal is required" in output


def sudo_hint(target: targets.Target) -> str:
    return tr(f"{target.name}: sudo wymaga hasla — lacz sie jako root albo dodaj uzytkownikowi {target.user or '?'} "
              "NOPASSWD dla apt-get w sudoers.",
              f"{target.name}: sudo asks for a password — connect as root or give user {target.user or '?'} "
              "NOPASSWD for apt-get in sudoers.")
