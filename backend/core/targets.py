"""
Zdalne cele — maszyny, kontenery i klastry, na ktorych Pipe (albo jego workery)
wykonuja komendy bez instalowania czegokolwiek po drugiej stronie.

  ssh         serwer przez SSH (klucze z /root/.ssh kontenera albo identity_file)
  docker      kontener na tym hoscie: `docker exec <kontener> sh -c ...`
  kubernetes  klaster (kubectl/helm z --context/--namespace) albo konkretny pod
              (`kubectl exec`)
  local       ten host — przydatne dla workerow dzialajacych rownolegle lokalnie

Rejestr lezy w DATA_DIR/targets.json. Wszystkie pola sa walidowane wzorcami,
a komenda jest przekazywana jako jeden argument przez shlex.quote — nazwa celu
ani parametry nie moga wstrzyknac dodatkowych polecen. Klasyfikacja
bezpieczenstwa dotyczy komendy DOCELOWEJ, nie opakowania ssh/kubectl.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from backend.core import memory

TARGET_KINDS = ("ssh", "docker", "kubernetes", "local")
MAX_TARGETS = 100

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9a-fA-F:.]+\]|[0-9a-fA-F:.]+)$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.IGNORECASE)
_PATH = re.compile(r"^/[A-Za-z0-9_./-]{1,255}$")
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_KUBE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,252}$")


class TargetError(ValueError):
    """Nieprawidlowy cel albo komenda dla celu."""


@dataclass
class Target:
    name: str
    kind: str
    description: str = ""
    host: str = ""
    user: str = ""
    port: int = 0
    identity_file: str = ""
    container: str = ""
    context: str = ""
    namespace: str = ""
    pod: str = ""
    pod_container: str = ""

    def describe(self) -> str:
        if self.kind == "ssh":
            where = f"ssh {self.user + '@' if self.user else ''}{self.host}{':' + str(self.port) if self.port else ''}"
        elif self.kind == "docker":
            where = f"kontener Docker {self.container} na tym hoscie"
        elif self.kind == "kubernetes":
            scope = f"kontekst {self.context or '(domyslny)'}, namespace {self.namespace or '(domyslny)'}"
            where = f"pod {self.pod} ({scope})" if self.pod else f"klaster Kubernetes ({scope})"
        else:
            where = "ten host (lokalnie)"
        return f"{self.name}: {where}" + (f" — {self.description}" if self.description else "")

    def command_hint(self) -> str:
        if self.kind == "kubernetes" and not self.pod:
            return ("Komendy musza zaczynac sie od `kubectl` albo `helm`; kontekst i namespace sa dodawane "
                    "automatycznie (inny namespace: -n, wszystkie: -A).")
        if self.kind == "local":
            return "Komendy wykonuja sie na hoscie Pipe, tak jak execute_command."
        return "Komendy wykonuja sie w powloce sh po stronie celu."


# ─── Walidacja ──────────────────────────────────────────────────────────────

def validate(target: Target) -> Target:
    target.name = (target.name or "").strip().lower()
    target.kind = (target.kind or "").strip().lower()
    if not _NAME.match(target.name):
        raise TargetError(f"Nazwa celu {target.name!r}: male litery, cyfry, '-' i '_' (1-32 znaki).")
    if target.kind not in TARGET_KINDS:
        raise TargetError(f"Rodzaj celu {target.kind!r} — dozwolone: {', '.join(TARGET_KINDS)}.")
    target.description = " ".join((target.description or "").split())[:300]
    for field_name, pattern in (("host", _HOST), ("user", _USER), ("identity_file", _PATH),
                                ("container", _CONTAINER), ("context", _KUBE), ("namespace", _KUBE),
                                ("pod", _KUBE), ("pod_container", _CONTAINER)):
        value = str(getattr(target, field_name) or "").strip()
        setattr(target, field_name, value)
        if value and not pattern.match(value):
            raise TargetError(f"Nieprawidlowa wartosc pola {field_name}: {value!r}.")
    try:
        target.port = int(target.port or 0)
    except (TypeError, ValueError):
        raise TargetError("Port musi byc liczba.") from None
    if not 0 <= target.port <= 65535:
        raise TargetError("Port poza zakresem 1-65535.")
    if target.kind == "ssh" and not target.host:
        raise TargetError("Cel ssh wymaga pola host.")
    if target.kind == "docker" and not target.container:
        raise TargetError("Cel docker wymaga pola container.")
    label = memory.find_secret(f"{target.description} {target.host} {target.user}")
    if label:
        raise TargetError(f"Opis celu wyglada na sekret ({label}) — sekrety trzymaj poza Pipe.")
    return target


# ─── Rejestr ────────────────────────────────────────────────────────────────

def targets_path() -> Path:
    return memory.data_dir() / "targets.json"


def known_hosts_path() -> Path:
    return memory.data_dir() / "known_hosts"


def load_targets() -> list[Target]:
    path = targets_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    names = {f.name for f in fields(Target)}
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            try:
                result.append(validate(Target(**{k: v for k, v in item.items() if k in names})))
            except (TargetError, TypeError):
                continue
    return sorted(result, key=lambda t: t.name)


def get_target(name: str) -> Target | None:
    name = (name or "").strip().lower()
    if name in ("local", "localhost", "host"):
        return Target("local", "local", "ten host")
    return next((t for t in load_targets() if t.name == name), None)


def save_target(target: Target) -> bool:
    """Dodaje albo zastepuje cel. Zwraca True, jesli cel jest nowy."""
    target = validate(target)
    if target.name == "local":
        raise TargetError("Nazwa 'local' jest zarezerwowana dla tego hosta.")
    targets = [t for t in load_targets() if t.name != target.name]
    created = len(targets) == len(load_targets())
    if len(targets) >= MAX_TARGETS:
        raise TargetError(f"Za duzo celow (limit {MAX_TARGETS}).")
    targets.append(target)
    memory._write_atomic(targets_path(), json.dumps([asdict(t) for t in sorted(targets, key=lambda t: t.name)],
                                                    ensure_ascii=False, indent=2) + "\n")
    return created


def remove_target(name: str) -> bool:
    targets = load_targets()
    kept = [t for t in targets if t.name != (name or "").strip().lower()]
    if len(kept) == len(targets):
        return False
    memory._write_atomic(targets_path(), json.dumps([asdict(t) for t in kept], ensure_ascii=False, indent=2) + "\n")
    return True


# ─── Budowanie komend ───────────────────────────────────────────────────────

def _kube_flags(target: Target, helm: bool = False) -> str:
    flags = ""
    if target.context:
        flags += f" {'--kube-context' if helm else '--context'} {shlex.quote(target.context)}"
    if target.namespace:
        flags += f" --namespace {shlex.quote(target.namespace)}"
    return flags


def wrap(target: Target, command: str) -> str:
    """Komenda, ktora wykonuje `command` na celu. Wynik to dokladnie to, co uruchomi Pipe."""
    command = (command or "").strip()
    if not command:
        raise TargetError("Pusta komenda.")
    if target.kind == "local":
        return command
    if target.kind == "ssh":
        parts = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", f"UserKnownHostsFile={known_hosts_path()}"]
        if target.identity_file:
            parts += ["-i", target.identity_file]
        if target.port:
            parts += ["-p", str(target.port)]
        parts.append(f"{target.user}@{target.host}" if target.user else target.host)
        return " ".join(shlex.quote(p) for p in parts) + " -- " + shlex.quote(command)
    if target.kind == "docker":
        return f"docker exec {shlex.quote(target.container)} sh -c {shlex.quote(command)}"
    # kubernetes
    if target.pod:
        container = f" -c {shlex.quote(target.pod_container)}" if target.pod_container else ""
        return (f"kubectl{_kube_flags(target)} exec {shlex.quote(target.pod)}{container} -- "
                f"sh -c {shlex.quote(command)}")
    from backend.core.security import split_command
    segments, _writes, _dynamic = split_command(command)
    for segment in segments[1:]:
        if segment.split()[0] in ("kubectl", "helm"):
            raise TargetError("Na celu-klastrze wykonuj jedna komende kubectl/helm naraz (dalsze segmenty, np. "
                              "| grep, dzialaja lokalnie na jej wyniku) — inaczej druga komenda poszlaby do "
                              "domyslnego kontekstu.")
    tokens = command.split(None, 1)
    program = tokens[0]
    rest = tokens[1] if len(tokens) > 1 else ""
    if program == "kubectl":
        return f"kubectl{_kube_flags(target)} {rest}".rstrip()
    if program == "helm":
        return f"helm{_kube_flags(target, helm=True)} {rest}".rstrip()
    raise TargetError(f"Cel {target.name} to klaster Kubernetes — komenda musi zaczynac sie od kubectl albo helm.")


def test_command(target: Target) -> str:
    """Komenda sprawdzajaca lacznosc (sam odczyt)."""
    if target.kind == "kubernetes" and not target.pod:
        return "kubectl version"
    return "echo pipe-ok && uname -srm"
