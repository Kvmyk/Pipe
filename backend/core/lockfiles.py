"""
Zaleznosci aplikacji z plikow blokad (lockfile) — dla skanu podatnosci (core/vulns.py).

  find(dir)    lockfile'e w katalogu aplikacji (tylko ten katalog, bez node_modules i podkatalogow)
  parse(path)  [(ekosystem OSV, nazwa, wersja)] — npm, PyPI, Go, crates.io, Packagist, RubyGems

Tylko dokladne wersje: `requirements.txt` bez `==` nic nie mowi o tym, co jest zainstalowane.
Same odczyty plikow, bez uruchamiania menedzerow pakietow. Bez importu `settings`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable

try:
    import tomllib                    # Python 3.11+; na starszym poetry.lock i Cargo.lock sa pomijane
except ImportError:                   # pragma: no cover
    tomllib = None

MAX_BYTES = 20 * 1024 * 1024
MAX_PACKAGES = 5000

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


PARSERS: dict[str, Callable[[str], list[Dep]]] = {
    "package-lock.json": _npm,
    "npm-shrinkwrap.json": _npm,
    "poetry.lock": _poetry,
    "Pipfile.lock": _pipfile,
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
