"""
Podatnosci w pakietach systemu — skan bez LLM i bez kluczy API.

  inventory()  pakiety z bazy dpkg (Debian/Ubuntu) albo apk (Alpine), pogrupowane po pakiecie
               ZRODLOWYM — OSV dla dystrybucji szuka po nazwie i wersji zrodla
  scan()       OSV.dev /v1/querybatch (same ID), szczegoly /v1/vulns/<id> tylko dla nowych
               albo zmienionych ID (pamiec w DATA_DIR/vulns.json), priorytet z CISA KEV
               (aktywnie wykorzystywane) i severity (tekst dystrybucji albo wynik CVSS v3)

Liczy sie tylko to, co ma poprawke: zwykly Debian ma setki CVE bez poprawki, wiec te sa
wylacznie liczba. Czuwanie (core/watch.py) skanuje, gdy zmieni sie lista pakietow, i co
VULN_INTERVAL (nowe podatnosci w starych pakietach); audyt (/audyt) i poranny raport
pokazuja wynik, alert przychodzi tylko o nowych pilnych (KEV, krytyczne, wysokie).

Do OSV trafiaja nazwy i wersje pakietow — nic wiecej. Synchroniczne (urllib), wywolujacy
uruchamia przez asyncio.to_thread. Bez importu `settings`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.core import memory, runtime
from backend.core.i18n import tr
from backend.core.websearch import WebError, get_json

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
STORE = "vulns.json"
KEV_STORE = "kev.json"
KEV_MAX_AGE = 24 * 3600
BATCH_SIZE = 1000            # limit OSV na jedno zapytanie zbiorcze
MAX_PAGES = 20               # kolejne strony wynikow dla pakietu z bardzo wieloma ID (jadro: tysiace)
MAX_DETAILS = 5000           # szczegolow na jeden skan — reszta w nastepnym
DETAIL_WORKERS = 16
MAX_DB_BYTES = 64 * 1024 * 1024
URGENT = ("critical", "high")
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
    name: str                                   # pakiet zrodlowy (Debian/Ubuntu) albo origin (Alpine)
    version: str                                # wersja zrodla
    binaries: list[str] = field(default_factory=list)   # zainstalowane pakiety z tego zrodla


def _read_host(path: str) -> str | None:
    local = runtime.to_local(path)
    try:
        with open(local, encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_DB_BYTES)
    except OSError:
        return None


def os_release() -> dict[str, str]:
    text = _read_host("/etc/os-release") or _read_host("/usr/lib/os-release") or ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def ecosystem(release: dict[str, str] | None = None) -> str | None:
    """Ekosystem OSV dla systemu hosta ('Debian:12', 'Ubuntu:22.04:LTS', 'Alpine:v3.20') albo None."""
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


def _dpkg(text: str) -> list[Package]:
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
        package = grouped.setdefault((source, source_version), Package(source, source_version))
        if binary not in package.binaries:
            package.binaries.append(binary)
    return sorted(grouped.values(), key=lambda p: p.name)


def _apk(text: str) -> list[Package]:
    grouped: dict[tuple[str, str], Package] = {}
    for block in text.split("\n\n"):
        fields = {line[0]: line[2:].strip() for line in block.splitlines() if len(line) > 2 and line[1] == ":"}
        name, version = fields.get("P"), fields.get("V")
        if not (name and version):
            continue
        origin = fields.get("o") or name
        package = grouped.setdefault((origin, version), Package(origin, version))
        package.binaries.append(name)
    return sorted(grouped.values(), key=lambda p: p.name)


def inventory() -> tuple[str | None, list[Package]]:
    """(ekosystem OSV, pakiety zrodlowe) — ekosystem None, gdy systemu nie obslugujemy."""
    eco = ecosystem()
    if eco is None:
        return None, []
    if eco.startswith("Alpine"):
        return eco, _apk(_read_host("/lib/apk/db/installed") or "")
    return eco, _dpkg(_read_host("/var/lib/dpkg/status") or "")


def inventory_hash(eco: str | None, packages: list[Package]) -> str:
    text = (eco or "") + "\n" + "\n".join(f"{p.name} {p.version}" for p in packages)
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
    """critical | high | medium | low | '' (nieznana) — najpierw ocena dystrybucji, potem CVSS v3."""
    from backend.core.software import _raw_severity

    raw = _raw_severity(vuln).split()
    word = _SEVERITY_WORDS.get(raw[0], "") if raw else ""
    if word:
        return word
    scores = [cvss3_score(str(item.get("score", ""))) for item in vuln.get("severity") or []]
    scores = [s for s in scores if s is not None]
    return level_from_score(max(scores)) if scores else ""


def _fixed_for(vuln: dict[str, Any], eco: str) -> dict[str, str]:
    """Pakiet -> wersja z poprawka w tym ekosystemie."""
    base = eco.split(":")[0]
    fixed: dict[str, str] = {}
    for affected in vuln.get("affected") or []:
        pkg = affected.get("package") or {}
        if str(pkg.get("ecosystem", "")) not in (eco, base) or not pkg.get("name"):
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if event.get("fixed"):
                    fixed[str(pkg["name"])] = str(event["fixed"])
    return fixed


def _detail(vuln: dict[str, Any], eco: str) -> dict[str, Any]:
    linked = [vuln.get("id", "")] + list(vuln.get("aliases") or []) + list(vuln.get("upstream") or [])         + list(vuln.get("related") or [])
    cves = sorted({str(a) for a in linked if str(a).startswith("CVE-")})
    related = sorted({str(a) for a in linked[1:] if a and not str(a).startswith("CVE-")})
    summary = str(vuln.get("summary") or vuln.get("details") or "").strip().split("\n")[0][:160]
    return {"modified": str(vuln.get("modified", "")), "cves": cves, "severity": severity_level(vuln),
            "summary": summary, "fixed": _fixed_for(vuln, eco), "related": related}


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
        """Wersja z poprawka, gdy wszystkie podatnosci wskazuja te sama (wersji Debiana nie porownujemy)."""
        versions = {v.fixed for v in self.fixable}
        return versions.pop() if len(versions) == 1 else ""


@dataclass
class Scan:
    at: float = 0.0
    ecosystem: str | None = None
    system: str = ""
    checked: int = 0
    items: list[PackageVulns] = field(default_factory=list)   # tylko pakiety z poprawkami
    unfixed: int = 0                                          # podatnosci bez poprawki (liczba)
    error: str = ""
    partial: bool = False                                     # nie wszystkie szczegoly pobrane

    @property
    def ok(self) -> bool:
        return self.ecosystem is not None and not self.error

    @property
    def urgent(self) -> list[PackageVulns]:
        return [p for p in self.items if p.urgent]

    @property
    def fixable_ids(self) -> set[str]:
        return {v.id for p in self.items for v in p.fixable}

    @property
    def urgent_ids(self) -> set[str]:
        return {v.id for p in self.items for v in p.fixable if v.urgent}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scan":
        items = [PackageVulns(name=p["name"], version=p["version"], binaries=list(p.get("binaries") or []),
                              fixable=[Vuln(**v) for v in p.get("fixable") or []], unfixed=int(p.get("unfixed", 0)))
                 for p in data.get("items") or []]
        return cls(at=float(data.get("at", 0)), ecosystem=data.get("ecosystem"), system=str(data.get("system", "")),
                   checked=int(data.get("checked", 0)), items=items, unfixed=int(data.get("unfixed", 0)),
                   error=str(data.get("error", "")), partial=bool(data.get("partial", False)))


def sort_items(items: list[PackageVulns]) -> list[PackageVulns]:
    rank = {"kev": -1, **LEVEL_ORDER}
    return sorted(items, key=lambda p: (rank.get(p.worst, 4), -len(p.fixable), p.name))


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


def select(result: Scan, scope: str) -> list[PackageVulns]:
    """Pakiety do zalatania: 'urgent' (KEV, krytyczne, wysokie — jak w alercie) albo wszystkie z poprawka."""
    return result.urgent if scope == "urgent" else list(result.items)


def patch_token(command: str) -> str:
    """Odcisk komendy z podgladu — wykonanie przechodzi tylko, gdy lista pakietow sie nie zmienila."""
    return hashlib.sha256(command.encode()).hexdigest()[:12]


def patch_preview(result: Scan, items: list[PackageVulns], command: str, reason: str = "") -> str:
    """Podglad latki dla klientow (Markdown): pakiety, dokladna komenda, czego bezpiecznik nie zrobi."""
    from backend.core.text import as_code

    urgent = sum(1 for p in items if p.urgent)
    lines = [tr(f"Latki dla {len(items)} pakietow" + (f" ({urgent} pilnych)" if urgent else "") + ":",
                f"Patches for {len(items)} packages" + (f" ({urgent} urgent)" if urgent else "") + ":")]
    lines += [f"- {describe_item(p, 2)}" for p in items[:15]]
    if len(items) > 15:
        lines.append(tr(f"- i {len(items) - 15} innych", f"- and {len(items) - 15} more"))
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


# ─── Pamiec skanu ───────────────────────────────────────────────────────────

def _store_path():
    return memory.data_dir() / STORE


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
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

def query_ids(eco: str, packages: list[Package]) -> dict[str, list[tuple[str, str]]]:
    """Pakiet -> [(id, modified)] z OSV /v1/querybatch."""
    found: dict[str, list[tuple[str, str]]] = {}
    for start in range(0, len(packages), BATCH_SIZE):
        chunk = packages[start:start + BATCH_SIZE]
        for package in chunk:
            found.setdefault(package.name, [])
        pending = [(p, "") for p in chunk]
        for _ in range(MAX_PAGES):
            if not pending:
                break
            queries = []
            for package, token in pending:
                query: dict[str, Any] = {"package": {"name": package.name, "ecosystem": eco}, "version": package.version}
                if token:
                    query["page_token"] = token
                queries.append(query)
            data = get_json(OSV_BATCH, {"queries": queries}, timeout=60)
            results = data.get("results") or []
            next_pending = []
            for (package, _), result in zip(pending, results):
                ids = [(str(v.get("id")), str(v.get("modified", ""))) for v in (result or {}).get("vulns") or [] if v.get("id")]
                found.setdefault(package.name, []).extend(ids)
                if (result or {}).get("next_page_token"):
                    next_pending.append((package, result["next_page_token"]))
            pending = next_pending
    return found


def _fetch_details(ids: list[str], eco: str) -> dict[str, dict[str, Any]]:
    def one(vuln_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return vuln_id, _detail(get_json(OSV_VULN + vuln_id, timeout=30), eco)
        except WebError:
            return vuln_id, None

    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for vuln_id, detail in pool.map(one, ids):
            if detail is not None:
                result[vuln_id] = detail
    return result


def _package_vulns(package: Package, ids: list[str], details: dict[str, dict[str, Any]],
                   kev: set[str]) -> PackageVulns | None:
    """
    Podatnosci jednego pakietu. Rekord CVE objety biuletynem (USN-x -> UBUNTU-CVE-y) nie jest liczony
    drugi raz; biuletyn bez wlasnej oceny dostaje najgorsza ocene objetych rekordow. None = brak szczegolow.
    """
    item = PackageVulns(package.name, package.version, list(package.binaries))
    if any(vid not in details for vid in ids):
        return None
    covered = {rel for vid in ids if ADVISORY.match(vid) for rel in details[vid].get("related") or []}
    for vid in ids:
        detail = details[vid]
        fixed = (detail.get("fixed") or {}).get(package.name, "")
        if not fixed:
            item.unfixed += 1
            continue
        if vid in covered:
            continue
        severity = str(detail.get("severity", ""))
        if not severity:
            levels = [str(details[r].get("severity", "")) for r in detail.get("related") or [] if r in details]
            severity = min(levels, key=lambda s: LEVEL_ORDER.get(s, 4), default="")
        item.fixable.append(Vuln(vid, list(detail.get("cves") or []), severity, str(detail.get("summary", "")),
                                 fixed, kev=any(c in kev for c in detail.get("cves") or [])))
    return item


def scan(*, force: bool = False, max_age: float = 0, now: float | None = None) -> tuple[Scan, bool]:
    """
    Skan podatnosci. Zwraca (wynik, czy odpytano OSV). Bez `force` OSV dostaje wszystkie pakiety tylko
    co `max_age` sekund (nowe podatnosci w starych pakietach); w miedzyczasie tylko pakiety, ktorych
    wersja sie zmienila — dzieki temu /audyt zaraz po latce trwa sekundy, nie minute.
    """
    now = time.time() if now is None else now
    eco, packages = inventory()
    state = load_state()
    previous = state.get("last")
    digest = inventory_hash(eco, packages)
    if eco is None:
        result = Scan(at=now, ecosystem=None, system=system_name())
        state["last"] = result.to_dict()
        save_state(state)
        return result, False
    expired = force or now - float(state.get("queried_at", 0)) >= max_age or state.get("found_eco") != eco
    if not expired and state.get("inventory") == digest and isinstance(previous, dict) \
            and not previous.get("error") and not previous.get("partial"):
        return Scan.from_dict(previous), False

    by_name = {p.name: p for p in packages}
    cache: dict[str, Any] = {} if expired else state.get("found") or {}
    found = {p.name: [(str(i), str(m)) for i, m in cache[p.name].get("ids") or []]
             for p in packages if (cache.get(p.name) or {}).get("version") == p.version}
    to_query = [p for p in packages if p.name not in found]
    try:
        if to_query:
            found.update(query_ids(eco, to_query))
    except WebError as exc:
        result = Scan.from_dict(previous) if isinstance(previous, dict) and previous.get("ecosystem") == eco else \
            Scan(at=now, ecosystem=eco, system=system_name(), checked=len(packages))
        result.error = str(exc)
        state["last"] = result.to_dict()
        save_state(state)
        return result, True

    details: dict[str, dict[str, Any]] = state.get("details") or {}
    wanted = {vid: modified for name, ids in found.items() for vid, modified in ids
              if not KERNEL.match(name) or ADVISORY.match(vid)}
    missing = [vid for vid, modified in wanted.items()
               if vid not in details or (modified and details[vid].get("modified") != modified)]
    partial = len(missing) > MAX_DETAILS
    details.update(_fetch_details(missing[:MAX_DETAILS], eco))
    details = {vid: d for vid, d in details.items() if vid in wanted}
    kev = kev_ids(now)

    items: list[PackageVulns] = []
    unfixed_total = 0
    for name, ids in found.items():
        package = by_name[name]
        item = _package_vulns(package, [vid for vid in dict.fromkeys(vid for vid, _ in ids) if vid in wanted],
                              details, kev)
        if item is None:
            partial = True
            continue
        unfixed_total += item.unfixed
        if item.fixable:
            items.append(item)

    result = Scan(at=now, ecosystem=eco, system=system_name(), checked=len(packages), items=sort_items(items),
                  unfixed=unfixed_total, partial=partial)
    state.update({"last": result.to_dict(), "inventory": digest, "details": details, "found_eco": eco,
                  "found": {p.name: {"version": p.version, "ids": found.get(p.name, [])} for p in packages}})
    if expired:
        state["queried_at"] = now
    save_state(state)
    return result, True


# ─── Powiadomienia ──────────────────────────────────────────────────────────

def notified_ids() -> set[str]:
    return set(load_state().get("notified") or [])


def mark_notified(ids: set[str]) -> None:
    state = load_state()
    state["notified"] = sorted(ids)
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
