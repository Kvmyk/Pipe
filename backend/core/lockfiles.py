"""
Zaleznosci aplikacji z plikow blokad (lockfile) — dla skanu podatnosci (core/vulns.py).

  find(dir)       lockfile'e w jednym katalogu
  find_tree(dir)  katalogi z lockfile'ami: katalog aplikacji i jego podkatalogi (backend/, frontend/, api/...)
                  do MAX_DEPTH w dol, bez node_modules, vendor, venv i katalogow budowania
  parse(path)     [(ekosystem OSV, nazwa, wersja)] — npm (package-lock, yarn, pnpm), PyPI (requirements z ==,
                  poetry, Pipfile, uv), Go, crates.io, Packagist, RubyGems

Tylko dokladne wersje: `requirements.txt` bez `==` nic nie mowi o tym, co jest zainstalowane.
Same odczyty plikow, bez uruchamiania menedzerow pakietow. Bez importu `settings`.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from typing import Callable

try:
    import tomllib                    # Python 3.11+; na starszym poetry.lock i Cargo.lock sa pomijane
except ImportError:                   # pragma: no cover
    tomllib = None

MAX_BYTES = 20 * 1024 * 1024
MAX_PACKAGES = 5000
MAX_DEPTH = 3                       # katalog aplikacji + 3 poziomy (np. apps/shop/backend)
MAX_DIRS = 1000                     # katalogow odwiedzonych w jednym drzewie — duzy katalog danych nie zatrzyma skanu
# Tu nie ma lockfile'i aplikacji, tylko zainstalowane zaleznosci, kopie z budowania albo dane
SKIP_DIRS = frozenset({
    "node_modules", "bower_components", "vendor", "venv", "env", "site-packages", "__pycache__",
    "dist", "build", "target", "out", "coverage", "tmp", "logs",
})

Dep = tuple[str, str, str]          # (ekosystem, nazwa, wersja)


def _npm(text: str) -> list[Dep]:
    data = json.loads(text)
    found: dict[tuple[str, str], None] = {}
    for key, info in (data.get("packages") or {}).items():          # lockfileVersion 2 i 3
        if key and "node_modules/" in key and isinstance(info, dict) and info.get("version") and not info.get("link"):
            found[(key.rsplit("node_modules/", 1)[1], str(info["version"]))] = None

    def walk(deps: dict) -> None:                                    # lockfileVersion 1
        for name, info in (deps or {}).items():
            if isinstance(info, dict) and info.get("version"):
                found[(name, str(info["version"]))] = None
                walk(info.get("dependencies") or {})
    if not found:
        walk(data.get("dependencies") or {})
    return [("npm", name, version) for name, version in found
            if not version.startswith(("file:", "link:", "git", "http"))]


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9._+!-]+)\s*(?:[;#].*)?$")


def _requirements(text: str) -> list[Dep]:
    return [("PyPI", m.group(1).lower(), m.group(2)) for line in text.splitlines() if (m := _REQ.match(line))]


def _toml(text: str) -> dict:
    return tomllib.loads(text) if tomllib is not None else {}


def _poetry(text: str) -> list[Dep]:
    data = _toml(text)
    return [("PyPI", str(p["name"]).lower(), str(p["version"])) for p in data.get("package") or []
            if p.get("name") and p.get("version")]


def _pipfile(text: str) -> list[Dep]:
    data = json.loads(text)
    deps = []
    for section in ("default", "develop"):
        for name, info in (data.get(section) or {}).items():
            version = str((info or {}).get("version", "")) if isinstance(info, dict) else ""
            if version.startswith("=="):
                deps.append(("PyPI", name.lower(), version[2:]))
    return deps


_GO_REQ = re.compile(r"^\s*(?:require\s+)?([a-zA-Z0-9.\-_/~]+\.[a-z]{2,}[a-zA-Z0-9.\-_/~]*)\s+(v[0-9][^\s]*)")


def _gomod(text: str) -> list[Dep]:
    deps = []
    for line in text.splitlines():
        if line.strip().startswith(("module ", "go ", "replace ", "exclude ", "//")):
            continue
        match = _GO_REQ.match(line)
        if match:
            deps.append(("Go", match.group(1), match.group(2).removesuffix("+incompatible")))
    return deps


def _cargo(text: str) -> list[Dep]:
    data = _toml(text)
    return [("crates.io", str(p["name"]), str(p["version"])) for p in data.get("package") or []
            if p.get("name") and p.get("version") and str(p.get("source", "")).startswith("registry+")]


def _composer(text: str) -> list[Dep]:
    data = json.loads(text)
    return [("Packagist", str(p["name"]), str(p["version"]).lstrip("v"))
            for section in ("packages", "packages-dev") for p in data.get(section) or []
            if p.get("name") and p.get("version") and not str(p["version"]).startswith("dev-")]


_GEM = re.compile(r"^    ([A-Za-z0-9][A-Za-z0-9._-]*) \(([0-9][^)\s]*)\)$")


def _gemfile(text: str) -> list[Dep]:
    deps, in_specs = [], False
    for line in text.splitlines():
        if line.strip() == "specs:":
            in_specs = True
            continue
        if in_specs and line and not line.startswith(" "):
            in_specs = False
        match = _GEM.match(line) if in_specs else None
        if match:
            deps.append(("RubyGems", match.group(1), match.group(2).split("-", 1)[0]))
    return deps


# Protokoly yarn (berry), ktore nie sa paczka z rejestru npm — OSV nic o nich nie wie
_NOT_REGISTRY = ("workspace:", "patch:", "link:", "portal:", "file:", "exec:", "git", "http", "github:")


def _yarn_name(spec: str) -> str:
    """'@babel/core@^7.0.0' / 'lodash@npm:^4.17.21' / 'alias@npm:real@^1' -> nazwa w rejestrze; '' = nie z rejestru."""
    at = spec.find("@", 1)
    if at < 0:
        return ""
    name, rest = spec[:at], spec[at + 1:]
    if rest.startswith("npm:"):
        rest = rest[4:]
        alias = rest.find("@", 1)
        if alias > 0:                                   # alias: zainstalowany jest pakiet po "npm:"
            name = rest[:alias]
    elif rest.startswith(_NOT_REGISTRY):
        return ""
    return name


_YARN_VERSION = re.compile(r'^\s+version:?\s+"?([0-9][^"\s]*)"?\s*$')


def _yarn(text: str) -> list[Dep]:
    """yarn.lock v1 (`version "1.2.3"`) i berry (`version: 1.2.3`): naglowek bloku to specyfikacje pakietu."""
    deps, name = [], ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            name = ""
            if line.rstrip().endswith(":") and not line.startswith("__metadata"):
                name = _yarn_name(line.rstrip()[:-1].split(",")[0].strip().strip('"'))
            continue
        match = _YARN_VERSION.match(line) if name else None
        if match:
            deps.append(("npm", name, match.group(1)))
            name = ""
    return deps


_PNPM_V5_KEY = re.compile(r"^(?P<name>(?:@[^/]+/)?[^/@]+)/(?P<version>\d[^_/(]*)")
_PNPM_KEY = re.compile(r"^  (['\"]?)(?P<key>[^'\"\s][^'\"]*?)\1:\s*$")


def _pnpm(text: str) -> list[Dep]:
    """pnpm-lock.yaml: klucze sekcji `packages:` — v5 '/nazwa/1.0.0', v6 '/nazwa@1.0.0', v9 'nazwa@1.0.0'."""
    head = re.search(r"^lockfileVersion:\s*['\"]?([0-9.]+)", text, re.MULTILINE)
    v5 = bool(head and head.group(1).startswith("5"))
    deps, in_packages = [], False
    for line in text.splitlines():
        if line and not line[0].isspace():
            in_packages = line.rstrip() == "packages:"
            continue
        match = _PNPM_KEY.match(line) if in_packages else None
        if not match:
            continue
        key = match.group("key").lstrip("/").split("(")[0]
        if v5:
            found = _PNPM_V5_KEY.match(key)
            name, version = (found.group("name"), found.group("version")) if found else ("", "")
        else:
            at = key.rfind("@")
            name, version = (key[:at], key[at + 1:]) if at > 0 else ("", "")
        if name and re.match(r"\d", version):
            deps.append(("npm", name, version))
    return deps


def _uv(text: str) -> list[Dep]:
    """uv.lock: pakiety z rejestru (bez samego projektu, pakietow edytowalnych i z gita)."""
    data = _toml(text)
    return [("PyPI", str(p["name"]).lower(), str(p["version"])) for p in data.get("package") or []
            if p.get("name") and p.get("version") and isinstance(p.get("source"), dict) and "registry" in p["source"]]


PARSERS: dict[str, Callable[[str], list[Dep]]] = {
    "package-lock.json": _npm,
    "npm-shrinkwrap.json": _npm,
    "yarn.lock": _yarn,
    "pnpm-lock.yaml": _pnpm,
    "poetry.lock": _poetry,
    "Pipfile.lock": _pipfile,
    "uv.lock": _uv,
    "go.mod": _gomod,
    "Cargo.lock": _cargo,
    "composer.lock": _composer,
    "Gemfile.lock": _gemfile,
}
_REQUIREMENTS = re.compile(r"^requirements([-_.][\w.-]+)?\.txt$")


def _parser(name: str) -> Callable[[str], list[Dep]] | None:
    return PARSERS.get(name) or (_requirements if _REQUIREMENTS.match(name) else None)


def find(local_dir: str) -> list[str]:
    """Lockfile'e w katalogu (sciezki lokalne, posortowane)."""
    try:
        names = os.listdir(local_dir)
    except OSError:
        return []
    return sorted(os.path.join(local_dir, n) for n in names
                  if _parser(n) and os.path.isfile(os.path.join(local_dir, n)))


def find_tree(local_dir: str, max_depth: int = MAX_DEPTH) -> dict[str, list[str]]:
    """
    Katalog -> jego lockfile'e, dla katalogu aplikacji i podkatalogow do `max_depth` w dol: projekt compose albo
    repozytorium ma je czesto w `backend/` i `frontend/`, a nie w korzeniu. Bez dowiazan symbolicznych, katalogow
    ukrytych i SKIP_DIRS, z limitem odwiedzonych katalogow.
    """
    found: dict[str, list[str]] = {}
    queue: deque[tuple[str, int]] = deque([(local_dir, 0)])
    visited = 0
    # wszerz: najpierw plytkie katalogi (tam sa aplikacje), wiec limit nie zjada pozniejszych przez jeden gleboki
    while queue and visited < MAX_DIRS:
        current, depth = queue.popleft()
        visited += 1
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        locks = [e.path for e in entries if _parser(e.name) and e.is_file(follow_symlinks=False)]
        if locks:
            found[current] = locks
        if depth < max_depth:
            queue.extend((e.path, depth + 1) for e in entries if e.is_dir(follow_symlinks=False)
                         and e.name not in SKIP_DIRS and not e.name.startswith("."))
    return found


def parse(local_path: str) -> list[Dep]:
    """Zaleznosci z jednego pliku; uszkodzony albo za duzy plik = pusta lista."""
    parser = _parser(os.path.basename(local_path))
    if parser is None:
        return []
    try:
        if os.path.getsize(local_path) > MAX_BYTES:
            return []
        with open(local_path, encoding="utf-8", errors="replace") as fh:
            deps = parser(fh.read())
    except (OSError, ValueError, TypeError, AttributeError, KeyError):     # TOMLDecodeError to ValueError
        return []
    return list(dict.fromkeys(deps))[:MAX_PACKAGES]
