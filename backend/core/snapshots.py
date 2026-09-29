"""
Wehikul czasu serwera — "co sie zmienilo, ze przestalo dzialac?".

Co SNAPSHOT_INTERVAL czuwanie robi tania migawke stanu hosta (same odczyty
plikow i `docker inspect`, bez LLM):

  system      jadro, system, identyfikator rozruchu (restart serwera)
  packages    zainstalowane pakiety i wersje (dpkg, apk)
  containers  kontenery: obraz, identyfikator obrazu, czy dziala
  ports       nasluchujace porty TCP hosta
  services    wlaczone uslugi systemd i pliki jednostek w /etc/systemd/system
  cron        wpisy crona hosta (linie, sekrety zredagowane)
  users       konta z powloka logowania albo uid 0
  ssh_keys    klucze w authorized_keys (odcisk + komentarz, bez samego klucza)
  configs     skroty waznych konfiguracji: sshd, sudoers, nginx, Caddy, compose...

Migawka jest zapisywana tylko, gdy cos sie zmienilo — najnowsza starsza niz
chwila T opisuje wiec stan w chwili T. `timeline()` pokazuje zmiany miedzy
kolejnymi migawkami (z przedzialem czasu), `diff()` — miedzy dowolnymi dwiema.
Wynik trafia do modelu przy badaniu awarii i do porannego raportu.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core import hostinfo, memory, runtime

SECTIONS = ("system", "packages", "containers", "ports", "services", "cron", "users", "ssh_keys", "configs")
# Sekcje, ktorych wartosci to wiele linii — roznica jest pokazywana linia po linii.
LINE_SECTIONS = frozenset({"cron", "ssh_keys"})
# Zmiany w tych sekcjach sa istotne dla bezpieczenstwa (nowe konto, klucz SSH, sudoers).
SECURITY_SECTIONS = frozenset({"users", "ssh_keys"})
SECURITY_CONFIGS = ("/etc/sudoers", "/etc/ssh/sshd_config", "/etc/passwd")

MAX_FILE_BYTES = 2 * 1024 * 1024
RECENT_KEEP_HOURS = 48

CONFIG_GLOBS = (
    "/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/*", "/etc/sudoers", "/etc/sudoers.d/*",
    "/etc/hosts", "/etc/fstab", "/etc/resolv.conf", "/etc/docker/daemon.json",
    "/etc/nginx/nginx.conf", "/etc/nginx/sites-enabled/*", "/etc/nginx/conf.d/*",
    "/etc/caddy/Caddyfile", "/etc/haproxy/haproxy.cfg", "/etc/ufw/user.rules",
    "/etc/fail2ban/jail.local", "/etc/environment",
)
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
CRON_GLOBS = ("/etc/crontab", "/etc/cron.d/*", "/var/spool/cron/crontabs/*", "/var/spool/cron/*")
UNIT_GLOBS = ("/etc/systemd/system/*.service", "/etc/systemd/system/*.timer", "/etc/systemd/system/*.socket")
LOGIN_SHELLS_EXCLUDED = ("nologin", "false", "sync", "halt", "shutdown")


def snapshots_dir() -> Path:
    return memory.data_dir() / "snapshots"


# ─── Odczyty ────────────────────────────────────────────────────────────────

def _read_host(path: str, limit: int = MAX_FILE_BYTES) -> str | None:
    local = runtime.to_local(path)
    try:
        if os.path.getsize(local) > limit:
            return None
        with open(local, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _glob_host(pattern: str) -> list[str]:
    """Pliki hosta pasujace do wzorca (sciezki hosta, posortowane)."""
    local = runtime.to_local(pattern)
    return sorted(runtime.to_host(p) for p in glob.glob(local) if os.path.isfile(p))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def packages() -> dict[str, str]:
    """Pakiety z bazy dpkg (Debian/Ubuntu) albo apk (Alpine) — bez uruchamiania menedzera."""
    result: dict[str, str] = {}
    status = _read_host("/var/lib/dpkg/status", limit=64 * 1024 * 1024)
    if status:
        for block in status.split("\n\n"):
            name = re.search(r"^Package: (.+)$", block, re.MULTILINE)
            version = re.search(r"^Version: (.+)$", block, re.MULTILINE)
            state = re.search(r"^Status: (.+)$", block, re.MULTILINE)
            if name and version and state and state.group(1).endswith(" installed"):
                arch = re.search(r"^Architecture: (.+)$", block, re.MULTILINE)
                key = name.group(1).strip()
                if arch and arch.group(1).strip() not in ("all", "amd64", "arm64") and key in result:
                    key = f"{key}:{arch.group(1).strip()}"
                result[key] = version.group(1).strip()
        return result
    apk = _read_host("/lib/apk/db/installed", limit=64 * 1024 * 1024)
    if apk:
        for block in apk.split("\n\n"):
            name = re.search(r"^P:(.+)$", block, re.MULTILINE)
            version = re.search(r"^V:(.+)$", block, re.MULTILINE)
            if name and version:
                result[name.group(1).strip()] = version.group(1).strip()
    return result


def users() -> dict[str, str]:
    result = {}
    for line in (_read_host("/etc/passwd") or "").splitlines():
        fields = line.split(":")
        if len(fields) < 7 or not fields[2].isdigit():
            continue
        name, uid, shell = fields[0], int(fields[2]), fields[6].strip()
        login = shell and not any(shell.endswith(x) for x in LOGIN_SHELLS_EXCLUDED)
        if uid == 0 or login:
            result[name] = f"uid={uid} shell={shell}"
    return result


def key_fingerprint(line: str) -> str | None:
    """'ssh-ed25519 AAAA... komentarz' -> 'ssh-ed25519 SHA256:... komentarz' (jak ssh-keygen -l)."""
    parts = line.strip().split()
    for index, part in enumerate(parts):
        if part.startswith(("ssh-", "ecdsa-", "sk-")) and index + 1 < len(parts):
            try:
                blob = base64.b64decode(parts[index + 1], validate=True)
            except (ValueError, base64.binascii.Error):
                return None
            digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            comment = " ".join(parts[index + 2:])[:80]
            options = " [z opcjami]" if index else ""
            return f"{part} SHA256:{digest} {comment}".rstrip() + options
    return None


def ssh_keys() -> dict[str, str]:
    result = {}
    paths = ["/root/.ssh/authorized_keys", "/root/.ssh/authorized_keys2"]
    paths += _glob_host("/home/*/.ssh/authorized_keys")
    for path in dict.fromkeys(paths):
        text = _read_host(path)
        if text is None:
            continue
        keys = [fp for fp in (key_fingerprint(l) for l in text.splitlines() if l.strip() and not l.startswith("#")) if fp]
        result[path] = "\n".join(sorted(keys))
    return result


def cron() -> dict[str, str]:
    result = {}
    for pattern in CRON_GLOBS:
        for path in _glob_host(pattern):
            text = _read_host(path)
            if text is None:
                continue
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
            redacted, _ = memory.redact_secrets("\n".join(lines))
            result[path] = redacted
    return result


def services() -> dict[str, str]:
    from backend.core import infra

    result = {f"wlaczona: {name}": "tak" for name in infra.enabled_services()}
    for pattern in UNIT_GLOBS:
        for path in _glob_host(pattern):
            text = _read_host(path)
            if text is not None:
                result[path] = _digest(text)
    return result


def configs(extra: list[str] | None = None) -> dict[str, str]:
    result = {}
    paths: list[str] = []
    for pattern in CONFIG_GLOBS:
        paths += _glob_host(pattern) if "*" in pattern else [pattern]
    for path in dict.fromkeys(paths + (extra or [])):
        text = _read_host(path)
        if text is not None:
            result[path] = _digest(text)
    return result


def compose_files(containers: list[Any] | None) -> list[str]:
    """Pliki compose projektow z etykiet kontenerow i wpisow [compose] w DIRECTORY."""
    dirs = {c.workdir for c in containers or [] if getattr(c, "workdir", "")}
    try:
        dirs |= {e.path for e in memory.load_directory() if e.kind == "compose"}
    except OSError:
        pass
    found = []
    for directory in sorted(dirs):
        for name in COMPOSE_FILES:
            path = f"{directory.rstrip('/')}/{name}"
            if os.path.isfile(runtime.to_local(path)):
                found.append(path)
    return found


def ports(proc: str | None = None) -> dict[str, str]:
    result = {}
    for sock in hostinfo.sockets(proc):
        if not sock.proto.startswith("tcp"):
            continue
        result[f"tcp/{sock.port} {sock.ip}"] = "publiczny" if sock.public else "lokalny"
    return result


def system(proc: str | None = None) -> dict[str, str]:
    from backend.core import infra

    base = proc or runtime.host_proc()
    result = {}
    kernel = hostinfo._read(f"{base}/sys/kernel/osrelease").strip()
    if kernel:
        result["jadro"] = kernel
    boot = hostinfo._read(f"{base}/sys/kernel/random/boot_id").strip()
    if boot:
        result["rozruch"] = boot
    hostname, os_name = infra.host_identity()
    if os_name:
        result["system"] = os_name
    if hostname:
        result["nazwa hosta"] = hostname
    return result


def container_state(containers: list[Any] | None) -> dict[str, str]:
    if containers is None:
        return {}
    return {c.name: f"{c.image} | {c.image_id or '?'} | {'dziala' if c.state == 'running' else 'zatrzymany'}"
            for c in containers}


def capture_sync(containers: list[Any] | None, proc: str | None = None) -> dict[str, dict[str, str]]:
    """Migawka (bez Dockera — kontenery podaje wywolujacy). Blad sekcji = pusta sekcja."""
    readers = {
        "system": lambda: system(proc),
        "packages": packages,
        "containers": lambda: container_state(containers),
        "ports": lambda: ports(proc),
        "services": services,
        "cron": cron,
        "users": users,
        "ssh_keys": ssh_keys,
        "configs": lambda: configs(compose_files(containers)),
    }
    if runtime.kind() == "kubernetes" and not runtime.host_root():
        # Bez zamontowanego wezla pliki to system plikow poda Pipe, nie serwera — nie udawajmy, ze to host.
        readers = {"containers": readers["containers"]}
    sections: dict[str, dict[str, str]] = {}
    for name, reader in readers.items():
        try:
            sections[name] = reader()
        except Exception:
            sections[name] = {}
    return sections


async def capture() -> dict[str, dict[str, str]]:
    import asyncio

    from backend.core import infra

    containers = await infra.docker_containers()
    sections = await asyncio.to_thread(capture_sync, containers)
    if containers is None:
        sections["containers"] = {}
        sections.setdefault("_meta", {})["docker"] = "niedostepny"
    return sections


# ─── Zapis ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Snapshot:
    t: float
    sections: dict[str, dict[str, str]]


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y%m%dT%H%M%S")


def list_snapshots() -> list[tuple[float, Path]]:
    directory = snapshots_dir()
    if not directory.is_dir():
        return []
    result = []
    for path in directory.glob("*.json"):
        try:
            result.append((datetime.strptime(path.stem, "%Y%m%dT%H%M%S").timestamp(), path))
        except ValueError:
            continue
    return sorted(result)


def load_snapshot(path: Path) -> Snapshot | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(float(raw["t"]), {k: dict(v) for k, v in raw["sections"].items()})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def latest() -> Snapshot | None:
    for _, path in reversed(list_snapshots()):
        snap = load_snapshot(path)
        if snap:
            return snap
    return None


def _comparable(sections: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {k: v for k, v in sections.items() if not k.startswith("_")}


def save(sections: dict[str, dict[str, str]], now: float | None = None, keep_days: int = 30) -> bool:
    """Zapisuje migawke, jesli stan sie zmienil od ostatniej. Zwraca True, gdy zapisano."""
    now = now if now is not None else time.time()
    previous = latest()
    comparable = _comparable(sections)
    if previous is not None:
        # Brak Dockera w tej chwili nie znaczy, ze kontenery zniknely — zostaw poprzedni stan.
        if sections.get("_meta", {}).get("docker") == "niedostepny":
            comparable["containers"] = previous.sections.get("containers", {})
        if _comparable(previous.sections) == comparable:
            return False
    path = snapshots_dir() / f"{_stamp(now)}.json"
    memory._write_atomic(path, json.dumps({"t": now, "sections": comparable}, ensure_ascii=False,
                                          sort_keys=True, separators=(",", ":")))
    prune(keep_days, now)
    return True


def prune(keep_days: int, now: float | None = None) -> None:
    """Ostatnie 48 h — wszystkie migawki; starsze — pierwsza z kazdego dnia; najstarsze niz keep_days — usuwane."""
    now = now if now is not None else time.time()
    snaps = list_snapshots()
    seen_days: set[str] = set()
    for index, (t, path) in enumerate(snaps):
        age_hours = (now - t) / 3600
        day = datetime.fromtimestamp(t).strftime("%Y%m%d")
        last = index == len(snaps) - 1
        if last or age_hours <= RECENT_KEEP_HOURS:
            continue
        if age_hours > keep_days * 24 or day in seen_days:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        seen_days.add(day)


def baseline(at: float) -> Snapshot | None:
    """Stan w chwili `at`: najnowsza migawka nie mlodsza niz `at` (albo najstarsza, jesli brak)."""
    snaps = list_snapshots()
    if not snaps:
        return None
    candidates = [path for t, path in snaps if t <= at] or [snaps[0][1]]
    return load_snapshot(candidates[-1])


# ─── Roznice ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Change:
    section: str
    kind: str        # added | removed | changed
    key: str
    detail: str = ""

    @property
    def security(self) -> bool:
        return self.section in SECURITY_SECTIONS or (
            self.section == "configs" and self.key.startswith(SECURITY_CONFIGS))


def diff(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]) -> list[Change]:
    changes: list[Change] = []
    for section in SECTIONS:
        before, after = old.get(section, {}), new.get(section, {})
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            if a is None:
                changes.append(Change(section, "added", key, b or ""))
            elif b is None:
                changes.append(Change(section, "removed", key, a))
            elif section in LINE_SECTIONS:
                old_lines, new_lines = set(a.splitlines()), set(b.splitlines())
                detail = "\n".join([f"+ {l}" for l in sorted(new_lines - old_lines)]
                                   + [f"- {l}" for l in sorted(old_lines - new_lines)])
                changes.append(Change(section, "changed", key, detail))
            else:
                changes.append(Change(section, "changed", key, f"{a} -> {b}"))
    return changes


def describe(change: Change) -> str:
    """Jedna linia po polsku."""
    s, k, key, d = change.section, change.kind, change.key, change.detail
    if s == "system":
        if key == "rozruch":
            return "serwer zostal zrestartowany (nowy rozruch)"
        return f"{key}: {d}" if k == "changed" else f"{key}: {d or '(brak)'}"
    if s == "packages":
        return {"added": f"zainstalowano pakiet {key} {d}", "removed": f"usunieto pakiet {key} ({d})",
                "changed": f"pakiet {key}: {d}"}[k]
    if s == "containers":
        if k == "added":
            return f"nowy kontener {key} ({d.split(' | ')[0]})"
        if k == "removed":
            return f"kontener {key} zniknal (byl: {d.split(' | ')[0]})"
        old, new = d.split(" -> ", 1)
        old_image, old_id, old_state = (old.split(" | ") + ["", "", ""])[:3]
        new_image, new_id, new_state = (new.split(" | ") + ["", "", ""])[:3]
        parts = []
        if old_image != new_image:
            parts.append(f"obraz {old_image} -> {new_image}")
        elif old_id != new_id:
            parts.append(f"nowa wersja obrazu {new_image} ({old_id[7:] or '?'} -> {new_id[7:] or '?'})")
        if old_state != new_state:
            parts.append(f"{old_state} -> {new_state}")
        return f"kontener {key}: " + ", ".join(parts or ["zmiana"])
    if s == "ports":
        return {"added": f"nowy port nasluchujacy {key} ({d})", "removed": f"port {key} przestal nasluchiwac",
                "changed": f"port {key}: {d}"}[k]
    if s == "services":
        if key.startswith("wlaczona: "):
            name = key.split(": ", 1)[1]
            return f"wlaczono usluge {name}" if k == "added" else f"wylaczono usluge {name}"
        return {"added": f"nowy plik jednostki systemd {key}", "removed": f"usunieto jednostke systemd {key}",
                "changed": f"zmieniono jednostke systemd {key}"}[k]
    if s == "cron":
        if k == "added":
            return f"nowy plik crona {key}:\n" + "\n".join(f"+ {l}" for l in d.splitlines()[:10])
        if k == "removed":
            return f"usunieto plik crona {key}"
        return f"zmieniono cron {key}:\n{d}"
    if s == "users":
        return {"added": f"nowe konto {key} ({d})", "removed": f"usunieto konto {key}",
                "changed": f"konto {key}: {d}"}[k]
    if s == "ssh_keys":
        if k == "added":
            return f"nowy plik authorized_keys {key}:\n" + "\n".join(f"+ {l}" for l in d.splitlines())
        if k == "removed":
            return f"usunieto {key}"
        return f"zmienily sie klucze SSH w {key}:\n{d}"
    return {"added": f"nowy plik {key}", "removed": f"usunieto plik {key}", "changed": f"zmieniono plik {key}"}[k]


SECTION_TITLES = {
    "system": "System", "packages": "Pakiety", "containers": "Kontenery", "ports": "Porty",
    "services": "Uslugi systemd", "cron": "Cron", "users": "Konta", "ssh_keys": "Klucze SSH",
    "configs": "Konfiguracja",
}
MAX_PACKAGES_LISTED = 12


def render(changes: list[Change], limit: int = 60) -> str:
    if not changes:
        return "(bez zmian)"
    lines: list[str] = []
    for section in SECTIONS:
        items = [c for c in changes if c.section == section]
        if not items:
            continue
        shown = items
        suffix = ""
        if section == "packages" and len(items) > MAX_PACKAGES_LISTED:
            shown = items[:MAX_PACKAGES_LISTED]
            suffix = f"  ... i {len(items) - MAX_PACKAGES_LISTED} innych pakietow"
        lines.append(f"{SECTION_TITLES[section]} ({len(items)}):")
        for change in shown:
            mark = "[BEZPIECZENSTWO] " if change.security else ""
            text = describe(change).replace("\n", "\n    ")
            lines.append(f"  - {mark}{text}")
        if suffix:
            lines.append(suffix)
    if len(lines) > limit:
        lines = lines[:limit] + [f"[... pominieto {len(lines) - limit} linii]"]
    return "\n".join(lines)


def _when(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def timeline(hours: float, current: dict[str, dict[str, str]], now: float | None = None) -> str:
    """
    Zmiany w ostatnich `hours` godzinach, pogrupowane w przedzialy miedzy migawkami
    (zmiana nastapila miedzy A a B). Ostatni przedzial konczy sie na stanie biezacym.
    """
    now = now if now is not None else time.time()
    start = now - hours * 3600
    snaps = list_snapshots()
    if not snaps:
        return ("Brak migawek — historia zmian dopiero sie zbiera (pierwsza migawka powstaje po starcie "
                "czuwania, kolejne co SNAPSHOT_INTERVAL).")
    base = baseline(start)
    if base is None:
        return "Nie udalo sie wczytac migawek."
    note = ""
    if base.t > start:
        note = f"Uwaga: najstarsza migawka jest z {_when(base.t)} — wczesniejszych zmian nie znam.\n"
    points: list[Snapshot] = [base]
    for t, path in snaps:
        if base.t < t <= now:
            snap = load_snapshot(path)
            if snap:
                points.append(snap)
    points.append(Snapshot(now, _comparable(current)))
    blocks = []
    total = 0
    for older, newer in zip(points, points[1:]):
        changes = diff(older.sections, newer.sections)
        if not changes:
            continue
        total += len(changes)
        until = "teraz" if newer is points[-1] else _when(newer.t)
        blocks.append(f"Miedzy {_when(older.t)} a {until}:\n{render(changes)}")
    if not blocks:
        return note + f"Brak zmian od {_when(base.t)}."
    return note + f"Zmiany od {_when(base.t)} ({total}):\n\n" + "\n\n".join(blocks)


def summary_since(hours: float, current: dict[str, dict[str, str]], now: float | None = None) -> tuple[list[Change], float | None]:
    """Zbiorcza roznica: stan sprzed `hours` godzin -> stan biezacy (dla raportu)."""
    now = now if now is not None else time.time()
    base = baseline(now - hours * 3600)
    if base is None:
        return [], None
    return diff(base.sections, _comparable(current)), base.t
