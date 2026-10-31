"""
Podatnosci w tym, co dziala na serwerze — skan bez LLM i bez kluczy API.

Cztery rodzaje celow (`Target.kind`):
  system  pakiety hosta z bazy dpkg (Debian/Ubuntu) albo apk (Alpine), pogrupowane po pakiecie
          ZRODLOWYM — OSV dla dystrybucji szuka po nazwie i wersji zrodla. Jadro liczy sie w najnowszej
          zainstalowanej wersji (stare zostaja do autoremove, a nowe dziala po restarcie)
  image   pakiety w obrazach dzialajacych kontenerow: ta sama baza czytana przez /proc/<pid>/root
          kontenera albo z jego warstw overlay (niczego w kontenerze nie uruchamiamy); jeden cel na obraz
  server  pakiety zdalnych serwerow (cele ssh z target_manage): ta sama baza odczytana przez ssh
          (core/fleet.py pobiera, tu tylko parsowanie)
  app     zaleznosci aplikacji z lockfile'i (core/lockfiles.py): katalogi z DIRECTORY, projekty compose
          i repozytoria git na hoscie, razem z podkatalogami (backend/, frontend/...)

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

import functools
import hashlib
import json
import math
import os
import re
import shlex
import threading
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
MAX_SERVERS = 30
DETAILS_FOR_LLM = 700        # tyle opisu z OSV dostaje model piszacy wyjasnienie
MAX_APPS = 50
MAX_APP_ROOTS = 100          # korzeni aplikacji przegladanych w jednym skanie (kazdy najwyzej lockfiles.MAX_DIRS katalogow)
APP_ROOTS_TTL = 900          # repozytoria git na hoscie (przejscie po katalogach) — najwyzej co 15 minut
URGENT = ("critical", "high")
DISTROS = ("Debian", "Ubuntu", "Alpine")
APP_KINDS = ("app", "compose", "repo")
# Biuletyny dystrybucji (Ubuntu USN, Debian DSA/DLA) wychodza razem z poprawka. Rekordy CVE dystrybucji
# (UBUNTU-CVE-*, DEBIAN-CVE-*) obejmuja tez to, czego nikt nie lata — dla jadra sa ich tysiace, wiec tam
# pobieramy tylko biuletyny.
ADVISORY = re.compile(r"^(USN|DSA|DLA)-")
KERNEL = re.compile(r"^linux(-|$)")
# Pakiety jadra z wersja ABI w nazwie (linux-image-5.15.0-91-generic): nowe jadro to NOWY pakiet, wiec
# `--only-upgrade` na nich nic nie da — jadro podbija metapakiet (linux-image-generic, linux-image-amd64...).
KERNEL_ABI = re.compile(r"\d+\.\d+\.\d+-\d+")
KERNEL_META = re.compile(r"^linux-(image|headers|modules|tools|generic|virtual|lowlatency|kvm|aws|azure|gcp|gke|"
                         r"oracle|oem|hwe|cloud|ibm|raspi)[a-z0-9.+-]*$")
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


def _order(char: str) -> int:
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    return -1 if char == "~" else ord(char) + 256


def _verrevcmp(a: str, b: str) -> int:
    """Porownanie czesci wersji jak w dpkg (lib/dpkg/version.c): '~' przed koncem, litery przed reszta znakow."""
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i, j = i + 1, j + 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        first = 0
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            first = first or ord(a[i]) - ord(b[j])
            i, j = i + 1, j + 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first:
            return first
    return 0


def dpkg_compare(a: str, b: str) -> int:
    """<0, 0, >0 jak `dpkg --compare-versions` (epoka:wersja-rewizja)."""
    def split(version: str) -> tuple[int, str, str]:
        epoch, _, rest = version.partition(":") if ":" in version else ("0", "", version)
        upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return (int(epoch) if epoch.isdigit() else 0), upstream, revision
    (ea, ua, ra), (eb, ub, rb) = split(a), split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


def _newest_kernels(packages: list[Package]) -> list[Package]:
    """
    Jadro w kilku wersjach naraz (stare zostaja do autoremove, nowe czeka na restart): liczy sie najnowsze.
    Inaczej po instalacji poprawionego jadra stare dalej wygladalo na niezalatane, a Pipe pisal, ze
    "repozytoria nie maja poprawki".
    """
    newest: dict[str, Package] = {}
    for package in packages:
        if KERNEL.match(package.name):
            known = newest.get(package.name)
            if known is None or dpkg_compare(package.version, known.version) > 0:
                newest[package.name] = package
    return [p for p in packages if not KERNEL.match(p.name) or newest.get(p.name) is p]


def kernel_metapackages(packages: list[Package]) -> list[str]:
    """Zainstalowane metapakiety jadra (linux-image-generic, linux-image-amd64...) — to one podbijaja jadro."""
    return sorted({b.split(":")[0] for p in packages for b in p.binaries
                   if KERNEL_META.match(b.split(":")[0]) and not KERNEL_ABI.search(b)})


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


def _os_inventory(read: Reader) -> tuple[str | None, str, list[Package], list[str]]:
    """(ekosystem, nazwa systemu, pakiety, metapakiety jadra) dla systemu plikow czytanego przez `read`."""
    release = os_release(read)
    eco = ecosystem(release)
    name = system_name(release) if release else tr("brak /etc/os-release", "no /etc/os-release")
    if eco is None:
        return None, name, [], []
    if eco.startswith("Alpine"):
        return eco, name, _apk(read("/lib/apk/db/installed") or "", eco), []
    everything = _dpkg(read("/var/lib/dpkg/status") or "", eco)
    return eco, name, _newest_kernels(everything), kernel_metapackages(everything)


def _os_packages(read: Reader) -> tuple[str | None, str, list[Package]]:
    """(ekosystem, nazwa systemu, pakiety) dla systemu plikow czytanego przez `read`."""
    eco, name, packages, _ = _os_inventory(read)
    return eco, name, packages


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


def _by_image(containers: list[Any] | None) -> dict[str, list[Any]]:
    by_image: dict[str, list[Any]] = {}
    for container in containers or []:
        if container.state == "running" and (container.image_id or container.image):
            by_image.setdefault(container.image_id or container.image, []).append(container)
    return by_image


def _container_meta(group: list[Any]) -> dict[str, Any]:
    """Kontenery obrazu i ich projekty compose — to, z czego /zalataj obrazy sklada komende."""
    compose = [{"container": c.name, "project": c.project, "service": c.service, "files": list(c.compose_files)}
               for c in group if c.project and c.service and c.compose_files]
    return {"containers": sorted(c.name for c in group), "compose": compose}


def image_targets(containers: list[Any] | None) -> list[Target]:
    """Jeden cel na obraz uzywany przez dzialajace kontenery."""
    targets = []
    for image_id, group in list(_by_image(containers).items())[:MAX_IMAGES]:
        first = group[0]
        eco, system, packages = _os_packages(container_reader(first))
        targets.append(Target("image", first.image or image_id, eco, packages,
                              {"image": first.image, "image_id": image_id, "system": system, **_container_meta(group)}))
    return targets


def refresh_containers(result: Scan, containers: list[Any] | None) -> Scan:
    """
    Kontenery i compose obrazow z biezacego stanu Dockera. Zapisany skan (bez zmian w pakietach wraca do
    VULN_INTERVAL) pamieta kontenery i etykiety z chwili skanu — po odtworzeniu kontenera albo po poprawce
    rozpoznawania compose /zalataj obrazy widzialo stare dane ("kontener bez compose").
    """
    if containers is None:
        return result
    by_image = _by_image(containers)
    for target in result.of_kind("image"):
        group = by_image.get(str(target.meta.get("image_id", "")))
        if group:
            target.meta.update(_container_meta(group))
    return result


@dataclass
class ServerDump:
    """Pliki zdalnego serwera odczytane przez ssh (core/fleet.py) — albo blad polaczenia."""
    name: str                                   # nazwa celu z target_manage
    address: str = ""                           # user@host do opisu
    files: dict[str, str] = field(default_factory=dict)
    listing: list[str] = field(default_factory=list)    # /etc/apt/sources.list.d (Ubuntu Pro: pliki *esm*)
    error: str = ""


def esm_enabled(names: list[str]) -> bool:
    """Ubuntu Pro (ESM) wlaczone: w sources.list.d sa repozytoria esm — wtedy apt znajdzie wersje '+esm'."""
    return any("esm" in name for name in names)


def _local_listing(path: str) -> list[str]:
    try:
        return sorted(os.listdir(runtime.to_local(path)))
    except OSError:
        return []


def reboot_required(read: Reader = _read_host) -> bool:
    """Po instalacji jadra albo bibliotek system prosi o restart (/run/reboot-required)."""
    return read("/run/reboot-required") is not None or read("/var/run/reboot-required") is not None


def server_targets(dumps: list[ServerDump] | None) -> list[Target]:
    """Jeden cel na zdalny serwer; brak polaczenia albo nieobslugiwany system = ekosystem None."""
    targets = []
    for dump in (dumps or [])[:MAX_SERVERS]:
        meta: dict[str, Any] = {"address": dump.address, "error": dump.error}
        if dump.error:
            targets.append(Target("server", dump.name, None, [], {**meta, "system": ""}))
            continue
        read: Reader = dump.files.get
        eco, system, packages, kernel = _os_inventory(read)
        targets.append(Target("server", dump.name, eco, packages, {
            **meta, "system": system, "kernel_meta": kernel, "esm": esm_enabled(dump.listing),
            "reboot": reboot_required(read)}))
    return targets


_repo_cache: dict[str, Any] = {"at": 0.0, "paths": []}


def _repo_paths(now: float | None = None) -> list[str]:
    """Repozytoria git na hoscie (infra.git_repos), zapamietane na APP_ROOTS_TTL — to przejscie po katalogach."""
    from backend.core import infra

    now = time.time() if now is None else now
    if now - float(_repo_cache["at"]) >= APP_ROOTS_TTL:
        try:
            _repo_cache["paths"] = [r.path for r in infra.git_repos()]
        except OSError:
            _repo_cache["paths"] = []
        _repo_cache["at"] = now
    return list(_repo_cache["paths"])


def app_roots(containers: list[Any] | None = None) -> list[str]:
    """
    Gdzie szukac aplikacji (sciezki hosta): katalogi z DIRECTORY (app, compose, repo), katalogi projektow compose
    dzialajacych kontenerow i repozytoria git pod /root, /home, /srv, /opt, /var/www, /data. Wczesniej tylko
    DIRECTORY — bez wczesniejszego "znajdz repozytoria" aplikacje w ogole nie byly skanowane.
    """
    roots: list[str] = []
    try:
        roots += [e.path for e in memory.load_directory() if e.kind in APP_KINDS]
    except OSError:
        pass
    roots += sorted({str(c.workdir) for c in containers or [] if getattr(c, "project", "") and getattr(c, "workdir", "")})
    roots += _repo_paths()
    return [r for r in dict.fromkeys(roots) if _HOST_PATH.match(r)]


def _outer_roots(roots: list[str]) -> list[str]:
    """
    Bez korzeni, ktore find_tree() przejdzie razem z innym: ten sam katalog albo jego bezposredni podkatalog
    (glebsze zostaja — duzy korzen jak /root moglby wyczerpac limit katalogow, zanim do nich dojdzie).
    """
    kept: list[str] = []
    for root in sorted(roots, key=lambda r: (r.count("/"), r)):
        if not any(root == k or (root.startswith(k.rstrip("/") + "/") and root.count("/") - k.rstrip("/").count("/") <= 1)
                   for k in kept):
            kept.append(root)
    return kept


def app_targets(containers: list[Any] | None = None) -> list[Target]:
    """Zaleznosci z lockfile'i: jeden cel na katalog z lockfile'ami (korzen aplikacji albo np. jej backend/)."""
    targets: list[Target] = []
    seen: set[str] = set()
    for root in _outer_roots(app_roots(containers))[:MAX_APP_ROOTS]:
        for local_dir, files in lockfiles.find_tree(runtime.to_local(root)).items():
            path = runtime.to_host(local_dir)
            if path in seen:
                continue
            seen.add(path)
            packages: dict[str, Package] = {}
            for local in files:
                for eco, name, version in lockfiles.parse(local):
                    package = Package(name, version, [name], eco)
                    packages.setdefault(package.key, package)
            if packages:
                ecos = ", ".join(sorted({p.ecosystem for p in packages.values()}))
                targets.append(Target("app", path, ecos, sorted(packages.values(), key=lambda p: (p.ecosystem, p.name)),
                                      {"files": [runtime.to_host(f) for f in files]}))
            if len(targets) >= MAX_APPS:
                return targets
    return targets


def all_targets(containers: list[Any] | None = None, servers: list[ServerDump] | None = None) -> list[Target]:
    eco, system, packages, kernel = _os_inventory(_read_host)
    meta = {"system": system, "kernel_meta": kernel, "esm": esm_enabled(_local_listing("/etc/apt/sources.list.d")),
            "reboot": reboot_required()}
    return [Target("system", "system", eco, packages, meta)] + image_targets(containers) + server_targets(servers) \
        + app_targets(containers)


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
    def key(self) -> str:
        return f"{self.ecosystem}|{self.name}|{self.version}"

    @property
    def urgent(self) -> bool:
        return any(v.urgent for v in self.fixable)

    @property
    def kev(self) -> bool:
        return any(v.kev for v in self.fixable)

    @property
    def pro_only(self) -> bool:
        """Poprawka tylko w Ubuntu Pro (ESM): wersja '...+esm1' — bez subskrypcji apt jej nie znajdzie."""
        return bool(self.fixable) and all(_ESM.search(v.fixed) for v in self.fixable)

    @property
    def worst(self) -> str:
        if self.kev:
            return "kev"
        return min((v.severity for v in self.fixable), key=lambda s: LEVEL_ORDER.get(s, 4), default="")

    @property
    def fixed_version(self) -> str:
        """
        Wersja usuwajaca wszystkie podatnosci: najwyzsza z poprawek — dla zaleznosci aplikacji jak semver, dla
        Debiana i Ubuntu jak dpkg; dla Alpine tylko gdy wszystkie wskazuja te sama.
        """
        versions = {v.fixed for v in self.fixable}
        if len(versions) == 1:
            return versions.pop()
        if not versions or self.ecosystem.startswith("Alpine"):
            return ""
        if self.ecosystem.startswith(DISTROS):
            return max(versions, key=functools.cmp_to_key(dpkg_compare))
        return max(versions, key=_version_key) if self.ecosystem else ""


_ESM = re.compile(r"[+~]esm\d*")


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
        if self.kind == "server":
            return f"{self.name} ({self.meta.get('system') or self.meta.get('error') or '?'})"
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
    targets: list[TargetResult] = field(default_factory=list) # obrazy kontenerow, zdalne serwery i aplikacje
    kernel_meta: list[str] = field(default_factory=list)      # metapakiety jadra hosta (to one podbijaja jadro)
    esm: bool = False                                         # Ubuntu Pro wlaczone (apt znajdzie wersje +esm)
    reboot: bool = False                                      # system czeka na restart (nowe jadro, biblioteki)

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
                   partial=bool(data.get("partial", False)), targets=targets,
                   kernel_meta=[str(n) for n in data.get("kernel_meta") or []], esm=bool(data.get("esm", False)),
                   reboot=bool(data.get("reboot", False)))


def sort_items(items: list[PackageVulns]) -> list[PackageVulns]:
    rank = {"kev": -1, **LEVEL_ORDER}
    return sorted(items, key=lambda p: (rank.get(p.worst, 4), -len(p.fixable), p.name))


def target_rank(target: TargetResult) -> tuple:
    """Kolejnosc obrazow, serwerow i aplikacji: najpierw najgrozniejsza podatnosc (KEV, krytyczna...), potem liczba."""
    rank = {"kev": -1, **LEVEL_ORDER}
    return min((rank.get(p.worst, 4) for p in target.items), default=5), -len(target.items), target.name


def describe_item(item: PackageVulns, explain: dict[str, str] | None = None, limit: int = 1) -> str:
    """
    Jedna linia z opisem: 'openssl 3.0.11-1 → 3.0.13-1 — krytyczna: atakujacy moze... (CVE-...); i 2 inne'.
    Dla audytu, raportu porannego i promptu — wszedzie, gdzie nie ma miejsca na pelny raport.
    """
    vulns = _sorted_vulns(item)
    parts = []
    for vuln in vulns[:limit]:
        word = severity_word(vuln)
        parts.append((f"{word.lower()}: " if word else "") + description(vuln, explain or {}) + f" ({vuln_ref(vuln)})")
    more = len(vulns) - limit
    if more > 0:
        parts.append(tr(f"kolejnych podatnosci: {more}", f"{more} more"))
    return package_label(item) + (" — " + "; ".join(parts) if parts else "")


def describe_target(target: TargetResult, explain: dict[str, str] | None = None, limit: int = 1) -> str:
    """'nginx:1.25 (shop-nginx): 12 pakietow z poprawka (pilne: 2) — openssl ... — krytyczna: opis (...)'."""
    urgent = len(target.urgent)
    head = tr(f"{target.label()}: {n_packages(len(target.items))} z poprawka",
              f"{target.label()}: {n_packages(len(target.items))} with a fix")
    if urgent:
        head += tr(f" (pilne: {urgent})", f" (urgent: {urgent})")
    items = sort_items(target.items)[:limit]
    return head + (" — " + "; ".join(describe_item(p, explain) for p in items) if items else "")


# ─── Czytelny raport (Markdown) i wyjasnienia ───────────────────────────────
#
# Kazda podatnosc ma opis: wyjasnienie modelu (jezyk Pipe), a bez niego tekst z OSV. Numer CVE jest na koncu
# w nawiasie — najpierw to, co grozi. Bez kodu i kursywy: w Telegramie, pipe web i CLI czyta sie to jak zwykly tekst.

SEVERITY_WORD = {"critical": "Krytyczna", "high": "Wysoka", "medium": "Srednia", "low": "Niska"}
SEVERITY_WORD_EN = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
REPORT_SYSTEM = 8            # pakietow hosta w raporcie
REPORT_TARGETS = 4           # obrazow i aplikacji
REPORT_PER_TARGET = 4        # pakietow na obraz/aplikacje
VULNS_PER_PACKAGE = 1        # podatnosci z opisem na pakiet (najpowazniejsza); reszta jako liczba przy pakiecie
ALERT_PACKAGES = 12          # pakietow hosta w alercie (i tak przycina MESSAGE_BUDGET)
ALERT_TARGETS = 4            # obrazow / aplikacji w alercie
ALERT_PER_TARGET = 3         # pakietow na obraz / aplikacje w alercie
# Alert, raport i podglad latki to JEDNA wiadomosc: tyle znakow Markdownu, po zamianie na HTML Telegrama
# (<b>, encje) zostaje zapas do jego limitu 4096. Pakiety, ktore sie nie mieszcza, sa policzone, nie pokazane.
MESSAGE_BUDGET = 3300
SHORT_COMMAND = 400          # dluzsza komenda latki jest w podgladzie skracana (pelna trafia do dziennika)
MAX_EXPLANATION = 280
_URL = re.compile(r"https?://\S+|www\.\S+")
_CVE = re.compile(r"CVE-\d{4}-\d+")
# Tytuly biuletynow, ktore nic nie mowia ('openssl - security update', 'OpenSSL vulnerabilities')
_GENERIC_SUMMARY = re.compile(r"(security update|vulnerabilit(y|ies)|regression|update)\s*$", re.IGNORECASE)


# Teksty Pipe sa bez polskich znakow; wyjasnienia modelu czasem je maja ("Atakujący" obok "Atakujacy") —
# w jednym raporcie to wyglada na blad, wiec opisy sa sprowadzane do tego samego zapisu.
_PL_ASCII = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def count(n: int, one: str, few: str, many: str, one_en: str = "", many_en: str = "") -> str:
    """'1 pakiet', '2 pakiety', '5 pakietow' (po angielsku 'package' / 'packages')."""
    if one_en:
        return tr(f"{n} {one if n == 1 else few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many}",
                  f"{n} {one_en if n == 1 else many_en}")
    return f"{n} {one if n == 1 else few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many}"


def n_packages(n: int) -> str:
    return count(n, "pakiet", "pakiety", "pakietow", "package", "packages")


def n_images(n: int) -> str:
    return count(n, "obraz", "obrazy", "obrazow", "image", "images")


def n_deps(n: int) -> str:
    return count(n, "zaleznosc", "zaleznosci", "zaleznosci", "dependency", "dependencies")


def plain(text: str, limit: int = MAX_EXPLANATION) -> str:
    """
    Opis z OSV albo wyjasnienie modelu jako zwykly tekst: bez znacznikow Markdowna/HTML i linkow
    (to tresc z internetu — nie moze wstawic uzytkownikowi linku ani formatowania), jedna linia, bez polskich znakow.
    """
    text = _URL.sub("", str(text or "")).translate(_PL_ASCII)
    text = re.sub(r"[`*<>\[\]]", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def osv_text(detail: dict[str, Any]) -> str:
    """Opis z rekordu OSV: tytul, a gdy tytul jest ogolnikiem biuletynu — tresc rekordu."""
    summary = str(detail.get("summary") or "").strip()
    body = str(detail.get("details") or "").strip()
    if body and (not summary or len(summary) < 30 or _GENERIC_SUMMARY.search(summary)):
        return body
    return summary


def _sorted_vulns(item: PackageVulns) -> list[Vuln]:
    return sorted(item.fixable, key=lambda v: (not v.kev, LEVEL_ORDER.get(v.severity, 4), v.id))


def severity_word(vuln: Vuln) -> str:
    if vuln.kev:
        return tr("Aktywnie wykorzystywana", "Actively exploited")
    return tr(SEVERITY_WORD, SEVERITY_WORD_EN).get(vuln.severity, "")


def vuln_ref(vuln: Vuln) -> str:
    """Numer, ktory ludzie znaja: CVE, gdy jest jeden; biuletyn obejmujacy kilka CVE — z ich liczba."""
    own = _CVE.search(vuln.id)
    if own:
        return own.group(0)
    cves = sorted(set(vuln.cves))
    if len(cves) == 1:
        return cves[0]
    if cves:
        return f"{plain(vuln.id, 60)}, {len(cves)} CVE"
    return plain(vuln.id, 60)


def description(vuln: Vuln, explain: dict[str, str]) -> str:
    return plain(explain.get(vuln.id) or vuln.summary) \
        or tr("baza OSV nie podaje opisu tej podatnosci", "the OSV database gives no description of this vulnerability")


def vuln_markdown(vuln: Vuln, explain: dict[str, str]) -> str:
    """'- **Krytyczna:** Atakujacy przez siec moze ... (CVE-2026-0001)'."""
    word = severity_word(vuln)
    return "- " + (f"**{word}:** " if word else "") + description(vuln, explain) + f" ({vuln_ref(vuln)})"


# Pakiety zrodlowe jadra dystrybucji — w raporcie "linux (jadro)", bo sama nazwa "linux" niewiele mowi
_KERNEL_SOURCE = re.compile(r"^linux(-signed([-\w.]*)?|-meta([-\w.]*)?|-hwe[-\w.]*|-(aws|azure|gcp|gke|oracle|kvm|lowlatency|oem)[-\w.]*)?$")


def display_name(item: PackageVulns) -> str:
    """Nazwa pakietu do pokazania: jadro dystrybucji z dopiskiem, ze to jadro."""
    name = plain(item.name, 80)
    if item.ecosystem.startswith(DISTROS) and _KERNEL_SOURCE.match(item.name):
        name += tr(" (jadro)", " (kernel)")
    return name


def package_label(item: PackageVulns) -> str:
    """'openssl 3.0.11-1 → 3.0.13-1' (wersja z poprawka, gdy jedna usuwa wszystko)."""
    label = f"{display_name(item)} {plain(item.version, 80)}"
    return label + (f" → {plain(item.fixed_version, 80)}" if item.fixed_version else "")


def item_markdown(item: PackageVulns, explain: dict[str, str], limit: int = VULNS_PER_PACKAGE) -> list[str]:
    """
    Pakiet (nazwa pogrubiona, wersja → poprawka, ile podatnosci), pod nim najpowazniejsze z opisem:
    '**vim** 8.2.1 → 8.2.4 · 11 podatnosci' / '- **Srednia:** Atakujacy moze ... (USN-8101-1)'.
    Poprawka tylko w Ubuntu Pro jest oznaczona — /zalataj zainstaluje ja dopiero z subskrypcja.
    """
    vulns = _sorted_vulns(item)
    head = f"**{display_name(item)}** {plain(item.version, 80)}"
    if item.fixed_version:
        head += f" → {plain(item.fixed_version, 80)}"
    if len(vulns) > limit:
        head += " · " + count(len(vulns), "podatnosc", "podatnosci", "podatnosci", "vulnerability", "vulnerabilities")
    if item.pro_only:
        head += tr(" · poprawka tylko w Ubuntu Pro", " · fix only in Ubuntu Pro")
    # pusta linia przed pakietem — bez niej Markdown dokleilby naglowek do listy poprzedniego pakietu
    return ["", head] + [vuln_markdown(v, explain) for v in vulns[:limit]]


def sections_markdown(sections: list[tuple[str, list[PackageVulns], int]], explain: dict[str, str], *,
                      prefix: list[str] | None = None, footer: list[str] | None = None,
                      budget: int = MESSAGE_BUDGET) -> str:
    """
    Sekcje (naglowek albo '', pakiety, ile lacznie) jako jedna wiadomosc: pakiety po kolei, dopoki miesci sie
    `budget` razem ze stopka; pod sekcja '… i N kolejnych pakietow'. Stopka (co dalej) zawsze zostaje.
    """
    lines = list(prefix or [])
    tail = list(footer or [])
    reserve = len("\n".join(tail)) + 60 * (len(sections) + 1)

    def fits(block: list[str]) -> bool:
        return len("\n".join(lines + block)) + reserve <= budget

    dropped: list[str] = []
    for head, items, total in sections:
        # naglowek tylko razem z pierwszym pakietem — sama "Aplikacja X: 9 zaleznosci" i "… i 9 kolejnych" nic nie mowi
        first = item_markdown(items[0], explain) if items else []
        if head and not fits(["", f"### {head}"] + first):
            dropped.append(head.rsplit(": ", 1)[0])
            continue
        if head:
            lines += ["", f"### {head}"]
        shown = 0
        for item in items:
            block = item_markdown(item, explain)
            if not fits(block):
                break
            lines += block
            shown += 1
        if total > shown:
            lines += ["", tr(f"… i {total - shown} kolejnych", f"… and {total - shown} more")]
    if dropped:
        lines += ["", tr("Nie zmiescilo sie w tej wiadomosci: ", "Did not fit in this message: ") + "; ".join(dropped)
                  + tr(" — pelna lista w pipe web albo zapytaj mnie.", " — the full list is in pipe web, or ask me.")]
    if tail:
        lines += [""] + tail
    return "\n".join(lines).strip()


def short_command(command: str, limit: int = SHORT_COMMAND) -> str:
    """Bardzo dluga komenda latki skrocona do podgladu: poczatek + ile argumentow pominieto."""
    if len(command) <= limit:
        return command
    head = command[:limit].rsplit(" ", 1)[0]
    rest = len(command[len(head):].split())
    return head + tr(f" … (+{rest} pakietow)", f" … (+{rest} packages)")


def report_sections(result: Scan) -> list[tuple[str, list[PackageVulns], int]]:
    """
    (naglowek, pakiety do pokazania, ile pakietow lacznie) — ta sama selekcja dla raportu i wyjasnien. Najpierw
    pakiety tego serwera, potem zdalne serwery, obrazy i aplikacje razem, od najgrozniejszej podatnosci: gdy
    wszystko sie nie miesci, wypada to, co mniej grozne, a nie to, co akurat bylo na koncu listy.
    """
    sections = []
    if result.items:
        urgent = len(result.urgent)
        where = tr("Pakiety tego serwera", "This server's packages") if result.of_kind("server") \
            else tr("Pakiety serwera", "Server packages")
        head = tr(f"{where} ({result.system}): {n_packages(len(result.items))} z poprawka",
                  f"{where} ({result.system}): {n_packages(len(result.items))} with a fix")
        head += tr(f", pilne: {urgent}", f", urgent: {urgent}") if urgent else ""
        sections.append((head, result.items[:REPORT_SYSTEM], len(result.items)))
    others: list[tuple[TargetResult, str]] = []
    for target in servers_with_items(result)[:REPORT_TARGETS]:
        others.append((target, tr(f"Serwer {plain(target.name, 60)} ({plain(target.meta.get('system', ''), 80)}): "
                                  f"{n_packages(len(target.items))} z poprawka",
                                  f"Server {plain(target.name, 60)} ({plain(target.meta.get('system', ''), 80)}): "
                                  f"{n_packages(len(target.items))} with a fix")))
    for target in sorted((t for t in result.of_kind("image") if t.items), key=target_rank)[:REPORT_TARGETS]:
        containers = ", ".join(target.meta.get("containers") or [])
        others.append((target, tr(f"Obraz {target.name}", f"Image {target.name}") + (f" ({containers})" if containers else "")
                       + tr(f": {n_packages(len(target.items))} z poprawka", f": {n_packages(len(target.items))} with a fix")))
    for target in sorted((t for t in result.of_kind("app") if t.items), key=target_rank)[:REPORT_TARGETS]:
        others.append((target, tr(f"Aplikacja {target.name} ({target.ecosystem}): {n_deps(len(target.items))} z poprawka",
                                  f"Application {target.name} ({target.ecosystem}): {n_deps(len(target.items))} with a fix")))
    for target, head in sorted(others, key=lambda pair: target_rank(pair[0])[:2]):
        sections.append((head, target.items[:REPORT_PER_TARGET], len(target.items)))
    return sections


def report_markdown(result: Scan | None, explain: dict[str, str], note: str = "") -> str:
    """Raport podatnosci dla /podatnosci (jedna wiadomosc): sekcje, pakiety z opisem najpowazniejszej, co dalej."""
    if result is None:
        return tr("Nie ma jeszcze wyniku skanu podatnosci", "There is no vulnerability scan result yet") \
            + (f" ({note})." if note else ".")
    sections = report_sections(result)
    if not sections:
        prefix = [tr("Brak znanych podatnosci z poprawka.", "No known vulnerabilities with a fix.")]
        if result.ecosystem is None:
            prefix.append(todo(result)[0])
    else:
        # najpierw co z tym zrobic (jedna linia na serwer, obrazy, aplikacje), potem szczegoly
        prefix = [tr("**Co z tym zrobic**", "**What to do about it**")] + [f"- {line}" for line in todo(result)]
    footer: list[str] = []
    if result.error:
        footer.append(tr(f"Uwaga: ostatnie zapytanie do OSV nie powiodlo sie ({result.error}).",
                         f"Note: the last OSV query failed ({result.error})."))
    if note:
        footer.append(note)
    return sections_markdown(sections, explain, prefix=prefix, footer=footer)


# ─── Widok dla pipe web (dane, nie Markdown) ────────────────────────────────
#
# pipe web rysuje raport, podglad i wynik latki z tych slownikow (karty, znaczniki, przyciski), Telegram i CLI
# dalej dostaja Markdown z `text`. Teksty przechodza przez plain() tak samo jak w Markdownie.

VIEW_ITEMS = 60              # pakietow na grupe (reszta jako liczba)
VIEW_VULNS = 3               # podatnosci z opisem na pakiet


def item_view(item: PackageVulns, explain: dict[str, str], held: bool = False) -> dict[str, Any]:
    vulns = _sorted_vulns(item)
    return {"name": plain(item.name, 80), "version": plain(item.version, 80), "fixed": plain(item.fixed_version, 80),
            "level": item.worst or "unknown", "count": len(vulns), "held": held, "pro_only": item.pro_only,
            "vulns": [{"level": "kev" if v.kev else (v.severity or "unknown"), "word": severity_word(v),
                       "text": description(v, explain), "ref": vuln_ref(v)} for v in vulns[:VIEW_VULNS]]}


def group_view(kind: str, title: str, subtitle: str, items: list[PackageVulns], explain: dict[str, str], *,
               held: set[str] | None = None, patchable: bool = False, note: str = "") -> dict[str, Any]:
    held = held or set()
    ordered = sort_items(items)
    return {"kind": kind, "title": plain(title, 160), "subtitle": plain(subtitle, 240), "count": len(ordered),
            "urgent": sum(1 for p in ordered if p.urgent), "kev": sum(1 for p in ordered if p.kev),
            "patchable": patchable, "note": note,
            "items": [item_view(p, explain, p.key in held) for p in ordered[:VIEW_ITEMS]]}


def report_view(result: Scan | None, explain: dict[str, str], note: str = "") -> dict[str, Any]:
    """Raport podatnosci jako dane: podsumowanie, grupy (serwer, obrazy, aplikacje), akcje latek, uwagi."""
    if result is None:
        return {"at": 0, "groups": [], "actions": [], "notes": [note] if note else [], "totals": {}, "supported": False}
    held = {p.key for p in waiting_items(result)}
    patch = image_patch(result)
    patchable_images = {t.name for t in patch.targets}
    groups = []
    if result.items:
        groups.append(group_view("system", tr("Pakiety serwera", "Server packages"), result.system, result.items, explain,
                                 held=held, patchable=bool(select(result, "all")), note=held_note(waiting_items(result))))
    for target in servers_with_items(result):
        waiting = waiting_server_items(target)
        groups.append(group_view("server", target.name, str(target.meta.get("system", "")), target.items, explain,
                                 held={p.key for p in waiting}, patchable=bool(select_server(target)),
                                 note=held_note(waiting)))
    images = sorted((t for t in result.of_kind("image") if t.items), key=target_rank)
    waiting = {t.name for t in patch.waiting}
    for target in images:
        containers = ", ".join(target.meta.get("containers") or [])
        manual = [m for m in patch.manual if any(m.startswith(f"{c}:") for c in target.meta.get("containers") or [])]
        note_text = tr("Bez zmian po ostatniej latce: ", "Unchanged after the last patch: ") + _waiting_why(target) + "." \
            if target.name in waiting else (manual[0] if manual else "")
        groups.append(group_view("image", target.name, containers, target.items, explain,
                                 patchable=target.name in patchable_images, note=note_text))
    from backend.core import appdeps
    held_apps = held_packages()
    for target in sorted((t for t in result.of_kind("app") if t.items), key=target_rank):
        bump = appdeps.plan_target(target, held_apps)
        waiting_keys = {p.key for p in target.items if appdeps.app_key(target, p) in held_apps}
        note_text = tr("Zalataj podbije je w plikach zaleznosci (" + ", ".join(b.manager for b in bump.bumps) + ").",
                       "Patch bumps them in the dependency files (" + ", ".join(b.manager for b in bump.bumps) + ").") \
            if bump.items else "; ".join(bump.manual) or tr(
                "Podbij wersje w pliku zaleznosci recznie i wdroz aplikacje.",
                "Bump the version in the dependency file by hand and deploy the app.")
        groups.append(group_view("app", target.name, target.ecosystem or "", target.items, explain,
                                 held=waiting_keys, patchable=bool(bump.items), note=note_text))
    all_items = result.items + [p for t in result.targets for p in t.items]
    notes = []
    if result.ecosystem is None:
        notes.append(tr(f"System serwera ({result.system}) nie jest obslugiwany — sprawdzam tylko obrazy i aplikacje.",
                        f"The server system ({result.system}) is not supported — checking only images and applications."))
    notes += server_notes(result)
    if patch.skipped:
        notes.append(waiting_note(ImagePatch(skipped=patch.skipped)))
    if result.error:
        notes.append(tr(f"Ostatnie zapytanie do OSV nie powiodlo sie ({result.error}) — to wynik poprzedniego skanu.",
                        f"The last OSV query failed ({result.error}) — this is the previous scan's result."))
    if result.partial:
        notes.append(tr("Czesc szczegolow nie zostala jeszcze pobrana — nastepny skan je dokonczy.",
                        "Some details are not fetched yet — the next scan finishes them."))
    if note:
        notes.append(note)
    # Jeden przycisk lata wszystko (jak samo /zalataj); "tylko obrazy", gdy jest tez cos innego do zalatania
    parts = patch_parts(result)
    actions = []
    if parts.total:
        actions.append({"scope": "all", "count": parts.total, "label": parts.describe()})
    if parts.images and parts.total > parts.images:
        actions.append({"scope": "images", "count": parts.images, "label": n_images(parts.images)})
    if parts.apps and parts.total > parts.apps:
        actions.append({"scope": "apps", "count": parts.apps,
                        "label": tr(f"zaleznosci aplikacji: {parts.apps}", f"app dependencies: {parts.apps}")})
    servers = servers_with_items(result)
    return {"at": result.at, "system": plain(result.system, 120), "supported": result.ecosystem is not None,
            "checked": result.checked, "todo": todo(result), "groups": groups, "actions": actions, "notes": notes,
            "totals": {"system": len(result.items), "images": len(images), "apps": sum(1 for g in groups if g["kind"] == "app"),
                       "servers": len(servers),
                       "urgent": sum(1 for p in all_items if p.urgent), "kev": sum(1 for p in all_items if p.kev),
                       "packages": len(all_items),
                       # pilne osobno dla kazdej czesci — jedna liczba dla wszystkiego naraz nie mowila, gdzie sa
                       "system_urgent": len(result.urgent),
                       "servers_urgent": sum(1 for t in servers if t.urgent),
                       "images_urgent": sum(1 for t in images if t.urgent),
                       "apps_urgent": sum(1 for g in groups if g["kind"] == "app" and g["urgent"])}}


# ─── Alerty: wysylane same, z opisem kazdej podatnosci ──────────────────────

def serious(item: PackageVulns) -> bool:
    """Zaleznosc aplikacji warta alertu: KEV albo krytyczna (drzewa npm maja mnostwo "wysokich" w narzedziach)."""
    return any(v.kev or v.severity == "critical" for v in item.fixable)


def urgent_images(result: Scan) -> list[TargetResult]:
    return sorted((t for t in result.of_kind("image") if t.urgent), key=target_rank)


def servers_with_items(result: Scan) -> list[TargetResult]:
    return sorted((t for t in result.of_kind("server") if t.items), key=target_rank)


def urgent_servers(result: Scan) -> list[TargetResult]:
    return sorted((t for t in result.of_kind("server") if t.urgent), key=target_rank)


def serious_apps(result: Scan) -> list[tuple[TargetResult, list[PackageVulns]]]:
    apps = [(t, sort_items([p for p in t.items if serious(p)])) for t in sorted(result.of_kind("app"), key=target_rank)]
    return [(t, items) for t, items in apps if items]


# Narzedzie, ktorym podbija sie zaleznosci w danym ekosystemie — w alercie i raporcie konkretnie, nie ogolnikiem
APP_TOOLS = {"npm": "npm audit fix", "PyPI": "nowa wersja w requirements albo poetry/uv lock",
             "Packagist": "composer update <pakiet>", "Go": "go get <modul>@<wersja>", "crates.io": "cargo update -p <pakiet>",
             "RubyGems": "bundle update <pakiet>"}
APP_TOOLS_EN = {"npm": "npm audit fix", "PyPI": "the new version in requirements or the poetry/uv lock",
                "Packagist": "composer update <package>", "Go": "go get <module>@<version>",
                "crates.io": "cargo update -p <package>", "RubyGems": "bundle update <package>"}


def app_tools(targets: list[TargetResult]) -> str:
    """'npm audit fix; nowa wersja w requirements ...' — tylko ekosystemy tych aplikacji, w ich kolejnosci."""
    ecosystems = list(dict.fromkeys(e.strip() for t in targets for e in (t.ecosystem or "").split(",") if e.strip()))
    tools = tr(APP_TOOLS, APP_TOOLS_EN)
    return "; ".join(tools[e] for e in ecosystems if e in tools)


def alert_items(result: Scan) -> list[PackageVulns]:
    """Pakiety, ktore pokaza alerty — ich podatnosci trzeba wyjasnic, zanim alert wyjdzie."""
    items = sort_items(result.urgent)[:ALERT_PACKAGES]
    items += [p for t in urgent_servers(result)[:ALERT_TARGETS] for p in sort_items(t.urgent)[:ALERT_PER_TARGET]]
    items += [p for t in urgent_images(result)[:ALERT_TARGETS] for p in sort_items(t.urgent)[:ALERT_PER_TARGET]]
    items += [p for _, found in serious_apps(result)[:ALERT_TARGETS] for p in found[:ALERT_PER_TARGET]]
    return items


def system_alert(result: Scan, explain: dict[str, str]) -> str:
    """Tresc alertu vuln:system (Markdown, jedna wiadomosc): pilne pakiety hosta z opisem, co zrobi przycisk."""
    urgent = sort_items(result.urgent)
    footer = [tr("Przycisk Zalataj (albo /zalataj serwer) instaluje te poprawki od razu i sprzata po sobie.",
                 "The Patch button (or /patch server) installs these fixes right away and cleans up after itself.")]
    if not result.esm and any(p.pro_only for p in urgent):
        footer.append(tr("Poprawek oznaczonych „tylko w Ubuntu Pro” nie zainstaluje bez subskrypcji (pro attach).",
                         "Fixes marked \"only in Ubuntu Pro\" will not install without the subscription (pro attach)."))
    return sections_markdown([("", urgent[:ALERT_PACKAGES], len(urgent))], explain, footer=footer)


def servers_alert(result: Scan, explain: dict[str, str]) -> str:
    """Tresc alertu vuln:servers: zdalne serwery i ich pilne pakiety z opisem."""
    servers = urgent_servers(result)
    sections = [(tr(f"Serwer {plain(t.name, 60)}", f"Server {plain(t.name, 60)}")
                 + f" ({plain(t.meta.get('system', ''), 80)})", sort_items(t.urgent)[:ALERT_PER_TARGET], len(t.urgent))
                for t in servers[:ALERT_TARGETS]]
    more = len(servers) - len(sections)
    footer = [tr(f"… i {more} kolejnych serwerow", f"… and {more} more servers")] if more > 0 else []
    return sections_markdown(sections, explain, footer=footer + [
        tr("Przycisk Zalataj (albo /zalataj serwery) instaluje te poprawki od razu, przez ssh.",
           "The Patch button (or /patch servers) installs these fixes right away, over ssh.")])


def images_alert(result: Scan, explain: dict[str, str], skip_projects: frozenset[str] | set[str] = frozenset()) -> str:
    """
    Tresc alertu vuln:images: obraz (i jego kontenery), pilne pakiety w nim z opisem. Obraz, ktorego /zalataj nie
    odswiezy (kontener bez compose, kontener samego Pipe), ma to w naglowku — przycisk nie obiecuje wiecej, niz zrobi.
    """
    patch = image_patch(result, skip_projects=skip_projects)
    refreshable = {t.name for t in patch.refreshable} | {t.name for t in patch.web}
    pipe_own = {t.name for t in result.of_kind("image")
                if patch.skipped and set(t.meta.get("containers") or []) <= set(patch.skipped)}
    sections = []
    for target in urgent_images(result)[:ALERT_TARGETS]:
        containers = ", ".join(target.meta.get("containers") or [])
        urgent = sort_items(target.urgent)
        head = tr(f"Obraz {plain(target.name, 120)}", f"Image {plain(target.name, 120)}") \
            + (f" ({plain(containers, 200)})" if containers else "")
        if target.name in pipe_own:
            head += tr(" — kontener Pipe, odswiezy go /aktualizuj", " — Pipe's own container, /update refreshes it")
        elif target in patch.waiting:
            head += tr(" — bez zmian po ostatniej latce, powod w /podatnosci", " — unchanged by the last patch, the reason is in /vulns")
        elif target.name not in refreshable:
            head += tr(" — kontener bez compose, tego nie odswieze sam", " — a container without compose, I cannot refresh it")
        sections.append((head, urgent[:ALERT_PER_TARGET], len(urgent)))
    more = len(urgent_images(result)) - len(sections)
    footer = [tr(f"… i {more} kolejnych obrazow", f"… and {more} more images")] if more > 0 else []
    return sections_markdown(sections, explain, footer=footer + [
        tr("Przycisk Zalataj (albo /zalataj obrazy) od razu pobiera nowszy obraz, a gdy go nie ma, instaluje poprawki "
           "w obrazie; potem uruchamia kontenery od nowa i usuwa stare obrazy.",
           "The Patch button (or /patch images) right away pulls a newer image or, where there is none, installs the "
           "fixes inside the image; then restarts the containers and removes the old images.")])


def apps_alert(result: Scan, explain: dict[str, str]) -> str:
    """Tresc alertu vuln:apps: aplikacja, jej krytyczne zaleznosci z opisem i narzedzie, ktorym sie je podbija."""
    apps = serious_apps(result)
    sections = [(tr(f"Aplikacja {plain(target.name, 200)}", f"Application {plain(target.name, 200)}")
                 + f" ({plain(target.ecosystem or '', 60)})", items[:ALERT_PER_TARGET], len(items))
                for target, items in apps[:ALERT_TARGETS]]
    more = len(apps) - len(sections)
    footer = [tr(f"… i {more} kolejnych aplikacji", f"… and {more} more applications")] if more > 0 else []
    tools = app_tools([t for t, _ in apps])
    return sections_markdown(sections, explain, footer=footer + [
        tr("Przycisk Zalataj (albo /zalataj aplikacje) podbije je do wersji po strzalce w plikach zaleznosci "
           f"(w izolowanym kontenerze: {tools}) i przebuduje projekt compose aplikacji; kopia plikow jest w dzienniku "
           "(/cofnij).",
           "The Patch button (or /patch apps) bumps them to the version after the arrow in the dependency files "
           f"(in an isolated container: {tools}) and rebuilds the app's compose project; the files are backed up in "
           "the journal (/undo).")])


def explanations(lang: str) -> dict[str, str]:
    """Zapisane wyjasnienia podatnosci w danym jezyku: id -> tekst."""
    return dict((load_state().get("explain") or {}).get(lang) or {})


def save_explanations(lang: str, mapping: dict[str, str]) -> None:
    def change(state: dict[str, Any]) -> None:
        explain = state.get("explain") if isinstance(state.get("explain"), dict) else {}
        explain.setdefault(lang, {}).update({k: plain(v) for k, v in mapping.items() if plain(v)})
        state["explain"] = explain
    update_state(change)


def needing_explanation(result: Scan, lang: str, extra: list[PackageVulns] | None = None,
                        limit: int = 60) -> list[dict[str, str]]:
    """Pokazywane podatnosci bez wyjasnienia w tym jezyku — dane dla modelu (opis z OSV)."""
    known = explanations(lang)
    details = load_state().get("details") or {}
    # `extra` najpierw — to, co zaraz zobaczy uzytkownik (alert, podglad latki), nie moze wypasc przez `limit`
    items = list(extra or []) + [item for _, shown, _ in report_sections(result) for item in shown]
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
#
# /zalataj sklada wszystko z ostatniego skanu, bez LLM, a core/patching.py wykonuje: pakiety hosta i zdalnych serwerow
# (apt/apk razem z porzadkami: przerwane dpkg, stare jadra, pobrane paczki), obrazy kontenerow z compose (nowszy obraz,
# a gdy go nie ma — poprawki doinstalowane w obrazie). Nazwy z baz pakietow i etykiet compose przechodza przez wzorce,
# wiec nic innego nie trafi do powloki.

_BINARY = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,127}(:[a-z0-9]{1,16})?$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_IMAGE_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63})?$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
APT = "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300"    # czeka na unattended-upgrades
ROLLBACK_TAG = "pipe-rollback-"             # poprzedni obraz pod tym tagiem: nie znika przy sprzataniu, /cofnij do niego wraca
PATCH_LABEL = "pipe.patch.base"             # obraz z poprawkami od Pipe: id obrazu, na ktorym powstal
# Aktualizacja Dockera z repozytoriow dystrybucji nie restartuje demona (kontenery by stanely) — dziala po restarcie
DOCKER_PACKAGES = re.compile(r"^(docker\.io|docker-ce|containerd(\.io)?|runc)$")
# Stare jadra, ktore apt sam uznaje za zbedne (zostawia dzialajace i najnowsze) — inaczej /boot sie zapelnia
_OLD_KERNELS = ("old=$(apt-get -s autoremove 2>/dev/null | awk '$1==\"Remv\"{print $2}' "
                "| grep -E '^linux-.*[0-9]+\\.[0-9]+\\.[0-9]+-[0-9]+' | tr '\\n' ' '); "
                f"[ -z \"$old\" ] || {APT} purge -y $old")


def n_servers(n: int) -> str:
    return count(n, "serwer", "serwery", "serwerow", "server", "servers")


def upgrade_binaries(items: list[PackageVulns], kernel_meta: list[str] | tuple[str, ...] = ()) -> list[str]:
    """
    Pakiety binarne do podbicia. Dla jadra zamiast pakietow z wersja w nazwie (nowe jadro to nowy pakiet, wiec
    `--only-upgrade` nic na nich nie robi) jego metapakiety — to one ciagna nowe jadro.
    """
    names: set[str] = set()
    kernel = False
    for item in items:
        if item.ecosystem.startswith(DISTROS) and KERNEL.match(item.name) and kernel_meta:
            kernel = True
            names |= {b for b in item.binaries if not KERNEL_ABI.search(b)}
        else:
            names |= set(item.binaries)
    if kernel:
        names |= set(kernel_meta)
    return sorted(b for b in names if _BINARY.match(b))


def upgrade_command(eco: str | None, items: list[PackageVulns], kernel_meta: list[str] | tuple[str, ...] = ()) -> str:
    """
    Komenda podbijajaca tylko te pakiety (bez pelnego upgrade) — do pokazania w audycie i dzienniku. Konfiguracje
    zostaja w obecnej wersji (--force-confold), apt o nic nie pyta. Wykonywane sa kroki z host_steps().
    """
    binaries = upgrade_binaries(items, kernel_meta)
    if not binaries or not eco:
        return ""
    if eco.startswith("Alpine"):
        return "apk update && apk add --upgrade " + " ".join(binaries)
    return ("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade "
            "-o Dpkg::Options::=--force-confold " + " ".join(binaries))


@dataclass
class Step:
    """Jedna komenda latki. `required` — jej kod wyjscia jest wynikiem czesci; reszta to porzadki."""
    label: str
    command: str
    required: bool = False
    warn: str = ""              # nieudany krok porzadkowy z tym ostrzezeniem w wyniku


def host_steps(eco: str | None, items: list[PackageVulns], kernel_meta: list[str] | tuple[str, ...] = ()) -> list[Step]:
    """
    Latka pakietow systemu (hosta albo zdalnego serwera) jako kroki z porzadkami: dokonczenie przerwanej
    instalacji, pobrane paczki i stare jadra przed (miejsce na dysku i w /boot) i po instalacji.
    """
    binaries = upgrade_binaries(items, kernel_meta)
    if not binaries or not eco:
        return []
    names = " ".join(binaries)
    count_text = n_packages(len(items))
    if eco.startswith("Alpine"):
        return [Step(tr("pobieram listy pakietow (apk update)", "fetching the package lists (apk update)"), "apk update",
                     warn=tr("apk update zglosil blad — czesc poprawek mogla nie zostac znaleziona",
                             "apk update reported an error — some fixes may not have been found")),
                Step(tr(f"instaluje poprawki: {count_text}", f"installing the fixes: {count_text}"),
                     f"apk add --upgrade {names}", required=True),
                Step(tr("usuwam pobrane paczki", "removing downloaded packages"), "apk cache clean 2>/dev/null; true")]
    return [
        Step(tr("porzadkuje apt: przerwane instalacje, pobrane paczki, stare jadra",
                "tidying apt: interrupted installs, downloaded packages, old kernels"),
             f"DEBIAN_FRONTEND=noninteractive dpkg --force-confold --configure -a; {APT} clean; {_OLD_KERNELS}"),
        Step(tr("pobieram listy pakietow (apt-get update)", "fetching the package lists (apt-get update)"),
             f"{APT} update",
             warn=tr("apt-get update zglosil blad (np. niedzialajace repozytorium) — czesc poprawek mogla nie zostac "
                     "znaleziona", "apt-get update reported an error (e.g. a broken repository) — some fixes may not "
                     "have been found")),
        Step(tr(f"instaluje poprawki: {count_text}", f"installing the fixes: {count_text}"),
             f"{APT} install -y --only-upgrade -o Dpkg::Options::=--force-confold {names}", required=True),
        Step(tr("usuwam stare jadra i pobrane paczki", "removing old kernels and downloaded packages"),
             f"{_OLD_KERNELS}; {APT} clean"),
    ]


def docker_packages(items: list[PackageVulns]) -> list[str]:
    """Pakiety Dockera w latce — nowa wersja demona dziala dopiero po jego restarcie (zatrzymuje kontenery)."""
    return sorted({b.split(":")[0] for p in items for b in p.binaries if DOCKER_PACKAGES.match(b.split(":")[0])})


def split_ref(ref: str) -> tuple[str, str]:
    """'nginx:1.25' -> ('nginx', '1.25'); 'host:5000/app' -> ('host:5000/app', 'latest'); digest/zle -> ('', '')."""
    if "@" in ref or not _IMAGE_REF.match(ref):
        return "", ""
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, _, tag = ref.rpartition(":")
        return repo, tag
    return ref, "latest"


def rollback_ref(ref: str) -> str:
    """
    Tag, pod ktorym zostaje poprzedni obraz: 'nginx:1.25' -> 'nginx:pipe-rollback-1.25'. Obraz bez tagu sprzatanie
    (docker image prune) by usunelo, a /cofnij do niego wraca — wiec zostaje jedna poprzednia wersja na obraz.
    """
    repo, tag = split_ref(ref)
    if not repo or not _TAG.match(tag):
        return ""
    suffix = ROLLBACK_TAG + tag
    if len(suffix) > 128:
        suffix = ROLLBACK_TAG + hashlib.sha256(tag.encode()).hexdigest()[:16]
    return f"{repo}:{suffix}"


def layer_run(target: TargetResult) -> str:
    """
    Komenda dla obrazu bez nowszej wersji (przypiety tag, obraz bazowy jeszcze nieprzebudowany): poprawki z
    repozytorium jego systemu, tylko pakiety z podatnosciami, bez Ubuntu Pro. '' = nie da sie (system bez apt/apk).
    """
    eco = target.ecosystem or ""
    binaries = upgrade_binaries([p for p in target.items if not p.pro_only])
    if not binaries or not eco.startswith(DISTROS):
        return ""
    names = " ".join(binaries)
    if eco.startswith("Alpine"):
        return f"apk upgrade --no-cache {names}"
    return ("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade --no-install-recommends "
            f"-o Dpkg::Options::=--force-confold {names} && apt-get clean && rm -rf /var/lib/apt/lists/*")


def _json_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def layer_commands(target: TargetResult, ref: str, base_id: str, config: dict[str, Any], name: str) -> str:
    """
    Warstwa z poprawkami na tym samym tagu — jak `copa patch`: kontener tymczasowy z obrazu wykonuje layer_run()
    jako root, `docker commit` zapisuje wynik z przywroconym USER, ENTRYPOINT i CMD obrazu oraz etykieta obrazu
    bazowego (PATCH_LABEL), kontener i jego anonimowe wolumeny znikaja. Bez `docker build` — dziala z kazdym
    builderem i magazynem obrazow. '' = nie da sie.
    """
    run = layer_run(target)
    repo, _ = split_ref(ref)
    user = str(config.get("User") or "")
    entry, cmd = _json_list(config.get("Entrypoint")), _json_list(config.get("Cmd"))
    if not run or not repo or not _IMAGE_ID.match(base_id) or entry is None or cmd is None \
            or (user and not _IMAGE_USER.match(user)) or not re.fullmatch(r"pipe-patch-[0-9a-f]{8}", name):
        return ""
    changes = [f"ENTRYPOINT {json.dumps(entry)}", f"CMD {json.dumps(cmd)}", f"LABEL {PATCH_LABEL}={base_id}"]
    if user:
        changes.append(f"USER {user}")
    flags = " ".join(f"--change {shlex.quote(c)}" for c in changes)
    return (f"docker run --name {name}{' --user root' if user else ''} --entrypoint sh {shlex.quote(ref)} -c "
            f"{shlex.quote(run)} && docker commit {flags} {name} {shlex.quote(ref)}; rc=$?; "
            f"docker rm -f -v {name} >/dev/null 2>&1; exit $rc")


@dataclass
class ComposeProject:
    """Projekt compose z uslugami, ktorych obrazy lata /zalataj obrazy."""
    project: str
    files: list[str]
    services: list[str] = field(default_factory=list)
    targets: list[TargetResult] = field(default_factory=list)

    @property
    def base(self) -> str:
        return "docker compose " + " ".join(f"-f {shlex.quote(f)}" for f in self.files) + f" -p {shlex.quote(self.project)}"

    def command(self, sub: str) -> str:
        return f"{self.base} {sub} " + " ".join(shlex.quote(s) for s in self.services)


@dataclass
class ImagePatch:
    command: str = ""
    inverse: list[str] = field(default_factory=list)     # powrot do poprzednich obrazow (dla /cofnij)
    manual: list[str] = field(default_factory=list)      # kontenery bez compose: tylko podpowiedz
    targets: list[TargetResult] = field(default_factory=list)
    waiting: list[TargetResult] = field(default_factory=list)   # po ostatniej latce bez zmian — nic wiecej nie pomoglo
    skipped: list[str] = field(default_factory=list)     # kontenery samego Pipe — te odswieza /aktualizuj
    refreshable: list[TargetResult] = field(default_factory=list)   # obrazy, ktore latka naprawde odswiezy (z compose)
    projects: list[ComposeProject] = field(default_factory=list)
    web: list[TargetResult] = field(default_factory=list)  # kontener /web (Caddy, cloudflared) — Pipe odtwarza go sam


# Kontener strony /web (core/webpublic.py): bez compose, ale Pipe zna jego opcje i odtwarza go sam.
WEB_CONTAINER = "pipe-web"


def image_patch(result: Scan, scope: str = "all", skip_projects: frozenset[str] | set[str] = frozenset(),
                now: float | None = None) -> ImagePatch:
    """
    Obrazy kontenerow z compose do zalatania: `pull --ignore-buildable` (obrazy z rejestru), `build --pull` (uslugi
    z `build:` — przebudowane na aktualnym obrazie bazowym; uslugi bez `build:` compose pomija) i `up -d`; obraz, ktorego
    to nie zmienilo, dostaje warstwe z poprawkami (core/patching.py). Komendy odwrotne oznaczaja poprzedni obraz jego
    starym tagiem i odtwarzaja uslugi — to prawdziwy rollback. Kontenery bez compose dostaja tylko podpowiedz
    (docker run trzeba powtorzyc z tymi samymi opcjami). `skip_projects` — projekt compose samego Pipe: `up -d`
    zatrzymalby Pipe w trakcie latania (od tego jest /aktualizuj). Obraz, ktory po ostatniej latce mial te same
    podatnosci, czeka HOLD_FOR — inaczej /zalataj probowaloby w kolko to samo.
    """
    patch = ImagePatch()
    projects: dict[tuple[str, tuple[str, ...]], ComposeProject] = {}
    retag: dict[str, str] = {}
    waiting = stale_images(now)
    for target in result.of_kind("image"):
        if not target.items or (scope == "urgent" and not target.urgent):
            continue
        ref, image_id = str(target.meta.get("image", "")), str(target.meta.get("image_id", ""))
        compose = [c for c in target.meta.get("compose") or []
                   if _COMPOSE_NAME.match(c.get("project", "")) and _COMPOSE_NAME.match(c.get("service", ""))
                   and c.get("files") and all(_HOST_PATH.match(f) for f in c["files"])]
        own = [c for c in compose if c["project"] in skip_projects]
        if own:
            patch.skipped += [str(c.get("container", "")) for c in own]
            compose = [c for c in compose if c["project"] not in skip_projects]
            if not compose:
                continue
        # kontenera /web wstrzymanie nie dotyczy: tag plywa, a `docker pull` bez nowego obrazu niczego nie zmienia
        if (waiting.get(target.name) or {}).get("ids") == target_fingerprint(target) \
                and WEB_CONTAINER not in (target.meta.get("containers") or []):
            patch.waiting.append(target)
            continue
        patch.targets.append(target)
        for item in compose:
            project = projects.setdefault((item["project"], tuple(item["files"])),
                                          ComposeProject(item["project"], list(item["files"])))
            if item["service"] not in project.services:
                project.services.append(item["service"])
            if target not in project.targets:
                project.targets.append(target)
        if compose:
            patch.refreshable.append(target)
        short = image_id.removeprefix("sha256:")[:12]
        if compose and _IMAGE_REF.match(ref) and re.fullmatch(r"[0-9a-f]{12}", short):
            retag[ref] = short
        with_compose = {c.get("container") for c in compose} | {c.get("container") for c in own}
        for container in target.meta.get("containers") or []:
            if container == WEB_CONTAINER:
                if target not in patch.web:
                    patch.web.append(target)
            elif container not in with_compose:
                patch.manual.append(tr(f"{container}: kontener bez compose — docker pull {ref}, potem odtworz go "
                                       f"z tymi samymi opcjami", f"{container}: a container without compose — "
                                       f"docker pull {ref}, then recreate it with the same options"))
    patch.projects = list(projects.values())
    patch.command = " && ".join(f"{p.command('pull --ignore-buildable')} && {p.command('build --pull')} && "
                                f"{p.command('up -d')}" for p in patch.projects)
    patch.inverse = [f"docker tag {short} {shlex.quote(ref)}" for ref, short in retag.items()] \
        + [p.command("up -d") for p in patch.projects]
    return patch


def built_locally(target: TargetResult) -> bool:
    """Obraz zbudowany przez compose z `build:` (nazwa `<projekt>-<usluga>`) — nowszy daje tylko nowszy obraz bazowy."""
    name = str(target.name).removeprefix("docker.io/library/").split(":")[0]
    return any(name in (f"{c.get('project')}-{c.get('service')}", f"{c.get('project')}_{c.get('service')}")
               for c in target.meta.get("compose") or [])


def _waiting_why(target: TargetResult) -> str:
    return tr("ani nowszy obraz bazowy w tej samej linii, ani poprawki z repozytorium systemu obrazu nie usunely "
              "podatnosci — zmien FROM w Dockerfile na nowsza glowna wersje",
              "neither a newer base image in the same line nor the fixes from the image system's repository removed "
              "the vulnerabilities — change FROM in the Dockerfile to a newer major version") if built_locally(target) else \
        tr("ani nowszy obraz pod tym tagiem, ani poprawki z repozytorium systemu obrazu nie usunely podatnosci — "
           "zmien tag w compose na nowsza wersje",
           "neither a newer image under this tag nor the fixes from the image system's repository removed the "
           "vulnerabilities — change the tag in compose to a newer version")


def _names(items: list[PackageVulns], limit: int = 6) -> str:
    names = ", ".join(plain(p.name, 60) for p in items[:limit])
    return names + (tr(f" i {len(items) - limit} innych", f" and {len(items) - limit} more") if len(items) > limit else "")


def held_note(items: list[PackageVulns]) -> str:
    """Dlaczego te pakiety nie sa w latce: poprawka tylko w Ubuntu Pro albo nie ma jej jeszcze w repozytoriach."""
    pro = [p for p in items if p.pro_only]
    held = [p for p in items if not p.pro_only]
    parts = []
    if held:
        parts.append(tr(f"Bez zmian po ostatniej instalacji ({len(held)}): {_names(held)} — repozytoria serwera nie "
                        f"maja jeszcze wersji z poprawka, sprobuje znowu za dobe.",
                        f"Unchanged after the last install ({len(held)}): {_names(held)} — the server's repositories "
                        f"do not have the fixed version yet, I will try again in a day."))
    if pro:
        parts.append(tr(f"Poprawki dla {len(pro)} ({_names(pro)}) sa tylko w Ubuntu Pro (ESM): pro attach, potem /zalataj.",
                        f"The fixes for {len(pro)} ({_names(pro)}) are only in Ubuntu Pro (ESM): pro attach, then /patch."))
    return " ".join(parts)


def waiting_note(patch: ImagePatch) -> str:
    """Obrazy pominiete w latce: bez zmian od ostatniego razu, kontenery samego Pipe."""
    lines = []
    if patch.waiting:
        lines.append(tr("Bez zmian po ostatniej latce (sprawdze znowu za dobe): ",
                        "Unchanged after the last patch (I will check again in a day): ")
                     + "; ".join(f"{plain(t.label(), 120)} — {_waiting_why(t)}" for t in patch.waiting[:5]) + ".")
    if patch.skipped:
        lines.append(tr(f"Kontenery Pipe ({', '.join(patch.skipped[:3])}) odswieza /aktualizuj.",
                        f"Pipe's own containers ({', '.join(patch.skipped[:3])}) are refreshed by /update."))
    return "\n\n".join(lines)


def _waiting_lines(prefix: str, items: list[PackageVulns]) -> list[str]:
    """'Serwer: 2 pakiety (openssl, curl) bez zmian po ostatniej instalacji — ...' i '... tylko w Ubuntu Pro'."""
    held = [p for p in items if not p.pro_only]
    pro = [p for p in items if p.pro_only]
    names = lambda found: ", ".join(plain(p.name, 40) for p in found[:4])     # noqa: E731
    lines = []
    if held:
        lines.append(tr(f"{prefix}: {n_packages(len(held))} ({names(held)}) bez zmian po ostatniej instalacji — "
                        "repozytoria serwera nie maja jeszcze nowej wersji; sprobuje znowu za dobe.",
                        f"{prefix}: {n_packages(len(held))} ({names(held)}) unchanged after the last install — the "
                        "server's repositories do not have the new version yet; I will retry in a day."))
    if pro:
        lines.append(tr(f"{prefix}: {n_packages(len(pro))} ({names(pro)}) — poprawka tylko w Ubuntu Pro (pro attach).",
                        f"{prefix}: {n_packages(len(pro))} ({names(pro)}) — the fix is only in Ubuntu Pro (pro attach)."))
    return lines


def todo(result: Scan, skip_projects: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """
    Co uzytkownik moze z tym zrobic — jedna linia na serwer, zdalne serwery, obrazy i aplikacje, kazda z konkretnym
    krokiem. Te same linie sa na gorze /podatnosci i /audyt, w /zalataj, gdy nie ma czego instalowac, i w pipe web.
    Podatnosci bez poprawki nie ma tu wcale: nic z nimi nie da sie zrobic, a liczba "512" tylko straszy.
    Gdy sa zdalne serwery, ten serwer to "Ten serwer" — samo "Serwer" obok "Serwer web2" myli.
    """
    lines: list[str] = []
    here = tr("Ten serwer", "This server") if result.of_kind("server") else tr("Serwer", "Server")
    patchable, waiting = select(result, "all"), waiting_items(result)
    if result.ecosystem is None:
        lines.append(tr(f"{here}: system {result.system} nie jest obslugiwany — sprawdzam tylko obrazy i aplikacje.",
                        f"{here}: {result.system} is not supported — I only check images and applications."))
    elif patchable:
        urgent = sum(1 for p in patchable if p.urgent)
        lines.append(tr(f"{here}: {n_packages(len(patchable))} z poprawka" + (f" (pilne: {urgent})" if urgent else "")
                        + " — zainstaluje /zalataj.",
                        f"{here}: {n_packages(len(patchable))} with a fix" + (f" (urgent: {urgent})" if urgent else "")
                        + " — /patch installs the fixes."))
    lines += _waiting_lines(here, waiting)
    if result.ecosystem is not None and not patchable and not waiting:
        lines.append(tr(f"{here}: pakiety aktualne.", f"{here}: packages up to date."))
    if result.reboot:
        lines.append(tr(f"{here}: czeka na restart — nowe jadro i biblioteki zaczna dzialac po nim (zaplanuj go).",
                        f"{here}: waiting for a reboot — the new kernel and libraries take effect after it (schedule it)."))

    clean_servers = 0
    for target in sorted(result.of_kind("server"), key=lambda t: t.name):
        name = plain(target.name, 60)
        if target.meta.get("error"):
            lines.append(tr(f"Serwer {name}: brak polaczenia ({plain(target.meta['error'], 120)}) — sprawdze przy "
                            "nastepnym skanie.", f"Server {name}: no connection ({plain(target.meta['error'], 120)}) — "
                            "I will check at the next scan."))
            continue
        if not target.ecosystem:
            lines.append(tr(f"Serwer {name}: system {plain(target.meta.get('system', '?'), 80)} nie jest obslugiwany.",
                            f"Server {name}: {plain(target.meta.get('system', '?'), 80)} is not supported."))
            continue
        chosen, held = select_server(target), waiting_server_items(target)
        if chosen:
            urgent = sum(1 for p in chosen if p.urgent)
            lines.append(tr(f"Serwer {name}: {n_packages(len(chosen))} z poprawka" + (f" (pilne: {urgent})" if urgent else "")
                            + f" — zainstaluje /zalataj (tylko ten serwer: /zalataj {name}).",
                            f"Server {name}: {n_packages(len(chosen))} with a fix" + (f" (urgent: {urgent})" if urgent else "")
                            + f" — /patch installs the fixes (only this server: /patch {name})."))
        lines += _waiting_lines(tr(f"Serwer {name}", f"Server {name}"), held)
        if target.meta.get("reboot"):
            lines.append(tr(f"Serwer {name}: czeka na restart (nowe jadro albo biblioteki).",
                            f"Server {name}: waiting for a reboot (new kernel or libraries)."))
        if not chosen and not held:
            clean_servers += 1
    if clean_servers:
        lines.append(tr(f"Zdalne serwery: {n_servers(clean_servers)} z aktualnymi pakietami.",
                        f"Remote servers: {n_servers(clean_servers)} with up-to-date packages."))

    images = [t for t in result.of_kind("image") if t.items]
    if images:
        patch = image_patch(result, skip_projects=skip_projects)
        names = lambda targets: ", ".join(plain(t.name, 60) for t in sorted(targets, key=target_rank)[:4]) + (  # noqa: E731
            tr(f" i {len(targets) - 4} innych", f" and {len(targets) - 4} more") if len(targets) > 4 else "")
        if patch.refreshable:
            lines.append(tr(f"Obrazy kontenerow: {n_images(len(patch.refreshable))} do zalatania ({names(patch.refreshable)}) — "
                            "zalata /zalataj (same obrazy: /zalataj obrazy).",
                            f"Container images: {n_images(len(patch.refreshable))} to patch ({names(patch.refreshable)}) — "
                            "/patch patches them (images only: /patch images)."))
        built = [t for t in patch.waiting if built_locally(t)]
        pulled = [t for t in patch.waiting if not built_locally(t)]
        if built:
            lines.append(tr(f"Obrazy budowane z Dockerfile ({names(built)}): nowszy obraz bazowy w tej samej linii i "
                            "poprawki z repozytorium nie pomogly — zmien FROM w Dockerfile na nowsza glowna wersje.",
                            f"Images built from a Dockerfile ({names(built)}): a newer base image in the same line and "
                            "the repository fixes did not help — change FROM in the Dockerfile to a newer major version."))
        if pulled:
            lines.append(tr(f"Obrazy z rejestru ({names(pulled)}): ani nowszy obraz, ani poprawki z repozytorium nie "
                            "pomogly — zmien tag w compose na nowsza wersje.",
                            f"Registry images ({names(pulled)}): neither a newer image nor the repository fixes "
                            "helped — change the tag in compose to a newer version."))
        manual = [m.split(":", 1)[0] for m in patch.manual]
        if manual:
            lines.append(tr(f"Kontenery bez compose ({', '.join(manual[:4])}): nie mam ich jak odswiezyc — docker pull "
                            "i uruchom je od nowa z tymi samymi opcjami.",
                            f"Containers without compose ({', '.join(manual[:4])}): I have no way to refresh them — docker pull "
                            "and start them again with the same options."))
        if patch.skipped:
            lines.append(tr("Kontenery Pipe: odswieza je /aktualizuj.", "Pipe's own containers: /update refreshes them."))
    elif any(t.ecosystem for t in result.of_kind("image")):
        lines.append(tr("Obrazy kontenerow: bez podatnosci z poprawka.", "Container images: no vulnerabilities with a fix."))

    lines += app_todo(result)
    return lines


def app_todo(result: Scan, now: float | None = None) -> list[str]:
    """
    Linie o zaleznosciach aplikacji: co podbije /zalataj (core/appdeps.py), czego ostatnia proba nie zmienila
    (ograniczenie wersji w manifescie) i czego Pipe nie umie podbic (yarn 1, Pipfile, wersja Go).
    Linie zaczynajace sie od "Aplikacje"/"Aplikacja" zostaja po latce (patching._REMAINING).
    """
    from backend.core import appdeps

    apps = sorted((t for t in result.of_kind("app") if t.items), key=target_rank)
    if not apps:
        return []
    held = held_packages(now)
    planned = [appdeps.plan_target(t, held) for t in apps]
    names = lambda targets: ", ".join(plain(t.name, 60) for t in targets[:4]) + (  # noqa: E731
        tr(f" i {len(targets) - 4} innych", f" and {len(targets) - 4} more") if len(targets) > 4 else "")
    lines: list[str] = []
    ready = [b for b in planned if b.items]
    if ready:
        count = sum(len(b.items) for b in ready)
        lines.append(tr(f"Zaleznosci aplikacji ({names([b.target for b in ready])}): {n_packages(count)} z poprawka — "
                        "podbije je /zalataj w plikach zaleznosci i przebuduje projekt compose (same aplikacje: "
                        "/zalataj aplikacje).",
                        f"Application dependencies ({names([b.target for b in ready])}): {n_packages(count)} with a fix — "
                        "/patch bumps them in the dependency files and rebuilds the compose project (apps only: "
                        "/patch apps)."))
    waiting = [t for t in apps if any(appdeps.app_key(t, p) in held for p in t.items)]
    if waiting:
        lines.append(tr(f"Aplikacje ({names(waiting)}): ostatnie podbicie nie zmienilo czesci zaleznosci — ograniczenie "
                        "wersji w manifescie (package.json, pyproject.toml, composer.json...) na to nie pozwala; zmien je "
                        "recznie (nie sprobuje znowu przez dobe).",
                        f"Applications ({names(waiting)}): the last bump did not change some dependencies — the version "
                        "constraint in the manifest (package.json, pyproject.toml, composer.json...) does not allow it; "
                        "change it by hand (I will not try again for a day)."))
    for bump in planned:
        for reason in bump.manual:
            lines.append(tr(f"Aplikacja {plain(bump.directory, 60)}: {reason}.",
                            f"Application {plain(bump.directory, 60)}: {reason}."))
    rest = [t for t, b in zip(apps, planned) if not b.items and not b.manual and t not in waiting]
    if rest:
        lines.append(tr(f"Aplikacje ({names(rest)}): podatne zaleznosci bez wersji, ktora mozna podbic automatycznie — "
                        f"podbij je recznie ({app_tools(rest)}) i wdroz aplikacje.",
                        f"Applications ({names(rest)}): vulnerable dependencies without a version I can bump "
                        f"automatically — bump them by hand ({app_tools(rest)}) and deploy the app."))
    return lines


def nothing_to_patch(result: Scan, skip_projects: frozenset[str] | set[str] = frozenset()) -> str:
    """
    /zalataj bez niczego do zainstalowania: najpierw to (jednym zdaniem), potem co zostaje i co trzeba zrobic samemu.
    """
    clean = (tr("Serwer: pakiety aktualne", "Server: packages up to date"),
             tr("Ten serwer: pakiety aktualne", "This server: packages up to date"),
             tr("Zdalne serwery:", "Remote servers:"))
    rest = [line for line in todo(result, skip_projects) if not line.startswith(clean)]
    if not rest:
        return tr("Nie ma nic do zalatania: brak znanych podatnosci z poprawka.",
                  "Nothing to patch: no known vulnerabilities with a fix.")
    return "\n".join([tr("Nie ma nic do zainstalowania.", "There is nothing to install."), "",
                      tr("Zostaje, ale /zalataj tego nie naprawi:", "What is left, which /patch cannot fix:")]
                     + [f"- {line}" for line in rest])


def select(result: Scan, scope: str, now: float | None = None) -> list[PackageVulns]:
    """
    Pakiety hosta do zalatania: 'urgent' (KEV, krytyczne, wysokie — jak w alercie) albo wszystkie z poprawka —
    bez tych, ktorych ostatnia instalacja nie zmienila (repozytoria nie maja poprawki) i bez poprawek tylko
    z Ubuntu Pro, gdy subskrypcja nie jest wlaczona (apt i tak by ich nie znalazl).
    """
    held = held_packages(now)
    items = result.urgent if scope == "urgent" else result.items
    return [p for p in items if p.key not in held and (result.esm or not p.pro_only)]


def waiting_items(result: Scan, now: float | None = None) -> list[PackageVulns]:
    """Pakiety hosta z poprawka, ktorych /zalataj teraz nie instaluje (patrz select) — raport mowi dlaczego."""
    held = held_packages(now)
    return [p for p in result.items if p.key in held or (p.pro_only and not result.esm)]


held_items = waiting_items


def server_key(target: TargetResult, item: PackageVulns) -> str:
    """Klucz wstrzymania pakietu zdalnego serwera — ten sam pakiet na innym serwerze (i na hoscie) to co innego."""
    return f"server:{target.name}|{item.key}"


def select_server(target: TargetResult, now: float | None = None) -> list[PackageVulns]:
    """Pakiety zdalnego serwera do zalatania — te same zasady co select()."""
    held = held_packages(now)
    esm = bool(target.meta.get("esm"))
    return [p for p in target.items if server_key(target, p) not in held and (esm or not p.pro_only)]


def waiting_server_items(target: TargetResult, now: float | None = None) -> list[PackageVulns]:
    held = held_packages(now)
    esm = bool(target.meta.get("esm"))
    return [p for p in target.items if server_key(target, p) in held or (p.pro_only and not esm)]


def server_notes(result: Scan) -> list[str]:
    """Zdalne serwery, ktorych nie dalo sie sprawdzic (brak polaczenia, nieobslugiwany system)."""
    notes = []
    for target in result.of_kind("server"):
        if target.meta.get("error"):
            notes.append(tr(f"Serwer {plain(target.name, 60)}: brak polaczenia ({plain(target.meta['error'], 120)}).",
                            f"Server {plain(target.name, 60)}: no connection ({plain(target.meta['error'], 120)})."))
        elif not target.ecosystem:
            notes.append(tr(f"Serwer {plain(target.name, 60)}: system {plain(target.meta.get('system', '?'), 80)} nie jest "
                            "obslugiwany.", f"Server {plain(target.name, 60)}: "
                            f"{plain(target.meta.get('system', '?'), 80)} is not supported."))
    return notes


@dataclass
class PatchParts:
    """Ile /zalataj zalata w kazdej czesci — przycisk w pipe web, potwierdzenie agenta, podsumowanie."""
    system: int = 0             # pakiety hosta
    images: int = 0             # obrazy kontenerow z compose
    servers: int = 0            # zdalne serwery z czyms do zalatania
    server_packages: int = 0
    apps: int = 0               # zaleznosci aplikacji, ktore podbije /zalataj (core/appdeps.py)

    @property
    def total(self) -> int:
        return self.system + self.images + self.server_packages + self.apps

    def describe(self) -> str:
        parts = []
        if self.system:
            parts.append(tr(f"{n_packages(self.system)} serwera", f"{n_packages(self.system)} on this server"))
        if self.images:
            parts.append(n_images(self.images))
        if self.servers:
            parts.append(tr(f"{n_servers(self.servers)} zdalnych ({n_packages(self.server_packages)})" if self.servers > 1
                            else f"serwer zdalny ({n_packages(self.server_packages)})",
                            f"{n_servers(self.servers)} remote ({n_packages(self.server_packages)})"))
        if self.apps:
            parts.append(tr(f"zaleznosci aplikacji: {self.apps}", f"app dependencies: {self.apps}"))
        return ", ".join(parts)


def patch_parts(result: Scan, skip_projects: frozenset[str] | set[str] = frozenset(), now: float | None = None) -> PatchParts:
    from backend.core import appdeps

    servers = [select_server(t, now) for t in result.of_kind("server")]
    return PatchParts(system=len(select(result, "all", now)) if result.ecosystem else 0,
                      images=len(image_patch(result, skip_projects=skip_projects, now=now).refreshable),
                      servers=sum(1 for s in servers if s), server_packages=sum(len(s) for s in servers),
                      apps=sum(len(b.items) for b in appdeps.plan(result, held_packages(now))))


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


_STATE_LOCK = threading.RLock()


def save_state(state: dict[str, Any]) -> None:
    with _STATE_LOCK:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({**state, "format": STORE_FORMAT}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def update_state(change: Callable[[dict[str, Any]], None]) -> None:
    """
    Odczyt, zmiana i zapis pod jedna blokada. Skan trwa minute w watku, a w tym czasie zapisuja sie wyjasnienia,
    powiadomienia i wynik latki — gdyby skan zapisal stan wczytany na poczatku, skasowalby je.
    """
    with _STATE_LOCK:
        state = load_state()
        change(state)
        save_state(state)


def last_scan() -> Scan | None:
    data = load_state().get("last")
    return Scan.from_dict(data) if isinstance(data, dict) else None


# Po latce: co sie nie zmienilo. Pakiet w tej samej wersji (apt nie znalazl poprawki — np. tylko Ubuntu Pro, mirror
# jeszcze bez biuletynu) i obraz z tymi samymi podatnosciami (pod tagiem nie ma nowszego) nie wracaja do /zalataj
# przez HOLD_FOR. Inna wersja pakietu albo inny zestaw podatnosci obrazu = nowy klucz, wiec blokada sama znika.
HOLD_FOR = 24 * 3600


def _fresh_entries(name: str, now: float | None) -> dict[str, dict[str, Any]]:
    now = time.time() if now is None else now
    entries = load_state().get(name)
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict) and now - float(v.get("at", 0)) < HOLD_FOR}


def held_packages(now: float | None = None) -> dict[str, dict[str, Any]]:
    """PackageVulns.key -> {at} dla pakietow, ktorych ostatnia instalacja nie podbila."""
    return _fresh_entries("held", now)


def stale_images(now: float | None = None) -> dict[str, dict[str, Any]]:
    """Obraz (tag) -> {ids, at}: po ostatniej latce te same podatnosci."""
    return _fresh_entries("stale_images", now)


def target_fingerprint(target: TargetResult) -> str:
    ids = sorted(f"{p.name}|{p.version}|{v.id}" for p in target.items for v in p.fixable)
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]


@dataclass
class PatchOutcome:
    """Co zmienila latka — porownanie wyniku sprzed i po ponownym skanie."""
    fixed: list[str] = field(default_factory=list)       # pakiety / obrazy bez podatnosci z poprawka albo w nowszej wersji
    unchanged: list[str] = field(default_factory=list)   # to samo co przed latka
    left: int = 0                                        # pakietow (obrazow) z podatnosciami po latce


def record_outcome(scope: str, before: list[Any], after: Scan, now: float | None = None) -> PatchOutcome:
    """
    Po instalacji: ktore pakiety (hosta albo zdalnego serwera `server:<nazwa>`) albo obrazy (`images`) sie zmienily,
    a ktore nie. Niezmienione trafiaja do `held` / `stale_images` — /zalataj nie sprobuje ich znowu przez HOLD_FOR,
    a raport mowi dlaczego.
    """
    now = time.time() if now is None else now
    outcome = PatchOutcome()
    with _STATE_LOCK:
        state = load_state()
        _record(state, outcome, scope, before, after, now)
        save_state(state)
    return outcome


def _record(state: dict[str, Any], outcome: PatchOutcome, scope: str, before: list[Any], after: Scan, now: float) -> None:
    if scope == "apps":
        # before: appdeps.AppBump — pakiet, ktory po podbiciu ma ten sam klucz (nazwa|wersja), sie nie zmienil
        current = {t.name: {p.key for p in t.items} for t in after.of_kind("app")}
        held = state.get("held") if isinstance(state.get("held"), dict) else {}
        for bump in before:
            if bump.directory not in current:
                continue                            # aplikacji nie bylo w skanie — nic nie wiadomo
            for item in bump.items:
                if item.key in current[bump.directory]:
                    outcome.unchanged.append(item.name)
                    held[f"app:{bump.directory}|{item.key}"] = {"at": now}
                else:
                    outcome.fixed.append(item.name)
        state["held"] = {k: v for k, v in held.items() if now - float(v.get("at", 0)) < HOLD_FOR}
        outcome.left = sum(len(t.items) for t in after.of_kind("app"))
        return
    if scope == "images":
        current = {t.name: t for t in after.of_kind("image")}
        stale = state.get("stale_images") if isinstance(state.get("stale_images"), dict) else {}
        for target in before:
            new = current.get(target.name)
            if new is not None and new.items and target_fingerprint(new) == target_fingerprint(target):
                outcome.unchanged.append(target.name)
                stale[target.name] = {"ids": target_fingerprint(new), "at": now}
            else:
                outcome.fixed.append(target.name)
                stale.pop(target.name, None)
        state["stale_images"] = stale
        outcome.left = sum(1 for t in after.of_kind("image") if t.items)
        return
    prefix, items = "", after.items
    if scope.startswith("server:"):
        name = scope.split(":", 1)[1]
        current = next((t for t in after.of_kind("server") if t.name == name), None)
        if current is None or not current.ecosystem:
            return                                  # po latce serwer nie odpowiedzial — nic nie wiadomo
        prefix, items = f"server:{name}|", current.items
    still = {p.key for p in items}
    held = state.get("held") if isinstance(state.get("held"), dict) else {}
    for item in before:
        if item.key in still:
            outcome.unchanged.append(item.name)
            held[prefix + item.key] = {"at": now}
        else:
            outcome.fixed.append(item.name)
    state["held"] = {k: v for k, v in held.items() if now - float(v.get("at", 0)) < HOLD_FOR}
    outcome.left = len(items)


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
        item.fixable.append(Vuln(vid, list(detail.get("cves") or []), severity, osv_text(detail),
                                 fixed, kev=any(c in kev for c in detail.get("cves") or [])))
    return item


def _current_meta(result: Scan, targets: list[Target]) -> Scan:
    """
    Stan, ktory zmienia sie bez zmiany pakietow — restart (reboot-required), Ubuntu Pro, metapakiety jadra, brak
    polaczenia z serwerem — z biezacego odczytu, takze gdy skan wraca z pamieci.
    """
    system = targets[0]
    result.kernel_meta = list(system.meta.get("kernel_meta") or [])
    result.esm, result.reboot = bool(system.meta.get("esm")), bool(system.meta.get("reboot"))
    current = {t.name: t for t in targets if t.kind == "server"}
    for target in result.of_kind("server"):
        if target.name in current:
            target.meta.update(current[target.name].meta)
    return result


def scan(*, force: bool = False, max_age: float = 0, now: float | None = None,
         containers: list[Any] | None = None, servers: list[ServerDump] | None = None) -> tuple[Scan, bool]:
    """
    Skan podatnosci hosta, obrazow dzialajacych kontenerow, zdalnych serwerow (`servers` — pliki odczytane przez
    ssh, core/fleet.py) i aplikacji. Zwraca (wynik, czy odpytano OSV). Bez `force` OSV dostaje wszystko tylko co
    `max_age` sekund (nowe podatnosci w starych wersjach); w miedzyczasie tylko pakiety, ktorych jeszcze nie
    pytalismy (nowa wersja, nowy obraz, nowa zaleznosc) — dzieki temu /audyt zaraz po latce trwa sekundy, nie minute.
    """
    now = time.time() if now is None else now
    targets = all_targets(containers, servers)
    system, others = targets[0], [t for t in targets[1:] if t.ecosystem]
    unsupported = [t for t in targets[1:] if not t.ecosystem]
    state = load_state()
    previous = state.get("last")
    digest = inventory_hash(targets)
    expired = force or now - float(state.get("queried_at", 0)) >= max_age
    if not expired and state.get("inventory") == digest and isinstance(previous, dict) \
            and not previous.get("error") and not previous.get("partial"):
        return _current_meta(refresh_containers(Scan.from_dict(previous), containers), targets), False

    packages = {p.key: p for t in [system] + others if t.ecosystem for p in t.packages}
    cache: dict[str, Any] = {} if expired else state.get("found") or {}
    found = {key: [(str(i), str(m)) for i, m in cache[key]] for key in packages if key in cache}
    to_query = [p for key, p in packages.items() if key not in found]
    blank = _current_meta(Scan(at=now, ecosystem=system.ecosystem, system=str(system.meta.get("system", "")),
                               checked=len(system.packages)), targets)
    if not packages:
        blank.targets = [TargetResult(t.kind, t.name, None, 0, [], 0, t.meta) for t in unsupported]
        update_state(lambda current: current.update({"last": blank.to_dict()}))
        return blank, False
    try:
        if to_query:
            found.update(query_ids(to_query))
    except WebError as exc:
        result = Scan.from_dict(previous) if isinstance(previous, dict) and previous.get("ecosystem") == system.ecosystem \
            else blank
        result.error = str(exc)
        _current_meta(result, targets)
        update_state(lambda current: current.update({"last": result.to_dict()}))
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

    result = _current_meta(Scan(at=now, ecosystem=system.ecosystem, system=str(system.meta.get("system", "")),
                                checked=len(system.packages)), targets)
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
    # tylko pola skanu — wyjasnienia i powiadomienia zapisane w trakcie skanu zostaja (update_state)
    fields = {"last": result.to_dict(), "inventory": digest, "details": details,
              "found": {key: found.get(key, []) for key in packages}}
    if expired:
        fields["queried_at"] = now
    update_state(lambda current: current.update(fields))
    return result, True


# ─── Powiadomienia ──────────────────────────────────────────────────────────

# Alerty z opisami (v0.34) maja inny klucz niz wczesniejsze — kazdy aktywny problem przychodzi raz w nowej formie.
NOTIFIED = "notified_described"


def notified_ids(key: str) -> set[str]:
    """ID, o ktorych alert `key` juz powiadomil (przezywa restart Pipe)."""
    return set((load_state().get(NOTIFIED) or {}).get(key) or [])


def mark_notified(key: str, ids: set[str]) -> None:
    def change(state: dict[str, Any]) -> None:
        notified = state.get(NOTIFIED) if isinstance(state.get(NOTIFIED), dict) else {}
        notified[key] = sorted(ids)
        state[NOTIFIED] = notified
        state.pop("notified", None)
    update_state(change)


def summary_line(result: Scan) -> str:
    """'pakiety serwera: 5 z poprawka, w tym 4 pilne (1 aktywnie wykorzystywany)' — linia raportu porannego."""
    urgent, kev = len(result.urgent), sum(1 for p in result.items if p.kev)
    line = tr(f"pakiety serwera: {len(result.items)} z poprawka", f"server packages: {len(result.items)} with a fix")
    if urgent:
        line += tr(f", w tym {count(urgent, 'pilny', 'pilne', 'pilnych')}", f", {urgent} of them urgent")
    if kev:
        line += tr(f" ({count(kev, 'aktywnie wykorzystywany', 'aktywnie wykorzystywane', 'aktywnie wykorzystywanych')})",
                   f" ({kev} actively exploited)")
    return line
