"""
Obraz bazowy w Dockerfile — /zalataj obrazy podbija `FROM` do nowszej wersji w tej samej linii (core/patching.py).

Obraz budowany przez compose (`build:`) dostaje poprawki tylko z nowszego obrazu bazowego. `build --pull` wystarcza,
gdy tag plywa (`node:20`, `python:3.12-slim`), ale nie przy tagu przypietym do wersji (`node:20.11-slim`): pod nim
nic sie juz nie zmieni. Tu Pipe szuka nowszego tagu w tej samej linii — zmienia sie tylko ostatnia liczba, reszta
tagu zostaje:

  node:20.11-slim            -> node:20.18-slim             (nie node:22)
  python:3.11.4-slim-bookworm -> python:3.11.10-slim-bookworm (nie 3.12)
  postgres:15.4              -> postgres:15.8

Pominiete: tag z jedna liczba albo bez liczby (plywa — wystarczy `build --pull`), obraz przypiety skrotem (`@sha256`),
`FROM` ze zmienna (`${NODE_VERSION}`), etapy budowania (`FROM builder`), `scratch` i rejestry inne niz Docker Hub
(lista tagow wymagalaby logowania). Tagi z publicznego API Docker Hub, bez klucza. Bez importu `settings`.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

HUB_TAGS = "https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&name={name}"
MAX_PAGES = 5

_FROM = re.compile(r"^(\s*FROM\s+)((?:--\S+\s+)*)(\S+)(.*)$", re.I)
_IMAGE = re.compile(r"^(?:docker\.io/)?([a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?):([A-Za-z0-9._-]{1,128})$")
_TAG = re.compile(r"^(v?)(\d+(?:\.\d+)+)(.*)$")


@dataclass
class Bump:
    path: str            # Dockerfile (sciezka hosta)
    line: int            # numer linii (od 0)
    image: str           # 'node'
    old: str             # '20.11-slim'
    new: str             # '20.18-slim'

    def describe(self) -> str:
        return f"{self.image}:{self.old} → {self.image}:{self.new}"


def from_lines(text: str) -> list[tuple[int, str, str]]:
    """Linie `FROM` z obrazem z Docker Hub i tagiem: [(numer linii, obraz, tag)]. Etapy (`AS x`) sa pomijane jako zrodla."""
    found, stages = [], set()
    for index, line in enumerate(text.split("\n")):
        match = _FROM.match(line)
        if not match:
            continue
        ref, rest = match.group(3), match.group(4)
        alias = re.search(r"\s+AS\s+(\S+)", rest, re.I)
        if alias:
            stages.add(alias.group(1).lower())
        if ref.lower() in stages or "$" in ref or "@" in ref:
            continue
        image = _IMAGE.match(ref)
        if image:
            found.append((index, image.group(1), image.group(2)))
    return found


def hub_repo(image: str) -> str:
    """'node' -> 'library/node', 'bitnami/redis' -> 'bitnami/redis'."""
    return image if "/" in image else f"library/{image}"


def line_of(tag: str) -> tuple[str, re.Pattern] | None:
    """Tag z co najmniej dwiema liczbami -> (prefiks do wyszukania, wzorzec tagow tej samej linii); inaczej None."""
    match = _TAG.match(tag)
    if not match:
        return None
    v, numbers, suffix = match.groups()
    head = numbers.rsplit(".", 1)[0]
    return f"{v}{head}.", re.compile("^" + re.escape(f"{v}{head}.") + r"(\d+)" + re.escape(suffix) + "$")


def newer_tag(image: str, tag: str, fetch: Callable[[str], Any]) -> str:
    """Najnowszy tag tej samej linii, wyzszy niz obecny ('' — nie ma albo nie da sie sprawdzic)."""
    line = line_of(tag)
    if line is None:
        return ""
    prefix, pattern = line
    current = int(pattern.match(tag).group(1)) if pattern.match(tag) else -1
    url: str | None = HUB_TAGS.format(repo=hub_repo(image), name=urllib.parse.quote(prefix))
    best, best_tag = current, ""
    for _ in range(MAX_PAGES):
        if not url:
            break
        try:
            data = fetch(url)
        except Exception:
            return ""
        for entry in (data or {}).get("results") or []:
            name = str((entry or {}).get("name") or "")
            match = pattern.match(name)
            if match and int(match.group(1)) > best:
                best, best_tag = int(match.group(1)), name
        url = (data or {}).get("next") or None
        if url and not str(url).startswith("https://hub.docker.com/"):
            break
    return best_tag


def plan(path: str, text: str, fetch: Callable[[str], Any]) -> list[Bump]:
    """Co podbic w jednym Dockerfile."""
    bumps = []
    for index, image, tag in from_lines(text):
        new = newer_tag(image, tag, fetch)
        if new and new != tag:
            bumps.append(Bump(path, index, image, tag, new))
    return bumps


def apply(text: str, bumps: list[Bump]) -> str:
    """Tresc Dockerfile z nowymi tagami — zmienia sie tylko tag w linii FROM, reszta pliku bez zmian."""
    lines = text.split("\n")
    for bump in bumps:
        match = _FROM.match(lines[bump.line])
        if match and match.group(3).endswith(f"{bump.image}:{bump.old}"):
            ref = match.group(3)[: -len(bump.old)] + bump.new
            lines[bump.line] = match.group(1) + match.group(2) + ref + match.group(4)
    return "\n".join(lines)


def dockerfiles(config: dict, services: list[str]) -> list[str]:
    """Dockerfile'e uslug z `docker compose config --format json` (sciezki hosta)."""
    found = []
    for name in services:
        build = ((config.get("services") or {}).get(name) or {}).get("build")
        if not isinstance(build, dict) or not str(build.get("context", "")).startswith("/"):
            continue
        dockerfile = str(build.get("dockerfile") or "Dockerfile")
        path = dockerfile if dockerfile.startswith("/") else str(build["context"]).rstrip("/") + "/" + dockerfile
        if re.match(r"^/[A-Za-z0-9._/@+-]{1,4095}$", path) and "/../" not in path and path not in found:
            found.append(path)
    return found
