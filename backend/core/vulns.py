"""
Podatnosci w tym, co dziala na serwerze — skan bez LLM i bez kluczy API.

Trzy rodzaje celow (`Target.kind`):
  system  pakiety hosta z bazy dpkg (Debian/Ubuntu) albo apk (Alpine), pogrupowane po pakiecie
          ZRODLOWYM — OSV dla dystrybucji szuka po nazwie i wersji zrodla
  image   pakiety w obrazach dzialajacych kontenerow: ta sama baza czytana przez /proc/<pid>/root
          kontenera albo z jego warstw overlay (niczego w kontenerze nie uruchamiamy); jeden cel na obraz
  app     zaleznosci aplikacji z lockfile'i w katalogach z DIRECTORY (core/lockfiles.py)

scan() pyta OSV.dev /v1/querybatch (same ID), szczegoly /v1/vulns/<id> tylko dla nowych albo
zmienionych ID (pamiec w DATA_DIR/vulns.json), priorytet z CISA KEV (aktywnie wykorzystywane)
i severity (tekst dystrybucji/GHSA albo wynik CVSS v3).

Liczy sie tylko to, co ma poprawke: zwykly Debian ma setki CVE bez poprawki, wiec te sa
wylacznie liczba. Czuwanie (core/watch.py) skanuje, gdy zmieni sie cokolwiek z powyzszego,
i co VULN_INTERVAL (nowe podatnosci w starych wersjach); audyt (/audyt) i poranny raport
pokazuja wynik, alert przychodzi tylko o nowych pilnych.

Do OSV trafiaja nazwy, wersje i ekosystemy pakietow — nic wiecej. Synchroniczne (urllib),
wywolujacy uruchamia przez asyncio.to_thread. Bez importu `settings`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from backend.core import lockfiles, memory, runtime
from backend.core.i18n import tr
from backend.core.websearch import WebError, get_json

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
STORE = "vulns.json"
STORE_FORMAT = 2             # zmiana ukladu pamieci skanu = pelny skan od nowa
KEV_STORE = "kev.json"
KEV_MAX_AGE = 24 * 3600
BATCH_SIZE = 1000            # limit OSV na jedno zapytanie zbiorcze
MAX_PAGES = 20               # kolejne strony wynikow dla pakietu z bardzo wieloma ID (jadro: tysiace)
MAX_DETAILS = 5000           # szczegolow na jeden skan — reszta w nastepnym
DETAIL_WORKERS = 16
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_IMAGES = 30
DETAILS_FOR_LLM = 700        # tyle opisu z OSV dostaje model piszacy wyjasnienie
MAX_APPS = 50
URGENT = ("critical", "high")
DISTROS = ("Debian", "Ubuntu", "Alpine")
APP_KINDS = ("app", "compose", "repo")
# Biuletyny dystrybucji (Ubuntu USN, Debian DSA/DLA) wychodza razem z poprawka. Rekordy CVE dystrybucji
# (UBUNTU-CVE-*, DEBIAN-CVE-*) obejmuja tez to, czego nikt nie lata — dla jadra sa ich tysiace, wiec tam
# pobieramy tylko biuletyny.
ADVISORY = re.compile(r"^(USN|DSA|DLA)-")
KERNEL = re.compile(r"^linux(-|$)")
LEVEL_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}

_SEVERITY_WORDS = {
    "critical": "critical", "high": "high", "important": "high", "moderate": "medium", "medium": "medium",
    "low": "low", "negligible": "low", "unimportant": "low",
}


# ─── Inwentarz ──────────────────────────────────────────────────────────────

@dataclass
class Package:
    name: str                                   # pakiet zrodlowy (Debian/Ubuntu), origin (Alpine), zaleznosc
    version: str                                # wersja zrodla / zaleznosci
    binaries: list[str] = field(default_factory=list)   # zainstalowane pakiety z tego zrodla
    ecosystem: str = ""

    @property
    def key(self) -> str:
        return f"{self.ecosystem}|{self.name}|{self.version}"


@dataclass
class Target:
    kind: str                                   # system | image | app
    name: str                                   # 'system', obraz, katalog aplikacji
    ecosystem: str | None                       # 'Debian:12' (system, obraz), 'npm, PyPI' (aplikacja); None = nieobslugiwany
    packages: list[Package] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)   # obraz: image, image_id, containers, compose; app: files


Reader = Callable[[str], "str | None"]


def _read_host(path: str) -> str | None:
    local = runtime.to_local(path)
    try:
        with open(local, encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_DB_BYTES)
    except OSError:
        return None


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def os_release(read: Reader = _read_host) -> dict[str, str]:
    return _parse_os_release(read("/etc/os-release") or read("/usr/lib/os-release") or "")


def ecosystem(release: dict[str, str] | None = None) -> str | None:
    """Ekosystem OSV dla systemu ('Debian:12', 'Ubuntu:22.04:LTS', 'Alpine:v3.20') albo None."""
    release = os_release() if release is None else release
    dist, version = release.get("ID", "").lower(), release.get("VERSION_ID", "")
    if dist == "debian" and re.fullmatch(r"\d+", version):
        return f"Debian:{version}"
    if dist == "ubuntu" and re.fullmatch(r"\d+\.\d+", version):
        return f"Ubuntu:{version}" + (":LTS" if "LTS" in release.get("VERSION", "") else "")
    if dist == "alpine" and re.match(r"\d+\.\d+", version):
        return "Alpine:v" + ".".join(version.split(".")[:2])
    return None


def system_name(release: dict[str, str] | None = None) -> str:
    release = os_release() if release is None else release
    return release.get("PRETTY_NAME") or release.get("ID") or tr("nieznany system", "unknown system")


def _dpkg(text: str, eco: str = "") -> list[Package]:
    grouped: dict[tuple[str, str], Package] = {}
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        name, version = fields.get("Package"), fields.get("Version")
        if not (name and version and fields.get("Status", "").endswith(" installed")):
            continue
        source, source_version = name, version
        match = re.fullmatch(r"(\S+)(?:\s+\((\S+)\))?", fields.get("Source", ""))
        if match:
            source = match.group(1)
            source_version = match.group(2) or version
        arch = fields.get("Architecture", "")
        binary = f"{name}:{arch}" if arch and arch not in ("all", "amd64", "arm64") else name
        package = grouped.setdefault((source, source_version), Package(source, source_version, ecosystem=eco))
        if binary not in package.binaries:
            package.binaries.append(binary)
    return sorted(grouped.values(), key=lambda p: p.name)


def _apk(text: str, eco: str = "") -> list[Package]:
    grouped: dict[tuple[str, str], Package] = {}
    for block in text.split("\n\n"):
        fields = {line[0]: line[2:].strip() for line in block.splitlines() if len(line) > 2 and line[1] == ":"}
        name, version = fields.get("P"), fields.get("V")
        if not (name and version):
            continue
        origin = fields.get("o") or name
        package = grouped.setdefault((origin, version), Package(origin, version, ecosystem=eco))
        package.binaries.append(name)
    return sorted(grouped.values(), key=lambda p: p.name)


def _os_packages(read: Reader) -> tuple[str | None, str, list[Package]]:
    """(ekosystem, nazwa systemu, pakiety) dla systemu plikow czytanego przez `read`."""
    release = os_release(read)
    eco = ecosystem(release)
    name = system_name(release) if release else tr("brak /etc/os-release", "no /etc/os-release")
    if eco is None:
        return None, name, []
    if eco.startswith("Alpine"):
        return eco, name, _apk(read("/lib/apk/db/installed") or "", eco)
    return eco, name, _dpkg(read("/var/lib/dpkg/status") or "", eco)


def inventory() -> tuple[str | None, list[Package]]:
    """(ekosystem OSV, pakiety zrodlowe hosta) — ekosystem None, gdy systemu nie obslugujemy."""
    eco, _, packages = _os_packages(_read_host)
    return eco, packages


def container_reader(container: Any) -> Reader:
    """
    Odczyt pliku z systemu plikow kontenera: /proc/<pid>/root (dziala, gdy proces zyje i mamy do niego
    dostep), potem warstwy overlay od najwyzszej (UpperDir, LowerDir) przez system plikow hosta.
    """
    roots = []
    if getattr(container, "pid", 0):
        roots.append(f"{runtime.host_proc()}/{container.pid}/root")
    roots += [runtime.to_local(layer) for layer in getattr(container, "layers", None) or []]

    def read(path: str) -> str | None:
        for root in roots:
            try:
                with open(root + path, encoding="utf-8", errors="replace") as fh:
                    return fh.read(MAX_DB_BYTES)
            except OSError:
                continue
        return None
    return read


_COMPOSE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_HOST_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]{1,4095}$")
_IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$")


def image_targets(containers: list[Any] | None) -> list[Target]:
    """Jeden cel na obraz uzywany przez dzialajace kontenery."""
    by_image: dict[str, list[Any]] = {}
    for container in containers or []:
        if container.state == "running" and (container.image_id or container.image):
            by_image.setdefault(container.image_id or container.image, []).append(container)
    targets = []
    for image_id, group in list(by_image.items())[:MAX_IMAGES]:
        first = group[0]
        eco, system, packages = _os_packages(container_reader(first))
        compose = [{"container": c.name, "project": c.project, "service": c.service, "files": list(c.compose_files)}
                   for c in group if c.project and c.service and c.compose_files]
        targets.append(Target("image", first.image or image_id, eco, packages,
                              {"image": first.image, "image_id": image_id, "system": system,
                               "containers": sorted(c.name for c in group), "compose": compose}))
    return targets


def app_targets() -> list[Target]:
    """Zaleznosci z lockfile'i w katalogach aplikacji z DIRECTORY."""
    try:
        entries = [e for e in memory.load_directory() if e.kind in APP_KINDS]
    except OSError:
        return []
    targets = []
    for entry in entries[:MAX_APPS * 2]:
        files = lockfiles.find(runtime.to_local(entry.path))
        if not files:
            continue
        packages: dict[str, Package] = {}
        for local in files:
            for eco, name, version in lockfiles.parse(local):
                package = Package(name, version, [name], eco)
                packages.setdefault(package.key, package)
        if packages:
            ecos = ", ".join(sorted({p.ecosystem for p in packages.values()}))
            targets.append(Target("app", entry.path, ecos, sorted(packages.values(), key=lambda p: (p.ecosystem, p.name)),
                                  {"files": [runtime.to_host(f) for f in files]}))
        if len(targets) >= MAX_APPS:
            break
    return targets


def all_targets(containers: list[Any] | None = None) -> list[Target]:
    eco, system, packages = _os_packages(_read_host)
    return [Target("system", "system", eco, packages, {"system": system})] + image_targets(containers) + app_targets()


def inventory_hash(targets: list[Target]) -> str:
    text = "\n".join(f"{t.kind}|{t.name}|{t.ecosystem}|" + ",".join(p.key for p in t.packages) for t in targets)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ─── Severity ───────────────────────────────────────────────────────────────

_CVSS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(value: float) -> float:
    whole = int(round(value * 100000))
    return whole / 100000.0 if whole % 10000 == 0 else (math.floor(whole / 10000) + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """Wynik bazowy CVSS 3.x z wektora ('CVSS:3.1/AV:N/AC:L/...'); None dla innych wersji."""
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _CVSS["CIA"][m["C"]]) * (1 - _CVSS["CIA"][m["I"]]) * (1 - _CVSS["CIA"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        exploitability = 8.22 * _CVSS["AV"][m["AV"]] * _CVSS["AC"][m["AC"]] * pr * _CVSS["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min(1.08 * (impact + exploitability) if changed else impact + exploitability, 10))


def level_from_score(score: float) -> str:
    return "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"


def severity_level(vuln: dict[str, Any]) -> str:
    """critical | high | medium | low | '' (nieznana) — najpierw ocena dystrybucji/GHSA, potem CVSS v3."""
    from backend.core.software import _raw_severity

    raw = _raw_severity(vuln).split()
    word = _SEVERITY_WORDS.get(raw[0], "") if raw else ""
    if word:
        return word
    scores = [cvss3_score(str(item.get("score", ""))) for item in vuln.get("severity") or []]
    scores = [s for s in scores if s is not None]
    return level_from_score(max(scores)) if scores else ""


def _fixed_for(vuln: dict[str, Any]) -> dict[str, str]:
    """'ekosystem|pakiet' -> wersja z poprawka, dla wszystkich ekosystemow rekordu."""
    fixed: dict[str, str] = {}
    for affected in vuln.get("affected") or []:
        pkg = affected.get("package") or {}
        if not pkg.get("name") or not pkg.get("ecosystem"):
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if event.get("fixed"):
                    fixed[f"{pkg['ecosystem']}|{pkg['name']}"] = str(event["fixed"])
    return fixed


def _detail(vuln: dict[str, Any]) -> dict[str, Any]:
    linked = [vuln.get("id", "")] + list(vuln.get("aliases") or []) + list(vuln.get("upstream") or []) \
        + list(vuln.get("related") or [])
    cves = sorted({str(a) for a in linked if str(a).startswith("CVE-")})
    related = sorted({str(a) for a in linked[1:] if a and not str(a).startswith("CVE-")})
    summary = str(vuln.get("summary") or vuln.get("details") or "").strip().split("\n")[0][:160]
    ecosystems = sorted({str((a.get("package") or {}).get("ecosystem", "")) for a in vuln.get("affected") or []} - {""})
    return {"modified": str(vuln.get("modified", "")), "cves": cves, "severity": severity_level(vuln),
            "summary": summary, "details": str(vuln.get("details") or "").strip()[:DETAILS_FOR_LLM],
            "fixed": _fixed_for(vuln), "related": related, "ecosystems": ecosystems}


def _fixed_version(detail: dict[str, Any], package: Package) -> str:
    fixed = detail.get("fixed") or {}
    return fixed.get(f"{package.ecosystem}|{package.name}") \
        or fixed.get(f"{package.ecosystem.split(':')[0]}|{package.name}", "")


# ─── Wynik ──────────────────────────────────────────────────────────────────

@dataclass
class Vuln:
    id: str
    cves: list[str]
    severity: str
    summary: str
    fixed: str
    kev: bool = False

    @property
    def urgent(self) -> bool:
        return self.kev or self.severity in URGENT

    def label(self) -> str:
        cve = next((c for c in self.cves if c != self.id), "")
        return self.id + (f" ({cve})" if cve else "")


@dataclass
class PackageVulns:
    name: str
    version: str
    binaries: list[str]
    fixable: list[Vuln] = field(default_factory=list)
    unfixed: int = 0
    ecosystem: str = ""

    @property
    def urgent(self) -> bool:
        return any(v.urgent for v in self.fixable)

    @property
    def kev(self) -> bool:
        return any(v.kev for v in self.fixable)

    @property
    def worst(self) -> str:
        if self.kev:
            return "kev"
        return min((v.severity for v in self.fixable), key=lambda s: LEVEL_ORDER.get(s, 4), default="")

    @property
    def fixed_version(self) -> str:
        """
        Wersja usuwajaca wszystkie podatnosci: dla zaleznosci aplikacji najwyzsza z poprawek (semver),
        dla dystrybucji tylko gdy wszystkie wskazuja te sama (wersji Debiana nie porownujemy).
        """
        versions = {v.fixed for v in self.fixable}
        if len(versions) == 1:
            return versions.pop()
        if self.ecosystem and not self.ecosystem.startswith(DISTROS) and versions:
            return max(versions, key=_version_key)
        return ""


def _version_key(version: str) -> tuple:
    return tuple(int(part) if part.isdigit() else -1 for part in re.split(r"[.+-]", version.lstrip("v"))[:6])


def _items_from(data: list[dict[str, Any]]) -> list[PackageVulns]:
    return [PackageVulns(name=p["name"], version=p["version"], binaries=list(p.get("binaries") or []),
                         fixable=[Vuln(**v) for v in p.get("fixable") or []], unfixed=int(p.get("unfixed", 0)),
                         ecosystem=str(p.get("ecosystem", "")))
            for p in data or []]


@dataclass
class TargetResult:
    """Wynik dla obrazu albo aplikacji (system hosta jest w polach Scan — tak jak przed etapem kontenerow)."""
    kind: str
    name: str
    ecosystem: str | None
    checked: int = 0
    items: list[PackageVulns] = field(default_factory=list)
    unfixed: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def urgent(self) -> list[PackageVulns]:
        return [p for p in self.items if p.urgent]

    @property
    def kev(self) -> bool:
        return any(p.kev for p in self.items)

    def label(self) -> str:
        if self.kind == "image":
            containers = ", ".join(self.meta.get("containers") or [])
            return f"{self.name}" + (f" ({containers})" if containers else "")
        return f"{self.name} ({self.ecosystem})"


@dataclass
class Scan:
    at: float = 0.0
    ecosystem: str | None = None
    system: str = ""
    checked: int = 0
    items: list[PackageVulns] = field(default_factory=list)   # pakiety hosta z poprawkami
    unfixed: int = 0                                          # podatnosci hosta bez poprawki (liczba)
    error: str = ""
    partial: bool = False                                     # nie wszystkie szczegoly pobrane
    targets: list[TargetResult] = field(default_factory=list) # obrazy kontenerow i aplikacje

    @property
    def ok(self) -> bool:
        """Skan sie udal i bylo co sprawdzic (system hosta albo obrazy/aplikacje)."""
        return not self.error and (self.ecosystem is not None or any(t.ecosystem for t in self.targets))

    @property
    def urgent(self) -> list[PackageVulns]:
        return [p for p in self.items if p.urgent]

    @property
    def fixable_ids(self) -> set[str]:
        return {v.id for p in self.items for v in p.fixable}

    @property
    def urgent_ids(self) -> set[str]:
        return {v.id for p in self.items for v in p.fixable if v.urgent}

    def of_kind(self, kind: str) -> list[TargetResult]:
        return [t for t in self.targets if t.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scan":
        targets = [TargetResult(kind=t["kind"], name=t["name"], ecosystem=t.get("ecosystem"),
                                checked=int(t.get("checked", 0)), items=_items_from(t.get("items")),
                                unfixed=int(t.get("unfixed", 0)), meta=dict(t.get("meta") or {}))
                   for t in data.get("targets") or []]
        return cls(at=float(data.get("at", 0)), ecosystem=data.get("ecosystem"), system=str(data.get("system", "")),
                   checked=int(data.get("checked", 0)), items=_items_from(data.get("items")),
                   unfixed=int(data.get("unfixed", 0)), error=str(data.get("error", "")),
                   partial=bool(data.get("partial", False)), targets=targets)


def sort_items(items: list[PackageVulns]) -> list[PackageVulns]:
    rank = {"kev": -1, **LEVEL_ORDER}
    return sorted(items, key=lambda p: (rank.get(p.worst, 4), -len(p.fixable), p.name))


def describe_item(item: PackageVulns, limit: int = 3) -> str:
    """'openssl 3.0.11-1 -> 3.0.13-1: DSA-... (CVE-...) [high], ...'."""
    vulns = sorted(item.fixable, key=lambda v: (not v.kev, LEVEL_ORDER.get(v.severity, 4), v.id))
    parts = []
    for vuln in vulns[:limit]:
        tag = "KEV" if vuln.kev else vuln.severity
        parts.append(vuln.label() + (f" [{tag}]" if tag else ""))
    more = len(vulns) - limit
    if more > 0:
        parts.append(tr(f"i {more} innych", f"and {more} more"))
    target = f" -> {item.fixed_version}" if item.fixed_version else ""
    return f"{item.name} {item.version}{target}: " + ", ".join(parts)


def describe_target(target: TargetResult, limit: int = 2) -> str:
    """'nginx:1.25 (shop-nginx): 12 pakietow z poprawkami, 2 pilne — openssl ..., libxml2 ...'."""
    urgent = len(target.urgent)
    head = tr(f"{target.label()}: {len(target.items)} pakietow z poprawkami", f"{target.label()}: {len(target.items)} packages with fixes")
    if urgent:
        head += tr(f", {urgent} pilnych", f", {urgent} urgent")
    names = ", ".join(f"{p.name} {p.version}" + (f" -> {p.fixed_version}" if p.fixed_version else "")
                      for p in target.items[:limit])
    return head + (f" — {names}" if names else "")


# ─── Czytelny raport (Markdown) i wyjasnienia ───────────────────────────────

SEVERITY_WORD = {"critical": "krytyczna", "high": "wysoka", "medium": "srednia", "low": "niska"}
SEVERITY_WORD_EN = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}
REPORT_SYSTEM = 8            # pakietow hosta w raporcie
REPORT_TARGETS = 4           # obrazow i aplikacji
REPORT_PER_TARGET = 4        # pakietow na obraz/aplikacje
VULNS_PER_PACKAGE = 3
MAX_EXPLANATION = 280
_URL = re.compile(r"https?://\S+|www\.\S+")


def plain(text: str, limit: int = MAX_EXPLANATION) -> str:
    """
    Opis z OSV albo wyjasnienie modelu jako zwykly tekst: bez znacznikow Markdowna/HTML i linkow
    (to tresc z internetu — nie moze wstawic uzytkownikowi linku ani formatowania), jedna linia.
    """
    text = _URL.sub("", str(text or ""))
    text = re.sub(r"[`*<>\[\]]", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _sorted_vulns(item: PackageVulns) -> list[Vuln]:
    return sorted(item.fixable, key=lambda v: (not v.kev, LEVEL_ORDER.get(v.severity, 4), v.id))


def vuln_markdown(vuln: Vuln, explain: dict[str, str]) -> str:
    """'- `DSA-1` · CVE-2026-0001 · **aktywnie wykorzystywana (KEV)** — co to oznacza'."""
    from backend.core.text import as_code

    parts = [as_code(vuln.id)]
    cve = next((c for c in vuln.cves if c not in vuln.id), "")      # DEBIAN-CVE-2026-1 nie powtarza CVE-2026-1
    if cve:
        parts.append(cve)
    if vuln.kev:
        parts.append(tr("**aktywnie wykorzystywana (KEV)**", "**actively exploited (KEV)**"))
    elif vuln.severity:
        word = tr(SEVERITY_WORD, SEVERITY_WORD_EN).get(vuln.severity, vuln.severity)
        parts.append(f"**{word}**" if vuln.severity in URGENT else word)
    text = plain(explain.get(vuln.id) or vuln.summary)
    return "- " + " · ".join(parts) + (f" — {text}" if text else "")


def item_markdown(item: PackageVulns, explain: dict[str, str], limit: int = VULNS_PER_PACKAGE) -> list[str]:
    """Pakiet (pogrubiony) z wersja i poprawka w kodzie, pod nim podatnosci z wyjasnieniem."""
    from backend.core.text import as_code

    head = f"**{plain(item.name, 80)}** {as_code(item.version)}"
    if item.fixed_version:
        head += f" → {as_code(item.fixed_version)}"
    vulns = _sorted_vulns(item)
    # pusta linia przed pakietem — bez niej Markdown dokleilby naglowek do listy poprzedniego pakietu
    lines = ["", head] + [vuln_markdown(v, explain) for v in vulns[:limit]]
    if len(vulns) > limit:
        lines.append(tr(f"- … i {len(vulns) - limit} innych", f"- … and {len(vulns) - limit} more"))
    return lines


def report_sections(result: Scan) -> list[tuple[str, list[PackageVulns], int]]:
    """(naglowek, pakiety do pokazania, ile pakietow lacznie) — ta sama selekcja dla raportu i wyjasnien."""
    sections = []
    if result.items:
        urgent = len(result.urgent)
        head = tr(f"Pakiety serwera ({result.system}): {len(result.items)} z poprawkami",
                  f"Server packages ({result.system}): {len(result.items)} with fixes")
        head += tr(f", {urgent} pilnych", f", {urgent} urgent") if urgent else ""
        sections.append((head, result.items[:REPORT_SYSTEM], len(result.items)))
    images = sorted((t for t in result.of_kind("image") if t.items), key=lambda t: (not t.kev, not t.urgent, -len(t.items)))
    for target in images[:REPORT_TARGETS]:
        containers = ", ".join(target.meta.get("containers") or [])
        head = tr(f"Obraz {target.name}", f"Image {target.name}") + (f" ({containers})" if containers else "") \
            + tr(f": {len(target.items)} pakietow z poprawkami", f": {len(target.items)} packages with fixes")
        sections.append((head, target.items[:REPORT_PER_TARGET], len(target.items)))
    apps = sorted((t for t in result.of_kind("app") if t.items), key=lambda t: (not t.kev, -len(t.items)))
    for target in apps[:REPORT_TARGETS]:
        head = tr(f"Aplikacja {target.name} ({target.ecosystem}): {len(target.items)} zaleznosci z poprawkami",
                  f"Application {target.name} ({target.ecosystem}): {len(target.items)} dependencies with fixes")
        sections.append((head, target.items[:REPORT_PER_TARGET], len(target.items)))
    return sections


def report_markdown(result: Scan | None, explain: dict[str, str], note: str = "") -> str:
    """Pelny raport podatnosci dla /podatnosci: sekcje, pakiety, podatnosci z wyjasnieniami, co dalej."""
    if result is None:
        return tr("Nie ma jeszcze wyniku skanu podatnosci", "There is no vulnerability scan result yet") \
            + (f" ({note})." if note else ".")
    sections = report_sections(result)
    lines: list[str] = []
    if result.ecosystem is None:
        lines.append(tr(f"System serwera ({result.system}) nie jest obslugiwany — sprawdzam tylko obrazy i aplikacje.",
                        f"The server system ({result.system}) is not supported — checking only images and applications."))
    if not sections:
        lines.append(tr("Brak znanych podatnosci z poprawka.", "No known vulnerabilities with a fix."))
    for head, items, total in sections:
        lines += ["", f"**{head}**"]
        for item in items:
            lines += item_markdown(item, explain)
        if total > len(items):
            lines += ["", tr(f"… i {total - len(items)} kolejnych pakietow (/audyt)", f"… and {total - len(items)} more packages (/audit)")]
    if result.unfixed:
        lines += ["", tr(f"Bez poprawki (czekaja na dystrybucje): {result.unfixed}.",
                         f"Without a fix (waiting for the distribution): {result.unfixed}.")]
    actions = []
    if result.items:
        actions.append(tr("/zalataj — pakiety serwera", "/patch — server packages"))
    if any(t.items for t in result.of_kind("image")):
        actions.append(tr("/zalataj obrazy — nowsze obrazy kontenerow", "/patch images — newer container images"))
    if actions:
        lines += ["", tr("Co dalej: ", "Next: ") + "; ".join(actions) + "."]
    if result.error:
        lines.append(tr(f"Uwaga: ostatnie zapytanie do OSV nie powiodlo sie ({result.error}).",
                        f"Note: the last OSV query failed ({result.error})."))
    if note:
        lines.append(note)
    return "\n".join(lines).strip()


def explanations(lang: str) -> dict[str, str]:
    """Zapisane wyjasnienia podatnosci w danym jezyku: id -> tekst."""
    return dict((load_state().get("explain") or {}).get(lang) or {})


def save_explanations(lang: str, mapping: dict[str, str]) -> None:
    state = load_state()
    explain = state.get("explain") if isinstance(state.get("explain"), dict) else {}
    explain.setdefault(lang, {}).update({k: plain(v) for k, v in mapping.items() if plain(v)})
    state["explain"] = explain
    save_state(state)


def needing_explanation(result: Scan, lang: str, extra: list[PackageVulns] | None = None,
                        limit: int = 60) -> list[dict[str, str]]:
    """Pokazywane podatnosci bez wyjasnienia w tym jezyku — dane dla modelu (opis z OSV)."""
    known = explanations(lang)
    details = load_state().get("details") or {}
    items = [item for _, shown, _ in report_sections(result) for item in shown] + list(extra or [])
    pending: dict[str, dict[str, str]] = {}
    for item in items:
        for vuln in _sorted_vulns(item)[:VULNS_PER_PACKAGE]:
            if vuln.id in known or vuln.id in pending:
                continue
            detail = details.get(vuln.id) or {}
            pending[vuln.id] = {"id": vuln.id, "package": item.name, "ecosystem": item.ecosystem,
                                "summary": vuln.summary, "details": str(detail.get("details", ""))[:DETAILS_FOR_LLM]}
            if len(pending) >= limit:
                return list(pending.values())
    return list(pending.values())


def parse_explanations(answer: str, wanted: set[str]) -> dict[str, str]:
    """Odpowiedz modelu {"id": "tekst"} — tylko znane ID, tekst oczyszczony."""
    match = re.search(r"\{.*\}", answer or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): plain(v) for k, v in data.items() if str(k) in wanted and isinstance(v, str) and plain(v)}


# ─── Latki ──────────────────────────────────────────────────────────────────

_BINARY = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,127}(:[a-z0-9]{1,16})?$")


def upgrade_command(eco: str | None, items: list[PackageVulns]) -> str:
    """
    Dokladna komenda podbijajaca tylko te pakiety (bez pelnego upgrade). Nazwy z bazy pakietow hosta
    przechodza przez wzorzec nazwy pakietu — nic innego nie trafi do powloki. Konfiguracje zostaja
    w obecnej wersji (--force-confold), apt o nic nie pyta.
    """
    binaries = sorted({b for p in items for b in p.binaries if _BINARY.match(b)})
    if not binaries or not eco:
        return ""
    if eco.startswith("Alpine"):
        return "apk update && apk add --upgrade " + " ".join(binaries)
    return ("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade "
            "-o Dpkg::Options::=--force-confold " + " ".join(binaries))


@dataclass
class ImagePatch:
    command: str = ""
    inverse: list[str] = field(default_factory=list)     # powrot do poprzednich obrazow (dla /cofnij)
    manual: list[str] = field(default_factory=list)      # kontenery bez compose: tylko podpowiedz
    targets: list[TargetResult] = field(default_factory=list)


def image_patch(result: Scan, scope: str = "all") -> ImagePatch:
    """
    Nowsze obrazy dla kontenerow z compose: `docker compose -f ... -p ... pull <uslugi>` i `up -d <uslugi>`.
    Komendy odwrotne oznaczaja poprzedni obraz jego starym tagiem i odtwarzaja uslugi — to prawdziwy rollback.
    Kontenery bez compose dostaja tylko podpowiedz (docker run trzeba powtorzyc z tymi samymi opcjami).
    """
    patch = ImagePatch()
    projects: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    retag: dict[str, str] = {}
    for target in result.of_kind("image"):
        if not target.items or (scope == "urgent" and not target.urgent):
            continue
        patch.targets.append(target)
        ref, image_id = str(target.meta.get("image", "")), str(target.meta.get("image_id", ""))
        compose = [c for c in target.meta.get("compose") or []
                   if _COMPOSE_NAME.match(c.get("project", "")) and _COMPOSE_NAME.match(c.get("service", ""))
                   and c.get("files") and all(_HOST_PATH.match(f) for f in c["files"])]
        for item in compose:
            services = projects.setdefault((item["project"], tuple(item["files"])), [])
            if item["service"] not in services:
                services.append(item["service"])
        short = image_id.removeprefix("sha256:")[:12]
        if compose and _IMAGE_REF.match(ref) and re.fullmatch(r"[0-9a-f]{12}", short):
            retag[ref] = short
        with_compose = {c.get("container") for c in compose}
        for container in target.meta.get("containers") or []:
            if container not in with_compose:
                patch.manual.append(tr(f"{container}: kontener bez compose — docker pull {ref}, potem odtworz go "
                                       f"z tymi samymi opcjami", f"{container}: a container without compose — "
                                       f"docker pull {ref}, then recreate it with the same options"))
    segments, recreate = [], []
    for (project, files), services in projects.items():
        base = "docker compose " + " ".join(f"-f {shlex.quote(f)}" for f in files) + f" -p {shlex.quote(project)}"
        names = " ".join(shlex.quote(s) for s in services)
        segments.append(f"{base} pull {names} && {base} up -d {names}")
        recreate.append(f"{base} up -d {names}")
    patch.command = " && ".join(segments)
    patch.inverse = [f"docker tag {short} {shlex.quote(ref)}" for ref, short in retag.items()] + recreate
    return patch


def select(result: Scan, scope: str) -> list[PackageVulns]:
    """Pakiety hosta do zalatania: 'urgent' (KEV, krytyczne, wysokie — jak w alercie) albo wszystkie z poprawka."""
    return result.urgent if scope == "urgent" else list(result.items)


def patch_token(command: str) -> str:
    """Odcisk komendy z podgladu — wykonanie przechodzi tylko, gdy lista pakietow sie nie zmienila."""
    return hashlib.sha256(command.encode()).hexdigest()[:12]


def patch_preview(result: Scan, items: list[PackageVulns], command: str, reason: str = "",
                  explain: dict[str, str] | None = None) -> str:
    """Podglad latki pakietow hosta (Markdown): pakiety, dokladna komenda, czego bezpiecznik nie zrobi."""
    from backend.core.text import as_code

    urgent = sum(1 for p in items if p.urgent)
    lines = [tr(f"Latki dla {len(items)} pakietow" + (f" ({urgent} pilnych)" if urgent else "") + ":",
                f"Patches for {len(items)} packages" + (f" ({urgent} urgent)" if urgent else "") + ":")]
    for item in items[:12]:
        lines += item_markdown(item, explain or {}, 2)
    if len(items) > 12:
        lines += ["", tr(f"… i {len(items) - 12} kolejnych pakietow", f"… and {len(items) - 12} more packages")]
    lines += ["", tr("Komenda: ", "Command: ") + as_code(command), "",
              tr("Bezpiecznik sprawdzi strony przed i po instalacji i zapisze wpis w dzienniku. Pakietow nie cofne "
                 "automatycznie (starej wersji czesto nie ma juz w repozytorium). Biblioteki i jadro zaczna dzialac "
                 "po restarcie uslug albo serwera.",
                 "The safety fuse checks the sites before and after the install and writes a journal entry. I cannot "
                 "roll packages back automatically (the old version is often gone from the repository). Libraries and "
                 "the kernel apply after a service or server restart.")]
    if result.error:
        lines.append(tr(f"Uwaga: ostatni skan OSV nie powiodl sie ({result.error}), lista moze byc nieaktualna.",
                        f"Note: the last OSV scan failed ({result.error}), the list may be out of date."))
    if reason:
        lines += ["", reason]
    return "\n".join(lines)


def image_patch_preview(patch: ImagePatch, reason: str = "", explain: dict[str, str] | None = None) -> str:
    """Podglad nowszych obrazow: obrazy, komenda, powrot, kontenery bez compose."""
    from backend.core.text import as_code

    lines = [tr(f"Nowsze obrazy dla {len(patch.targets)} obrazow z podatnosciami:",
                f"Newer images for {len(patch.targets)} images with vulnerabilities:")]
    for target in patch.targets[:6]:
        containers = ", ".join(target.meta.get("containers") or [])
        lines += ["", f"**{plain(target.name, 120)}**" + (f" ({plain(containers, 200)})" if containers else "")]
        for item in target.items[:3]:
            lines += item_markdown(item, explain or {}, 1)
        if len(target.items) > 3:
            lines += ["", tr(f"… i {len(target.items) - 3} kolejnych pakietow", f"… and {len(target.items) - 3} more packages")]
    if patch.command:
        lines += ["", tr("Komenda: ", "Command: ") + as_code(patch.command), "",
                  tr("Pomoze tylko wtedy, gdy pod tym samym tagiem jest nowszy obraz (oficjalne obrazy sa regularnie "
                     "przebudowywane). Wlasny obraz trzeba przebudowac, a przypiety tag zmienic w compose. Bezpiecznik "
                     "sprawdzi konfiguracje compose, czy kontenery dzialaja i strony; /cofnij wraca do poprzednich obrazow.",
                     "This only helps when a newer image exists under the same tag (official images are rebuilt "
                     "regularly). Your own image has to be rebuilt, and a pinned tag changed in compose. The safety fuse "
                     "checks the compose config, that the containers run and the sites; /undo goes back to the previous images.")]
    if patch.manual:
        lines += [""] + [f"- {m}" for m in patch.manual]
    if reason:
        lines += ["", reason]
    return "\n".join(lines)


# ─── Pamiec skanu ───────────────────────────────────────────────────────────

def _store_path():
    return memory.data_dir() / STORE


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"format": STORE_FORMAT}
    if not isinstance(state, dict) or state.get("format") != STORE_FORMAT:
        return {"format": STORE_FORMAT}            # stary uklad (v0.32.0): pelny skan od nowa
    return state


def save_state(state: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({**state, "format": STORE_FORMAT}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def last_scan() -> Scan | None:
    data = load_state().get("last")
    return Scan.from_dict(data) if isinstance(data, dict) else None


def kev_ids(now: float | None = None) -> set[str]:
    """CVE z katalogu CISA KEV (aktywnie wykorzystywane). Raz na dobe; przy bledzie ostatnia kopia."""
    now = time.time() if now is None else now
    path = memory.data_dir() / KEV_STORE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    if cached.get("ids") and now - float(cached.get("at", 0)) < KEV_MAX_AGE:
        return set(cached["ids"])
    try:
        data = get_json(KEV_URL, timeout=30)
        ids = sorted({str(v.get("cveID")) for v in data.get("vulnerabilities") or [] if v.get("cveID")})
    except WebError:
        return set(cached.get("ids") or [])
    if ids:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": now, "ids": ids}), encoding="utf-8")
    return set(ids)


# ─── Skan ───────────────────────────────────────────────────────────────────

def query_ids(packages: list[Package]) -> dict[str, list[tuple[str, str]]]:
    """Package.key -> [(id, modified)] z OSV /v1/querybatch."""
    found: dict[str, list[tuple[str, str]]] = {}
    for start in range(0, len(packages), BATCH_SIZE):
        chunk = packages[start:start + BATCH_SIZE]
        for package in chunk:
            found.setdefault(package.key, [])
        pending = [(p, "") for p in chunk]
        for _ in range(MAX_PAGES):
            if not pending:
                break
            queries = []
            for package, token in pending:
                query: dict[str, Any] = {"package": {"name": package.name, "ecosystem": package.ecosystem},
                                         "version": package.version}
                if token:
                    query["page_token"] = token
                queries.append(query)
            data = get_json(OSV_BATCH, {"queries": queries}, timeout=60)
            results = data.get("results") or []
            next_pending = []
            for (package, _), result in zip(pending, results):
                ids = [(str(v.get("id")), str(v.get("modified", ""))) for v in (result or {}).get("vulns") or [] if v.get("id")]
                found[package.key].extend(ids)
                if (result or {}).get("next_page_token"):
                    next_pending.append((package, result["next_page_token"]))
            pending = next_pending
    return found


def _fetch_details(ids: list[str]) -> dict[str, dict[str, Any]]:
    def one(vuln_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return vuln_id, _detail(get_json(OSV_VULN + vuln_id, timeout=30))
        except WebError:
            return vuln_id, None

    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for vuln_id, detail in pool.map(one, ids):
            if detail is not None:
                result[vuln_id] = detail
    return result


def _wanted(package: Package, vuln_id: str) -> bool:
    """Dla jadra dystrybucji tylko biuletyny — patrz ADVISORY."""
    return not (package.ecosystem.startswith(DISTROS) and KERNEL.match(package.name)) or bool(ADVISORY.match(vuln_id))


def _package_vulns(package: Package, ids: list[str], details: dict[str, dict[str, Any]],
                   kev: set[str]) -> PackageVulns | None:
    """
    Podatnosci jednego pakietu. Rekord CVE objety biuletynem (USN-x -> UBUNTU-CVE-y) nie jest liczony
    drugi raz; biuletyn bez wlasnej oceny dostaje najgorsza ocene objetych rekordow. None = brak szczegolow.
    """
    item = PackageVulns(package.name, package.version, list(package.binaries), ecosystem=package.ecosystem)
    if any(vid not in details for vid in ids):
        return None
    covered = {rel for vid in ids if ADVISORY.match(vid) for rel in details[vid].get("related") or []}
    seen_cves: set[str] = set()
    if not package.ecosystem.startswith(DISTROS):
        # GHSA, PYSEC, GO... opisuja ten sam CVE kilka razy — najpierw te z ocena
        ids = sorted(ids, key=lambda v: (not details[v].get("severity"), not v.startswith("GHSA-"), v))
    base = package.ecosystem.split(":")[0]
    for vid in ids:
        detail = details[vid]
        ecosystems = detail.get("ecosystems")
        if ecosystems and not any(e == package.ecosystem or e.split(":")[0] == base for e in ecosystems):
            continue                       # rekord innej dystrybucji/ekosystemu — nie dotyczy tego pakietu
        fixed = _fixed_version(detail, package)
        if not fixed:
            item.unfixed += 1
            continue
        if vid in covered:
            continue
        cves = set(detail.get("cves") or [])
        if not package.ecosystem.startswith(DISTROS) and cves and cves <= seen_cves:
            continue
        seen_cves |= cves
        severity = str(detail.get("severity", ""))
        if not severity:
            levels = [str(details[r].get("severity", "")) for r in detail.get("related") or [] if r in details]
            severity = min(levels, key=lambda s: LEVEL_ORDER.get(s, 4), default="")
        item.fixable.append(Vuln(vid, list(detail.get("cves") or []), severity, str(detail.get("summary", "")),
                                 fixed, kev=any(c in kev for c in detail.get("cves") or [])))
    return item


def scan(*, force: bool = False, max_age: float = 0, now: float | None = None,
         containers: list[Any] | None = None) -> tuple[Scan, bool]:
    """
    Skan podatnosci hosta, obrazow dzialajacych kontenerow i aplikacji z DIRECTORY. Zwraca (wynik, czy
    odpytano OSV). Bez `force` OSV dostaje wszystko tylko co `max_age` sekund (nowe podatnosci w starych
    wersjach); w miedzyczasie tylko pakiety, ktorych jeszcze nie pytalismy (nowa wersja, nowy obraz,
    nowa zaleznosc) — dzieki temu /audyt zaraz po latce trwa sekundy, nie minute.
    """
    now = time.time() if now is None else now
    targets = all_targets(containers)
    system, others = targets[0], [t for t in targets[1:] if t.ecosystem]
    unsupported = [t for t in targets[1:] if not t.ecosystem]
    state = load_state()
    previous = state.get("last")
    digest = inventory_hash(targets)
    expired = force or now - float(state.get("queried_at", 0)) >= max_age
    if not expired and state.get("inventory") == digest and isinstance(previous, dict) \
            and not previous.get("error") and not previous.get("partial"):
        return Scan.from_dict(previous), False

    packages = {p.key: p for t in [system] + others if t.ecosystem for p in t.packages}
    cache: dict[str, Any] = {} if expired else state.get("found") or {}
    found = {key: [(str(i), str(m)) for i, m in cache[key]] for key in packages if key in cache}
    to_query = [p for key, p in packages.items() if key not in found]
    blank = Scan(at=now, ecosystem=system.ecosystem, system=str(system.meta.get("system", "")),
                 checked=len(system.packages))
    if not packages:
        state["last"] = blank.to_dict()
        save_state(state)
        return blank, False
    try:
        if to_query:
            found.update(query_ids(to_query))
    except WebError as exc:
        result = Scan.from_dict(previous) if isinstance(previous, dict) and previous.get("ecosystem") == system.ecosystem \
            else blank
        result.error = str(exc)
        state["last"] = result.to_dict()
        save_state(state)
        return result, True

    details: dict[str, dict[str, Any]] = state.get("details") or {}
    wanted = {vid: modified for key, ids in found.items() for vid, modified in ids if _wanted(packages[key], vid)}
    missing = [vid for vid, modified in wanted.items()
               if vid not in details or (modified and details[vid].get("modified") != modified)]
    partial = len(missing) > MAX_DETAILS
    details.update(_fetch_details(missing[:MAX_DETAILS]))
    details = {vid: d for vid, d in details.items() if vid in wanted}
    kev = kev_ids(now)

    def evaluate(target: Target) -> tuple[list[PackageVulns], int, bool]:
        items, unfixed, incomplete = [], 0, False
        for package in target.packages:
            ids = [vid for vid in dict.fromkeys(vid for vid, _ in found.get(package.key, [])) if vid in wanted]
            item = _package_vulns(package, ids, details, kev)
            if item is None:
                incomplete = True
                continue
            unfixed += item.unfixed
            if item.fixable:
                items.append(item)
        return sort_items(items), unfixed, incomplete

    result = Scan(at=now, ecosystem=system.ecosystem, system=str(system.meta.get("system", "")),
                  checked=len(system.packages))
    if system.ecosystem:
        result.items, result.unfixed, incomplete = evaluate(system)
        partial = partial or incomplete
    for target in others:
        items, unfixed, incomplete = evaluate(target)
        partial = partial or incomplete
        result.targets.append(TargetResult(target.kind, target.name, target.ecosystem, len(target.packages),
                                           items, unfixed, target.meta))
    for target in unsupported:
        result.targets.append(TargetResult(target.kind, target.name, None, 0, [], 0, target.meta))
    result.partial = partial
    state.update({"last": result.to_dict(), "inventory": digest, "details": details,
                  "found": {key: found.get(key, []) for key in packages}})
    if expired:
        state["queried_at"] = now
    save_state(state)
    return result, True


# ─── Powiadomienia ──────────────────────────────────────────────────────────

def notified_ids(key: str) -> set[str]:
    """ID, o ktorych alert `key` juz powiadomil (przezywa restart Pipe)."""
    return set((load_state().get("notified") or {}).get(key) or [])


def mark_notified(key: str, ids: set[str]) -> None:
    state = load_state()
    notified = state.get("notified") if isinstance(state.get("notified"), dict) else {}
    notified[key] = sorted(ids)
    state["notified"] = notified
    save_state(state)


def summary_line(result: Scan) -> str:
    urgent = result.urgent
    kev = sum(1 for p in result.items if p.kev)
    line = tr(f"{len(result.items)} pakietow z poprawkami podatnosci", f"{len(result.items)} packages with vulnerability fixes")
    extra = []
    if kev:
        extra.append(tr(f"{kev} aktywnie wykorzystywanych (KEV)", f"{kev} actively exploited (KEV)"))
    if urgent:
        extra.append(tr(f"{len(urgent)} pilnych", f"{len(urgent)} urgent"))
    return line + (f" ({', '.join(extra)})" if extra else "")
